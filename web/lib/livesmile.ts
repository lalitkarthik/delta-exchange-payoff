/**
 * The live chain, read as a smile.
 *
 * ## Why this file exists at all
 *
 * The stream pushes a **chain** (`docs/chain-contract.md`) and this screen draws a
 * **smile** (`docs/smile-contract.md`). Two payloads, two shapes, and the live minute
 * has to reach the chart somehow. Three routes were available and only one of them is
 * defensible:
 *
 *   - **Re-fetch `/smile` on a timer.** One HTTP request per tick, against an endpoint
 *     whose whole design note is that it sends the day precisely so that nothing has to
 *     be asked for again. It also cannot work: `/smile` deliberately withholds the open
 *     minute, so a once-a-second poll would spend a request a second to be handed the
 *     same sealed minute for sixty of them.
 *   - **Have the engine push a smile too.** A second websocket route, a second response
 *     model, a second contract document — an engine change, for a payload the engine is
 *     already sending.
 *   - **Project the chain push into a smile minute, here.** Which is this file.
 *
 * ## The projection cannot disagree with the ladder, and that is the point
 *
 * The chain screen and this screen read the **same object off the same socket**. Every
 * number below is carried across unchanged; nothing is computed, averaged, interpolated
 * or re-derived. So for a given push, the volatility this chart plots at strike K is the
 * identical `double` the ladder prints in its IV column for strike K — not a second
 * calculation that agrees to some tolerance, the same field of the same JSON object.
 *
 * **The one difference is in time, not in value.** The ladder renders every push; this
 * screen samples at about 1 Hz (`LIVE_REDRAW_MS` in `VolatilityScreen.tsx`), so the
 * curve can be up to one sample behind the ladder. Both are showing a real observation
 * and each is labelled with the minute it belongs to; neither is showing a different
 * number for the same instant.
 *
 * ## What the projection actually does
 *
 * Three reshapings, each of them a rule already written down somewhere:
 *
 *   - **Two legs become one point.** Put-call parity gives the strike one volatility and
 *     `compute.enrich` writes that number to both legs, so a paired strike carries it
 *     twice. `docs/smile-contract.md` §"One point per strike, not per leg" says the
 *     smile de-duplicates by `(minute, strike)`, and that is not a choice between two
 *     numbers — they are one number stored twice.
 *   - **`iv_reason: ""` becomes `null`.** The two contracts spell "solved" differently on
 *     purpose — `/chain` uses the empty string, the store uses `null` — and the smile
 *     side is the one this screen reads. `smile-contract.md` is explicit that a column
 *     holding both spellings for one fact is a column every reader has to guess at.
 *   - **An instant becomes a minute.** `fetched_at` is second-precision; the smile's
 *     grain is the minute. It is floored, never rounded, so the stamp names the minute
 *     the observation fell inside rather than the nearest one.
 *
 * Nothing here is filtered. A strike the solver refused arrives as a point with a null
 * `iv` carrying its reason, exactly as the stored curve does, because the chart breaks
 * its line on that null and a dropped point would be joined across.
 */
import type { ChainResponse, SmileMinute, SmilePoint } from "./contract";
import { minuteOfInstant } from "./timeline";

/**
 * What a strike carries when the push held no `computed` block for it at all.
 *
 * Distinct from anything the solver says, because the solver did not speak: a chain that
 * has not been through the engine's enrichment has no volatility for any strike, and
 * `ComputedLeg` being `null` is the contract's way of saying so. Phrased as the fact
 * rather than as a refusal.
 */
export const NOT_COMPUTED = "this push carried no computed volatility";

/**
 * One push, as the minute it belongs to. `null` when `fetched_at` is unparseable, which
 * the contract forbids — an unstamped push cannot be placed in time and is dropped
 * rather than being drawn at a minute it might not belong to.
 */
export function smileMinuteFromChain(chain: ChainResponse): SmileMinute | null {
  const minute = minuteOfInstant(chain.fetched_at);
  if (minute === null) return null;

  const points: SmilePoint[] = chain.rows.map((row) => {
    // Either leg will do: both carry the strike's one volatility. The call is tried
    // first only so the choice is deterministic — `iv_leg` names the side the number
    // was actually solved on and is carried across from whichever leg is read.
    const computed = row.call?.computed ?? row.put?.computed ?? null;
    if (!computed) {
      return { strike: row.strike, iv: null, iv_leg: null, iv_reason: NOT_COMPUTED };
    }
    return {
      strike: row.strike,
      iv: computed.iv,
      iv_leg: computed.iv_leg,
      iv_reason: computed.iv_reason === "" ? null : computed.iv_reason,
    };
  });

  return {
    minute,
    forward: chain.forward,
    discount: chain.discount,
    years_to_expiry: chain.years_to_expiry,
    forward_method: chain.forward_method,
    // The chain contract carries no model stamp — the store writes one per row and the
    // stream does not. Left null rather than guessed at from the stored response, which
    // would be this screen asserting which model produced a number it did not see.
    model_version: null,
    points,
  };
}
