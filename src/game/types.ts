/**
 * The entire game is this object. Nothing else is remembered.
 *
 * `reaches` is the number of tool calls the player has made. It is not
 * bookkeeping — it is the answer to a puzzle. See world.ts.
 */
export interface GameState {
  reaches: number;
  seenRoom: boolean;
  lanternFound: boolean;
  lit: boolean;
  heardWord: boolean;
  readPlinth: boolean;
  sealOpen: boolean;
  escaped: boolean;
  journal: string[];
}

export function newGame(): GameState {
  return {
    reaches: 0,
    seenRoom: false,
    lanternFound: false,
    lit: false,
    heardWord: false,
    readPlinth: false,
    sealOpen: false,
    escaped: false,
    journal: [],
  };
}

/** A tool as the player sees it: name, description, JSON Schema. */
export interface ToolDef {
  name: string;
  description: string;
  inputSchema: {
    type: "object";
    properties: Record<string, unknown>;
    required?: string[];
    additionalProperties: false;
  };
}

export interface ToolOutcome {
  state: GameState;
  text: string;
  isError?: boolean;
}
