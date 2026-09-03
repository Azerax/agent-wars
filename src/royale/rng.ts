/**
 * Seeded RNG (mulberry32). Every match is a pure function of its seed, so a
 * bug is reproducible and a test can play a whole game and assert the outcome.
 * The seed is derived from the match state, never held as hidden mutable
 * cursor state, so the same match replays identically.
 */
export function rngFrom(seed: number): () => number {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function pick<T>(rand: () => number, list: readonly T[]): T {
  return list[Math.floor(rand() * list.length)];
}

export function range(rand: () => number, lo: number, hi: number): number {
  return lo + Math.floor(rand() * (hi - lo + 1));
}
