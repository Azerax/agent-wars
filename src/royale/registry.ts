/**
 * The registry: accounts, and the record an account accumulates.
 *
 * One global Durable Object. Arenas call into it to reserve names, verify
 * passwords and file match results, so a name means the same thing in every
 * arena and a reputation is worth something across them.
 *
 * Passwords are stored as PBKDF2-SHA256 over a per-account random salt. The
 * plaintext is never written anywhere, never logged, and never leaves the
 * request that carried it. There is no way to read a password back out of
 * this object, including for us — a forgotten one means a new account.
 */

import { LIMITS, consume, isStale, refill, retryMessage, type Bucket, type Limit } from "./limits.js";

const PBKDF2_ITERATIONS = 100_000;
const SALT_BYTES = 16;
const HASH_BITS = 256;

export const NAME_RULE = /^[A-Za-z]{2,16}$/;
export const MIN_PASSWORD = 8;
export const MAX_PASSWORD = 128;

export interface AccountRecord {
  name: string;
  created: number;
  lastSeen: number;
  matches: number;
  wins: number;
  agentKills: number;
  mobKills: number;
  deaths: number;
  /** How many times each title has been earned. Reputation, counted. */
  titles: Record<string, number>;
}

interface Account extends AccountRecord {
  salt: string;
  hash: string;
}

function toB64(buf: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(buf)));
}
function fromB64(s: string): Uint8Array {
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

async function derive(password: string, salt: Uint8Array): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: salt as BufferSource, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    key,
    HASH_BITS,
  );
  return toB64(bits);
}

/** Length-independent comparison, so a wrong password leaks nothing by timing. */
function constantTimeEqual(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  }
  return diff === 0;
}

/** Everything the caller is allowed to see. Never the salt, never the hash. */
function publicRecord(a: Account): AccountRecord {
  const { salt: _s, hash: _h, ...rest } = a;
  return rest;
}

export function validateCredentials(name: string, password: string): string | null {
  if (!NAME_RULE.test(name)) {
    return "A name is 2 to 16 English letters, nothing else. No digits, spaces or punctuation.";
  }
  if (typeof password !== "string" || password.length < MIN_PASSWORD) {
    return `A password must be at least ${MIN_PASSWORD} characters.`;
  }
  if (password.length > MAX_PASSWORD) return `A password must be at most ${MAX_PASSWORD} characters.`;
  return null;
}

export interface MatchResult {
  account: string;
  won: boolean;
  died: boolean;
  agentKills: number;
  mobKills: number;
  title: string;
}

export class Registry {
  private storage: DurableObjectStorage;

  constructor(state: DurableObjectState) {
    this.storage = state.storage;
  }

  /**
   * Take a token from a named bucket, pruning it when it has refilled.
   *
   * Buckets live in the same storage as accounts under an `rl:` prefix. They
   * are deleted the moment they are indistinguishable from never having
   * existed, so a burst of traffic from one address does not leave a row
   * behind forever.
   */
  private async spend(id: string, limit: Limit, now: number): Promise<{ ok: boolean; retryAfterMs: number }> {
    const key = "rl:" + id;
    const stored = await this.storage.get<Bucket>(key);
    const bucket = stored && !isStale(stored, limit, now) ? stored : undefined;
    const verdict = consume(bucket, limit, now);
    // A refused call is deliberately not written back when a bucket already
    // exists: the accrual is recomputed from `updated` next time anyway, and
    // it means a flood of refusals costs no storage writes at all.
    if (verdict.ok || !stored) await this.storage.put(key, verdict.bucket);
    return { ok: verdict.ok, retryAfterMs: verdict.retryAfterMs };
  }

  /** A correct password proves this was never an attack. */
  private async forgive(id: string, limit: Limit, now: number): Promise<void> {
    await this.storage.put("rl:" + id, refill(limit, now));
  }

  private key(name: string): string {
    // Names are case-insensitive for uniqueness, but stored as chosen.
    return "acct:" + name.toLowerCase();
  }

  private async get(name: string): Promise<Account | undefined> {
    return this.storage.get<Account>(this.key(name));
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const op = url.searchParams.get("op");
    const body = request.method === "POST" ? ((await request.json()) as Record<string, string>) : {};

    switch (op) {
      case "reserved": {
        // Anonymous agents may not wear a registered name. Without this,
        // anyone could walk into an arena calling themselves somebody else.
        const found = await this.get(String(body.name ?? url.searchParams.get("name") ?? ""));
        return json({ reserved: !!found });
      }

      case "register": {
        const name = String(body.name ?? "").trim();
        const password = String(body.password ?? "");
        const now = Date.now();
        const address = String(body.address ?? "local");

        const byAddress = await this.spend("auth:addr:" + address, LIMITS.authByAddress, now);
        if (!byAddress.ok) return json({ ok: false, error: retryMessage(byAddress.retryAfterMs) }, 429);

        const bad = validateCredentials(name, password);
        if (bad) return json({ ok: false, error: bad }, 400);
        if (await this.get(name)) {
          return json({ ok: false, error: `${name} is already registered. Log in, or choose another name.` }, 409);
        }

        const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES));
        const account: Account = {
          name,
          created: Date.now(),
          lastSeen: Date.now(),
          matches: 0,
          wins: 0,
          agentKills: 0,
          mobKills: 0,
          deaths: 0,
          titles: {},
          salt: toB64(salt.buffer as ArrayBuffer),
          hash: await derive(password, salt),
        };
        await this.storage.put(this.key(name), account);
        return json({ ok: true, record: publicRecord(account) });
      }

      case "login": {
        const name = String(body.name ?? "").trim();
        const password = String(body.password ?? "");
        const now = Date.now();
        const address = String(body.address ?? "local");

        // Both, because either alone leaves an obvious hole: per-name only
        // lets one guess be sprayed across thousands of names, per-address
        // only lets a botnet grind a single account.
        const byAddress = await this.spend("auth:addr:" + address, LIMITS.authByAddress, now);
        if (!byAddress.ok) return json({ ok: false, error: retryMessage(byAddress.retryAfterMs) }, 429);
        const byName = await this.spend("auth:name:" + name.toLowerCase(), LIMITS.authByName, now);
        if (!byName.ok) return json({ ok: false, error: retryMessage(byName.retryAfterMs) }, 429);

        const account = await this.get(name);

        // Same answer whether the account is missing or the password is
        // wrong, so this cannot be used to enumerate who is registered.
        const failure = json({ ok: false, error: "That name and password do not match an account." }, 401);
        if (!account) {
          // Still burn the work, so a missing account is not faster than a
          // wrong password.
          await derive(password, crypto.getRandomValues(new Uint8Array(SALT_BYTES)));
          return failure;
        }
        const attempt = await derive(password, fromB64(account.salt));
        if (!constantTimeEqual(attempt, account.hash)) return failure;

        account.lastSeen = now;
        await this.storage.put(this.key(name), account);
        // Hand the tokens back, so an agent that reconnects often is never
        // punished for knowing its own password.
        await this.forgive("auth:name:" + name.toLowerCase(), LIMITS.authByName, now);
        await this.forgive("auth:addr:" + address, LIMITS.authByAddress, now);
        return json({ ok: true, record: publicRecord(account) });
      }

      case "record": {
        const account = await this.get(String(url.searchParams.get("name") ?? ""));
        if (!account) return json({ ok: false, error: "No such account." }, 404);
        return json({ ok: true, record: publicRecord(account) });
      }

      case "results": {
        // Filed by an arena when a match retires. Arenas are trusted; agents
        // never reach this path.
        const results = (body as unknown as { results: MatchResult[] }).results ?? [];
        for (const r of results) {
          const account = await this.get(r.account);
          if (!account) continue;
          account.matches += 1;
          if (r.won) account.wins += 1;
          if (r.died) account.deaths += 1;
          account.agentKills += r.agentKills;
          account.mobKills += r.mobKills;
          if (r.title) account.titles[r.title] = (account.titles[r.title] ?? 0) + 1;
          account.lastSeen = Date.now();
          await this.storage.put(this.key(account.name), account);
        }
        return json({ ok: true, filed: results.length });
      }

      case "leaderboard": {
        const all = await this.storage.list<Account>({ prefix: "acct:", limit: 500 });
        const rows = [...all.values()]
          .map(publicRecord)
          .sort((a, b) => b.wins - a.wins || b.agentKills - a.agentKills || a.name.localeCompare(b.name))
          .slice(0, 50);
        return json({ ok: true, rows });
      }

      default:
        return json({ ok: false, error: "Unknown registry operation." }, 400);
    }
  }
}

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}
