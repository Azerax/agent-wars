import { test } from "node:test";
import assert from "node:assert/strict";

import { consume, fullBucket, refill, isStale, LIMITS, addressOf } from "../dist/royale/limits.js";

const LIMIT = { burst: 3, perMs: 1000 };

test("a burst is allowed, and then it is not", () => {
  let b;
  const t = 1_000_000;
  for (let i = 0; i < 3; i++) {
    const v = consume(b, LIMIT, t);
    assert.ok(v.ok, `call ${i + 1} of the burst should pass`);
    b = v.bucket;
  }
  const refused = consume(b, LIMIT, t);
  assert.equal(refused.ok, false, "the fourth in the same instant is refused");
  assert.ok(refused.retryAfterMs > 0);
  assert.ok(refused.retryAfterMs <= LIMIT.perMs);
});

test("tokens come back over time, not all at once", () => {
  let b = { tokens: 0, updated: 1_000_000 };

  assert.equal(consume(b, LIMIT, 1_000_500).ok, false, "half a token is not a token");

  const one = consume(b, LIMIT, 1_001_000);
  assert.ok(one.ok, "a full second earns exactly one");
  b = one.bucket;
  assert.equal(consume(b, LIMIT, 1_001_000).ok, false, "and only one");
});

test("a bucket never fills past its burst, however long it idles", () => {
  const b = { tokens: 0, updated: 0 };
  const v = consume(b, LIMIT, 1_000_000_000);
  assert.ok(v.ok);
  assert.equal(v.bucket.tokens, LIMIT.burst - 1, "a week idle is still only a burst");
});

test("a bucket that has refilled is worth nothing and can be dropped", () => {
  const spent = { tokens: 0, updated: 1_000_000 };
  assert.equal(isStale(spent, LIMIT, 1_000_000), false, "just spent: keep it");
  assert.equal(isStale(spent, LIMIT, 1_001_000), false, "one token back: still keep it");
  assert.equal(isStale(spent, LIMIT, 1_003_000), true, "fully refilled: identical to absent");
  assert.equal(isStale(fullBucket(LIMIT, 0), LIMIT, 0), true, "an untouched bucket is nothing");
});

test("consume never mutates the bucket it was given", () => {
  const b = { tokens: 1, updated: 5 };
  const snapshot = { ...b };
  consume(b, LIMIT, 5);
  consume(b, LIMIT, 5);
  assert.deepEqual(b, snapshot, "a refused call must be safe to discard");
});

test("refill hands the whole burst back", () => {
  const spent = { tokens: 0, updated: 1000 };
  assert.equal(consume(spent, LIMIT, 1000).ok, false);
  const given = refill(LIMIT, 1000);
  assert.equal(given.tokens, LIMIT.burst);
  assert.ok(consume(given, LIMIT, 1000).ok);
});

test("the configured limits are sane", () => {
  for (const [name, l] of Object.entries(LIMITS)) {
    assert.ok(l.burst >= 1, `${name} must allow at least one call`);
    assert.ok(l.perMs > 0, `${name} must refill`);
  }
  // A per-name limit is also an account-lockout weapon, so it is kept as
  // loose as it can be while remaining useless for guessing: about 8,600
  // attempts a day against one account, and ten seconds of lockout rather
  // than a minute for an agent caught by someone else's failures.
  const perDay = (86_400_000 / LIMITS.authByName.perMs) + LIMITS.authByName.burst;
  assert.ok(perDay < 20_000, "guessing must stay hopeless");
  assert.ok(LIMITS.authByName.perMs <= 15_000, "an innocent lockout must be short");
  assert.ok(
    LIMITS.authByAddress.perMs >= LIMITS.authByName.perMs,
    "the address bucket caps hashing work and should be the tighter one",
  );
});

test("only the edge-set address header is trusted", () => {
  const spoofed = new Request("https://x/", {
    headers: { "x-forwarded-for": "1.2.3.4", "x-real-ip": "5.6.7.8" },
  });
  assert.equal(addressOf(spoofed), "local", "client-set headers must not choose the bucket");

  const real = new Request("https://x/", { headers: { "cf-connecting-ip": "9.9.9.9" } });
  assert.equal(addressOf(real), "9.9.9.9");
});
