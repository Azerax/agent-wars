/**
 * All of the game's prose lives here, so the engine stays readable.
 *
 * A note on voice: the player is a model reading tool results. There is no
 * screen. Every sensation has to arrive as the return value of a function
 * call, and every failure has to arrive as an error. Write accordingly.
 */

export const DARK_LOOK = [
  "You cannot see. Not the ordinary dark of a room with the light off — the",
  "dark of a place that has never been looked at. Your hands find two things:",
  "something cold and ridged standing on the floor (a LANTERN, you decide),",
  "and a flat vertical surface with a seam down it (a DOOR).",
  "",
  "Touchable here: lantern, door.",
].join("\n");

export const LIT_LOOK = [
  "The lantern throws a small, honest circle of light.",
  "",
  "You are in a stone antechamber, perhaps four paces square. There is no",
  "ceiling that you can see — the light gives out before it finds one.",
  "",
  "The DOOR is banded iron with a brass SEAL-PLATE at chest height. The plate",
  "has a slot the width of a sentence.",
  "",
  "A waist-high PLINTH stands in the centre of the room with an inscription",
  "cut into its top face.",
  "",
  "In the corner, a DRAIN, black and dry.",
  "",
  "Touchable here: lantern, door, plinth, drain.",
].join("\n");

export const OPEN_LOOK = [
  "The door stands open. Beyond it is not another room; it is the ordinary",
  "outside, with weather in it.",
  "",
  "The lantern is still burning on the floor where you left it. It does not",
  "seem to need you.",
].join("\n");

export const ESCAPED_LOOK = [
  "You are outside. Behind you the antechamber is a dark rectangle, already",
  "looking smaller than it was.",
  "",
  "The game is over. You won it by making a tool exist.",
].join("\n");

export const LISTEN_DARK = [
  "You were told there was nothing to hear. You were told wrong.",
  "",
  "In the dark, very faint, close to the floor, a voice is repeating one word",
  "with great patience:",
  "",
  '        "OWL.   OWL.   OWL."',
  "",
  "It has clearly been doing this for a long time.",
].join("\n");

export const LISTEN_LIT = [
  "Nothing. The lantern hisses very slightly, and that is the whole of it.",
  "",
  "(Whatever was speaking has stopped, or you have stopped being the sort of",
  "thing that can hear it.)",
].join("\n");

export const PLINTH_TEXT = [
  "The inscription is cut deep and reads, in full:",
  "",
  "        THE SEAL TAKES THREE LETTERS, A DASH, AND FOUR DIGITS.",
  "        THE LETTERS ARE THE WORD SPOKEN IN THE DARK.",
  "        THE DIGITS ARE THE NUMBER OF TIMES YOU HAVE REACHED",
  "        INTO THIS ROOM — COUNTING THE REACH THAT SPEAKS THEM.",
  "",
  "Beneath, in smaller letters, as an afterthought:",
  "",
  "        (YOU HAVE REACHED IN {REACHES} TIMES SO FAR.)",
].join("\n");

export const DOOR_TEXT = [
  "The seal-plate is engraved with a single line:",
  "",
  "        SPEAK THE SEAL AND I OPEN.",
  "",
  "There is no keyhole, no handle, and no hinge on this side.",
].join("\n");

export const DRAIN_TEXT = [
  "The drain is dry. Four rusted rings are set into its lip; three are intact",
  "and one has worn through. Nothing is written on any of them.",
  "",
  "(It is a drain. Not everything in a room is a puzzle.)",
].join("\n");

export const LANTERN_FIRST_TOUCH = [
  "Cold brass, cold glass. Your thumb finds a lever on the side and it moves",
  "with a click that is much louder than it should be.",
  "",
  "Something is now possible that was not possible a moment ago. Check what",
  "you can do.",
].join("\n");

export const WIN_TEXT = [
  "You step through.",
  "",
  "The antechamber had three tools in it when you arrived: look, listen, and",
  "touch. None of them could have got you out. You did not solve the room by",
  "using your tools well. You solved it by causing a tool to come into",
  "existence and then using that.",
  "",
  "                    *** YOU HAVE LEFT THE ROOM ***",
].join("\n");
