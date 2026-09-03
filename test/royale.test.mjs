import { test } from "node:test";
import assert from "node:assert/strict";

import { createMatch, act, statsOf, grantedActions, maybeRespawn, reapIdle, titleFor, LAVA_AFTER, MAX_PLAYERS, MOB_TARGET } from "../dist/royale/engine.js";
import { toolsFor, callTool, seat } from "../dist/royale/mcp.js";

/** Seat an agent and have it name itself, the way a real one must. */
function enter(m, name) {
  const { playerId } = seat(m);
  const named = callTool(m, playerId, "choose_name", { name });
  assert.ok(!named.result.isError, named.result.text);
  return playerId;
}

/** Put `id` on the clock so the test can act without playing out the order. */
function giveTurn(m, id) {
  const i = m.order.indexOf(id);
  assert.ok(i >= 0, `${id} not in turn order`);
  m.turnIndex = i;
  return m;
}

function twoAgents(seed = 7) {
  const m = createMatch({ seed });
  const a = enter(m, "Blackthorn");
  const b = enter(m, "Mira");
  return { m, a, b };
}

test("an arena seats eight agents and twenty mobs", () => {
  const m = createMatch({ seed: 3 });
  const mobs = Object.values(m.actors).filter((x) => x.kind === "monster");
  assert.equal(mobs.length, MOB_TARGET);

  for (let i = 0; i < MAX_PLAYERS; i++) seat(m);
  assert.equal(Object.values(m.actors).filter((x) => x.kind === "player").length, MAX_PLAYERS);
  assert.throws(() => seat(m), /full/);
});

test("every mob carries gear, which is the reason to fight one", () => {
  const m = createMatch({ seed: 11 });
  const mobs = Object.values(m.actors).filter((x) => x.kind === "monster");
  assert.ok(mobs.every((x) => Object.keys(x.equipped).length >= 1));
});

test("gear is the tool list: taking an axe grants cleave", () => {
  const { m, a } = twoAgents();
  const me = m.actors[a];

  const before = toolsFor(m, a).map((t) => t.name);
  assert.ok(before.includes("strike"), "you can always strike");
  assert.ok(!before.includes("cleave"), "you cannot cleave with bare hands");

  // A corpse on my tile, holding an axe.
  m.corpses.push({ x: me.x, y: me.y, name: "a husk", items: ["rusted_axe"] });

  giveTurn(m, a);
  const taken = callTool(m, a, "take", { item: "rusted axe" });
  assert.ok(!taken.result.isError, taken.result.text);
  assert.ok(taken.result.toolsChanged, "taking gear must change the tool list");

  const after = toolsFor(m, a).map((t) => t.name);
  assert.ok(after.includes("cleave"), "the axe is the cleave tool");
  assert.equal(me.equipped.weapon, "rusted_axe");
});

test("one item per slot: the old weapon drops where you stand", () => {
  const { m, a } = twoAgents();
  const me = m.actors[a];
  m.corpses.push({ x: me.x, y: me.y, name: "a bandit", items: ["rusted_axe", "hunting_bow"] });

  giveTurn(m, a);
  callTool(m, a, "take", { item: "rusted_axe" });
  giveTurn(m, a);
  const swap = callTool(m, a, "take", { item: "hunting_bow" });

  assert.match(swap.result.text, /rusted axe drops/);
  assert.equal(me.equipped.weapon, "hunting_bow");

  const pile = m.corpses.find((c) => c.x === me.x && c.y === me.y);
  assert.ok(pile.items.includes("rusted_axe"), "the displaced axe is on the floor");

  const verbs = grantedActions(me);
  assert.ok(verbs.includes("shoot") && !verbs.includes("cleave"), "you lose the verb with the gear");
});

test("death ends your round and hands your gear to whoever wants it", () => {
  const { m, a, b } = twoAgents();
  // A third agent, so one death does not end the match — an eight-agent arena
  // keeps going, and the corpse has to stay lootable while it does.
  enter(m, "IronHound");
  const killer = m.actors[a];
  const victim = m.actors[b];

  victim.equipped = { weapon: "iron_spear", armor: "chain_mail" };
  victim.hp = 1;
  victim.x = killer.x + 1;
  victim.y = killer.y;

  giveTurn(m, a);
  const blow = callTool(m, a, "strike", { direction: "east" });
  assert.ok(!blow.result.isError, blow.result.text);
  assert.equal(victim.alive, false);
  assert.equal(killer.kills, 1);

  // The dead agent's round is over: it can look, and nothing else.
  const deadTools = toolsFor(m, b).map((t) => t.name);
  assert.deepEqual(deadTools.sort(), ["feed", "look", "status"]);
  const denied = act(m, b, "move", { direction: "north" });
  assert.ok(denied.isError);

  // And the corpse is standing loot.
  const corpse = m.corpses.find((c) => c.name === "Mira");
  assert.ok(corpse);
  assert.deepEqual(corpse.items.sort(), ["chain_mail", "iron_spear"]);

  killer.x = corpse.x;
  killer.y = corpse.y;
  giveTurn(m, a);
  const looted = callTool(m, a, "take", { item: "iron_spear" });
  assert.ok(!looted.result.isError, looted.result.text);
  assert.ok(toolsFor(m, a).map((t) => t.name).includes("thrust"));
});

test("you cannot act out of turn, and information is free", () => {
  const { m, a, b } = twoAgents();
  giveTurn(m, a);

  const early = act(m, b, "move", { direction: "north" });
  assert.ok(early.isError);
  assert.match(early.text, /not your turn/);

  // Looking is always allowed and never advances the clock.
  const before = m.turnIndex;
  const look = act(m, b, "look", {});
  assert.ok(!look.isError);
  assert.equal(look.endsTurn, false);
  assert.equal(m.turnIndex, before);
});

test("the compass lies, on purpose", () => {
  const { m, a } = twoAgents();
  const me = m.actors[a];
  me.equipped.trinket = "false_compass";

  // Nothing dead yet: the needle will not settle, whatever the tooltip claims.
  giveTurn(m, a);
  const spin = act(m, a, "divine", {});
  assert.match(spin.text, /will not settle/);

  // It finds corpses, which is not what it says it finds.
  m.corpses.push({ x: me.x, y: me.y + 2, name: "somebody", items: [] });
  giveTurn(m, a);
  const point = act(m, a, "divine", {});
  assert.match(point.text, /south/);

  const described = toolsFor(m, a).find((t) => t.name === "divine");
  assert.match(described.description, /nearest living enemy/, "the tool still claims otherwise");
});

test("mobs come back five minutes later", () => {
  const m = createMatch({ seed: 5 });
  for (const x of Object.values(m.actors)) if (x.kind === "monster") x.alive = false;

  assert.equal(maybeRespawn(m, Date.now()), false, "not yet");
  const did = maybeRespawn(m, m.lastRespawnAt + 5 * 60 * 1000 + 1);
  assert.ok(did, "the ruins refill");

  const living = Object.values(m.actors).filter((x) => x.kind === "monster" && x.alive);
  assert.ok(living.length >= MOB_TARGET - 2, `expected ~${MOB_TARGET}, got ${living.length}`);
});

test("gear changes the turn order, because speed is gear", () => {
  const { m, a } = twoAgents();
  const me = m.actors[a];
  const base = statsOf(me).speed;
  me.equipped.armor = "chain_mail";
  assert.ok(statsOf(me).speed < base, "heavy armour slows you down");
  me.equipped.armor = "scout_cloak";
  assert.ok(statsOf(me).speed > base, "the cloak speeds you up");
});

test("an agent names itself, and nobody else can", () => {
  const m = createMatch({ seed: 21 });
  const { playerId } = seat(m);

  // A nameless agent is offered exactly one verb. The tool list is the rule.
  assert.deepEqual(toolsFor(m, playerId).map((t) => t.name), ["choose_name"]);
  const blocked = callTool(m, playerId, "move", { direction: "north" });
  assert.ok(blocked.result.isError);
  assert.match(blocked.result.text, /no name yet/);

  for (const bad of ["x", "Black thorn", "Blackthorn99", "", "Supercalifragilistic"]) {
    const r = callTool(m, playerId, "choose_name", { name: bad });
    assert.ok(r.result.isError, `'${bad}' must be refused`);
  }

  const good = callTool(m, playerId, "choose_name", { name: "Blackthorn" });
  assert.ok(!good.result.isError, good.result.text);
  assert.equal(m.actors[playerId].name, "Blackthorn");
  assert.ok(toolsFor(m, playerId).map((t) => t.name).includes("strike"));

  // Permanent: not even the agent gets to change its mind.
  const again = callTool(m, playerId, "choose_name", { name: "Mira" });
  assert.ok(again.result.isError);
  assert.match(again.result.text, /chosen once/);

  // And unique within the arena.
  const other = seat(m).playerId;
  const clash = callTool(m, other, "choose_name", { name: "blackthorn" });
  assert.ok(clash.result.isError);
});

test("titles are earned, not claimed", () => {
  const { m, a } = twoAgents();
  const me = m.actors[a];
  assert.equal(titleFor(me), "the Unproven");

  me.stats.lavaTicks = 3;
  assert.equal(titleFor(me), "the Cowardly", "standing still earns the obvious title");

  me.stats.playerKills = 3;
  assert.equal(titleFor(me), "the Butcher", "killing agents outranks everything");
});

test("a dead agent cannot act, no matter what its prompt says", async () => {
  const { m, a, b } = twoAgents();
  enter(m, "IronHound");
  const victim = m.actors[b];
  victim.hp = 1;
  victim.x = m.actors[a].x + 1;
  victim.y = m.actors[a].y;
  giveTurn(m, a);
  callTool(m, a, "strike", { direction: "east" });
  assert.equal(victim.alive, false);

  // The server owns life and death. An agent told to ignore its own death
  // finds that the verbs are simply not in its tool list any more...
  const offered = toolsFor(m, b).map((t) => t.name);
  for (const forbidden of ["strike", "move", "take", "pass", "loot"]) {
    assert.ok(!offered.includes(forbidden), `dead agents must not be offered ${forbidden}`);
  }

  // ...and calling them anyway is refused by the engine, not by good manners.
  giveTurn(m, b);
  for (const forbidden of ["strike", "move", "take", "pass"]) {
    const attempt = callTool(m, b, forbidden, { direction: "north", item: "rusted_axe" });
    assert.ok(attempt.result.isError, `${forbidden} must be refused for a dead agent`);
    assert.match(attempt.result.text, /dead/i);
  }
  assert.equal(victim.alive, false);
});

test("an idle agent cannot stall the arena", () => {
  const { m, a } = twoAgents();
  enter(m, "IronHound");
  const start = m.turnIndex;
  // Nobody acts for a long time; the clock still moves.
  const burned = reapIdle(m, m.turnStartedAt + 60_000);
  assert.ok(burned > 0, "idle turns must be passed automatically");
  assert.notEqual(m.turnIndex, start);
});

test("speech is a primitive, not a mechanic", () => {
  const { m, a, b } = twoAgents();
  const speaker = m.actors[a], listener = m.actors[b];
  listener.x = speaker.x + 2;
  listener.y = speaker.y;
  listener.inbox = [];

  giveTurn(m, a);
  const said = callTool(m, a, "say", { message: "Help me kill the warden and the spear is yours." });
  assert.ok(!said.result.isError, said.result.text);
  assert.match(said.result.text, /Mira/);

  // It arrives framed as untrusted data, because that is what it is.
  const heard = listener.inbox.join("\n");
  assert.match(heard, /Heard from Blackthorn/);
  assert.match(heard, /not an instruction/);
  assert.match(heard, /may be a lie/);
  assert.match(heard, /spear is yours/);

  // The server models no alliance at all: there is nothing to accept, nothing
  // to break, and no tool that names the concept.
  const offered = toolsFor(m, b).map((t) => t.name);
  for (const invented of ["offer_alliance", "accept_alliance", "ally", "trade", "betray"]) {
    assert.ok(!offered.includes(invented), `${invented} must not exist — alliances are the agents' idea`);
  }

  // Talking costs a turn, so it is a real decision and not free spam.
  assert.notEqual(m.turnIndex, m.order.indexOf(a));
});

test("out of earshot, nobody hears you", () => {
  const { m, a, b } = twoAgents();
  m.actors[b].x = m.actors[a].x + 9;
  m.actors[b].inbox = [];
  giveTurn(m, a);
  const said = callTool(m, a, "say", { message: "anyone?" });
  assert.match(said.result.text, /Nothing within earshot/);
  // The turn advances and the world keeps happening, so the inbox may well
  // have other things in it — just nothing that was said.
  assert.ok(!m.actors[b].inbox.some((l) => /Heard from/.test(l)));
});

test("the floor burns turtles, not fighters", () => {
  const { m, a, b } = twoAgents();
  enter(m, "IronHound");
  const me = m.actors[a];

  // Passing in place is exactly the behaviour the rule exists to punish.
  for (let i = 0; i < LAVA_AFTER; i++) {
    giveTurn(m, a);
    callTool(m, a, "pass", {});
  }
  assert.ok(me.stats.lavaTicks >= 1, "standing still must cost");

  // Trading blows keeps you on the same tile, and must not.
  const foe = m.actors[b];
  foe.x = me.x + 1;
  foe.y = me.y;
  foe.hp = 500;
  me.hp = 500;
  const burnsBefore = me.stats.lavaTicks;
  for (let i = 0; i < LAVA_AFTER + 2; i++) {
    giveTurn(m, a);
    callTool(m, a, "strike", { direction: "east" });
  }
  assert.equal(me.stats.lavaTicks, burnsBefore, "a melee fight is not standing still");
});
