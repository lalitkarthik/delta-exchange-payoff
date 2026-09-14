/**
 * The four edits the analyse tab makes to a strategy, as pure functions of what was
 * typed.
 *
 * **Separate from `lib/legs.ts`, which is the chain's B/S buttons.** Those two take a
 * contract and a direction and know nothing about text; these take a character someone
 * is part way through typing and have to decide whether it means anything yet. One
 * module would have to answer both questions and would drag `looksCanonical` and the
 * half-typed-number rules onto the ladder, which has no use for either.
 *
 * **A rejected edit returns the array it was given, not an equal copy.** The screen
 * re-asks `POST /analyse` whenever the legs change identity, so a fresh array for a
 * half-typed `"1e"` would fire a request a keystroke for a strategy nobody changed.
 * Identity is the signal, and these functions are careful with it.
 *
 * **`commit*` is the pair of each `with*`, and it exists because the inputs are
 * uncontrolled.** A refused edit returns the same array, so nothing re-renders and the
 * DOM keeps what was typed: a quantity box reading `0` beside metrics, a curve and a URL
 * that all describe a quantity of 1. `commit*` returns the strategy **and the text the
 * field must show**, so the caller can re-seed the box when the two disagree. The
 * rejection was always correct; what was missing was telling the reader.
 *
 * **`Number`, never `parseFloat`.** The project's rule is about decimals arriving from
 * the engine, which are JSON numbers and are never parsed; a character a human typed
 * into a text field has to be read somehow. `Number` is what `lib/legs-url.ts` already
 * reads a `@price` fragment with, and it refuses `"1.2.3"` and `"12abc"` outright where
 * `parseFloat` would silently accept the good prefix and drop the rest — which on a
 * price is the difference between 1.2 and 1.23.
 */
import { looksCanonical } from "./instrument";
import type { LegRequest } from "./payoff";

/** The strategy with `index` replaced, or the same array when there is no such leg. */
function replace(
  legs: LegRequest[],
  index: number,
  edit: (leg: LegRequest) => LegRequest | null,
): LegRequest[] {
  const leg = legs[index];
  if (leg === undefined) return legs;
  const next = edit(leg);
  if (next === null) return legs;
  return legs.map((candidate, i) => (i === index ? next : candidate));
}

/** Decimal digits and nothing else. `Number` alone would read `"1e3"` as 1000 and
 *  `"0x10"` as 16 — both positive integers, and neither one a quantity `lib/legs-url.ts`
 *  can spell: its fragment is `(\d+)`, so a quantity accepted here that could not survive
 *  the round trip would be a strategy the link cannot carry. */
const DIGITS = /^\d+$/;

/**
 * "What if I did two of these."
 *
 * A positive integer, and nothing else — the contract's own words for `quantity`, and
 * the sign is never here: it lives in `direction`, which is why `"-1"` is refused rather
 * than being read as a sale.
 *
 * An edit to the quantity already in force is not an edit: see this module's header on
 * identity, and `AnalyseScreen` for what a fresh array costs.
 */
export function withQuantity(legs: LegRequest[], index: number, typed: string): LegRequest[] {
  return replace(legs, index, (leg) => {
    const text = typed.trim();
    if (!DIGITS.test(text)) return null;
    const quantity = Number(text);
    if (quantity < 1 || quantity === leg.quantity) return null;
    return { ...leg, quantity };
  });
}

/**
 * "What if I were filled at 900" — and the way back out of it.
 *
 * **An empty field deletes `entry_price` rather than setting it to zero.** Absent means
 * the engine prices the leg from the book by crossing the spread — buy at the ask, sell
 * at the bid — and a field with no way back to that would trap a leg at whatever was
 * last typed. `null` is not `0` here as everywhere else: a price of zero is a real zero
 * and is kept.
 *
 * A negative price is refused. An option is not sold for a negative premium; the credit
 * of a sale is `direction`, and reading a minus sign here as one would put the same fact
 * on the leg twice.
 */
export function withEntryPrice(legs: LegRequest[], index: number, typed: string): LegRequest[] {
  return replace(legs, index, (leg) => {
    if (typed.trim() === "") {
      if (leg.entry_price === undefined) return null;
      const { entry_price: _dropped, ...rest } = leg;
      return rest;
    }
    const entryPrice = Number(typed.trim());
    if (!Number.isFinite(entryPrice) || entryPrice < 0) return null;
    if (entryPrice === leg.entry_price) return null;
    return { ...leg, entry_price: entryPrice };
  });
}

/**
 * A different contract, with the quantity and any typed price riding along — the edit
 * that turns "the 77,000 call" into "the 78,000 call" without rebuilding the leg.
 *
 * **`looksCanonical` gates it, exactly as in `lib/legs-url.ts`.** The engine's
 * `Instrument.from_canonical` remains the strict authority and answers 400 naming the
 * part that was wrong; this only keeps a half-typed string from being sent as a leg on
 * every keystroke.
 */
export function withInstrument(legs: LegRequest[], index: number, typed: string): LegRequest[] {
  return replace(legs, index, (leg) => {
    const instrument = typed.trim();
    if (!looksCanonical(instrument) || instrument === leg.instrument) return null;
    return { ...leg, instrument };
  });
}

/**
 * What a field must show for the leg as it now stands — the other half of a commit.
 *
 * Empty for an absent `entry_price`, which is the field's way of saying "priced from the
 * book". `String` on a number rather than any formatting: this is the text of a value,
 * not a presentation of it, and a thousands separator here would not survive being read
 * back.
 */
function quantityText(leg: LegRequest): string {
  return String(leg.quantity);
}

function priceText(leg: LegRequest): string {
  return leg.entry_price === undefined || leg.entry_price === null ? "" : String(leg.entry_price);
}

/** A strategy, and the text the field that produced it must now show. When `text` differs
 *  from what was typed, the edit was refused (or normalised) and an uncontrolled input has
 *  to be re-seeded — otherwise it goes on displaying a value nothing else on the screen
 *  agrees with. */
export interface Commit {
  legs: LegRequest[];
  text: string;
}

export function commitQuantity(legs: LegRequest[], index: number, typed: string): Commit {
  const next = withQuantity(legs, index, typed);
  const leg = next[index];
  return { legs: next, text: leg === undefined ? typed : quantityText(leg) };
}

export function commitEntryPrice(legs: LegRequest[], index: number, typed: string): Commit {
  const next = withEntryPrice(legs, index, typed);
  const leg = next[index];
  return { legs: next, text: leg === undefined ? typed : priceText(leg) };
}

export function commitInstrument(legs: LegRequest[], index: number, typed: string): Commit {
  const next = withInstrument(legs, index, typed);
  const leg = next[index];
  return { legs: next, text: leg === undefined ? typed : leg.instrument };
}

/** The strategy without this leg. Removing the last one leaves `[]`, which is a screen
 *  state — "this link names no legs" — and never a request. */
export function removeLeg(legs: LegRequest[], index: number): LegRequest[] {
  if (legs[index] === undefined) return legs;
  return [...legs.slice(0, index), ...legs.slice(index + 1)];
}

/** The same contract, traded the other way. B and S are separate positions, so the
 *  entry price does not survive the flip: it was a fill on the other side of the spread,
 *  and keeping it would claim a price nobody was given. Dropped, so the engine re-prices
 *  the leg off the book. */
export function toggleDirection(legs: LegRequest[], index: number): LegRequest[] {
  return replace(legs, index, (leg) => {
    const { entry_price: _dropped, ...rest } = leg;
    return { ...rest, direction: leg.direction === 1 ? -1 : 1 };
  });
}

/** One more leg, copied off the last one at a single lot and no price — a starting point
 *  to edit, since a blank contract string is not a contract. */
export function appendLeg(legs: LegRequest[]): LegRequest[] {
  const last = legs[legs.length - 1];
  if (last === undefined) return legs;
  return [...legs, { instrument: last.instrument, direction: last.direction, quantity: 1 }];
}
