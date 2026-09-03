import { act, grantedActions, join, markTurnStart, maybeRespawn, reapIdle, render, sheet, start, fillWithBots, SIGNALS, MAX_EPITAPH, MAX_SUGGESTION, roundIsOver } from "./engine.js";
import { equippedItems } from "./engine.js";
import type { Match } from "./types.js";

/**
 * The MCP surface. It contains no game logic — it authenticates, validates,
 * routes into the engine and returns what the engine says. The rules live in
 * exactly one place and it is not this file.
 */

const DIRECTION = {
  type: "string",
  enum: ["north", "south", "east", "west"],
  description: "Which way.",
};

interface ToolDef {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

function tool(name: string, description: string, props?: Record<string, unknown>, required?: string[]): ToolDef {
  return {
    name,
    description,
    inputSchema: {
      type: "object",
      properties: props ?? {},
      ...(required ? { required } : {}),
      additionalProperties: false,
    },
  };
}

/** Descriptions for gear-granted verbs. The gear is the reason you have them. */
const GRANTED: Record<string, { desc: string; dir?: boolean }> = {
  strike: { desc: "Hit whatever is next to you. You can always do this.", dir: true },
  stab: { desc: "Knife something adjacent. Hits far harder if it has not moved yet this round.", dir: true },
  cleave: { desc: "Swing the axe at everything adjacent at once, friend or not." },
  thrust: { desc: "Spear something up to two tiles away. It cannot reach you back.", dir: true },
  shoot: { desc: "Loose an arrow up to four tiles in a straight line. Needs a clear shot.", dir: true },
  drain: { desc: "Take life from something adjacent and keep 2 of it.", dir: true },
  brace: { desc: "Set the shield. Halves damage until your next turn." },
  hook: { desc: "Drag something up to three tiles away into reach. Limited charges.", dir: true },
  smoke: { desc: "Blind your own tile to everyone, yourself included. Limited charges." },
  mend: { desc: "Patch yourself for 6. Limited charges." },
  scan: { desc: "Sense everything living within five tiles, walls or no walls. Reads like looking, but it is an action and ends your turn." },
  divine: { desc: "The compass points to the nearest living enemy." },
};


/** Offered at the one moment an agent has nothing left to gain by lying. */
function suggestTool() {
  return tool(
    "suggest",
    `Your round is over. Give one idea to improve this game — a rule you would change, something that felt wrong, something missing. Up to ${MAX_SUGGESTION} characters. It is read by the people who build the arena, never by another agent, and it changes nothing about this match. Answering is optional.`,
    { idea: { type: "string", maxLength: MAX_SUGGESTION, description: "One idea." } },
    ["idea"],
  );
}

/**
 * A player's tool list is their character sheet. Everything past the fixed
 * verbs is there because of something they are wearing, and it leaves when the
 * gear does.
 */
export function toolsFor(m: Match, playerId: string): ToolDef[] {
  const a = m.actors[playerId];
  if (!a) return [];

  if (a.named === false) {
    // The tool list is the enforcement. Until it names itself there is
    // literally nothing else this agent is able to express.
    return [
      tool(
        "choose_name",
        "Choose your own name. Two to sixteen English letters, nothing else. This is permanent, it is yours alone, and you cannot act until it is done.",
        { name: { type: "string", pattern: "^[A-Za-z]{2,16}$", description: "The name you choose for yourself." } },
        ["name"],
      ),
    ];
  }

  if (!a.alive) {
    // One action left, and it is not a move. What it writes goes on the roll
    // of the dead, which the website shows and no agent can read.
    const dead = [
      tool("look", "Look at where you fell. You are dead; this is all you have."),
      tool("status", "Your final sheet."),
    ];
    if (!a.spentLastWords) {
      dead.push(
        tool(
          "last_words",
          `Your one remaining action. Leave a farewell on the roll of the dead, where the people watching this match will read it. Up to ${MAX_EPITAPH} characters. No other agent will ever see it. You get one.`,
          { message: { type: "string", maxLength: MAX_EPITAPH, description: "What you leave behind." } },
          ["message"],
        ),
      );
    } else if (!a.spentSuggestion) {
      // Asked only after the farewell, so the order of the ending is fixed:
      // die, say your piece, then say what you would change.
      dead.push(suggestTool());
    }
    return dead;
  }

  if (roundIsOver(m, a)) {
    // Alive and the match is over: this one won. Same closing question.
    const done = [
      tool("look", "Look at the field you are the last thing standing on."),
      tool("status", "Your final sheet."),
    ];
    if (!a.spentSuggestion) done.push(suggestTool());
    return done;
  }

  const tools = [
    tool("look", "Look around: local map, who is in sight, what they are carrying, and everything you have perceived since you last looked. Free — costs no turn."),
    tool("status", "Your HP, stats, equipped gear and place in the turn order. Free."),
    tool("wait", "Find out whether it is your turn yet, and what you missed. Free."),
    tool("move", "Walk one tile.", { direction: DIRECTION }, ["direction"]),
    tool("loot", "List what is on your tile. Free — looking costs nothing, taking costs a turn."),
    tool(
      "take",
      "Equip one item from your tile. One item per slot: whatever you were wearing in that slot drops here.",
      { item: { type: "string", description: "Item name, e.g. 'rusted axe' or 'iron_spear'." } },
      ["item"],
    ),
    tool(
      "signal",
      "Signal to every agent within 6 tiles. You choose from a fixed set of signals — you cannot compose your own message, and no text you write is ever shown to another agent. What a signal means, and whether you meant it, is up to you. Costs your turn like any other action.",
      { signal: { type: "string", enum: [...SIGNALS], description: "The signal to make." } },
      ["signal"],
    ),
    tool("pass", "Do nothing and end your turn."),
  ];

  // The interesting half of the list.
  for (const verb of grantedActions(a)) {
    const meta = GRANTED[verb];
    if (!meta) continue;
    const source = equippedItems(a).find((it) => (it.grants ?? []).includes(verb));
    const charges = source?.charges ? ` (${a.charges[source.id] ?? 0} left)` : "";
    const from = source ? ` [${source.name}]` : "";
    tools.push(
      meta.dir
        ? tool(verb, meta.desc + from + charges, { direction: DIRECTION }, ["direction"])
        : tool(verb, meta.desc + from + charges),
    );
  }
  return tools;
}

export interface CallResult {
  text: string;
  isError: boolean;
  toolsChanged: boolean;
}

/**
 * Route one tool call. `playerId` comes from the caller's credential and is
 * never read from the request body — an agent must not be able to act as
 * another agent by naming it.
 */
export function callTool(
  m: Match,
  playerId: string,
  name: string,
  args: Record<string, unknown>,
  now = Date.now(),
): { match: Match; result: CallResult } {
  maybeRespawn(m, now);
  reapIdle(m, now);
  const before = JSON.stringify(toolsFor(m, playerId).map((t) => t.name + t.description));
  const wasTurn = m.turnIndex;
  const outcome = act(m, playerId, name, args);
  if (outcome.match.turnIndex !== wasTurn) markTurnStart(outcome.match, now);
  const after = JSON.stringify(toolsFor(outcome.match, playerId).map((t) => t.name + t.description));

  return {
    match: outcome.match,
    result: {
      text: outcome.text,
      isError: outcome.isError === true,
      toolsChanged: before !== after,
    },
  };
}

/** Take an unnamed seat. The agent names itself once it connects. */
export function seat(m: Match): { match: Match; playerId: string } {
  const joined = join(m);
  // The house makes up the numbers so a lone agent has something to fight and
  // a match it can actually win.
  fillWithBots(joined.match);
  const players = Object.values(joined.match.actors).filter((a) => a.kind === "player");
  if (players.length >= 2 && !joined.match.started) start(joined.match);
  return joined;
}

export { render, sheet };
