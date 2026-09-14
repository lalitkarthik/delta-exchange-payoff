/**
 * The structure chain: every straddle and strangle the board can build, in one grid.
 *
 * Rows are strikes. Columns are the **wing width** `D`, in price points. The cell at
 * `(K, D)` is the structure made of the call at `K + D` and the put at `K − D`, so the
 * `D = 0` column is the straddle and every column right of it is a strangle widening
 * symmetrically. One feature at a time fills the grid: net premium, volatility, or one
 * of the four Greeks.
 *
 * **Pure, and that is where the whole feature lives.** The client has no test runner for
 * components, so anything computed inside one ships unverified — the rule `lib/smile.ts`
 * and `lib/ivrv.ts` already follow, and this file keeps it. Everything below can be
 * asserted from `bun tests/structures.test.ts` against a literal chain.
 *
 * ## The wings snap to listed strikes, or the cell is empty
 *
 * `K + D` is arithmetic; a tradeable option is not. Delta's ladder is not uniformly
 * spaced — it thickens around spot and thins in the tails — so each wing is resolved to
 * the **nearest listed strike**, ties to the lower, which is the convention
 * `chain.nearest_strike` uses on the engine side and the one the ATM lookup already
 * follows. A wing that lands further than half a column-step from where it was asked for
 * is **rejected**: silently showing a 200-wide strangle under a `50` heading would put a
 * correct number in a column that lies about what it is. A rejected wing, an unlisted
 * side, or a leg missing the number the feature wants all produce an empty cell, and
 * `unlisted` says which of those it was so the screen can hatch the first and leave the
 * rest blank — the ladder's own distinction between "not there" and "there but unquoted".
 *
 * ## The price is a midpoint, and a long structure is a debit
 *
 * ```
 * price(K, D) = direction × −( mid(call at K+D) + mid(put at K−D) )
 * mid(leg)    = (bid + ask) / 2,  null unless both sides are quoted
 * ```
 *
 * Negative for a long structure because buying it is cash **out**, which is the sign the
 * reference terminal uses and the only one under which the LONG/SHORT toggle reads as a
 * flip rather than a relabelling. A midpoint is **not an executable price** — nobody
 * fills there — and the screen says so; it is used because it is the same number the
 * volatility was solved from, so the price column and the IV column describe one board
 * rather than two.
 *
 * ## The volatility and the Greeks are ours
 *
 * `leg.computed`, never `leg.delta`/`leg.gamma`/`leg.vega`/`leg.theta`. Those are Delta's
 * own, fitted to prices that are five seconds stale and quoted on a spot convention;
 * they are a reference column and never an input, and
 * `engine/tests/test_no_delta_inputs.py` pins it. The two are not blended, not even as a
 * fallback on a strike ours could not solve — one cell mixing two conventions is a cell
 * no heading could describe.
 *
 * A Greek is the plain **sum** of the two legs, times `direction`. Both are per one unit
 * of the underlying, and nothing here scales by a lot size — the rule the whole stack
 * states: a lot is carried so a screen can multiply, not so a solver can. A screen that
 * wants per-contract figures multiplies at the point of drawing.
 *
 * Volatility is not a sum — two vols do not add — so it is the **vega-weighted mean** of
 * the two strikes:
 *
 * ```
 * iv(K, D) = (ivC·νC + ivP·νP) / (νC + νP)
 * ```
 *
 * which is the weighting under which the structure's price is first-order insensitive to
 * how the vol is split between the wings. It falls back to the plain mean when both vegas
 * are present and sum to zero (a structure at neither wing of which anything moves), and
 * is `null` when either strike did not solve. A decimal fraction here as everywhere; the
 * percent sign is `formatIv`'s business.
 *
 * ## `null` is not `0`
 *
 * Around 40% of listed strikes are illiquid enough that no volatility solves, and a leg
 * with no volatility carries no Greeks at all. Such a cell is empty, which is not the
 * same as zero even where zero would look plausible. `ivCoverage` counts how much of the
 * drawn grid is whole, and the screen prints it, because nothing else on screen would say
 * that half the board is missing.
 */
import type { ChainResponse, ComputedLeg, Leg } from "./contract";

/** What the grid is filled with. One at a time — six numbers per cell would be a table
 *  nobody reads, and the terminal this follows toggles for the same reason. */
export const FEATURES = ["price", "iv", "delta", "gamma", "vega", "theta"] as const;
export type Feature = (typeof FEATURES)[number];

/** Long is `1` and buys the structure; short is `-1` and sells it. Every cell flips. */
export type Direction = 1 | -1;

/** One structure: which two listed strikes it was actually built from, and the number the
 *  chosen feature asks of them. */
export interface Cell {
  /** The listed strike the call wing resolved to, or `null` when none was close enough. */
  readonly callStrike: number | null;
  readonly putStrike: number | null;
  /** `null` when either leg cannot supply this feature. Never `0` standing in for absence. */
  readonly value: number | null;
  /** True when a wing could not be resolved at all — the screen hatches these, and leaves
   *  a cell that merely lacks a quote blank. */
  readonly unlisted: boolean;
}

export interface StructureRow {
  readonly strike: number;
  readonly cells: Cell[];
}

export interface StructureGrid {
  /** Column headings, in points. Ascending. `0` is the straddle. */
  readonly offsets: number[];
  readonly rows: StructureRow[];
  /** Echoed from the chain so the table can mark its row without re-deriving it. */
  readonly atmStrike: number | null;
}

export interface GridRequest {
  /** The first column's wing width, in points. `0` for the straddle. */
  readonly offsetMin: number;
  /** The gap between columns, in points. Also the snapping tolerance: half of it. */
  readonly step: number;
  readonly columns: number;
  /** How many rows above and below the ATM strike. The grid is at most `2n + 1` rows. */
  readonly rowsEitherSide: number;
  readonly direction: Direction;
  readonly feature: Feature;
}

/** Floats off the wire: strikes are round numbers but arrive as doubles, and a wing width
 *  is a sum of them. Anything inside this is the same strike. */
const STRIKE_TOLERANCE = 1e-6;

/**
 * The grid, or `null` for the whole board when there is nothing to anchor it to.
 *
 * **Refused rather than approximated when `atm_strike` is `null` or the board is empty.**
 * Every row is positioned relative to the money; with no ATM strike there is no window to
 * draw and no honest way to pick one, so nothing is drawn rather than a window picked at
 * random — the project's standing rule that a missing input refuses rather than defaults.
 */
export function structureGrid(chain: ChainResponse, req: GridRequest): StructureGrid | null {
  if (chain.rows.length === 0) return null;
  if (chain.atm_strike === null) return null;

  const offsets = columnOffsets(req);
  const strikes = chain.rows.map((row) => row.strike);
  const rows = windowAround(chain, chain.atm_strike, req.rowsEitherSide);
  const legAt = new Map<number, { call: Leg | null; put: Leg | null }>();
  for (const row of chain.rows) legAt.set(row.strike, { call: row.call, put: row.put });

  return {
    offsets,
    atmStrike: chain.atm_strike,
    rows: rows.map((strike) => ({
      strike,
      cells: offsets.map((offset) => cell(strike, offset, strikes, legAt, req)),
    })),
  };
}

/** `offsetMin`, `offsetMin + step`, … — the headings, computed once so the columns and the
 *  snapping tolerance cannot disagree about what a column means. */
function columnOffsets(req: GridRequest): number[] {
  const count = Math.max(0, Math.floor(req.columns));
  return Array.from({ length: count }, (_, i) => req.offsetMin + i * req.step);
}

/**
 * The listed strikes to draw rows for: `rowsEitherSide` above and below the ATM strike,
 * and the ATM strike itself.
 *
 * Counted in **listed strikes, not points**, so the window holds the same number of
 * tradeable rows wherever the ladder thickens or thins. A board with fewer strikes than
 * asked for gives what it has rather than padding.
 */
function windowAround(chain: ChainResponse, atm: number, eitherSide: number): number[] {
  const strikes = chain.rows.map((row) => row.strike);
  const n = Math.max(0, Math.floor(eitherSide));
  let centre = strikes.findIndex((strike) => Math.abs(strike - atm) < STRIKE_TOLERANCE);
  if (centre < 0) centre = nearestIndex(strikes, atm);
  if (centre < 0) return [];
  return strikes.slice(Math.max(0, centre - n), centre + n + 1);
}

/** One structure, resolved and valued. */
function cell(
  strike: number,
  offset: number,
  strikes: number[],
  legAt: Map<number, { call: Leg | null; put: Leg | null }>,
  req: GridRequest,
): Cell {
  // `D = 0` needs no special branch: both wings ask for `strike` and snap onto it.
  const callStrike = snap(strikes, strike + offset, req.step);
  const putStrike = snap(strikes, strike - offset, req.step);
  if (callStrike === null || putStrike === null) {
    return { callStrike, putStrike, value: null, unlisted: true };
  }
  const call = legAt.get(callStrike)?.call ?? null;
  const put = legAt.get(putStrike)?.put ?? null;
  if (call === null || put === null) {
    return { callStrike, putStrike, value: null, unlisted: true };
  }
  return { callStrike, putStrike, value: value(call, put, req), unlisted: false };
}

/**
 * The nearest listed strike to `wanted`, or `null` when the nearest is further than half
 * a step away.
 *
 * **Ties go to the lower strike**, matching `chain.nearest_strike` on the engine side, so
 * the two halves of the stack resolve the same request to the same contract. The
 * tolerance is half a step because that is the width a column owns: beyond it the wing
 * belongs under a different heading, and drawing it under this one would misname it.
 * A zero or negative step disables the check rather than rejecting everything — a single
 * column has no neighbour to be confused with.
 */
function snap(strikes: number[], wanted: number, step: number): number | null {
  const i = nearestIndex(strikes, wanted);
  if (i < 0) return null;
  const found = strikes[i]!;
  if (step > 0 && Math.abs(found - wanted) > step / 2 + STRIKE_TOLERANCE) return null;
  return found;
}

/** Index of the nearest entry, ties to the lower. `-1` on an empty list. */
function nearestIndex(strikes: number[], wanted: number): number {
  let best = -1;
  let bestGap = Infinity;
  for (let i = 0; i < strikes.length; i += 1) {
    const gap = Math.abs(strikes[i]! - wanted);
    // Strict `<` with an ascending list keeps the first — the lower — of an exact tie.
    if (gap < bestGap - STRIKE_TOLERANCE) {
      best = i;
      bestGap = gap;
    }
  }
  return best;
}

/** What the chosen feature makes of one call and one put. */
function value(call: Leg, put: Leg, req: GridRequest): number | null {
  if (req.feature === "price") {
    const premium = sum(mid(call), mid(put));
    return premium === null ? null : -req.direction * premium;
  }
  if (req.feature === "iv") {
    const blended = blendedIv(call.computed, put.computed);
    // Volatility is a property of the structure, not of the side you took it from: a
    // short straddle is short the same vol the long one is long. No direction here.
    return blended;
  }
  const greek = sum(call.computed?.[req.feature] ?? null, put.computed?.[req.feature] ?? null);
  return greek === null ? null : req.direction * greek;
}

/** The bid/ask midpoint, or `null` unless both sides are quoted. A one-sided market has
 *  no midpoint, and taking the quoted side as one would report a price nobody named. */
function mid(leg: Leg): number | null {
  if (leg.bid === null || leg.ask === null) return null;
  return (leg.bid + leg.ask) / 2;
}

/** `a + b`, or `null` if either is absent. Absence propagates: half a structure is not a
 *  structure, and reporting one leg's Greek as the pair's would understate it silently. */
function sum(a: number | null, b: number | null): number | null {
  if (a === null || b === null) return null;
  return a + b;
}

/** The vega-weighted mean of the two strikes' volatility — see this module's header. */
function blendedIv(call: ComputedLeg | null, put: ComputedLeg | null): number | null {
  const ivC = call?.iv ?? null;
  const ivP = put?.iv ?? null;
  if (ivC === null || ivP === null) return null;
  const vC = call?.vega ?? null;
  const vP = put?.vega ?? null;
  if (vC === null || vP === null) return (ivC + ivP) / 2;
  const weight = vC + vP;
  if (weight === 0) return (ivC + ivP) / 2;
  return (ivC * vC + ivP * vP) / weight;
}

/**
 * How much of the drawn grid carries a number: cells with a value, out of cells that
 * resolved to a pair of listed legs.
 *
 * The denominator deliberately excludes cells with no wing to price — those are a fact
 * about the ladder's spacing, not about liquidity, and counting them would blame the
 * board for a column the reader chose. The screen prints this because a grid half full
 * of blanks looks the same as a thin one.
 */
export function ivCoverage(grid: StructureGrid): { solved: number; total: number } {
  let solved = 0;
  let total = 0;
  for (const row of grid.rows) {
    for (const cell of row.cells) {
      if (cell.unlisted) continue;
      total += 1;
      if (cell.value !== null) solved += 1;
    }
  }
  return { solved, total };
}

/**
 * The ladder's own column spacing: the most common gap between neighbouring listed
 * strikes.
 *
 * The **mode**, not the mean or the minimum. Delta's ladder thickens around spot and
 * thins into the tails, so a mean sits between two spacings and belongs to neither, and
 * the minimum is whatever the densest patch happens to be. The mode is the step most of
 * the board is actually on, which is the one under which most cells snap cleanly.
 * Ties go to the **smaller** gap — a step too fine leaves empty columns, which shows the
 * reader the spacing; a step too coarse silently swallows strikes between headings.
 *
 * `null` on a board with fewer than two strikes, where there is no gap to be the mode of.
 * The screen seeds its STEP control from this and then leaves it alone: after the first
 * chain of a series the number is the reader's, not ours.
 */
export function defaultStep(chain: ChainResponse): number | null {
  const strikes = chain.rows.map((row) => row.strike);
  if (strikes.length < 2) return null;
  const counts = new Map<number, number>();
  for (let i = 1; i < strikes.length; i += 1) {
    const gap = Math.round(strikes[i]! - strikes[i - 1]!);
    if (gap <= 0) continue;
    counts.set(gap, (counts.get(gap) ?? 0) + 1);
  }
  let best: number | null = null;
  let bestCount = 0;
  for (const [gap, count] of counts) {
    if (count > bestCount || (count === bestCount && best !== null && gap < best)) {
      best = gap;
      bestCount = count;
    }
  }
  return best;
}

/** The largest absolute value drawn, for a tint that has to hold both signs against one
 *  scale. `0` on a grid with nothing in it, rather than `-Infinity`. */
export function peakValue(grid: StructureGrid): number {
  let most = 0;
  for (const row of grid.rows) {
    for (const cell of row.cells) {
      if (cell.value !== null) most = Math.max(most, Math.abs(cell.value));
    }
  }
  return most;
}
