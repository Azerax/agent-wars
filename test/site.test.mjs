import { test } from "node:test";
import assert from "node:assert/strict";

import { LOBBY_HTML, ARENA_HTML } from "../dist/royale/site.js";

const pages = { lobby: LOBBY_HTML, arena: ARENA_HTML };

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
