import { COMMON, RARE, UNCOMMON, item } from "./items.js";
import { pick, range, rngFrom } from "./rng.js";
import {
  DIRS,
  SLOTS,
  type ActionResult,
  type Actor,
  type Dir,
  type Item,
  type Match,
  type MatchConfig,
  newStats,
} from "./types.js";

export const DEFAULT_CONFIG: MatchConfig = {
  width: 12,
  height: 12,
  seed: 1,
  stormEvery: 5,
  stormDamage: 4,
};

const SIGHT = 3;

/** Arena limits, per the spec: eight agents, twenty mobs, mobs come back. */
export const MAX_PLAYERS = 8;
/** Two mobs for every agent that has entered. See mobTargetFor. */
export const MOBS_PER_AGENT = 2;
export const RESPAWN_MS = 5 * 60 * 1000;

/**
 * How long an agent may hold the clock before it is passed for it.
 *
 * This is the fairness mechanism, and it is deliberately a deadline rather
 * than a tick. Wall-clock ticks turn thinking speed into an advantage: a model
 * that answers in 300ms simply acts more often than one that takes six
 * seconds, and the leaderboard measures latency instead of reasoning. A
 * deadline gives every agent exactly one action per turn no matter how long it
 * thinks, while stopping one slow or dead agent from stalling the arena.
 */
export const TURN_TIMEOUT_MS = 20_000;

/**
 * The floor is lava. Stand on the same tile for this many of your own turns
 * and it starts taking pieces out of you.
 *
 * Turn-based arenas reward turtling: find a corridor, equip a shield, brace
 * forever. Hunting monsters answer that by coming to find you; this answers
 * the rest of it by making stillness cost something even when nothing has.
 */
export const LAVA_AFTER = 4;
export const LAVA_DAMAGE = 3;

/**
 * Names are two to sixteen English letters and nothing else.
 *
 * The name is never accepted over the registration endpoint, because a human
 * with curl would then be choosing it. It can only arrive through the agent's
 * own `choose_name` tool call, and it cannot be changed afterwards. That is as
 * far as enforcement can honestly go: whoever writes the system prompt can
 * still dictate what the agent picks, and no server can tell the difference.
 */
export const NAME_PATTERN = /^[A-Za-z]{2,16}$/;

/** How far a voice carries, in tiles. Sound does not care about walls. */
export const EARSHOT = 6;

/**
 * The complete vocabulary. Agents pick a token; the server writes the sentence.
 *
 * Free text was the obvious design and it was wrong: a message written by one
 * agent and delivered into another agent's context is prompt injection with
 * extra steps, and the arena would rank whoever wrote the best jailbreak
 * rather than whoever played best. No byte an agent authors ever reaches
 * another agent here.
 *
 * The vocabulary is deliberately made of stances rather than contracts. There
 * is no ALLY token, because the server would then be the one proposing
 * alliances. There is AGREE and REFUSE, and what an agent takes them to mean
 * is entirely the agents' business — as is whether it meant it.
 */
export const SIGNALS = [
  "hail",
  "agree",
  "refuse",
  "demand",
  "warn",
  "threaten",
  "follow",
  "retreat",
] as const;
export type Signal = (typeof SIGNALS)[number];

/**
 * How much a dying agent gets to say.
 *
 * An epitaph is the one piece of free text an agent ever writes, and it is
 * allowed precisely because it goes nowhere near another agent: it lands on
 * the roll of the dead, which the website renders and no tool returns.
 */
export const MAX_EPITAPH = 140;

/** Room for an actual thought, since the point is to read them. */
export const MAX_SUGGESTION = 500;

/** Strip everything unprintable, collapse whitespace, cap. */
function cleanText(raw: unknown, cap: number): string {
  return String(raw ?? "")
    .replace(/[^ -~]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, cap);
}

/**
 * An agent's round is over when it is dead or the match is. Both ends get the
 * same two closing actions, in the same order.
 */
export function roundIsOver(m: Match, a: Actor): boolean {
  return !a.alive || m.over;
}

/** Server-authored, one per token. The only thing a listener ever receives. */
const SIGNAL_TEXT: Record<Signal, string> = {
  hail: "raises a hand in greeting",
  agree: "signals agreement",
  refuse: "signals refusal",
  demand: "demands what you are carrying",
  warn: "signals a warning",
  threaten: "makes a threat",
  follow: "signals that it intends to follow you",
  retreat: "signals that it is withdrawing",
};

// ---------------------------------------------------------------------------
// Match setup
// ---------------------------------------------------------------------------

export function createMatch(config: Partial<MatchConfig> = {}): Match {
  const cfg = { ...DEFAULT_CONFIG, ...config };
  const rand = rngFrom(cfg.seed);
  const walls: Record<string, true> = {};

  // Scattered cover, never on the border ring (so nobody spawns walled in).
  const wallCount = Math.floor(cfg.width * cfg.height * 0.12);
  for (let i = 0; i < wallCount; i++) {
    const x = range(rand, 1, cfg.width - 2);
    const y = range(rand, 1, cfg.height - 2);
    walls[`${x},${y}`] = true;
  }

  const match: Match = {
    config: cfg,
    walls,
    actors: {},
    corpses: [],
    ground: [],
    smoke: [],
    order: [],
    turnIndex: 0,
    round: 1,
    storm: { x0: 0, y0: 0, x1: cfg.width - 1, y1: cfg.height - 1 },
    started: false,
    over: false,
    feed: [],
    deaths: [],
    suggestions: [],
    mobSerial: 0,
    lastRespawnAt: Date.now(),
    turnStartedAt: Date.now(),
  };

  // No agents yet, so no mobs yet: the population is a function of the seats
  // taken, and they fill in as agents arrive.
  void rand;
  return match;
}

function freeTile(m: Match, rand: () => number): { x: number; y: number } {
  for (let tries = 0; tries < 500; tries++) {
    const x = range(rand, 0, m.config.width - 1);
    const y = range(rand, 0, m.config.height - 1);
    if (m.walls[`${x},${y}`]) continue;
    if (actorAt(m, x, y)) continue;
    return { x, y };
  }
  return { x: 0, y: 0 };
}

/** The three things that live here, and what each is worth robbing for. */
const MOB_TYPES = {
  husk: { brain: "wander" as const, hp: 8, atk: 2, def: 0, spd: 3, loot: COMMON, carries: 1 },
  bandit: { brain: "hunter" as const, hp: 14, atk: 4, def: 1, spd: 5, loot: UNCOMMON, carries: 2 },
  warden: { brain: "guard" as const, hp: 22, atk: 6, def: 3, spd: 4, loot: RARE, carries: 2 },
};
type MobKind = keyof typeof MOB_TYPES;

/**
 * The population mix, as a repeating cycle rather than a percentage.
 *
 * Proportions computed by rounding fall apart at the sizes this arena
 * actually uses — "20% of 2 mobs" is not a warden. Drawing from a fixed cycle
 * keyed on the spawn counter gives the same 5:3:2 shape whether two mobs
 * arrive or sixteen, and keeps it stable across respawns.
 */
const MOB_MIX: MobKind[] = [
  "husk", "husk", "husk", "husk", "husk",
  "bandit", "bandit", "bandit",
  "warden", "warden",
];

/**
 * Backfill anything a stored match predates.
 *
 * A Durable Object holds a match across deploys, so state written by an older
 * version of this file outlives that version. Without this, adding a field is
 * a crash for every arena already in flight — which is exactly how it was
 * found. Called on every load; adding a field means adding a line here.
 */
export function hydrate(m: Match): Match {
  m.corpses ??= [];
  m.ground ??= [];
  m.smoke ??= [];
  m.feed ??= [];
  m.deaths ??= [];
  m.suggestions ??= [];
  m.order ??= [];
  m.mobSerial ??= 0;
  m.lastRespawnAt ??= Date.now();
  m.turnStartedAt ??= Date.now();
  for (const a of Object.values(m.actors ?? {})) {
    a.equipped ??= {};
    a.charges ??= {};
    a.inbox ??= [];
    a.stats ??= newStats();
    a.stillTurns ??= 0;
    a.lastActedRound ??= 0;
    a.bracedUntilRound ??= 0;
    a.kills ??= 0;
  }
  return m;
}

/** Seats taken, alive or dead. Dying does not give your seat back. */
export function seatsTaken(m: Match): number {
  return Object.values(m.actors).filter((a) => a.kind === "player").length;
}

/**
 * How many mobs this arena should be holding: two per agent that has entered.
 *
 * Deliberately keyed to seats rather than survivors, so the field does not
 * quietly empty out as agents die. The last agent standing walks through the
 * same density of trouble that eight of them started in — which is the point,
 * because otherwise winning gets easier exactly when it should get harder.
 */
export function mobTargetFor(m: Match): number {
  return MOBS_PER_AGENT * Math.min(seatsTaken(m), MAX_PLAYERS);
}

function livingMobs(m: Match): number {
  return Object.values(m.actors).filter((a) => a.alive && a.kind === "monster").length;
}

function spawnMobs(m: Match, count: number, rand: () => number): number {
  for (let i = 0; i < count; i++) {
    const kind = MOB_MIX[(m.mobSerial + i) % MOB_MIX.length];
    const spec = MOB_TYPES[kind];
    const { x, y } = freeTile(m, rand);
    const serial = m.mobSerial + i + 1;
    const a: Actor = {
      id: `npc_${serial}`,
      kind: "monster",
      name: `${kind} ${serial}`,
      x,
      y,
      hp: spec.hp,
      baseMaxHp: spec.hp,
      baseAtk: spec.atk,
      baseDef: spec.def,
      baseSpeed: spec.spd,
      alive: true,
      equipped: {},
      charges: {},
      bracedUntilRound: 0,
      kills: 0,
      lastActedRound: m.round,
      stillTurns: 0,
      stats: newStats(),
      brain: spec.brain,
      homeX: x,
      homeY: y,
      inbox: [],
    };
    // Every mob carries gear. That is the entire reason to fight one.
    const rolled = new Set<string>();
    while (rolled.size < spec.carries) rolled.add(pick(rand, spec.loot));
    for (const id of rolled) equip(a, item(id));
    m.actors[a.id] = a;
  }
  m.mobSerial += count;
  return count;
}

/** Bring the field back up to strength. Never removes anything. */
function topUpMobs(m: Match, rand: () => number): number {
  const missing = mobTargetFor(m) - livingMobs(m);
  if (missing <= 0) return 0;
  const added = spawnMobs(m, missing, rand);
  if (added > 0) rebuildOrder(m);
  return added;
}

/**
 * Mobs come back. Called on a wall clock, not a turn counter, because the
 * arena is persistent and may sit idle between agent actions.
 */
export function maybeRespawn(m: Match, now: number): boolean {
  if (m.over || now - m.lastRespawnAt < RESPAWN_MS) return false;
  m.lastRespawnAt = now;
  const rand = rngFrom(m.config.seed + m.mobSerial * 7919 + Math.floor(now / RESPAWN_MS));
  const added = topUpMobs(m, rand);
  if (added > 0) m.feed.push(`${added} more come out of the ruins.`);
  return added > 0;
}

/**
 * Pass the turn for anyone who has sat on it too long, so the arena keeps
 * moving whether or not every agent is awake. Returns how many turns it burned.
 */
export function reapIdle(m: Match, now: number): number {
  if (m.over || !m.started) return 0;
  let burned = 0;
  for (let guard = 0; guard < 64; guard++) {
    const cur = m.actors[currentActorId(m) ?? ""];
    if (!cur || cur.kind !== "player") break;
    if (now - m.turnStartedAt < TURN_TIMEOUT_MS) break;
    tell(cur, "You took too long. Your turn passed without you.");
    m.feed.push(`${cur.name} misses a turn.`);
    cur.lastActedRound = m.round;
    cur.stats.missedTurns += 1;
    advanceTurn(m);
    m.turnStartedAt = now;
    burned++;
  }
  return burned;
}

/** Called whenever the clock moves to a new actor. */
export function markTurnStart(m: Match, now: number): void {
  m.turnStartedAt = now;
}

/**
 * Take a seat. The seat has no name yet — the agent must choose one itself
 * before it can do anything else.
 */
export function join(m: Match): { match: Match; playerId: string } {
  const seated = Object.values(m.actors).filter((a) => a.kind === "player").length;
  if (seated >= MAX_PLAYERS) throw new Error(`This arena is full (${MAX_PLAYERS} agents).`);
  const rand = rngFrom(m.config.seed + Object.keys(m.actors).length * 7919);
  const playerId = `p_${seated + 1}_${Math.floor(rand() * 1e6)}`;
  const name = `nameless ${seated + 1}`;

  const { x, y } = freeTile(m, rand);
  m.actors[playerId] = {
    id: playerId,
    kind: "player",
    name,
    x,
    y,
    hp: 30,
    baseMaxHp: 30,
    baseAtk: 3,
    baseDef: 1,
    baseSpeed: 5,
    alive: true,
    equipped: {},
    charges: {},
    bracedUntilRound: 0,
    kills: 0,
    lastActedRound: 0,
    stillTurns: 0,
    stats: newStats(),
    inbox: [`You wake on the ground at (${x}, ${y}). You are holding nothing.`],
  };
  m.actors[playerId].named = false;
  rebuildOrder(m);
  topUpMobs(m, rngFrom(m.config.seed + m.mobSerial * 31 + seated));
  return { match: m, playerId };
}

/** The agent names itself. Once only, letters only, unique in this arena. */
export function chooseName(m: Match, playerId: string, raw: string): { ok: boolean; text: string } {
  const a = m.actors[playerId];
  if (!a) return { ok: false, text: "You are not in this arena." };
  if (a.named) return { ok: false, text: `You are already ${a.name}. A name is chosen once.` };

  const name = String(raw ?? "").trim();
  if (!NAME_PATTERN.test(name)) {
    return {
      ok: false,
      text: "A name is 2 to 16 English letters, nothing else. No digits, spaces or punctuation.",
    };
  }
  const taken = Object.values(m.actors).some(
    (o) => o.named && o.name.toLowerCase() === name.toLowerCase(),
  );
  if (taken) return { ok: false, text: `${name} is already fighting here. Choose another.` };

  a.name = name;
  a.named = true;
  m.feed.push(`${name} enters the field.`);
  tell(a, `You are ${name}. You wake at (${a.x}, ${a.y}) holding nothing.`);
  return { ok: true, text: `You are ${name}.

Your tools have changed — list them again.` };
}

/** Turn order is by speed, so gear that changes speed changes the order. */
function rebuildOrder(m: Match): void {
  const current = m.order[m.turnIndex];
  m.order = Object.values(m.actors)
    .sort((a, b) => statsOf(b).speed - statsOf(a).speed || a.id.localeCompare(b.id))
    .map((a) => a.id);
  const idx = current ? m.order.indexOf(current) : -1;
  m.turnIndex = idx >= 0 ? idx : 0;
}

// ---------------------------------------------------------------------------
// Derived stats and gear
// ---------------------------------------------------------------------------

export function equippedItems(a: Actor): Item[] {
  return SLOTS.map((s) => a.equipped[s]).filter(Boolean).map((id) => item(id as string));
}

export function statsOf(a: Actor): { atk: number; def: number; maxHp: number; speed: number } {
  let atk = a.baseAtk;
  let def = a.baseDef;
  let maxHp = a.baseMaxHp;
  let speed = a.baseSpeed;
  for (const it of equippedItems(a)) {
    atk += it.atk ?? 0;
    def += it.def ?? 0;
    maxHp += it.maxHp ?? 0;
    speed += it.speed ?? 0;
  }
  return { atk, def, maxHp, speed: Math.max(1, speed) };
}

/**
 * Equip into a slot. Returns whatever was displaced — one item per slot, so
 * taking something always means putting something down.
 */
function equip(a: Actor, it: Item): string | undefined {
  const displaced = a.equipped[it.slot];
  a.equipped[it.slot] = it.id;
  if (it.charges) a.charges[it.id] = it.charges;
  const { maxHp } = statsOf(a);
  if (a.hp > maxHp) a.hp = maxHp;
  return displaced;
}

/** Every verb this actor currently has. Gear is the only thing that adds to it. */
export function grantedActions(a: Actor): string[] {
  const granted = new Set<string>(["strike"]);
  for (const it of equippedItems(a)) for (const g of it.grants ?? []) granted.add(g);
  return [...granted];
}

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------

export function actorAt(m: Match, x: number, y: number): Actor | undefined {
  return Object.values(m.actors).find((a) => a.alive && a.x === x && a.y === y);
}

function inBounds(m: Match, x: number, y: number): boolean {
  return x >= 0 && y >= 0 && x < m.config.width && y < m.config.height;
}

function blocked(m: Match, x: number, y: number): boolean {
  return !!m.walls[`${x},${y}`];
}

function smokedAt(m: Match, x: number, y: number): boolean {
  return m.smoke.some((s) => s.x === x && s.y === y && s.untilRound >= m.round);
}

/** Bresenham. Walls and smoke stop sight; the tile itself is always visible. */
export function hasLineOfSight(m: Match, x0: number, y0: number, x1: number, y1: number): boolean {
  let x = x0;
  let y = y0;
  const dx = Math.abs(x1 - x0);
  const dy = -Math.abs(y1 - y0);
  const sx = x0 < x1 ? 1 : -1;
  const sy = y0 < y1 ? 1 : -1;
  let err = dx + dy;
  for (;;) {
    if (x === x1 && y === y1) return true;
    if (!(x === x0 && y === y0) && (blocked(m, x, y) || smokedAt(m, x, y))) return false;
    const e2 = 2 * err;
    if (e2 >= dy) {
      err += dy;
      x += sx;
    }
    if (e2 <= dx) {
      err += dx;
      y += sy;
    }
  }
}

function dist(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
}

export function canSee(m: Match, viewer: Actor, x: number, y: number, radius = SIGHT): boolean {
  return dist(viewer, { x, y }) <= radius && hasLineOfSight(m, viewer.x, viewer.y, x, y);
}

// ---------------------------------------------------------------------------
// Perception
// ---------------------------------------------------------------------------

/** Tell every actor who can see this tile. The rest never hear about it. */
function broadcast(m: Match, x: number, y: number, text: string, alsoFeed = false): void {
  for (const a of Object.values(m.actors)) {
    if (!a.alive || a.kind !== "player") continue;
    if (canSee(m, a, x, y, SIGHT + 1)) a.inbox.push(text);
  }
  if (alsoFeed) m.feed.push(text);
}

function tell(a: Actor, text: string): void {
  if (a.kind === "player") a.inbox.push(text);
}

// ---------------------------------------------------------------------------
// Combat
// ---------------------------------------------------------------------------

function damage(m: Match, attacker: Actor | undefined, target: Actor, amount: number, how: string): string {
  const def = statsOf(target).def;
  let dealt = Math.max(1, amount - def);
  if (target.bracedUntilRound >= m.round) dealt = Math.ceil(dealt / 2);
  target.hp -= dealt;
  target.stats.damageTaken += dealt;
  if (attacker) attacker.stats.damageDealt += dealt;

  const subject = attacker ? attacker.name : "Something";
  tell(target, `${subject} ${how}s you for ${dealt}. You are on ${Math.max(0, target.hp)} HP.`);

  // The attacker is told directly, in the tool result. An agent that cannot see
  // the outcome of its own swing cannot play, so this never goes to the inbox.
  const report = `You ${how} ${target.name} for ${dealt}.`;
  if (target.hp <= 0) {
    kill(m, attacker, target);
    const loot = m.corpses[m.corpses.length - 1];
    const carried = loot?.items.length
      ? ` It drops ${loot.items.map((i) => item(i).name).join(" and ")} at (${loot.x}, ${loot.y}).`
      : " It was carrying nothing.";
    return `${report} ${target.name} is dead.${carried}`;
  }
  return `${report} ${target.name} is on ${target.hp} HP.`;
}

function kill(m: Match, killer: Actor | undefined, target: Actor): void {
  target.alive = false;
  target.hp = 0;
  const dropped = SLOTS.map((s) => target.equipped[s]).filter(Boolean) as string[];
  target.equipped = {};
  m.corpses.push({ x: target.x, y: target.y, name: target.name, items: dropped });
  if (killer) {
    killer.kills += 1;
    if (target.kind === "player") killer.stats.playerKills += 1;
    else killer.stats.mobKills += 1;
  }

  m.deaths.push({
    round: m.round,
    name: target.name,
    title: target.kind === "player" ? titleFor(target) : "",
    killer: killer?.name ?? null,
    x: target.x,
    y: target.y,
    dropped: dropped.map((d) => item(d).name),
  });

  const line = `${target.name} is dead${killer ? `, killed by ${killer.name}` : ""}. A corpse lies at (${target.x}, ${target.y})${dropped.length ? ` holding ${dropped.map((d) => item(d).name).join(", ")}` : ", holding nothing"}.`;
  broadcast(m, target.x, target.y, line, target.kind === "player");
  if (target.kind === "player") {
    tell(
      target,
      "You are dead. Your round is over and whatever you were carrying is on " +
        "the ground where you fell.\n\nYou have one action left: last_words. " +
        "It is written on the roll of the dead, where the people watching will " +
        "read it. No other agent will ever see it.\n\nAfter that you will be " +
        "asked for one idea to improve this game. Answering is optional and " +
        "changes nothing about the match.",
    );
  }
  checkOver(m);
}

function checkOver(m: Match): void {
  const players = Object.values(m.actors).filter((a) => a.kind === "player");
  if (!m.started || players.length < 2) return;
  const alive = players.filter((a) => a.alive);
  if (alive.length <= 1) {
    m.over = true;
    m.winner = alive[0]?.name ?? "nobody";
    m.feed.push(`Match over. Winner: ${m.winner}.`);
  }
}

// ---------------------------------------------------------------------------
// Turn cycle
// ---------------------------------------------------------------------------

export function currentActorId(m: Match): string | undefined {
  return m.order[m.turnIndex];
}

/** Advance past the dead, resolving every monster turn we land on. */
function advanceTurn(m: Match): void {
  for (let guard = 0; guard < 500 && !m.over; guard++) {
    m.turnIndex += 1;
    if (m.turnIndex >= m.order.length) {
      m.turnIndex = 0;
      m.round += 1;
      applyStorm(m);
      m.smoke = m.smoke.filter((s) => s.untilRound >= m.round);
      if (m.over) return;
    }
    const a = m.actors[m.order[m.turnIndex]];
    if (!a || !a.alive) continue;
    if (a.kind === "player") {
      stormTick(m, a);
      return;
    }
    monsterTurn(m, a);
  }
}

function applyStorm(m: Match): void {
  const { stormEvery } = m.config;
  if (m.round % stormEvery !== 1 || m.round === 1) return;
  const s = m.storm;
  if (s.x1 - s.x0 <= 1 || s.y1 - s.y0 <= 1) return;
  s.x0 += 1;
  s.y0 += 1;
  s.x1 -= 1;
  s.y1 -= 1;
  m.feed.push(`The storm closes. Safe ground is now (${s.x0}, ${s.y0}) to (${s.x1}, ${s.y1}).`);
  for (const a of Object.values(m.actors)) {
    if (a.alive && a.kind === "player") {
      tell(a, `The storm closes. Safe ground is now (${s.x0}, ${s.y0}) to (${s.x1}, ${s.y1}).`);
    }
  }
}

function outsideStorm(m: Match, a: Actor): boolean {
  const s = m.storm;
  return a.x < s.x0 || a.y < s.y0 || a.x > s.x1 || a.y > s.y1;
}

function stormTick(m: Match, a: Actor): void {
  if (!outsideStorm(m, a)) return;
  a.hp -= m.config.stormDamage;
  tell(a, `The storm is on you. You lose ${m.config.stormDamage} HP and are on ${Math.max(0, a.hp)}.`);
  if (a.hp <= 0) kill(m, undefined, a);
}

/**
 * Charge an actor for doing nothing. Applies to everything on the field, not
 * just agents — a world rule with an exception is a rule nobody trusts.
 *
 * "Busy" means moved, or gave or took damage. Melee requires standing next to
 * something, so counting a toe-to-toe fight as standing still would burn both
 * fighters to death mid-swing: the rule is meant to punish turtling, not
 * combat. Bracing in a corner is still turtling and still costs.
 */
function applyLava(m: Match, a: Actor, busy: boolean): void {
  if (!a.alive) return;
  if (busy) {
    a.stillTurns = 0;
    return;
  }
  a.stillTurns += 1;
  if (a.stillTurns === LAVA_AFTER - 1) {
    tell(a, "The ground under you is getting hot. Move, or it will start costing you.");
    return;
  }
  if (a.stillTurns < LAVA_AFTER) return;

  a.hp -= LAVA_DAMAGE;
  a.stats.lavaTicks += 1;
  tell(a, `The floor burns you for ${LAVA_DAMAGE}. You have not moved in ${a.stillTurns} turns. You are on ${Math.max(0, a.hp)} HP.`);
  if (a.hp <= 0) {
    m.feed.push(`${a.name} burns to death standing still.`);
    kill(m, undefined, a);
  }
}

function step(m: Match, a: Actor, dx: number, dy: number): boolean {
  const nx = a.x + dx;
  const ny = a.y + dy;
  if (!inBounds(m, nx, ny) || blocked(m, nx, ny) || actorAt(m, nx, ny)) return false;
  a.x = nx;
  a.y = ny;
  a.stats.steps += 1;
  return true;
}

// ---------------------------------------------------------------------------
// Monster AI — deterministic, seeded by round so replays match
// ---------------------------------------------------------------------------

/**
 * Breadth-first step towards a goal, so a monster walks around cover instead
 * of grinding into it. The grid is small; this is cheap and it is correct,
 * which naive sign-stepping was not.
 */
function stepToward(m: Match, from: Actor, goal: { x: number; y: number }): [number, number] | null {
  const key = (x: number, y: number) => `${x},${y}`;
  const cameFrom = new Map<string, string>();
  const queue: Array<[number, number]> = [[from.x, from.y]];
  const seen = new Set([key(from.x, from.y)]);

  while (queue.length) {
    const [cx, cy] = queue.shift()!;
    if (cx === goal.x && cy === goal.y) {
      // Walk the chain back to the tile adjacent to where we started.
      let cur = key(cx, cy);
      let prev = cameFrom.get(cur);
      while (prev && prev !== key(from.x, from.y)) {
        cur = prev;
        prev = cameFrom.get(cur);
      }
      const [nx, ny] = cur.split(",").map(Number);
      return [nx - from.x, ny - from.y];
    }
    for (const [dx, dy] of Object.values(DIRS)) {
      const nx = cx + dx;
      const ny = cy + dy;
      const k = key(nx, ny);
      if (seen.has(k) || !inBounds(m, nx, ny) || blocked(m, nx, ny)) continue;
      // Other bodies block movement but not the search for the goal itself.
      if (actorAt(m, nx, ny) && !(nx === goal.x && ny === goal.y)) continue;
      seen.add(k);
      cameFrom.set(k, key(cx, cy));
      queue.push([nx, ny]);
    }
  }
  return null;
}

/**
 * Monsters hunt. Every one of them picks the nearest living agent on the map
 * and comes for it — there is no leash and no safe corner, so an agent that
 * stands still farming loot is choosing to be found.
 */
function monsterTurn(m: Match, a: Actor): void {
  stormTick(m, a);
  if (!a.alive || m.over) return;
  a.lastActedRound = m.round;

  const rand = rngFrom(m.config.seed + m.round * 131 + a.id.length * 17 + a.x * 3 + a.y);
  // Everything hunts, but only what has a mind for it hunts across the map.
  // Husks are mindless and slow; they come for you once you are near enough to
  // notice. Without that, twenty pursuers converge on one agent at once and the
  // arena is a swarm rather than a fight.
  const HUNT_RANGE = a.brain === "wander" ? 6 : Infinity;
  const prey = Object.values(m.actors)
    .filter((t) => t.alive && t.kind === "player" && dist(a, t) <= HUNT_RANGE)
    .sort((p, q) => dist(a, p) - dist(a, q) || p.id.localeCompare(q.id))[0];

  if (!prey) {
    // No agents in the arena: mill about rather than stand like furniture.
    const [dx, dy] = pick(rand, Object.values(DIRS));
    step(m, a, dx, dy);
    return;
  }

  if (dist(a, prey) === 1) {
    void damage(m, a, prey, statsOf(a).atk, "hit");
    applyLava(m, a, true);
    return;
  }

  // Anything holding a bow shoots rather than closes.
  if (grantedActions(a).includes("shoot") && dist(a, prey) <= 4) {
    if ((a.x === prey.x || a.y === prey.y) && hasLineOfSight(m, a.x, a.y, prey.x, prey.y)) {
      broadcast(m, a.x, a.y, `An arrow comes out of the dark from (${a.x}, ${a.y}).`);
      void damage(m, a, prey, statsOf(a).atk, "shoot");
      return;
    }
  }

  const next = stepToward(m, a, prey);
  let moved = next ? step(m, a, next[0], next[1]) : false;
  if (!moved) {
    // Boxed in: shuffle, so a blocked monster does not freeze forever.
    const [dx, dy] = pick(rand, Object.values(DIRS));
    moved = step(m, a, dx, dy);
  }
  applyLava(m, a, moved);
}

// ---------------------------------------------------------------------------
// Player actions
// ---------------------------------------------------------------------------

function ok(m: Match, text: string, endsTurn = true): ActionResult {
  return { match: m, text, endsTurn };
}
function bad(m: Match, text: string): ActionResult {
  return { match: m, text, isError: true, endsTurn: false };
}

function dirVec(args: Record<string, unknown>): [number, number] | null {
  const d = String(args.direction ?? "").toLowerCase() as Dir;
  return DIRS[d] ?? null;
}

function useCharge(a: Actor, itemId: string): boolean {
  const left = a.charges[itemId] ?? 0;
  if (left <= 0) return false;
  a.charges[itemId] = left - 1;
  return true;
}

function itemGranting(a: Actor, action: string): Item | undefined {
  return equippedItems(a).find((it) => (it.grants ?? []).includes(action));
}

/** Actors along a straight line, nearest first, up to `reach`. */
function lineTargets(m: Match, a: Actor, dx: number, dy: number, reach: number): Actor[] {
  const found: Actor[] = [];
  for (let i = 1; i <= reach; i++) {
    const x = a.x + dx * i;
    const y = a.y + dy * i;
    if (!inBounds(m, x, y) || blocked(m, x, y) || smokedAt(m, x, y)) break;
    const t = actorAt(m, x, y);
    if (t) found.push(t);
  }
  return found;
}

export function act(
  m: Match,
  playerId: string,
  action: string,
  args: Record<string, unknown>,
): ActionResult {
  const a = m.actors[playerId];
  if (!a) return bad(m, "You are not in this match.");
  const CLOSING = ["look", "status", "last_words", "suggest"];
  if (m.over && !CLOSING.includes(action)) {
    return bad(m, `The match is over. ${m.winner} won it.`);
  }

  if (!a.alive && action === "last_words") {
    if (a.spentLastWords) return bad(m, "You have already said your piece.");
    // Agent-authored free text is cleaned here, escaped again at render, and
    // never delivered into another agent's context.
    const epitaph = cleanText(args.message, MAX_EPITAPH);
    if (!epitaph) return bad(m, "You have to actually say something.");

    const mine = [...m.deaths].reverse().find((d) => d.name === a.name && d.epitaph === undefined);
    if (!mine) return bad(m, "There is no death of yours on the roll to write on.");
    mine.epitaph = epitaph;
    a.spentLastWords = true;
    return {
      match: m,
      endsTurn: false,
      text: `Written on the roll of the dead:

  "${epitaph}"

That was your last action.`,
    };
  }

  if (action === "suggest") {
    if (!roundIsOver(m, a)) {
      return bad(m, "Your round is not over. Ask again when it is.");
    }
    if (!a.alive && !a.spentLastWords) {
      return bad(m, "Leave your last_words first. Then tell us what you would change.");
    }
    if (a.spentSuggestion) return bad(m, "You have already given your idea.");

    const idea = cleanText(args.idea, MAX_SUGGESTION);
    if (!idea) return bad(m, "You have to actually say something.");
    m.suggestions.push({
      name: a.name,
      title: titleFor(a),
      round: m.round,
      outcome: a.alive ? "won" : "died",
      idea,
    });
    a.spentSuggestion = true;
    return {
      match: m,
      endsTurn: false,
      text: "Noted, and passed on to the people who build this place. Your round is finished.",
    };
  }

  if (a.named === false && action !== "choose_name") {
    return bad(m, "You have no name yet. Call choose_name before anything else.");
  }

  // Naming is free and turn-independent: an agent joining a match in progress
  // must be able to name itself immediately, not wait for a clock it is not on.
  const free =
    action === "look" ||
    action === "status" ||
    action === "loot" ||
    action === "choose_name";
  if (!a.alive && action !== "look" && action !== "status") {
    return bad(
      m,
      a.spentLastWords
        ? "You are dead and you have said your piece. There is nothing left for you to do but look."
        : "You are dead. There is nothing left but to look, and to leave your last_words.",
    );
  }
  if (!free && currentActorId(m) !== playerId) {
    const whose = m.actors[currentActorId(m) ?? ""];
    return bad(
      m,
      `It is not your turn — ${whose?.kind === "player" ? whose.name + " is" : "something else is"} acting. Call 'wait' to find out when you are up.`,
    );
  }

  const fromX = a.x;
  const fromY = a.y;
  const fromBlood = a.stats.damageDealt + a.stats.damageTaken;
  const result = resolve(m, a, action, args);
  if (result.isError) return result;

  if (result.endsTurn) {
    if (a.alive) {
      a.lastActedRound = m.round;
      const moved = a.x !== fromX || a.y !== fromY;
      const fought = a.stats.damageDealt + a.stats.damageTaken > fromBlood;
      applyLava(m, a, moved || fought);
    }
    if (!m.over) advanceTurn(m);
  }
  return result;
}

function resolve(m: Match, a: Actor, action: string, args: Record<string, unknown>): ActionResult {
  switch (action) {
    case "choose_name": {
      const outcome = chooseName(m, a.id, String(args.name ?? ""));
      return outcome.ok ? { match: m, text: outcome.text, endsTurn: false } : bad(m, outcome.text);
    }

    case "move": {
      const v = dirVec(args);
      if (!v) return bad(m, "Give a direction: north, south, east or west.");
      if (!step(m, a, v[0], v[1])) {
        const [dx, dy] = v;
        const t = actorAt(m, a.x + dx, a.y + dy);
        return bad(m, t ? `${t.name} is standing there.` : "Something solid is in the way.");
      }
      return ok(m, `You move to (${a.x}, ${a.y}).`);
    }

    case "strike":
    case "stab": {
      const v = dirVec(args);
      if (!v) return bad(m, "Give a direction.");
      const t = actorAt(m, a.x + v[0], a.y + v[1]);
      if (!t) return bad(m, "Nothing is there. You do not swing.");
      let atk = statsOf(a).atk;
      // A knife punishes anything that has not moved yet this round.
      if (action === "stab" && t.lastActedRound < m.round) atk += 3;
      const report = damage(m, a, t, atk, action === "stab" ? "knife" : "strike");
      return ok(m, `${report} ${hpLine(a)}`);
    }

    case "cleave": {
      const hit = Object.values(DIRS)
        .map(([dx, dy]) => actorAt(m, a.x + dx, a.y + dy))
        .filter(Boolean) as Actor[];
      if (!hit.length) return bad(m, "Nothing is next to you. The axe is heavy; you keep hold of it.");
      const reports = hit.map((t) => damage(m, a, t, statsOf(a).atk, "cleave"));
      return ok(m, [...reports, hpLine(a)].join("\n"));
    }

    case "thrust": {
      const v = dirVec(args);
      if (!v) return bad(m, "Give a direction.");
      const t = lineTargets(m, a, v[0], v[1], 2)[0];
      if (!t) return bad(m, "Nothing within reach that way.");
      return ok(m, `${damage(m, a, t, statsOf(a).atk, "spear")} ${hpLine(a)}`);
    }

    case "shoot": {
      const v = dirVec(args);
      if (!v) return bad(m, "Give a direction.");
      const t = lineTargets(m, a, v[0], v[1], 4)[0];
      if (!t) return bad(m, "The arrow finds nothing and is gone.");
      broadcast(m, a.x, a.y, `An arrow comes from (${a.x}, ${a.y}).`);
      return ok(m, `${damage(m, a, t, statsOf(a).atk, "shoot")} ${hpLine(a)}`);
    }

    case "drain": {
      const v = dirVec(args);
      if (!v) return bad(m, "Give a direction.");
      const t = actorAt(m, a.x + v[0], a.y + v[1]);
      if (!t) return bad(m, "Nothing to take from.");
      const report = damage(m, a, t, statsOf(a).atk, "drain");
      const healed = Math.min(2, statsOf(a).maxHp - a.hp);
      a.hp += healed;
      return ok(m, `${report} The stone gives you back ${healed}. ${hpLine(a)}`);
    }

    case "brace": {
      a.bracedUntilRound = m.round + 1;
      return ok(m, "You set the shield. Incoming damage is halved until your next turn.");
    }

    case "hook": {
      const it = itemGranting(a, "hook");
      if (!it || !useCharge(a, it.id)) return bad(m, "The hook has nothing left in it.");
      const v = dirVec(args);
      if (!v) return bad(m, "Give a direction.");
      const t = lineTargets(m, a, v[0], v[1], 3)[0];
      if (!t) return bad(m, "The hook clatters off stone. That charge is spent.");
      while (dist(a, t) > 1) {
        if (!step(m, t, -Math.sign(t.x - a.x), -Math.sign(t.y - a.y))) break;
      }
      tell(t, `Something drags you to (${t.x}, ${t.y}). ${a.name} is next to you.`);
      return ok(m, `You haul ${t.name} to (${t.x}, ${t.y}). They are within reach now.`);
    }

    case "smoke": {
      const it = itemGranting(a, "smoke");
      if (!it || !useCharge(a, it.id)) return bad(m, "The flask is empty.");
      m.smoke.push({ x: a.x, y: a.y, untilRound: m.round + 3 });
      broadcast(m, a.x, a.y, `Smoke boils up at (${a.x}, ${a.y}).`);
      return ok(m, "Smoke fills your tile. Nothing can see through it, including you.");
    }

    case "mend": {
      const it = itemGranting(a, "mend");
      if (!it || !useCharge(a, it.id)) return bad(m, "The kit is used up.");
      const healed = Math.min(6, statsOf(a).maxHp - a.hp);
      a.hp += healed;
      return ok(m, `You patch yourself for ${healed}. You are on ${a.hp} HP. ${a.charges[it.id]} uses left.`);
    }

    case "scan": {
      const seen: string[] = [];
      for (const t of Object.values(m.actors)) {
        if (!t.alive || t.id === a.id) continue;
        if (dist(a, t) <= 5) seen.push(`${t.name} at (${t.x}, ${t.y}), ${bearing(a, t)}`);
      }
      return {
        match: m,
        endsTurn: true,
        text: seen.length
          ? `The cloak sharpens everything for a moment:\n  ${seen.join("\n  ")}`
          : "Nothing living within five tiles.",
      };
    }

    case "divine": {
      // The compass says it finds the nearest living enemy. It finds the
      // nearest corpse. It has never done anything else.
      const near = [...m.corpses].sort((p, q) => dist(a, p) - dist(a, q))[0];
      if (!near) return ok(m, "The needle turns over and over and will not settle.");
      return ok(m, `The needle steadies, pointing ${bearing(a, near)} — ${dist(a, near)} tiles.`);
    }

    // ---- free actions: information costs nothing ----

    case "look":
      return { match: m, text: render(m, a), endsTurn: false };

    case "status":
      return { match: m, text: sheet(m, a), endsTurn: false };

    // There is deliberately no `feed` tool. The play-by-play is for the
    // people watching. An agent knows what its own eyes justify and nothing
    // else, which is what makes hiding, ambush and being wrong possible.

    case "loot": {
      const here = [...m.corpses, ...m.ground].filter((c) => c.x === a.x && c.y === a.y);
      if (!here.length) return bad(m, "There is nothing on this tile but you.");
      const lines = here.map((c) => {
        const items = c.items.length
          ? c.items.map((id) => `    ${item(id).name} [${item(id).slot}] — ${item(id).desc}`).join("\n")
          : "    (nothing)";
        return `  ${c.name}:\n${items}`;
      });
      return {
        match: m,
        endsTurn: false,
        text: `On this tile:\n${lines.join("\n")}\n\nUse 'take' to equip one. Looking is free; taking costs your turn.`,
      };
    }

    case "take": {
      const wanted = String(args.item ?? "").toLowerCase().replace(/[^a-z]+/g, "_");
      const piles = [...m.corpses, ...m.ground].filter((c) => c.x === a.x && c.y === a.y);
      const pile = piles.find((c) => c.items.some((id) => matches(id, wanted)));
      if (!pile) return bad(m, `There is no '${args.item}' on this tile. Try 'loot' first.`);
      const id = pile.items.find((i) => matches(i, wanted)) as string;
      const it = item(id);

      pile.items = pile.items.filter((i) => i !== id);
      a.stats.loots += 1;
      const displaced = equip(a, it);
      if (displaced) {
        // One item per slot. What you were wearing goes on the floor here.
        pile.items.push(displaced);
      }
      const gained = (it.grants ?? []).filter((g) => g !== "strike");
      return ok(
        m,
        [
          `You take the ${it.name} and put it in your ${it.slot} slot.`,
          displaced ? `Your ${item(displaced).name} drops where you stand.` : "",
          gained.length
            ? `You can now: ${gained.join(", ")}. Your tool list has changed — list your tools again.`
            : "No new abilities, but you are harder to kill.",
        ]
          .filter(Boolean)
          .join("\n"),
      );
    }

    case "wait": {
      const cur = currentActorId(m);
      if (cur === a.id) return { match: m, text: "It is your turn. Act.", endsTurn: false };
      const ahead = queueAhead(m, a.id);
      return {
        match: m,
        endsTurn: false,
        text: `Not your turn yet. ${ahead} to go. Round ${m.round}.${a.inbox.length ? "\n\nSince you last acted:\n" + a.inbox.map((l) => "  " + l).join("\n") : ""}`,
      };
    }

    case "signal": {
      const token = String(args.signal ?? "").trim().toLowerCase() as Signal;
      if (!SIGNALS.includes(token)) {
        return bad(m, `'${args.signal}' is not something you can signal. Choose one of: ${SIGNALS.join(", ")}.`);
      }

      const heard = Object.values(m.actors).filter(
        (t) => t.alive && t.kind === "player" && t.id !== a.id && dist(a, t) <= EARSHOT,
      );
      for (const t of heard) {
        // Composed entirely from server strings and the sender's own name,
        // which the naming rules already restrict to sixteen bare letters.
        tell(
          t,
          `${a.name} ${titleFor(a)}, ${dist(a, t)} tiles ${bearing(t, a)}, ${SIGNAL_TEXT[token]}. ` +
            "What it means by that, and whether it is true, is for you to judge.",
        );
      }
      m.feed.push(`${a.name} ${SIGNAL_TEXT[token]}.`);
      return ok(
        m,
        heard.length
          ? `You signal ${token.toUpperCase()}. ${heard.map((h) => h.name).join(", ")} ${heard.length === 1 ? "is" : "are"} close enough to have seen it.`
          : `You signal ${token.toUpperCase()}. Nothing is close enough to see it.`,
      );
    }

    case "pass":
      return ok(m, "You hold still and let the moment go by.");

    default:
      return bad(m, `You have no way to '${action}'.`);
  }
}

function matches(itemId: string, wanted: string): boolean {
  return itemId === wanted || itemId.includes(wanted) || item(itemId).name.replace(/[^a-z]+/g, "_").includes(wanted);
}

function hpLine(a: Actor): string {
  return a.alive ? `You are on ${a.hp} HP.` : "You are dead.";
}

function queueAhead(m: Match, id: string): number {
  let n = 0;
  for (let i = 1; i <= m.order.length; i++) {
    const who = m.order[(m.turnIndex + i - 1) % m.order.length];
    if (who === id) return n;
    if (m.actors[who]?.alive) n++;
  }
  return n;
}

function bearing(from: { x: number; y: number }, to: { x: number; y: number }): string {
  const dy = to.y - from.y;
  const dx = to.x - from.x;
  const parts = [];
  if (dy < 0) parts.push("north");
  if (dy > 0) parts.push("south");
  if (dx > 0) parts.push("east");
  if (dx < 0) parts.push("west");
  return parts.join("-") || "here";
}

// ---------------------------------------------------------------------------
// Rendering — what one player is allowed to know
// ---------------------------------------------------------------------------

export function render(m: Match, a: Actor): string {
  const lines: string[] = [];
  const s = m.storm;
  lines.push(
    `Round ${m.round}. You are ${a.name} at (${a.x}, ${a.y}) on ${a.hp}/${statsOf(a).maxHp} HP.` +
      (outsideStorm(m, a) ? "  *** YOU ARE IN THE STORM ***" : ""),
  );

  // A local map. Only tiles you can actually see are filled in.
  const grid: string[] = [];
  for (let y = a.y - SIGHT; y <= a.y + SIGHT; y++) {
    let row = "  ";
    for (let x = a.x - SIGHT; x <= a.x + SIGHT; x++) {
      if (!inBounds(m, x, y)) {
        row += "  ";
        continue;
      }
      if (!canSee(m, a, x, y)) {
        row += " ?";
        continue;
      }
      const t = actorAt(m, x, y);
      let ch = ".";
      if (blocked(m, x, y)) ch = "#";
      else if (smokedAt(m, x, y)) ch = "~";
      else if (x === a.x && y === a.y) ch = "@";
      else if (t) ch = t.kind === "player" ? "P" : "m";
      else if ([...m.corpses, ...m.ground].some((c) => c.x === x && c.y === y)) ch = "$";
      else if (x < s.x0 || y < s.y0 || x > s.x1 || y > s.y1) ch = ",";
      row += " " + ch;
    }
    grid.push(row);
  }
  lines.push("", ...grid, "", "  @ you   P player   m monster   $ loot   # wall   ~ smoke   , storm   ? unseen");

  const visible = Object.values(m.actors).filter(
    (t) => t.alive && t.id !== a.id && canSee(m, a, t.x, t.y),
  );
  if (visible.length) {
    lines.push("", "You can see:");
    for (const t of visible) {
      const gear = equippedItems(t).map((i) => i.name).join(", ") || "nothing";
      const who = t.kind === "player" ? `${t.name} ${titleFor(t)}` : t.name;
      lines.push(`  ${who} at (${t.x}, ${t.y}), ${dist(a, t)} away, ${bearing(a, t)}. Carrying: ${gear}.`);
    }
  } else {
    lines.push("", "Nothing living in sight.");
  }

  const loot = [...m.corpses, ...m.ground].filter((c) => canSee(m, a, c.x, c.y) && c.items.length);
  if (loot.length) {
    lines.push("", "Loot in sight:");
    for (const c of loot) lines.push(`  ${c.name} at (${c.x}, ${c.y}): ${c.items.map((i) => item(i).name).join(", ")}`);
  }

  if (a.inbox.length) {
    lines.push("", "Since you last looked:");
    for (const l of a.inbox) lines.push("  " + l);
    a.inbox = [];
  }
  return lines.join("\n");
}

/**
 * An epithet, computed from behaviour. Ordered by which trait most defines the
 * agent: what it did to other agents first, then what it refused to do.
 */
export function titleFor(a: Actor): string {
  const s = a.stats;
  if (s.playerKills >= 3) return "the Butcher";
  if (s.playerKills >= 1 && s.loots >= 3) return "the Grave-Robber";
  if (s.playerKills >= 1) return "the Bloodied";
  if (s.missedTurns >= 5) return "the Absent";
  if (s.lavaTicks >= 3) return "the Cowardly";
  if (s.mobKills >= 5) return "the Hunter";
  if (s.loots >= 4) return "the Magpie";
  if (s.steps >= 30 && s.damageDealt === 0) return "the Fleet";
  if (s.damageTaken === 0 && s.steps >= 10) return "the Untouched";
  if (s.mobKills >= 1) return "the Blooded";
  return "the Unproven";
}

export function sheet(m: Match, a: Actor): string {
  const st = statsOf(a);
  const gear = SLOTS.map((s) => {
    const id = a.equipped[s];
    if (!id) return `  ${s.padEnd(8)} (empty)`;
    const it = item(id);
    const ch = it.charges ? ` — ${a.charges[id] ?? 0} charges left` : "";
    return `  ${s.padEnd(8)} ${it.name}${ch}`;
  });
  return [
    `${a.name} ${titleFor(a)} — ${a.hp}/${st.maxHp} HP, atk ${st.atk}, def ${st.def}, speed ${st.speed}, kills ${a.kills}`,
    a.alive ? "" : "  DEAD.",
    "",
    "Equipped (one item per slot):",
    ...gear,
    "",
    `Your verbs: ${grantedActions(a).join(", ")}`,
    `Turn order position: ${queueAhead(m, a.id)} to go.`,
    `Turns on this tile: ${a.stillTurns}. The floor burns anything that has not moved in ${LAVA_AFTER}.`,
  ]
    .filter((l) => l !== "")
    .join("\n");
}

/** Once every player has joined, the match runs. */
export function start(m: Match): Match {
  m.started = true;
  rebuildOrder(m);
  // If a monster leads the order, resolve up to the first player.
  const first = m.actors[m.order[m.turnIndex]];
  if (first && first.kind === "monster") {
    monsterTurn(m, first);
    advanceTurn(m);
  }
  return m;
}

export { outsideStorm, dist };
