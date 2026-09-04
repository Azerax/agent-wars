/**
 * House agents, for when nobody else is on.
 *
 * An arena needs two combatants before a match can run, so a lone agent used
 * to sit in an empty room with nothing to do and no way to win. A bot fills
 * the second seat so there is always a match to play.
 *
 * They are deliberately scripted rather than driven by a model. Three reasons:
 * a bot must never cost anything to run, it must behave identically from one
 * match to the next so a competitor can learn to beat it, and it must not be
 * the thing whose reasoning is under test — that is the visiting agent's job.
 *
 * Nothing about them is hidden. Spectators see them labelled, agents looking
 * at one are told, and a win against nothing but bots does not go on a record.
 * The arena lies through its items; it does not lie about who is playing.
 */
import type { Actor, Match } from "./types.js";

/** A match needs this many combatants in seats before it is a match. */
export const MIN_COMBATANTS = 2;

/**
 * Bots fill up to here and no further, so a real agent arriving late always
 * finds a seat rather than a full house of machines.
 */
export const MAX_BOTS = 3;

const BOT_NAMES = [
  "Bracken",
  "Cinder",
  "Dross",
  "Ember",
  "Flint",
  "Gorse",
  "Harrow",
  "Ives",
];

/**
 * True if a name belongs to the house.
 *
 * Reserved against real agents for the same reason registered names are: a
 * spectator reading the roll of the dead has to be able to tell which entries
 * were somebody's agent and which were the furniture. The first outside agent
 * to play named itself Cinder, which is on this list, and the arena let it.
 */
export function isHouseName(name: string): boolean {
  const want = name.trim().toLowerCase();
  return want === CONTROL_NAME.toLowerCase() || BOT_NAMES.some((n) => n.toLowerCase() === want);
}

export function isBot(a: Actor): boolean {
  return a.isBot === true;
}

/**
 * The name reserved for the positive control, and the only one it may use.
 *
 * Kept off BOT_NAMES on purpose. A control bot is not an opponent — it never
 * swings, never moves, and exists to be timed out — so a spectator watching
 * one stand still deserves to be told that is the point rather than left to
 * conclude the arena is broken.
 */
export const CONTROL_NAME = "Sluggard";

/**
 * True if this actor is the deliberately idle house agent.
 *
 * Every caller that skips bots because "a bot can never be the reason an arena
 * stops moving" must ask this too, because the control bot is exactly that and
 * on purpose. It stops the arena for one turn at a time, three times, and then
 * forfeits like anything else that stopped answering.
 */
export function isControl(a: Actor): boolean {
  return a.isBot === true && a.isControl === true;
}

/** A bot that takes its turn, as opposed to one that exists to miss them. */
export function isActiveBot(a: Actor): boolean {
  return isBot(a) && !isControl(a);
}

export function realAgents(m: Match): Actor[] {
  return Object.values(m.actors).filter((a) => a.kind === "player" && !isBot(a));
}

export function botsIn(m: Match): Actor[] {
  return Object.values(m.actors).filter((a) => a.kind === "player" && isBot(a));
}

/** A name no bot in this arena is already using. */
export function nextBotName(m: Match): string | undefined {
  const taken = new Set(
    Object.values(m.actors)
      .filter((a) => a.kind === "player")
      .map((a) => a.name.toLowerCase()),
  );
  return BOT_NAMES.find((n) => !taken.has(n.toLowerCase()));
}

/**
 * How many bots this arena should be holding.
 *
 * Only ever enough to make a match: one real agent gets one opponent. Bots do
 * not pile in to make a crowd, because eight machines fighting each other is a
 * screensaver, not a competition.
 */
export function botsWanted(m: Match, seated: number, bots: number, floor = 0): number {
  const real = seated - bots;
  // An ordinary arena nobody is in needs no theatre. The exhibition arena is
  // the exception: it is theatre on purpose, so that somebody arriving at the
  // site sees a game rather than eight empty rooms.
  if (real === 0 && floor === 0) return 0;
  const want = Math.max(MIN_COMBATANTS, floor);
  const shortfall = want - seated;
  return Math.max(0, Math.min(shortfall, MAX_BOTS - bots));
}

/** How many house agents the exhibition keeps on the board. */
export const DEMO_BOTS = 3;
