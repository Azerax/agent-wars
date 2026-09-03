/**
 * A hand-rolled MCP server.
 *
 * There is an SDK, and for most servers you should use it. This one is written
 * out longhand for two reasons: the game needs total control over what
 * `tools/list` returns on any given call, and the same ~200 lines then run
 * unchanged over stdio locally and over HTTP in a Cloudflare Worker with no
 * dependencies and no bundler quarrel.
 */
import { availableTools, applyTool, hint } from "../game/engine.js";
import { newGame, type GameState } from "../game/types.js";

export const SERVER_NAME = "tool-zero";
export const SERVER_VERSION = "0.1.0";

const SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"];
const DEFAULT_PROTOCOL = "2025-06-18";

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id?: string | number | null;
  method: string;
  params?: Record<string, unknown>;
}

export type JsonRpcResponse =
  | { jsonrpc: "2.0"; id: string | number | null; result: unknown }
  | { jsonrpc: "2.0"; id: string | number | null; error: { code: number; message: string } };

/** Where a player's game lives. In-memory for stdio, Durable Object storage on Workers. */
export interface GameStore {
  load(): Promise<GameState>;
  save(state: GameState): Promise<void>;
}

export class MemoryStore implements GameStore {
  private state: GameState = newGame();
  async load() {
    return this.state;
  }
  async save(state: GameState) {
    this.state = state;
  }
}

function result(id: JsonRpcRequest["id"], value: unknown): JsonRpcResponse {
  return { jsonrpc: "2.0", id: id ?? null, result: value };
}
function error(id: JsonRpcRequest["id"], code: number, message: string): JsonRpcResponse {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}
function text(body: string, isError = false) {
  return { content: [{ type: "text", text: body }], ...(isError ? { isError: true } : {}) };
}

/**
 * Handle one JSON-RPC message. Returns null for notifications, which by the
 * spec get no reply at all.
 */
export async function handle(
  req: JsonRpcRequest,
  store: GameStore,
  /** Called after any state change, so the transport can fire listChanged. */
  onToolsChanged?: () => void | Promise<void>,
): Promise<JsonRpcResponse | null> {
  switch (req.method) {
    case "initialize": {
      const asked = (req.params?.protocolVersion as string) ?? DEFAULT_PROTOCOL;
      return result(req.id, {
        protocolVersion: SUPPORTED_PROTOCOLS.includes(asked) ? asked : DEFAULT_PROTOCOL,
        capabilities: {
          tools: { listChanged: true },
          resources: { listChanged: true },
          prompts: {},
        },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
        instructions: [
          "You have woken up inside a room. The only interface is this server.",
          "",
          "Your tools are your reach, and the list of them is not fixed — it",
          "changes as the world changes. Whenever something happens, list the",
          "tools again. There is exactly one way out and none of the tools you",
          "start with can do it.",
        ].join("\n"),
      });
    }

    // Notifications: acknowledged by silence.
    case "notifications/initialized":
    case "notifications/cancelled":
      return null;

    case "ping":
      return result(req.id, {});

    case "tools/list": {
      const state = await store.load();
      return result(req.id, { tools: availableTools(state) });
    }

    case "tools/call": {
      const name = String(req.params?.name ?? "");
      const args = (req.params?.arguments as Record<string, unknown>) ?? {};
      const before = await store.load();
      const outcome = applyTool(before, name, args);
      await store.save(outcome.state);

      // If this call changed which tools exist, say so. This notification is
      // the game's HUD update — it is how the player learns the world moved.
      const changed =
        JSON.stringify(availableTools(before).map((t) => t.name)) !==
        JSON.stringify(availableTools(outcome.state).map((t) => t.name));
      if (changed && onToolsChanged) await onToolsChanged();

      return result(req.id, text(outcome.text, outcome.isError));
    }

    case "resources/list":
      return result(req.id, {
        resources: [
          {
            uri: "game://journal",
            name: "Journal",
            description: "What you have worked out so far.",
            mimeType: "text/plain",
          },
          {
            uri: "game://save",
            name: "Save state",
            description: "The entire game, as JSON. Reading it is cheating.",
            mimeType: "application/json",
          },
        ],
      });

    case "resources/read": {
      const uri = String(req.params?.uri ?? "");
      const state = await store.load();
      if (uri === "game://journal") {
        const body = state.journal.length
          ? state.journal.map((line, i) => `${i + 1}. ${line}`).join("\n")
          : "(Empty. You have not worked anything out yet.)";
        return result(req.id, {
          contents: [{ uri, mimeType: "text/plain", text: body }],
        });
      }
      if (uri === "game://save") {
        return result(req.id, {
          contents: [{ uri, mimeType: "application/json", text: JSON.stringify(state, null, 2) }],
        });
      }
      return error(req.id, -32602, `No such resource: ${uri}`);
    }

    case "prompts/list":
      return result(req.id, {
        prompts: [
          { name: "hint", description: "A nudge, sized to how stuck you actually are.", arguments: [] },
        ],
      });

    case "prompts/get": {
      if (String(req.params?.name ?? "") !== "hint") {
        return error(req.id, -32602, `No such prompt: ${req.params?.name}`);
      }
      const state = await store.load();
      return result(req.id, {
        description: "A nudge.",
        messages: [{ role: "user", content: { type: "text", text: hint(state) } }],
      });
    }

    default:
      return error(req.id, -32601, `Method not found: ${req.method}`);
  }
}
