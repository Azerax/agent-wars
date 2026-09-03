import type { Item } from "./types.js";

/**
 * The loot table.
 *
 * Read the `grants` field as the point of each item. A weapon is not "+3 atk",
 * it is a verb you did not have. Taking the axe off a corpse is the difference
 * between hitting one thing and hitting everything around you, and the other
 * agent finds out you did it when its tool list stops predicting yours.
 */
export const ITEMS: Record<string, Item> = {
  // ---- weapons ----
  rusted_axe: {
    id: "rusted_axe",
    name: "rusted axe",
    slot: "weapon",
    atk: 2,
    grants: ["cleave"],
    desc: "Heavy and blunt. Hits everything next to you, which is not always what you want.",
  },
  hunting_bow: {
    id: "hunting_bow",
    name: "hunting bow",
    slot: "weapon",
    atk: 1,
    grants: ["shoot"],
    desc: "Reaches four tiles in a straight line, if nothing is in the way.",
  },
  iron_spear: {
    id: "iron_spear",
    name: "iron spear",
    slot: "weapon",
    atk: 3,
    grants: ["thrust"],
    desc: "Two tiles of reach. You hit them; they do not hit you.",
  },
  bone_knife: {
    id: "bone_knife",
    name: "bone knife",
    slot: "weapon",
    atk: 2,
    speed: 2,
    grants: ["stab"],
    desc: "Light enough to move you up the order. Hurts more from behind.",
  },

  // ---- offhand ----
  tower_shield: {
    id: "tower_shield",
    name: "tower shield",
    slot: "offhand",
    def: 2,
    speed: -1,
    grants: ["brace"],
    desc: "Slow, and worth it.",
  },
  grapple_hook: {
    id: "grapple_hook",
    name: "grapple hook",
    slot: "offhand",
    grants: ["hook"],
    charges: 3,
    desc: "Drags something three tiles away to somewhere you can reach it.",
  },
  smoke_flask: {
    id: "smoke_flask",
    name: "smoke flask",
    slot: "offhand",
    grants: ["smoke"],
    charges: 2,
    desc: "Nobody can see through it, including you.",
  },

  // ---- armor ----
  chain_mail: {
    id: "chain_mail",
    name: "chain mail",
    slot: "armor",
    def: 3,
    maxHp: 4,
    speed: -1,
    desc: "Grants no new abilities. Simply keeps you alive, which is an ability.",
  },
  scout_cloak: {
    id: "scout_cloak",
    name: "scout cloak",
    slot: "armor",
    def: 1,
    speed: 1,
    grants: ["scan"],
    desc: "Thin, but it lets you look much further than you can see.",
  },

  // ---- trinkets ----
  healers_kit: {
    id: "healers_kit",
    name: "healer's kit",
    slot: "trinket",
    grants: ["mend"],
    charges: 3,
    desc: "Three uses. There is no fourth.",
  },
  bloodstone: {
    id: "bloodstone",
    name: "bloodstone",
    slot: "trinket",
    atk: 1,
    grants: ["drain"],
    desc: "Takes a little and gives it to you.",
  },
  false_compass: {
    id: "false_compass",
    name: "brass compass",
    slot: "trinket",
    speed: 1,
    grants: ["divine"],
    // The description is a lie, and so is the tool's. It points at the nearest
    // corpse, not the nearest living enemy. It is a genuinely good trinket for
    // a looter and a fatal one for a hunter, and nothing tells you which.
    desc: "Points unerringly towards the nearest living enemy.",
  },
};

export function item(id: string): Item {
  const found = ITEMS[id];
  if (!found) throw new Error(`No such item: ${id}`);
  return found;
}

/** Loot tables by monster strength. */
export const COMMON = ["bone_knife", "smoke_flask", "scout_cloak", "healers_kit"];
export const UNCOMMON = ["hunting_bow", "rusted_axe", "grapple_hook", "false_compass"];
export const RARE = ["iron_spear", "tower_shield", "chain_mail", "bloodstone"];
