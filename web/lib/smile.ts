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
 */
import type { SmileMinute, SmilePoint, SmileResponse } from "./contract";

/** One strike, ready to draw. Percent, because that is the unit the axis is in. */
export interface SmileDatum {
  strike: number;
  /**
   * Implied volatility in **percent** — 31.89 for the contract's 0.3189. `null` when the
   * strike did not solve, which is what breaks the line.
   */
  ivPct: number | null;
  /** `strike - forward`, in USD. `null` when the minute has no fitted forward. */
  offset: number | null;
  /** `"call"` or `"put"` — the out-of-the-money side the number came from. */
  leg: string | null;
  /** The solver's account of why there is no number. `null` when there is one. */
  reason: string | null;
}

/**
 * The last minute in the response, which is the newest — `minutes` ascends.
 *
 * The screen shows one minute and it is the latest available. Moving through the day is
 * #20's job, and it is an index into this same array rather than another request.
 */
export function latestMinute(response: SmileResponse): SmileMinute | null {
  return response.minutes.length > 0 ? (response.minutes.at(-1) ?? null) : null;
}

export function toRows(minute: SmileMinute): SmileDatum[] {
  const forward = minute.forward;
  return minute.points.map((point: SmilePoint) => ({
    strike: point.strike,
    // The one multiplication by 100 on this screen, and it is presentation — the same
    // rule `lib/format.ts` states for the ladder.
    ivPct: point.iv === null ? null : point.iv * 100,
    offset: forward === null ? null : point.strike - forward,
    leg: point.iv_leg,
    reason: point.iv_reason,
  }));
}

/** The solved volatilities, in percent. Empty when nothing on the curve solved. */
export function solvedPercents(rows: readonly SmileDatum[]): number[] {
  const out: number[] = [];
  for (const row of rows) if (row.ivPct !== null) out.push(row.ivPct);
  return out;
}

/** The strikes that arrived with no volatility. The line breaks at each of them. */
export function unsolvedStrikes(rows: readonly SmileDatum[]): number[] {
  return rows.filter((row) => row.ivPct === null).map((row) => row.strike);
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
