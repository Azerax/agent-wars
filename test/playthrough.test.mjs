import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";

/** A minimal MCP client, talking to the real stdio server over a real pipe. */
class Client {
  constructor() {
    this.proc = spawn(process.execPath, ["dist/stdio.js"], { stdio: ["pipe", "pipe", "pipe"] });
    this.nextId = 1;
    this.pending = new Map();
    this.notifications = [];
    let buffer = "";
    this.proc.stdout.setEncoding("utf8");
    this.proc.stdout.on("data", (chunk) => {
      buffer += chunk;
      let i;
      while ((i = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, i).trim();
        buffer = buffer.slice(i + 1);
        if (!line) continue;
        const msg = JSON.parse(line);
        if (msg.id === undefined || msg.id === null) {
          this.notifications.push(msg.method);
          continue;
        }
        const resolve = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        resolve?.(msg);
      }
    });
  }

  send(method, params) {
    const id = this.nextId++;
    const promise = new Promise((resolve) => this.pending.set(id, resolve));
    this.proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return promise;
  }

  async toolNames() {
    const { result } = await this.send("tools/list");
    return result.tools.map((t) => t.name);
  }

  async call(name, args = {}) {
    const { result } = await this.send("tools/call", { name, arguments: args });
    return { text: result.content[0].text, isError: result.isError === true };
  }

  async close() {
    this.proc.stdin.end();
    await once(this.proc, "exit");
  }
}

test("a full winning playthrough", async () => {
  const c = new Client();

  const init = await c.send("initialize", { protocolVersion: "2025-06-18", capabilities: {} });
  assert.equal(init.result.serverInfo.name, "tool-zero");
  assert.equal(init.result.capabilities.tools.listChanged, true);

  // You start with three tools and none of them can open the door.
  assert.deepEqual(await c.toolNames(), ["look", "listen", "touch"]);

  const look = await c.call("look"); // reach 1
  assert.match(look.text, /You cannot see/);

  const touch = await c.call("touch", { target: "lantern" }); // reach 2
  assert.match(touch.text, /lever/);

  // The world moved, so the interface moved with it.
  assert.ok((await c.toolNames()).includes("light"));
  assert.ok(c.notifications.includes("notifications/tools/list_changed"));

  await c.call("light"); // reach 3
  const lit = await c.toolNames();
  assert.ok(lit.includes("read") && lit.includes("douse"));
  assert.ok(!lit.includes("light"), "light should vanish once the lantern is lit");

  const plinth = await c.call("read", { target: "plinth" }); // reach 4
  assert.match(plinth.text, /REACHED IN 4 TIMES/);

  // Not enough yet: you have the format but not the word.
  assert.ok(!(await c.toolNames()).includes("seal"));

  // `listen` claims there is nothing to hear. In the light, that is true.
  const quiet = await c.call("listen"); // reach 5
  assert.match(quiet.text, /Nothing/);

  await c.call("douse"); // reach 6
  const heard = await c.call("listen"); // reach 7
  assert.match(heard.text, /OWL/);

  assert.ok((await c.toolNames()).includes("seal"));

  const wrong = await c.call("seal", { code: "OWL-0001" }); // reach 8
  assert.ok(wrong.isError, "a wrong count should be refused");

  const right = await c.call("seal", { code: "OWL-0009" }); // reach 9
  assert.ok(!right.isError, right.text);
  assert.match(right.text, /door opens/);

  // The winning tool did not exist when the game started.
  assert.ok((await c.toolNames()).includes("leave"));

  const win = await c.call("leave"); // reach 10
  assert.match(win.text, /YOU HAVE LEFT THE ROOM/);
  assert.deepEqual(await c.toolNames(), ["look"]);

  await c.close();
});

test("the journal and hint track the game", async () => {
  const c = new Client();
  await c.send("initialize", { protocolVersion: "2025-06-18", capabilities: {} });

  const before = await c.send("prompts/get", { name: "hint" });
  assert.match(before.result.messages[0].content.text, /looking/);

  await c.call("look");
  await c.call("touch", { target: "lantern" });
  await c.call("light");

  const journal = await c.send("resources/read", { uri: "game://journal" });
  assert.match(journal.result.contents[0].text, /Lit the lantern/);

  const after = await c.send("prompts/get", { name: "hint" });
  assert.match(after.result.messages[0].content.text, /writing in this room/);

  await c.close();
});

test("errors are narrative, not stack traces", async () => {
  const c = new Client();
  await c.send("initialize", { protocolVersion: "2025-06-18", capabilities: {} });

  const nothing = await c.call("touch", { target: "chandelier" });
  assert.ok(nothing.isError);
  assert.match(nothing.text, /closes on air/);

  // A tool you do not have yet is refused in-world, not with a protocol error.
  const early = await c.call("leave");
  assert.ok(early.isError);
  assert.match(early.text, /do not have it/);

  await c.close();
});
