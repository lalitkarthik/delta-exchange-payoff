/**
 * The canonical instrument string, on the web side.
 *
 * `engine/src/deltapayoff/events/instrument.py` owns the type — `Instrument`, its
 * `canonical()` and `from_canonical`. This file does not port that type; it only builds
 * and recognises the one string form, which is all a browser needs: the ladder builds a
 * string to put in a URL and hand to `/bars`, and the page recognises one arriving in a
 * pasted link. Nothing here parses a string back into typed fields, because nothing on
 * the web app needs a strike or an expiry it does not already have from the chain
 * response that produced the string.
 *
 * `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY`, e.g. `DELTA-BTC-20260627-60000-C-USD` —
 * hyphen separated, ISO date, strike without a trailing zero, the quote currency last.
 * This app only ever talks to one venue, so `VENUE` is the literal `DELTA` everywhere
 * below; the engine's own type carries the field because it is venue-independent, and
 * this file is not.
 *
 * **The currency is required, not optional, #60 (I1).** `events.instrument.Instrument
 * .from_canonical` rejects a five-part string — the pre-I1 shape with no currency —
 * loudly rather than defaulting one onto it, so `canonicalInstrument` must be handed a
 * currency to build a string `/bars` will accept at all.
 */

export const VENUE = "DELTA";

/** `"call"`/`"put"`, the spelling `ChainLadder`'s `side` prop already uses, to `C`/`P`. */
export type Right = "call" | "put";

function rightCode(side: Right): "C" | "P" {
  return side === "call" ? "C" : "P";
}

/**
 * `DD-MM-YYYY` (Delta's own spelling, and `ChainResponse.expiry`'s) to `YYYYMMDD`.
 *
 * A plain string rearrangement, not a `Date` round trip — going through `Date` risks a
 * local-time shift for a date with no time component, which is exactly the kind of
 * off-by-one this project has been bitten by elsewhere. `expiry` is validated upstream
 * by the engine's own `DD-MM-YYYY` contract, so this assumes the shape rather than
 * re-checking it.
 */
function expiryToIso(expiry: string): string {
  const [day, month, year] = expiry.split("-");
  return `${year}${month}${day}`;
}

/**
 * The canonical string for one leg of one strike on one chain.
 *
 * A JS number already prints without a trailing zero or a leading `+` — `String(60000)`
 * is `"60000"`, `String(1234.5)` is `"1234.5"` — so, unlike the engine's `Decimal`-typed
 * strike, no separate formatting step is needed here to match `format_strike`'s output.
 *
 * `quoteCurrency` comes from the `ChainResponse` the leg was clicked on
 * (`chain.quote_currency`, #60/I1) — never a literal `"USD"` here, which would be
 * exactly the guess `docs/chain-contract.md` says the browser must never make.
 */
export function canonicalInstrument(
  underlying: string,
  expiry: string,
  strike: number,
  side: Right,
  quoteCurrency: string,
): string {
  return (
    `${VENUE}-${underlying}-${expiryToIso(expiry)}-${strike}-` +
    `${rightCode(side)}-${quoteCurrency}`
  );
}

/**
 * Loosely: does this look like a canonical instrument string at all?
 *
 * Used only to decide whether a query parameter is worth sending to `/bars` rather than
 * silently ignored — the engine is the authority on whether it is actually valid, and
 * answers 400 if not. This is not a validator the way `Instrument.from_canonical` is; it
 * exists so a mistyped or truncated link opens the chain page without a panel stuck
 * trying to load nothing, rather than so a malformed string is rejected here first.
 *
 * **Six parts, #60 (I1)** — the pre-I1 five-part shape no longer matches, exactly as
 * the engine's own parser no longer accepts it. A link copied before this ticket landed
 * opens the chain page cleanly with no panel, the same disposition every other
 * malformed value already gets.
 */
export function looksCanonical(text: string): boolean {
  return /^[^-]+-[^-]+-\d{8}-[0-9.]+-[CP]-[A-Z]{3}$/.test(text);
}

/** What the panel needs back out of a canonical string to find its row on the live or
 * historical ladder: the strike and which side. Not the engine's full parse — no venue,
 * no `Decimal`, no expiry-as-date — because nothing on the web app needs more than this
 * to match a `ChainRow`. */
export interface ParsedInstrument {
  underlying: string;
  strike: number;
  side: Right;
}

/**
 * The inverse of `canonicalInstrument`, loosely. `null` on anything `looksCanonical`
 * rejects, or a strike that does not parse as a number.
 *
 * **Gated on `looksCanonical` rather than re-checking the same shape by hand.** The two
 * used to validate independently — `looksCanonical` required an eight-digit date and
 * this function never looked at that segment at all, so a string with a malformed date
 * could fail one and pass the other. Routing every parse through the one regex is what
 * keeps that impossible rather than merely unlikely today.
 */
export function parseCanonical(text: string): ParsedInstrument | null {
  if (!looksCanonical(text)) return null;
  const parts = text.split("-");
  const [, underlying, , strikeText, rightCode] = parts;
  const strike = Number(strikeText);
  if (!Number.isFinite(strike)) return null;
  if (rightCode !== "C" && rightCode !== "P") return null;
  return { underlying: underlying!, strike, side: rightCode === "C" ? "call" : "put" };
}
