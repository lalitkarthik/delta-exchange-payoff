/**
 * Which contracts the ladder's B/S buttons should already be showing as held, and the
 * two edits those buttons make.
 *
 * The strategy has been sitting in the URL the whole time and the ladder rendered none
 * of it onto the table, so a strike already in the position looked exactly like one
 * that was not. These three functions are the whole of that: the buttons in
 * `ChainLadder.tsx` just read and call them.
 *
 * **A leg is identified by its instrument and its direction, and nothing else.** Unlike
 * the sibling project's `strike`/`optionType`/`expiry` triple, this project's leg
 * already carries the whole contract as one canonical string (`docs/payoff-contract.md`),
 * so there is no second key to keep in step with it. Bought and sold are different
 * positions, not one position with a sign — `CONTEXT.md`'s separation of `direction`
 * from `quantity` — so B and S light independently and never both.
 *
 * **`quantity` is edited in place, not merged away.** `addLeg` increments an existing
 * matching leg rather than appending a second one-lot leg beside it, and `dropFirst`
 * decrements one lot off a matching leg before ever deleting it outright. This is a
 * deliberate divergence from the sibling project's own `positions.ts`
 * (`convex-hedge-payoff/web/lib/positions.ts:41-60`), whose `dropFirst` always deletes
 * the whole leg and relies on its Legs strip to repair a two-lot position split across
 * two one-lot legs. This project has no such strip on the chain screen — the analyse
 * tab's in-place quantity editing is #6, not this ticket — so a `legs=…:B2` link, which
 * `lib/legs-url.ts` decodes as one leg at quantity 2, has to survive being clicked back
 * off one lot at a time with nothing else able to fix a wrongly-split position.
 */
import type { Direction, LegRequest } from "./payoff";

function matches(leg: LegRequest, instrument: string, direction: Direction): boolean {
  return leg.instrument === instrument && leg.direction === direction;
}

/** Whether this exact contract, traded this way, is anywhere in the strategy. */
export function heldAt(legs: LegRequest[], instrument: string, direction: Direction): boolean {
  return legs.some((leg) => matches(leg, instrument, direction));
}

/**
 * The strategy with one more lot of this leg — a new one-lot leg if none is held yet,
 * or an existing matching leg's `quantity` incremented by one.
 *
 * No `entry_price` on a newly appended leg. Left absent rather than captured from the
 * ladder at click time so the engine fills it at analysis time by crossing the spread —
 * `docs/payoff-contract.md`: buy at the ask, sell at the bid — which is the true price
 * of a fill happening now rather than the fleeting quote under the pointer. An existing
 * leg's `entry_price` (should #6 ever let one carry one on this screen) is left exactly
 * as it was; only `quantity` changes.
 */
export function addLeg(legs: LegRequest[], instrument: string, direction: Direction): LegRequest[] {
  const at = legs.findIndex((leg) => matches(leg, instrument, direction));
  if (at === -1) return [...legs, { instrument, direction, quantity: 1 }];
  return legs.map((leg, i) => (i === at ? { ...leg, quantity: leg.quantity + 1 } : leg));
}

/**
 * The strategy with **one lot** of the first matching leg removed — the leg's
 * `quantity` decremented, or the leg dropped outright once it reaches zero.
 *
 * Every other leg, including a second one that happens to share this leg's instrument
 * and direction (reachable off a decoded URL, never off `addLeg` — see the module
 * comment), is untouched: only the first match moves.
 */
export function dropFirst(
  legs: LegRequest[],
  instrument: string,
  direction: Direction,
): LegRequest[] {
  const at = legs.findIndex((leg) => matches(leg, instrument, direction));
  if (at === -1) return legs;
  const leg = legs[at]!;
  if (leg.quantity > 1) {
    return legs.map((candidate, i) =>
      i === at ? { ...candidate, quantity: candidate.quantity - 1 } : candidate,
    );
  }
  return [...legs.slice(0, at), ...legs.slice(at + 1)];
}
