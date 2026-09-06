/**
 * Everything the smile chart has to work out before it can draw: both domains, all three
 * tick sets, how many decimals the volatility axis needs, which strikes get a dotted
 * rule, and the strike lookup the hover reads.
 *
 * Pure and separate from the component for the reason the rest of `lib/` is: the client
 * has no test runner, so the arithmetic that decides where the axis starts is worth
 * being readable in one place rather than being spread through a JSX tree. The component
 * that consumes this holds only Recharts.
 */
import type { ChartOverlay } from "./overlay";
import { linearTicks, logDomain, logTicks, offsetTicks, paddedDomain, tickDecimals } from "./scale";
import { OVERLAY_COLUMN, solvedPercents, unsolvedStrikes, type SmileRow } from "./smile";

/** Roughly how many ticks each axis wants. Chosen for a 900-ish pixel plot. */
const STRIKE_TICK_TARGET = 8;
const OFFSET_TICK_TARGET = 8;
const IV_TICK_TARGET = 8;

/** A little air, so a point never sits on the frame. */
const X_PAD = 0.02;
const Y_PAD = 0.08;

export type VolScale = "linear" | "log";

export interface SmileChartModel {
  xDomain: [number, number];
  strikeTicks: number[];
  /** Strike positions the top axis labels as an offset. Empty when there is no forward. */
  offsetPositions: number[];
  yDomain: [number, number];
  ivTicks: number[];
  /** Whether the volatility axis is actually logarithmic — see the note below. */
  useLog: boolean;
  ivDecimals: number;
  unsolved: number[];
  byStrike: Map<number, SmileRow>;
}

export function smileChartModel(
  rows: readonly SmileRow[],
  forward: number | null,
  scale: VolScale,
  overlays: readonly ChartOverlay[],
): SmileChartModel {
  const strikes = rows.map((row) => row.strike);
  const solved = solvedPercents(rows);

  // The vertical domain has to hold every curve on the axis, not just the primary —
  // an overlay clipped at the frame would read as a curve that flattens out there.
  const spanning = [...solved];
  for (const overlay of overlays) {
    for (const row of rows) {
      const value = row[OVERLAY_COLUMN[overlay.id]];
      if (value !== null) spanning.push(value);
    }
  }

  const xDomain = paddedDomain(
    Math.min(...strikes),
    Math.max(...strikes),
    X_PAD,
  );
  const strikeTicks = linearTicks(xDomain[0], xDomain[1], STRIKE_TICK_TARGET);

  // Only meaningful when the minute has a forward. A minute whose parity regression
  // failed carries `forward: null`, and then there is no offset to label and no
  // reference to draw — the bottom axis stands alone rather than a zero being invented.
  const offsetPositions =
    forward === null ? [] : offsetTicks(xDomain[0], xDomain[1], forward, OFFSET_TICK_TARGET);

  // The log branch drops any non-positive volatility, which the solver does not
  // produce — but a log axis given a zero silently renders nothing at all, and a blank
  // chart is the worst failure mode on this screen.
  const positives = spanning.filter((v) => v > 0);
  const useLog = scale === "log" && positives.length > 0;

  const yDomain: [number, number] = useLog
    ? logDomain(Math.min(...positives), Math.max(...positives), Y_PAD)
    : spanning.length > 0
      ? paddedDomain(Math.min(...spanning), Math.max(...spanning), Y_PAD)
      : [0, 1];
  const ivTicks = useLog
    ? logTicks(yDomain[0], yDomain[1], IV_TICK_TARGET)
    : linearTicks(yDomain[0], yDomain[1], IV_TICK_TARGET);

  return {
    xDomain,
    strikeTicks,
    offsetPositions,
    yDomain,
    ivTicks,
    useLog,
    ivDecimals: tickDecimals(ivTicks),
    // A dotted rule is a claim about **this** minute's solver, so it is read off the
    // primary curve alone. An overlay's own refusals belong to another minute and
    // marking them here would put that minute's failures on this one's axis.
    unsolved: unsolvedStrikes(rows),
    byStrike: new Map(rows.map((row) => [row.strike, row])),
  };
}
