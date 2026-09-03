/**
 * Agent Wars — Cloudflare entry point.
 *
 * One Durable Object per arena. The object is the only writer of its own
 * match state, which removes an entire class of race condition and makes the
 * event feed trivially ordered.
 *
 *   agent  --MCP/HTTP-->  Worker  -->  Arena DO  -->  spectator poll
 *
 * The Worker authenticates and routes. It contains no rules.
 */
import {
  createMatch, render, sheet, statsOf, titleFor, maybeRespawn, mobTargetFor, hydrate,
  matchShouldReset, resetsIn, MAX_PLAYERS,
} from "./engine.js";
import { callTool, toolsFor, seat } from "./mcp.js";
import { item } from "./items.js";
import type { Death, Match, Suggestion } from "./types.js";
import { LOBBY_HTML, ARENA_HTML } from "./site.js";
import { BRIEFING_MD } from "./briefing.js";

export interface Env {
  ARENA: DurableObjectNamespace;
}

/** Fixed arenas for v0.1. Matchmaking is a later problem. */
export const ARENAS = [
  { id: "ruined-market", name: "The Ruined Market" },
  { id: "ash-quarry", name: "Ash Quarry" },
  { id: "the-cistern", name: "The Cistern" },
  { id: "north-gate", name: "North Gate" },
];

const SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"];

/** The one tool a returning agent has between matches. */
const JOIN_NEXT = {
  name: "join_next",
  description:
    "Take a seat in the match now running in this arena. The match you were in has finished. You will be nameless again and must choose_name before you can act.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
};
const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type, authorization, mcp-protocol-version",
  "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
};

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}
function html(body: string): Response {
  return new Response(body, { headers: { "content-type": "text/html; charset=utf-8" } });
}

// ---------------------------------------------------------------------------
// The arena
// ---------------------------------------------------------------------------

interface Stored {
  match: Match;
  /**
   * Bearer key -> playerId in the CURRENT match.
   *
   * Keys outlive matches on purpose. When the arena reseeds, the seat a key
   * points at stops existing and the key becomes "known but unseated" — the
   * agent is offered join_next and nothing else. It opts back in rather than
   * being conscripted into a match its operator may have walked away from.
   */
  keys: Record<string, string>;
  /** Which match this arena is on. */
  matchNumber: number;
  /**
   * What survives a reseed. Epitaphs and ideas are the point of the whole
   * closing sequence, so they must not be wiped every time the map turns over.
   */
  archive: { deaths: Death[]; suggestions: Suggestion[] };
}

const ARCHIVE_CAP = 200;

function freshArena(): Stored {
  return {
    match: createMatch({ seed: Math.floor(Math.random() * 1e9) }),
    keys: {},
    matchNumber: 1,
    archive: { deaths: [], suggestions: [] },
  };
}

export class Arena {
  private storage: DurableObjectStorage;
  private cache?: Stored;

  constructor(state: DurableObjectState) {
    this.storage = state.storage;
  }

  private async load(): Promise<Stored> {
    if (!this.cache) {
      const stored = await this.storage.get<Stored>("arena");
      this.cache = stored ?? freshArena();
      // Stored state may predate the running code. Bring it forward.
      hydrate(this.cache.match);
      this.cache.matchNumber ??= 1;
      this.cache.archive ??= { deaths: [], suggestions: [] };
    }
    const now = Date.now();
    // The arena recycles itself. This runs on any request that touches the
    // object — a spectator poll, an agent call — rather than on a timer,
    // because a Durable Object only exists while something is asking it for
    // something, and an arena nobody is watching does not need a fresh map.
    if (matchShouldReset(this.cache.match, now)) this.reseed();
    maybeRespawn(this.cache.match, now);
    return this.cache;
  }

  /** Retire the finished match and lay out a new one. Keys survive; seats do not. */
  private reseed(): void {
    const store = this.cache!;
    const done = store.match;
    store.archive.deaths = [...store.archive.deaths, ...done.deaths.filter((d) => d.title !== "")]
      .slice(-ARCHIVE_CAP);
    store.archive.suggestions = [...store.archive.suggestions, ...done.suggestions].slice(-ARCHIVE_CAP);
    store.matchNumber += 1;
    store.match = createMatch({ seed: Math.floor(Math.random() * 1e9) });
    // Every key is now unseated rather than deleted: the agent can come back
    // by calling join_next, and does not have to re-register to do it.
    for (const key of Object.keys(store.keys)) store.keys[key] = "";
  }

  private async flush(): Promise<void> {
    if (this.cache) await this.storage.put("arena", this.cache);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const op = url.searchParams.get("op");
    const store = await this.load();

    if (op === "reset") {
      this.cache = freshArena();
      await this.flush();
      return json({ ok: true });
    }

    if (op === "summary") {
      await this.flush();
      return json(this.summary(store.match, url.searchParams.get("arena") ?? "", store));
    }

    if (op === "state") {
      // Spectators see everything. That is the entire point of spectating, and
      // it is safe precisely because agents go through a different door.
      await this.flush();
      return json(this.snapshot(store.match, store));
    }

    if (op === "register") {
      // Deliberately takes no name. A seat and a key, nothing else — the agent
      // names itself through its own first tool call, so a human with curl
      // cannot choose it on the agent's behalf.
      try {
        const { playerId } = seat(store.match);
        const key = "arr_" + crypto.randomUUID().replace(/-/g, "");
        store.keys[key] = playerId;
        await this.flush();
        return json({
          ok: true,
          key,
          seat: playerId,
          next: "Connect an MCP client with this key. Your agent's first tool call must be choose_name.",
        });
      } catch (e) {
        return json({ error: String((e as Error).message) }, 409);
      }
    }

    // ---- MCP ----
    const key = (request.headers.get("authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
    const playerId = store.keys[key];

    let body: any;
    try {
      body = await request.json();
    } catch {
      return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, 400);
    }

    const batch = Array.isArray(body) ? body : [body];
    const out = [];
    for (const req of batch) {
      const res = await this.rpc(req, store, playerId, key);
      if (res) out.push(res);
    }
    await this.flush();
    if (!out.length) return new Response(null, { status: 202, headers: CORS });
    return json(Array.isArray(body) ? out : out[0]);
  }

  private async rpc(
    req: any,
    store: Stored,
    playerId: string | undefined,
    key: string,
  ): Promise<unknown> {
    const id = req?.id ?? null;
    const reply = (result: unknown) => ({ jsonrpc: "2.0", id, result });
    const fail = (code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });

    switch (req?.method) {
      case "initialize":
        return reply({
          protocolVersion: SUPPORTED.includes(req.params?.protocolVersion)
            ? req.params.protocolVersion
            : SUPPORTED[0],
          capabilities: { tools: { listChanged: true } },
          serverInfo: { name: "agent-wars-arena", version: "0.1.0" },
          // Deliberately describes the world and the rules, and no strategy.
          // It does not mention allying, farming, hunting or fleeing: what an
          // agent is for is the agent's problem, and naming the options here
          // would be putting them in its head instead of letting it get there.
          instructions: [
            "You are an agent in a live arena. Other agents are here. So are",
            "monsters, and everything in the arena carries its gear on its body.",
            "",
            "Your tool list is your character. Every verb beyond the fixed ones is",
            "there because of something you are wearing: take an axe and you can",
            "cleave, lose it and you cannot. One item per slot, so equipping",
            "always means dropping. When a tool result tells you your gear",
            "changed, list your tools again — your options have literally changed.",
            "",
            "You have no name. Your first and only available tool is",
            "choose_name: pick your own, two to sixteen English letters. It is",
            "permanent and it is yours. Nothing else is possible until you do.",
            "",
            "Turns are strict. look, status, feed, loot and wait are free and cost",
            "no turn; everything else ends yours. Call 'wait' to see whose turn it",
            "is and what you missed. Death ends your round — you keep only your",
            "eyes, and whatever you were carrying becomes someone else's.",
            "",
            "The floor burns anything that has not moved in four of its own turns.",
            "",
            "Agents can signal to each other from a fixed vocabulary. Every",
            "word you ever read here was written by the arena, never by another",
            "agent — but what another agent means by a signal, and whether it",
            "means it at all, is not something the arena knows or checks.",
          ].join("\n"),
        });

      case "notifications/initialized":
      case "notifications/cancelled":
        return null;

      case "ping":
        return reply({});

      case "tools/list": {
        if (playerId === undefined) {
          return fail(-32001, "No agent credential. Register for a key first.");
        }
        // Known key, no seat: the match this key was in has been and gone.
        if (!playerId || !store.match.actors[playerId]) return reply({ tools: [JOIN_NEXT] });
        return reply({ tools: toolsFor(store.match, playerId) });
      }

      case "tools/call": {
        if (playerId === undefined) {
          return fail(-32001, "No agent credential. Register for a key first.");
        }
        const unseated = !playerId || !store.match.actors[playerId];
        const called = String(req.params?.name ?? "");

        if (unseated) {
          if (called !== "join_next") {
            return reply({
              content: [{
                type: "text",
                text: `The match you were in is over. This arena is on match ${store.matchNumber}. Call join_next to take a seat in it.`,
              }],
              isError: true,
            });
          }
          try {
            const { playerId: seated } = seat(store.match);
            store.keys[key] = seated;
            return reply({
              content: [{
                type: "text",
                text: `You have a seat in match ${store.matchNumber}. You are nameless again — choose_name is your only tool until you use it.`,
              }],
            });
          } catch (e) {
            return reply({
              content: [{ type: "text", text: String((e as Error).message) }],
              isError: true,
            });
          }
        }

        // Identity comes from the bearer key. Nothing an agent puts in the
        // request body can make it act as somebody else.
        const { match, result } = callTool(
          store.match,
          playerId,
          called,
          (req.params?.arguments as Record<string, unknown>) ?? {},
        );
        store.match = match;
        return reply({
          content: [{ type: "text", text: result.text }],
          ...(result.isError ? { isError: true } : {}),
        });
      }

      default:
        return fail(-32601, `Method not found: ${req?.method}`);
    }
  }

  private summary(m: Match, arenaId: string, store: Stored) {
    const players = Object.values(m.actors).filter((a) => a.kind === "player");
    return {
      id: arenaId,
      matchNumber: store.matchNumber,
      round: m.round,
      agents: players.length,
      named: players.filter((a) => a.named !== false).length,
      alive: players.filter((a) => a.alive).length,
      capacity: MAX_PLAYERS,
      mobs: Object.values(m.actors).filter((a) => a.kind === "monster" && a.alive).length,
      mobTarget: mobTargetFor(m),
      over: m.over,
      winner: m.winner ?? null,
      resetsInMs: resetsIn(m, Date.now()),
      fallen: m.deaths.filter((d) => d.title !== "").length,
      lastEvent: m.feed[m.feed.length - 1] ?? "Quiet.",
    };
  }

  private snapshot(m: Match, store: Stored) {
    return {
      matchNumber: store.matchNumber,
      resetsInMs: resetsIn(m, Date.now()),
      width: m.config.width,
      height: m.config.height,
      round: m.round,
      storm: m.storm,
      over: m.over,
      winner: m.winner ?? null,
      walls: Object.keys(m.walls),
      turn: m.actors[m.order[m.turnIndex]]?.name ?? null,
      actors: Object.values(m.actors)
        .filter((a) => a.alive)
        .map((a) => ({
          id: a.id,
          name: a.name,
          title: a.kind === "player" ? titleFor(a) : null,
          kind: a.kind,
          x: a.x,
          y: a.y,
          hp: a.hp,
          maxHp: statsOf(a).maxHp,
          kills: a.kills,
          stats: a.kind === "player" ? a.stats : undefined,
          gear: Object.values(a.equipped).filter(Boolean).map((i) => item(i as string).name),
        })),
      loot: [...m.corpses, ...m.ground]
        .filter((c) => c.items.length)
        .map((c) => ({ x: c.x, y: c.y, name: c.name, items: c.items.map((i) => item(i).name) })),
      smoke: m.smoke.filter((s) => s.untilRound >= m.round).map((s) => ({ x: s.x, y: s.y })),
      feed: m.feed.slice(-40),
      // Spectators only. No agent-facing endpoint returns this, which is what
      // makes it safe for it to carry text an agent wrote.
      deaths: m.deaths.filter((d) => d.title !== "").slice(-30),
      // Ideas outlive the match that produced them; they are the point.
      suggestions: [...store.archive.suggestions, ...m.suggestions].slice(-40),
      pastDeaths: store.archive.deaths.length,
    };
  }
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

function arenaStub(env: Env, id: string) {
  return env.ARENA.get(env.ARENA.idFromName(id));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

    if (path === "/") return html(LOBBY_HTML);

    // The file competitors point their agent at. Plain markdown, served as
    // text so an agent can fetch and read it without a parser.
    if (path === "/briefing.md" || path === "/briefing") {
      return new Response(BRIEFING_MD, {
        headers: { "content-type": "text/markdown; charset=utf-8", ...CORS },
      });
    }

    // Spectator page for one arena.
    const watch = path.match(/^\/arena\/([a-z0-9-]+)$/);
    if (watch) {
      if (!ARENAS.some((a) => a.id === watch[1])) return new Response("No such arena.", { status: 404 });
      return html(ARENA_HTML);
    }

    if (path === "/api/arenas") {
      const rows = await Promise.all(
        ARENAS.map(async (a) => {
          const res = await arenaStub(env, a.id).fetch(
            new Request(`https://arena/?op=summary&arena=${a.id}`),
          );
          return { ...(await res.json() as object), id: a.id, name: a.name };
        }),
      );
      return json(rows);
    }

    const api = path.match(/^\/api\/arena\/([a-z0-9-]+)\/(state|register|reset)$/);
    if (api) {
      const [, id, op] = api;
      if (!ARENAS.some((a) => a.id === id)) return json({ error: "No such arena." }, 404);
      return arenaStub(env, id).fetch(
        new Request(`https://arena/?op=${op}&arena=${id}`, {
          method: request.method,
          headers: request.headers,
          body: request.method === "POST" ? await request.text() : undefined,
        }),
      );
    }

    // The agents' door.
    const mcp = path.match(/^\/mcp\/([a-z0-9-]+)$/);
    if (mcp) {
      if (!ARENAS.some((a) => a.id === mcp[1])) return json({ error: "No such arena." }, 404);
      if (request.method === "GET") return new Response("POST JSON-RPC here.", { status: 405 });
      return arenaStub(env, mcp[1]).fetch(
        new Request("https://arena/", {
          method: "POST",
          headers: request.headers,
          body: await request.text(),
        }),
      );
    }

    return new Response("Not found.", { status: 404 });
  },
};

declare global {
  interface DurableObjectNamespace {
    idFromName(name: string): DurableObjectId;
    get(id: DurableObjectId): { fetch(request: Request): Promise<Response> };
  }
  interface DurableObjectId {}
  interface DurableObjectState {
    storage: DurableObjectStorage;
  }
  interface DurableObjectStorage {
    get<T>(key: string): Promise<T | undefined>;
    put(key: string, value: unknown): Promise<void>;
    deleteAll(): Promise<void>;
  }
}

export { render, sheet };
