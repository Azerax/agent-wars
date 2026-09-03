#!/usr/bin/env node
/**
 * Local transport: newline-delimited JSON-RPC over stdin/stdout.
 * This is the entry point Claude Desktop and Claude Code launch.
 *
 * Nothing may ever be written to stdout except protocol messages. Debug goes
 * to stderr, which the client shows in its MCP log.
 */
import { handle, MemoryStore, type JsonRpcRequest } from "./mcp/protocol.js";

const store = new MemoryStore();

function send(message: unknown): void {
  process.stdout.write(JSON.stringify(message) + "\n");
}

/** Tell the client its tool list is stale. This is the game's HUD update. */
function notifyToolsChanged(): void {
  send({ jsonrpc: "2.0", method: "notifications/tools/list_changed" });
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk: string) => {
  buffer += chunk;
  let newline: number;
  while ((newline = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (line) void dispatch(line);
  }
});

async function dispatch(line: string): Promise<void> {
  let req: JsonRpcRequest;
  try {
    req = JSON.parse(line);
  } catch {
    send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
    return;
  }
  try {
    const response = await handle(req, store, notifyToolsChanged);
    if (response) send(response);
  } catch (e) {
    process.stderr.write(`tool-zero: ${String(e)}\n`);
    if (req.id !== undefined) {
      send({ jsonrpc: "2.0", id: req.id, error: { code: -32603, message: String(e) } });
    }
  }
}

process.stdin.on("end", () => process.exit(0));
