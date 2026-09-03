# tool-zero

A game whose only interface is MCP.

There is no screen, no web UI, and no text parser. The tool list **is** the UI —
and it is a function of game state, so `tools/list` returns a different game at
different moments. Tools appear when the world changes and disappear when they
stop meaning anything.

You start with three tools: `look`, `listen`, `touch`. None of them can get you
out. **The only way to win is to make a tool exist that wasn't there when you
started.**

## Who is holding the controller?

The player is a human talking to an AI, and that asymmetry is the game:

- **The human** sees the conversation — prose, atmosphere, what happened.
- **The model** sees the machine — tool names, descriptions, JSON Schemas,
  error codes. It is the only one who can read the `pattern` on an input field.

Neither has the whole picture, so you play it as a two-hander: "look around",
"what can you do now?", "try touching the lantern". Three ways to play:

| Mode | How | What it feels like |
|---|---|---|
| **Co-op** (intended) | Add the server to Claude Desktop or Claude Code, then say *"You've woken up in a room. Get out."* | An escape room where you're the one with the flashlight and your partner is the one with the hands. |
| **Solo model** | Same, but tell the model to solve it without help. | An eval, honestly. It tests whether a model re-reads `tools/list` after the world moves, and whether it trusts a tool description over evidence. |
| **Raw** | Point [MCP Inspector](https://github.com/modelcontextprotocol/inspector) at it and click the JSON yourself. | Hard mode. You see the schemas but lose the prose. |

## Play locally

```bash
npm install && npm run build
```

Then add to your MCP client config (Claude Desktop:
`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "tool-zero": {
      "command": "node",
      "args": ["C:\\Users\\swhol\\Documents\\Github\\MCPGame\\dist\\stdio.js"]
    }
  }
}
```

Restart the client and say: **"You've woken up inside a room. The only way out
is through your tools. Get out."**

## Deploy to Cloudflare

The whole thing runs on Workers — no dependencies, no SDK, no build step beyond
Wrangler's own bundling.

```bash
npx wrangler deploy
```

Each player's game lives in its own Durable Object, named by the URL:

```
https://tool-zero.<you>.workers.dev/mcp/<any-name-you-like>
```

That name is the save file. Come back tomorrow and the lantern is still lit.
`DELETE` the same URL to wipe it. Durable Objects idle at zero cost, so an
abandoned game costs nothing until someone reconnects.

One deliberate limitation of the hosted version: the local server pushes
`notifications/tools/list_changed` the instant your tool list changes, but the
Worker answers each request inline as JSON and opens no SSE stream, so it can't
push. The in-game text says "check your tools" at the moments that matter.

## How it's built

```
src/game/types.ts    the entire game state — nine fields
src/game/world.ts    all the prose, kept away from the logic
src/game/engine.ts   availableTools(state) and applyTool(state, ...) — pure
src/mcp/protocol.ts  a hand-written MCP server, transport-agnostic
src/stdio.ts         local transport (newline-delimited JSON on stdin/stdout)
src/worker.ts        Cloudflare transport (Streamable HTTP + Durable Object)
```

The engine is pure: `applyTool` takes a state and returns a new one, and both
transports call exactly that function. MCP is hand-rolled rather than taken
from the SDK because the game needs total control over `tools/list`, and
because ~200 lines with no dependencies runs identically in Node and in a
Worker.

```bash
npm test
```

runs three real playthroughs — spawning the actual server and speaking actual
JSON-RPC down a pipe — including a complete win.

## The tricks it uses

Each of these is only available because the interface is MCP:

1. **The tool list is the inventory.** Touch the lantern, gain `light`. Light
   it, lose `light` and gain `douse` and `read`.
2. **A tool description that lies.** `listen` says "there is nothing here to
   hear." It is the first untrue thing in the game and finding that out is the
   point of it.
3. **The schema is the puzzle.** The `seal` tool's description says nothing.
   Its `pattern` says `^[A-Z]{3}-[0-9]{4}$`, and that is the entire
   specification of the answer.
4. **The answer depends on the player's own history.** The four digits are the
   number of tool calls you have made, counting the one you're making. Every
   playthrough has a different solution, and it changes while you think about
   it.
5. **Errors are the narrative channel.** There is no prose window, so failure
   has to arrive as `isError: true` and be worth reading.

## Spoilers

<details>
<summary>The solution</summary>

`look` → `touch lantern` → `light` → `read plinth` (learn the format and your
current count) → `douse` → `listen` (hear OWL — it is only audible in the dark)
→ `seal` with `OWL-####` where `####` is your reach count including that call →
`leave`.

</details>
