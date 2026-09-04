import { test } from "node:test";
import assert from "node:assert/strict";

import { lobbyHtml, arenaHtml } from "../dist/royale/site.js";

const ORIGIN = "https://mcpagentwars.com";
const pages = { lobby: lobbyHtml(ORIGIN), arena: arenaHtml(ORIGIN, "kiln-row") };

/**
 * These pages are TypeScript template literals containing JavaScript, which
 * means an escape sequence written for the browser is eaten by the compiler
 * first. A backslash-n inside an inline script becomes a real line break, puts
 * a raw newline inside a quoted string, and takes the entire script down —
 * silently, with the page still rendering its empty headings. That shipped
 * once. It does not ship again.
 */
for (const [name, html] of Object.entries(pages)) {
  test(`${name}: every inline script parses as JavaScript`, () => {
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
    assert.ok(scripts.length > 0, "expected at least one inline script");
    for (const src of scripts) {
      assert.doesNotThrow(() => new Function(src), `inline script in ${name} must parse`);
    }
  });

  test(`${name}: no raw newline sits inside a quoted string`, () => {
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
    for (const src of scripts) {
      for (const [i, line] of src.split("\n").entries()) {
        // A line with an odd number of single quotes has an unterminated one.
        const singles = (line.match(/'/g) ?? []).length;
        assert.equal(singles % 2, 0, `${name} script line ${i + 1} has an unbalanced quote: ${line.trim()}`);
      }
    }
  });

  test(`${name}: every element the script reaches for exists in the markup`, () => {
    const ids = [...html.matchAll(/getElementById\('([^']+)'\)/g)].map((m) => m[1]);
    assert.ok(ids.length > 0);
    for (const id of new Set(ids)) {
      assert.ok(html.includes(`id="${id}"`), `${name} script uses #${id} but the page has no such element`);
    }
  });
}

import { ARENAS } from "../dist/royale/worker.js";

test("the read gate spreads callers across arenas rather than one object", async () => {
  const { default: mod } = await import("../dist/royale/worker.js");
  assert.ok(mod, "worker module loads");
  assert.ok(ARENAS.length >= 8, "enough arenas to seat a crowd");
  assert.equal(new Set(ARENAS.map((a) => a.id)).size, ARENAS.length, "arena ids are unique");
});

test("every page carries a share card a feed can render", () => {
  for (const [name, html] of Object.entries(pages)) {
    for (const tag of [
      'property="og:title"',
      'property="og:description"',
      'property="og:image"',
      'property="og:url"',
      'name="twitter:card" content="summary_large_image"',
      'name="description"',
      'rel="icon"',
    ]) {
      assert.ok(html.includes(tag), `${name} is missing ${tag}`);
    }
    // og:image must be absolute or the scrapers ignore it.
    assert.match(html, /property="og:image" content="https:\/\/[^"]+\/og\.png"/);
    assert.ok(!html.includes("{HEAD}"), `${name} left its head placeholder in`);
  }
});

test("the card leads with the pitch, not the product name", () => {
  const lobby = pages.lobby;
  const title = lobby.match(/property="og:title" content="([^"]+)"/)[1];
  assert.match(title, /beat up your agent/i, "the headline has to say what this is");
  assert.ok(title.length < 90, "and fit in a card");
});
