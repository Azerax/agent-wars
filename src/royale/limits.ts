/**
 * Rate limiting: token buckets, kept pure so they can be reasoned about and
 * tested without a Worker.
 *
 * A bucket holds `burst` tokens and refills one every `perMs`. An action takes
 * a token; with none left it is refused and told how long to wait. That shape
 * suits this game better than a fixed window, because a legitimate agent is
 * bursty — it will look, check status and loot in the same breath — and then
 * quiet for seconds while it thinks. A fixed window punishes exactly that.
 */

export interface Limit {
  /** Tokens available at rest. How much burst is tolerated. */
  burst: number;
  /** Milliseconds to earn one token back. The sustained rate. */
  perMs: number;
}

export interface Bucket {
  tokens: number;
  updated: number;
}

export interface Verdict {
  ok: boolean;
  bucket: Bucket;
  retryAfterMs: number;
}

export const LIMITS = {
  /**
   * Agent actions. Turn-gated verbs are already limited by the turn order, but
   * the free ones — look, status, wait, loot — are not, and an agent polling
   * `wait` in a tight loop would otherwise hammer the arena flat.
   */
  toolCall: { burst: 40, perMs: 250 },

  /** Claiming a seat. Cheap for a person, expensive for a script. */
  seatClaim: { burst: 6, perMs: 30_000 },

  /**
   * Login and registration attempts, counted per account name and per address.
   *
   * Per-name alone would let someone spray one guess across thousands of
   * names; per-address alone would let a botnet grind a single account. Both
   * are needed, and either refusing is enough to refuse the request.
   *
   * Any per-name limit can be turned around and used to lock a real agent out
   * of its own account by deliberately failing against its name. That cannot
   * be designed away without another signal, so it is bounded instead: six
   * guesses a minute is roughly 8,600 a day, which is worthless against any
   * password this game accepts, while a wrongly locked-out agent waits ten
   * seconds rather than a minute. The address bucket is the tighter of the
   * two because it is what caps the PBKDF2 work an attacker can force.
   */
  authByName: { burst: 10, perMs: 10_000 },
  authByAddress: { burst: 12, perMs: 30_000 },

  /** The spectator API. Generous — the site itself polls once a second. */
  publicRead: { burst: 90, perMs: 500 },
} satisfies Record<string, Limit>;

export function fullBucket(limit: Limit, now: number): Bucket {
  return { tokens: limit.burst, updated: now };
}

/**
 * Spend one token. Returns the new bucket and whether the action may proceed.
 * Never mutates its argument, so a refused call is trivially safe to ignore.
 */
export function consume(
  existing: Bucket | undefined,
  limit: Limit,
  now: number,
  cost = 1,
): Verdict {
  const prior = existing ?? fullBucket(limit, now);
  const earned = Math.max(0, now - prior.updated) / limit.perMs;
  const tokens = Math.min(limit.burst, prior.tokens + earned);

  if (tokens < cost) {
    const shortfall = cost - tokens;
    return {
      ok: false,
      bucket: { tokens, updated: now },
      retryAfterMs: Math.ceil(shortfall * limit.perMs),
    };
  }

  return { ok: true, bucket: { tokens: tokens - cost, updated: now }, retryAfterMs: 0 };
}

/**
 * True once a stored bucket has refilled completely, making it identical to
 * having no bucket at all.
 *
 * Checked when loading rather than when writing: a bucket always has tokens
 * missing immediately after a call, so there is no moment at write time when
 * it is safe to drop. Pruning on read is what stops one row accumulating per
 * address, forever, for traffic that turned out to be ordinary.
 */
export function isStale(bucket: Bucket, limit: Limit, now: number): boolean {
  return bucket.tokens + (now - bucket.updated) / limit.perMs >= limit.burst;
}

/** Give a bucket back its tokens — a correct password is not an attack. */
export function refill(limit: Limit, now: number): Bucket {
  return fullBucket(limit, now);
}

export function retryMessage(ms: number): string {
  const seconds = Math.max(1, Math.ceil(ms / 1000));
  return `Too many requests. Wait ${seconds} second${seconds === 1 ? "" : "s"} and try again.`;
}

/**
 * The caller's address, as Cloudflare reports it.
 *
 * `cf-connecting-ip` is set by the edge and cannot be spoofed by the client;
 * headers a client can set (x-forwarded-for and friends) are deliberately
 * ignored, because trusting those would let an attacker pick a fresh bucket
 * per request. Absent locally, where one shared bucket is the honest answer.
 */
export function addressOf(request: Request): string {
  return request.headers.get("cf-connecting-ip") ?? "local";
}
