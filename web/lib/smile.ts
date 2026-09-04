/**
 * Turning a `/smile` response into the rows the chart draws, and into the things the
 * header has to admit about them.
 *
 * Everything here is pure and none of it renders. That is deliberate: the client has no
 * test runner, so the fewer decisions the component makes the fewer of them ship
 * unverified, and the ones that live here can at least be read in one place.
 *
 * Two rules from `docs/smile-contract.md` are load-bearing and are implemented here
 * rather than in the chart:
 *
 *   - **An unsolved strike is a point with a `null` volatility, never a missing point.**
 *     It stays in the row list, carrying its reason, and the `null` is what makes
 *     Recharts break the line. Dropping it would join its neighbours through the gap.
 *   - **`model_versions` is a list.** A response spanning two stamps says so; it never
 *     picks one.
 *
 * A third rule arrived with the scrubber and is enforced here for the same reason: a
 * **thin** minute — one the store holds only a handful of the board's contracts for —
 * must break the line at the strikes it does not hold, not draw a segment across them.
 * The contract sends a `null` point for a strike that failed to solve, but a strike with
 * no row at all simply does not arrive, so there is nothing in the payload to break on.
 * `strikeGrid` recovers the board from the whole response and `toRows` fills the holes
 * with absences. That inserts no number anywhere; it inserts the fact that there isn't
 * one, which is exactly what the contract does with `iv: null`.
 */
import type { SmileMinute, SmilePoint, SmileResponse } from "./contract";

/** One strike, ready to draw. Percent, because that is the unit the axis is in. */
export interface SmileDatum {
  strike: number;
  /**
   * Implied volatility in **percent** — 31.89 for the contract's 0.3189. `null` when the
   * strike did not solve or was not stored at this minute, which is what breaks the line.
   */
  ivPct: number | null;
  /** `strike - forward`, in USD. `null` when the minute has no fitted forward. */
  offset: number | null;
  /** `"call"` or `"put"` — the out-of-the-money side the number came from. */
  leg: string | null;
  /** The solver's account of why there is no number. `null` when there is one. */
  reason: string | null;
  /**
   * `true` when this minute actually carried a row for this strike.
   *
   * The distinction the chart needs: a stored strike with no volatility is a strike the
   * solver refused and gets a dotted rule saying so, while an unstored one is a strike
   * this minute knows nothing about at all and gets only the break in the line. Marking
   * both the same way would put six hundred dotted rules on a thin minute and claim the
   * solver had refused every one of them.
   */
  stored: boolean;
}

/**
 * What a strike carries when the minute holds no row for it.
 *
 * Deliberately not phrased as a solver reason: the solver never saw this strike at this
 * minute. `docs/smile-contract.md` reserves `iv_reason` for the solver's own account.
 */
export const NOT_STORED = "no row stored at this minute";

/**
 * Every strike the response mentions anywhere, ascending — the board for this expiry.
 *
 * Read across the whole day rather than off one minute, which is what makes a thin
 * minute detectable at all. It also holds the strike axis still while the scrubber
 * moves: an axis that rescaled at every step would make two adjacent minutes look
 * different when only the sampling had changed.
 */
export function strikeGrid(response: SmileResponse): number[] {
  const seen = new Set<number>();
  for (const minute of response.minutes) {
    for (const point of minute.points) seen.add(point.strike);
  }
  return [...seen].sort((a, b) => a - b);
}

/**
 * One minute as rows, optionally laid out against the board's full strike grid.
 *
 * Without a grid this is the minute's own points and nothing else. With one, every
 * strike on the board appears, and the ones this minute did not store arrive as
 * absences — `ivPct: null`, `stored: false` — so the line breaks there.
 */
export function toRows(minute: SmileMinute, grid?: readonly number[]): SmileDatum[] {
  const forward = minute.forward;
  const row = (point: SmilePoint): SmileDatum => ({
    strike: point.strike,
    // The one multiplication by 100 on this screen, and it is presentation — the same
    // rule `lib/format.ts` states for the ladder.
    ivPct: point.iv === null ? null : point.iv * 100,
    offset: forward === null ? null : point.strike - forward,
    leg: point.iv_leg,
    reason: point.iv_reason,
    stored: true,
  });

  if (!grid || grid.length === 0) return minute.points.map(row);

  const byStrike = new Map(minute.points.map((point) => [point.strike, point]));
  return grid.map((strike) => {
    const point = byStrike.get(strike);
    if (point) return row(point);
    return {
      strike,
      ivPct: null,
      offset: forward === null ? null : strike - forward,
      leg: null,
      reason: NOT_STORED,
      stored: false,
    };
  });
}

/** The solved volatilities, in percent. Empty when nothing on the curve solved. */
export function solvedPercents(rows: readonly SmileDatum[]): number[] {
  const out: number[] = [];
  for (const row of rows) if (row.ivPct !== null) out.push(row.ivPct);
  return out;
}

/**
 * The strikes that **arrived** with no volatility. The line breaks at each of them and a
 * dotted rule says the solver refused it.
 *
 * A strike this minute did not store is excluded: the line still breaks there, because
 * its `ivPct` is null too, but no rule is drawn. The rule is a claim about the solver,
 * and there is no claim to make about a row that was never written.
 */
export function unsolvedStrikes(rows: readonly SmileDatum[]): number[] {
  return rows.filter((row) => row.stored && row.ivPct === null).map((row) => row.strike);
}

/**
 * Hours to settlement, from the minute's own ACT/365 clock.
 *
 * Read off `years_to_expiry` rather than differenced against the browser's clock: the
 * volatilities on screen were solved against that number, and a second source for the
 * same fact is a second answer waiting to disagree.
 */
export function hoursToExpiry(minute: SmileMinute): number | null {
  const years = minute.years_to_expiry;
  return years === null ? null : years * 365 * 24;
}

/** Under a day is where implied volatility stops behaving, so it is where we warn. */
export const DYING_HOURS = 24;

export function isDying(minute: SmileMinute): boolean {
  const hours = hoursToExpiry(minute);
  return hours !== null && hours < DYING_HOURS;
}

/** `4.4 hours` under two days, `21.8 days` above it. */
export function formatTimeToExpiry(hours: number): string {
  if (hours < 0) return "expired";
  if (hours < 48) return `${hours.toFixed(1)} hours`;
  return `${(hours / 24).toFixed(1)} days`;
}

/**
 * A signed offset from the forward, in USD: `+1,553`, `-1,647`, `0`.
 *
 * Whole dollars. The offsets on the top axis are round by construction and the ones in
 * the tooltip run to a few thousand, so a cent would be noise in both places. The sign
 * is always printed, including on the positive side — a column of offsets where only
 * half carry a sign reads as two different quantities.
 */
export function formatOffset(offset: number): string {
  const whole = Math.round(offset);
  if (whole === 0) return "0";
  const sign = whole > 0 ? "+" : "-";
  return `${sign}${Math.abs(whole).toLocaleString("en-US")}`;
}

/** A volatility in percent, at the precision the axis it sits on needs. */
export function formatPercent(pct: number, decimals: number): string {
  return `${pct.toFixed(decimals)}%`;
}
