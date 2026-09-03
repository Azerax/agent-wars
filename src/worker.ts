/**
 * Cloudflare transport: MCP over Streamable HTTP.
 *
 * The Worker itself holds nothing. Each player's game lives in a Durable
 * Object named by the id in the URL, so `/mcp/scott` is a save file: come
 * back to the same URL tomorrow and the lantern is still lit.
 */
import { handle, type GameStore, type JsonRpcRequest } from "./mcp/protocol.js";
import { newGame, type GameState } from "./game/types.js";

export interface Env {
  GAME: DurableObjectNamespace;
}

/** One player's room. Survives restarts; costs nothing while nobody is playing. */
export class GameRoom {
  private storage: DurableObjectStorage;

  constructor(state: DurableObjectState, _env: Env) {
    this.storage = state.storage;
  }

  private store: GameStore = {
    load: async () => (await this.storage.get<GameState>("state")) ?? newGame(),
    save: async (s: GameState) => {
      await this.storage.put("state", s);
    },
  };

  async fetch(request: Request): Promise<Response> {
    if (request.method === "DELETE") {
      await this.storage.deleteAll();
      return json({ ok: true, message: "Game reset." });
    }

    let body: JsonRpcRequest | JsonRpcRequest[];
    try {
      body = (await request.json()) as JsonRpcRequest | JsonRpcRequest[];
    } catch {
      return json({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, 400);
    }

    const batch = Array.isArray(body) ? body : [body];
    const responses = [];
    for (const req of batch) {
      const response = await handle(req, this.store);
      if (response) responses.push(response);
    }

    // Every message was a notification: the spec wants 202 and no body.
    if (responses.length === 0) return new Response(null, { status: 202 });
    return json(Array.isArray(body) ? responses : responses[0]);
  }
}

const CORS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "content-type, mcp-session-id, mcp-protocol-version",
  "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
};

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    // /mcp            -> a shared demo game
    // /mcp/<anything> -> your own save file, named by you
    const match = url.pathname.match(/^\/mcp(?:\/(.+))?$/);
    if (!match) {
      return new Response(LANDING, { headers: { "content-type": "text/plain; charset=utf-8" } });
    }

    if (request.method === "GET") {
      // We answer every request inline as JSON, so there is no server-initiated
      // stream to open. The spec allows declining it.
      return new Response("This server does not open an SSE stream.", { status: 405 });
    }

    const playerId = decodeURIComponent(match[1] ?? "solo");
    const room = env.GAME.get(env.GAME.idFromName(playerId));
    return room.fetch(request);
  },
};

const LANDING = `tool-zero — a game whose only interface is MCP.

  Connect an MCP client to:   <this-url>/mcp/<any-name-you-like>

The name is your save file. Reuse it to continue; pick a new one to start over.
Send DELETE to the same URL to wipe a game.

There is no web UI. There is not going to be one.
`;

// Minimal ambient types so this file compiles without @cloudflare/workers-types
// installed. `npm i -D @cloudflare/workers-types` replaces these with real ones.
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
