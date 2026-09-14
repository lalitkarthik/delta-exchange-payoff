/**
 * The Strategy, as a string in the address bar.
 *
 * **State lives in the URL, not in a store** (spec `2026-09-10-payoff-design.md`): a
 * strategy is small, it is the thing a trader wants to send someone, and a reload that
 * loses four clicks is the kind of small betrayal that stops a tool being used.
 *
 * One leg, one fragment: `<instrument>:<B|S><quantity>[@<entry_price>]`, fragments
 * joined by `,`. `instrument` is the canonical string `docs/payoff-contract.md` and
 * `lib/instrument.ts` both use — `DELTA-BTC-20260904-77000-C-USD` — so it is not split
 * back into strike/expiry/right here the way the sibling project's per-field encoding
 * does; this project's leg is already one field for "which contract", and colons and
 * commas never appear inside one, so `:` and `,` are safe delimiters.
 *
 * Prior art: `convex-hedge-payoff/web/lib/legs-url.ts` and its test. Taken for the
 * **shape** — round trip, `B`/`S` for people rather than `1`/`-1`, and above all a
 * malformed fragment throwing rather than being dropped — not for the per-field format,
 * which does not fit a leg named by one canonical string.
 *
 * **A malformed leg throws, naming the part that was wrong.** The tempting alternative —
 * skip the fragment that does not parse — would show a trader a chart of three legs when
 * their link named four, with nothing on screen saying so. That is the one failure mode
 * this codec exists to make impossible.
 */

import type { Direction, LegRequest } from "./payoff";
import { looksCanonical } from "./instrument";

/** Thrown for any fragment that is not a complete, well-formed leg. */
export class LegsUrlError extends Error {
  constructor(fragment: string, reason: string) {
    super(`cannot read "${fragment}" as a leg: ${reason}`);
    this.name = "LegsUrlError";
  }
}

/** `legs` back into the string the address bar carries. Empty legs encode to `""`. */
export function encodeLegs(legs: LegRequest[]): string {
  return legs
    .map((leg) => {
      const side = leg.direction === 1 ? "B" : "S";
      const price =
        leg.entry_price === undefined || leg.entry_price === null ? "" : `@${leg.entry_price}`;
      return `${leg.instrument}:${side}${leg.quantity}${price}`;
    })
    .join(",");
}

/**
 * `<B|S><quantity>[@<entry_price>]`, the half of a fragment after the instrument.
 *
 * **The price group is `.+`, not a decimal-shaped pattern.** It used to be
 * `\d+(?:\.\d+)?`, which cannot match the exponential notation `String(n)` switches to
 * outside roughly `1e-6`..`1e21` (`String(1e-7)` is `"1e-7"`) — so `encodeLegs` could
 * silently write a fragment its own `decodeLegs` then rejected. The shape is checked
 * afterwards, by `Number.isFinite` on whatever this group captured, which is the same
 * split `quantity` already uses: match loosely here, validate the meaning below.
 */
const SIDE = /^([BS])(\d+)(?:@(.+))?$/;

/**
 * One fragment into one leg, or an exception naming the part that was wrong.
 *
 * **`looksCanonical` gates the instrument half, not a second regex.** It is the same
 * loose check `ContractPanel` already trusts to decide whether a query parameter is
 * worth sending on — the engine's `Instrument.from_canonical` remains the strict
 * authority and answers 400 on anything this misses, but a client-side check that
 * disagreed with it about the shape would be a second copy of the rule that can drift.
 */
function decodeLeg(fragment: string): LegRequest {
  const sep = fragment.lastIndexOf(":");
  if (sep === -1) {
    throw new LegsUrlError(
      fragment,
      "missing ':' separating the instrument from <B|S><quantity>[@<entry_price>]",
    );
  }
  const instrument = fragment.slice(0, sep);
  const rest = fragment.slice(sep + 1);

  if (!looksCanonical(instrument)) {
    throw new LegsUrlError(fragment, `"${instrument}" is not a canonical instrument string`);
  }

  const match = SIDE.exec(rest);
  if (!match) {
    throw new LegsUrlError(
      fragment,
      `expected <B|S><quantity>[@<entry_price>] after the instrument, got "${rest}"`,
    );
  }
  const [, side, quantityText, priceText] = match;
  const quantity = Number(quantityText);
  if (!Number.isInteger(quantity) || quantity < 1) {
    throw new LegsUrlError(fragment, `quantity must be a positive integer, got "${quantityText}"`);
  }

  const direction: Direction = side === "B" ? 1 : -1;
  const leg: LegRequest = { instrument, direction, quantity };
  if (priceText !== undefined) {
    const entryPrice = Number(priceText);
    if (!Number.isFinite(entryPrice)) {
      throw new LegsUrlError(fragment, `entry_price must be a finite number, got "${priceText}"`);
    }
    leg.entry_price = entryPrice;
  }
  return leg;
}

/**
 * The string back into `legs`, or nothing parsed at all and an exception.
 *
 * `Array.prototype.map` is what makes "nothing at all" true by construction: it throws
 * on the first fragment `decodeLeg` rejects and never produces a partial array for a
 * caller to catch and keep — there is no local accumulator here to hold one.
 */
export function decodeLegs(encoded: string | null | undefined): LegRequest[] {
  if (!encoded) return [];
  return encoded.split(",").map(decodeLeg);
}

/**
 * The link the chain's Analyse button opens, in a new tab.
 *
 * Here rather than in `ChainScreen` because it is the third thing this file already
 * knows: how a strategy is spelled in an address bar. Building it beside `encodeLegs`
 * is what keeps the writer and the reader of that spelling in each other's sight, which
 * is `lib/view.ts`'s own argument for keeping `parseView` and `viewQuery` together.
 *
 * **The minute travels with the strategy.** A tab opened from a stored minute has to
 * stay at that minute — a historical reading that silently became a live one would be
 * the same betrayal as opening on the wrong minute — and `null` is "live", the absence
 * of a stamp rather than the newest one, exactly as on the chain screen.
 *
 * `:` and `,` are put back after `URLSearchParams` escapes them. Both are legal in a
 * query string, and a link someone reads before clicking should show the strategy it
 * opens rather than `%3A` and `%2C`.
 */
export function analyseHref(legs: LegRequest[], minute: string | null): string {
  const params = new URLSearchParams({ legs: encodeLegs(legs) });
  if (minute) params.set("minute", minute);
  return `/analyse?${params.toString().replace(/%3A/g, ":").replace(/%2C/g, ",")}`;
}

/**
 * The one invariant behind every `synced*` URL a screen in this app writes back into
 * its own address bar — **a rewrite must never outlive an unreported parse failure**
 * — as a gate any screen's own URL builder can be run through, not only
 * `analyseHref`'s.
 *
 * Every screen here syncs the address bar to its own state a moment after the state
 * settles. When the state was decoded *from* that address bar and the decode failed,
 * those two facts combine into a quiet disaster: `decodeLegs` throws naming the
 * fragment that was wrong, the screen puts that sentence on the page — and 200 ms
 * later the sync replaces the reader's `?legs=<the text they need to fix>` with
 * `?legs=`. The notice then describes a string nobody can see any more, and a reload
 * turns a broken link into an empty one.
 *
 * **This was diagnosed on the chain screen in P4's review, and the wrong remedy was
 * applied there** — clearing the notice once the reader made an edit, which stops the
 * notice going stale but does nothing about the rewrite that erases the text before
 * they can act on it. It surfaced again on the analyse screen in P6, correctly fixed
 * there with `syncedAnalyseHref`, and only *then* fixed properly on the chain screen
 * too, by routing `ChainScreen`'s own query builder through this same gate. So the
 * rule lives here, beside the codec whose throwing it protects, once: **any screen
 * that syncs a decoded strategy back into the URL calls `syncedUrl` (directly, or via
 * `syncedAnalyseHref`) rather than writing its query unconditionally, and honours the
 * `null`.**
 *
 * `build` runs only when there is nothing to protect — `legsError === null` — so a
 * caller whose own URL builder isn't free never pays for it while a parse failure is
 * on screen.
 *
 * `null` only while a decode failure is unreported. An emptied strategy is not one —
 * the reader removed the last leg, an empty strategy is the truth about what is on
 * screen, and a link that still named the legs they deleted would be the lie in the
 * other direction.
 */
export function syncedUrl(legsError: string | null, build: () => string): string | null {
  return legsError === null ? build() : null;
}

/**
 * The address a screen should write for this strategy — or **`null` for "leave the
 * address bar exactly as it is"**, `syncedUrl`'s own contract. See that function for
 * the invariant and its history.
 */
export function syncedAnalyseHref(
  legs: LegRequest[],
  minute: string | null,
  legsError: string | null,
): string | null {
  return syncedUrl(legsError, () => analyseHref(legs, minute));
}
