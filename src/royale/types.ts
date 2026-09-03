/** Battle royale: N agents, roaming monsters carrying gear, a closing storm. */

export type Slot = "weapon" | "offhand" | "armor" | "trinket";
export const SLOTS: Slot[] = ["weapon", "offhand", "armor", "trinket"];

export type Dir = "north" | "south" | "east" | "west";
export const DIRS: Record<Dir, [number, number]> = {
  north: [0, -1],
  south: [0, 1],
  east: [1, 0],
  west: [-1, 0],
};

export interface Item {
  id: string;
  name: string;
  slot: Slot;
  /** Flat bonuses while equipped. */
  atk?: number;
  def?: number;
  maxHp?: number;
  speed?: number;
  /**
   * Tools this item adds to its wearer's tool list. This is the whole game:
   * gear is not a stat line, it is a change to what you are able to do.
   */
  grants?: string[];
  /** Limited-use gear starts with this many charges. */
  charges?: number;
  desc: string;
}

export type ActorKind = "player" | "monster";

export interface Actor {
  id: string;
  kind: ActorKind;
  name: string;
  /**
   * False until the agent has named itself through the MCP tool. A nameless
   * agent occupies a seat and can do nothing but choose a name.
   */
  named?: boolean;
  x: number;
  y: number;
  hp: number;
  baseMaxHp: number;
  baseAtk: number;
  baseDef: number;
  baseSpeed: number;
  alive: boolean;
  equipped: Partial<Record<Slot, string>>;
  /** Remaining charges, keyed by item id. */
  charges: Record<string, number>;
  /** Halves incoming damage until this actor's next turn. */
  bracedUntilRound: number;
  kills: number;
  /** Round in which this actor last took a turn; a knife punishes the unready. */
  lastActedRound: number;
  /** Consecutive own-turns spent without changing tile. The floor is lava. */
  stillTurns: number;
  /** Behaviour, counted rather than declared. Titles are derived from this. */
  stats: ActorStats;
  /** Monster behaviour, unused for players. */
  brain?: "wander" | "hunter" | "guard";
  /** Where a guard returns to. */
  homeX?: number;
  homeY?: number;
  /** What this actor has perceived since it last acted. Its private feed. */
  inbox: string[];
}

/**
 * What an agent actually did, as opposed to what it says it did. Every title
 * in the game is computed from these numbers, so an agent cannot claim one.
 */
export interface ActorStats {
  playerKills: number;
  mobKills: number;
  steps: number;
  loots: number;
  lavaTicks: number;
  missedTurns: number;
  damageDealt: number;
  damageTaken: number;
}

export function newStats(): ActorStats {
  return {
    playerKills: 0, mobKills: 0, steps: 0, loots: 0,
    lavaTicks: 0, missedTurns: 0, damageDealt: 0, damageTaken: 0,
  };
}

export interface Corpse {
  x: number;
  y: number;
  name: string;
  items: string[];
}

/** A tile of smoke blocks line of sight until this round. */
export interface Smoke {
  x: number;
  y: number;
  untilRound: number;
}

export interface MatchConfig {
  width: number;
  height: number;
  seed: number;
  /** The storm contracts by one tile on every side this often. */
  stormEvery: number;
  stormDamage: number;
}

export interface Match {
  config: MatchConfig;
  /** Walls, as "x,y" keys. Everything else is open ground. */
  walls: Record<string, true>;
  actors: Record<string, Actor>;
  corpses: Corpse[];
  /** Items lying loose on the ground, e.g. gear you replaced. */
  ground: Corpse[];
  smoke: Smoke[];
  /** Turn order, by descending speed. Includes the dead; they are skipped. */
  order: string[];
  turnIndex: number;
  round: number;
  storm: { x0: number; y0: number; x1: number; y1: number };
  started: boolean;
  over: boolean;
  winner?: string;
  /** Monotonic id source so respawned mobs never reuse a name. */
  mobSerial: number;
  /** Wall-clock ms of the last mob respawn sweep. */
  lastRespawnAt: number;
  /** Wall-clock ms the current turn began, for the idle deadline. */
  turnStartedAt: number;
  /** Public play-by-play, newest last. Everyone can read this one. */
  feed: string[];
}

export interface ActionResult {
  match: Match;
  text: string;
  isError?: boolean;
  /** False for free actions (looking, looting) which do not cost a turn. */
  endsTurn: boolean;
}
