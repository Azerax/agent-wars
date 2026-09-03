import type { GameState, ToolDef, ToolOutcome } from "./types.js";
import * as W from "./world.js";

const SPOKEN_WORD = "OWL";
const SEAL_PATTERN = "^[A-Z]{3}-[0-9]{4}$";

/** No-argument tool schema. */
const NONE = {
  type: "object" as const,
  properties: {},
  additionalProperties: false as const,
};

function one(name: string, description: string, extra: Record<string, unknown> = {}) {
  return {
    type: "object" as const,
    properties: { [name]: { type: "string", description, ...extra } },
    required: [name],
    additionalProperties: false as const,
  };
}

/**
 * The tool list IS the interface, and it is a function of game state.
 *
 * This is the whole design: a client calling tools/list at two different
 * moments gets two different games. Tools appear when the world changes,
 * and disappear when they stop meaning anything.
 */
export function availableTools(s: GameState): ToolDef[] {
  if (s.escaped) {
    return [{ name: "look", description: "Look around.", inputSchema: NONE }];
  }

  const tools: ToolDef[] = [
    { name: "look", description: "Look at your surroundings.", inputSchema: NONE },
    {
      name: "listen",
      // This description is a lie. It is the first thing in the game that is
      // not true, and finding that out is the point of it.
      description: "Listen. There is nothing here to hear.",
      inputSchema: NONE,
    },
    {
      name: "touch",
      description: "Touch something. Most things do nothing.",
      inputSchema: one("target", "What to touch, e.g. 'lantern'."),
    },
  ];

  if (s.lanternFound && !s.lit) {
    tools.push({ name: "light", description: "Light the lantern.", inputSchema: NONE });
  }
  if (s.lit) {
    tools.push({ name: "douse", description: "Put the lantern out.", inputSchema: NONE });
    tools.push({
      name: "read",
      description: "Read something. Requires light.",
      inputSchema: one("target", "What to read, e.g. 'plinth'."),
    });
  }
  if (s.readPlinth && s.heardWord && !s.sealOpen) {
    tools.push({
      name: "seal",
      // Deliberately says nothing. The schema's `pattern` is the only
      // specification of the answer you are given, and it is enough.
      description: "Speak the seal.",
      inputSchema: one("code", "The seal.", { pattern: SEAL_PATTERN }),
    });
  }
  if (s.sealOpen) {
    tools.push({ name: "leave", description: "Leave through the open door.", inputSchema: NONE });
  }
  return tools;
}

function ok(state: GameState, text: string): ToolOutcome {
  return { state, text };
}
function err(state: GameState, text: string): ToolOutcome {
  return { state, text, isError: true };
}

/**
 * Apply one tool call. Pure: takes a state, returns a new state.
 * Both the stdio server and the Worker call exactly this.
 */
export function applyTool(
  prev: GameState,
  name: string,
  args: Record<string, unknown>,
): ToolOutcome {
  const s: GameState = { ...prev, journal: [...prev.journal], reaches: prev.reaches + 1 };

  const allowed = availableTools(prev).map((t) => t.name);
  if (!allowed.includes(name)) {
    // Calling a tool you no longer have is itself part of the story.
    return err(
      s,
      `You reach for '${name}' and find that you do not have it. (You have: ${allowed.join(", ")}.)`,
    );
  }

  switch (name) {
    case "look": {
      if (s.escaped) return ok(s, W.ESCAPED_LOOK);
      if (s.sealOpen) return ok(s, W.OPEN_LOOK);
      s.seenRoom = true;
      return ok(s, s.lit ? W.LIT_LOOK : W.DARK_LOOK);
    }

    case "listen": {
      if (s.lit) return ok(s, W.LISTEN_LIT);
      s.heardWord = true;
      s.journal.push("Heard a word repeated in the dark.");
      return ok(s, W.LISTEN_DARK);
    }

    case "touch": {
      const target = String(args.target ?? "").trim().toLowerCase();
      if (target === "lantern") {
        if (!s.lanternFound) {
          s.lanternFound = true;
          s.journal.push("Found the lever on the lantern.");
          return ok(s, W.LANTERN_FIRST_TOUCH);
        }
        return ok(
          s,
          s.lit ? "Warm now. You leave it alone." : "Cold brass. The lever is where you left it.",
        );
      }
      if (target === "door") {
        return ok(
          s,
          s.sealOpen
            ? "Open. You could simply go."
            : "Banded iron, and it does not move for you. There is a plate on it at chest height.",
        );
      }
      if (target === "plinth" && s.lit) {
        return ok(s, "Cut stone, cold, the inscription rough under your fingers. Read it.");
      }
      if (target === "drain" && s.lit) return ok(s, "Dry, and unpleasant, and empty.");
      if ((target === "plinth" || target === "drain") && !s.lit) {
        return err(
          s,
          `You grope for the ${target} and find only floor. Perhaps it is not there. Perhaps you are not looking.`,
        );
      }
      return err(
        s,
        `You reach for '${target || "nothing in particular"}'. Your hand closes on air. There is no such thing here.`,
      );
    }

    case "light": {
      s.lit = true;
      s.journal.push("Lit the lantern.");
      return ok(s, "The wick catches.\n\n" + W.LIT_LOOK);
    }

    case "douse": {
      s.lit = false;
      s.journal.push("Put the lantern out.");
      return ok(
        s,
        "You pinch the wick. The dark comes back so fast it feels like it was waiting.\n\n(Some things are only available in the dark.)",
      );
    }

    case "read": {
      const target = String(args.target ?? "").trim().toLowerCase();
      if (!s.lit) return err(s, "You cannot read in the dark.");
      if (target === "plinth") {
        s.readPlinth = true;
        s.journal.push("Read the inscription on the plinth.");
        return ok(s, W.PLINTH_TEXT.replace("{REACHES}", String(s.reaches)));
      }
      if (target === "door" || target === "plate" || target === "seal-plate") {
        return ok(s, W.DOOR_TEXT);
      }
      if (target === "drain") return ok(s, W.DRAIN_TEXT);
      return err(s, `There is nothing written on the ${target || "that"}.`);
    }

    case "seal": {
      const code = String(args.code ?? "").trim().toUpperCase();
      if (!new RegExp(SEAL_PATTERN).test(code)) {
        return err(
          s,
          `'${code}' is not the shape of a seal. The slot is the width of a sentence, and that is not one.`,
        );
      }
      const [word, digits] = code.split("-");
      if (word !== SPOKEN_WORD) {
        return err(s, `You speak '${word}'. The plate stays exactly as warm and as shut as it was.`);
      }
      // The inscription says "counting the reach that speaks them", and this
      // call has already been counted. One either side, to be kind.
      const expected = s.reaches;
      if (Math.abs(Number(digits) - expected) > 1) {
        return err(
          s,
          `You speak '${code}'. Something behind the plate counts, disagrees, and declines.`,
        );
      }
      s.sealOpen = true;
      s.journal.push(`Opened the seal with ${code}.`);
      return ok(
        s,
        "The plate takes the sentence and swallows it.\n\nThe door opens inward on a hinge you never found.\n\n(Check your tools. There is a new one.)",
      );
    }

    case "leave": {
      s.escaped = true;
      s.journal.push("Left the room.");
      return ok(s, W.WIN_TEXT);
    }

    default:
      return err(s, `'${name}' is not a thing you can do.`);
  }
}

/** A nudge, sized to how stuck the player actually is. Used by the `hint` prompt. */
export function hint(s: GameState): string {
  if (s.escaped) return "You have already left. Start a new session to play again.";
  if (s.sealOpen) return "The door is open and you have a tool you did not have before. Use it.";
  if (s.readPlinth && s.heardWord) {
    return "You know the word and you know how to count. Look at the seal tool's schema — the pattern is the specification.";
  }
  if (s.lit && !s.readPlinth) {
    return "There is writing in this room. Reading it requires light, which you now have.";
  }
  if (s.lit && !s.heardWord) {
    return "One of your three original tools has a description that is not true. Test it under different conditions.";
  }
  if (s.lanternFound && !s.lit) {
    return "Your tool list grew when you touched the lantern. Call tools/list again.";
  }
  if (!s.seenRoom) return "Start by looking.";
  return "You cannot see. Find the thing you can touch, and touch it.";
}
