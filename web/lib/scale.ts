/**
 * Axis arithmetic: domains and tick positions, as pure functions of the data.
 *
 * Separate from `smile.ts` because it is a different subject — this file knows nothing
 * about volatility, strikes or forwards, only about turning a range of numbers into a
 * domain and a set of round tick values. It is also the part of the chart that is worth
 * reasoning about away from a browser, and one day testing without one.
 *
 * **Recharts' own tick choice is not used anywhere.** Its defaults are computed from the
 * pixel width and produce values like 77,433 on a strike axis. Every axis on the smile
 * passes an explicit `ticks` array built here, which is also what makes the claim that
 * the two x-axes are evenly spaced something the code enforces rather than something the
 * library happened to do.
 */

/** 1, 2, 2.5, 5, 10 — the steps a reader can divide in their head. */
const STEPS = [1, 2, 2.5, 5, 10] as const;

/**
 * The smallest of those steps at or above `rough`, scaled to `rough`'s magnitude.
 * 380 becomes 500; 0.7 becomes 1; 1,900 becomes 2,000.
 */
export function niceStep(rough: number): number {
  if (!(rough > 0) || !Number.isFinite(rough)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalised = rough / magnitude;
  for (const step of STEPS) {
    if (normalised <= step) return step * magnitude;
  }
  return 10 * magnitude;
}

/**
 * Snap `value` to the precision `step` implies.
 *
 * Without this, accumulating a 0.2 step from 28.4 prints 30.599999999999998 on an axis.
 * The tick is a label, and a label with sixteen digits of float residue is a bug the
 * reader sees.
 */
function snap(value: number, step: number): number {
  const places = Math.min(12, Math.max(0, -Math.floor(Math.log10(step)) + 1));
  return Number(value.toFixed(places));
}

/** Round ticks inside `[lo, hi]`, about `target` of them. Never fewer than the ends. */
export function linearTicks(lo: number, hi: number, target: number): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return [snap(lo, 1)];
  const step = niceStep((hi - lo) / Math.max(1, target));
  const ticks: number[] = [];
  // A hair of tolerance at both ends: a tick that lands exactly on the domain edge is
  // lost to float error about half the time otherwise.
  const epsilon = step * 1e-9;
  for (let v = Math.ceil(lo / step) * step; v <= hi + epsilon; v += step) {
    ticks.push(snap(v, step));
  }
  return ticks;
}

/**
 * Ticks at round multiples of a step **measured from `origin`**, returned in the original
 * coordinate.
 *
 * This is the top axis. Offset from the forward is a pure shift of the strike, so both
 * axes can share one linear scale: these positions are strikes, and only the label
 * subtracts the forward. `origin` itself is always a tick, which puts a `0` exactly on
 * the reference line.
 */
export function offsetTicks(lo: number, hi: number, origin: number, target: number): number[] {
  if (!Number.isFinite(origin) || hi <= lo) return [];
  const step = niceStep((hi - lo) / Math.max(1, target));
  const ticks: number[] = [];
  const first = Math.ceil((lo - origin) / step);
  const last = Math.floor((hi - origin) / step);
  for (let k = first; k <= last; k++) ticks.push(origin + k * step);
  return ticks;
}

/** `[lo, hi]` widened by `fraction` of its own width at each end. */
export function paddedDomain(lo: number, hi: number, fraction: number): [number, number] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1];
  if (hi === lo) {
    // A single point still needs a domain with width, or the scale divides by zero.
    const pad = Math.abs(lo) * fraction || 1;
    return [lo - pad, hi + pad];
  }
  const pad = (hi - lo) * fraction;
  return [lo - pad, hi + pad];
}

/**
 * A log domain and its ticks.
 *
 * `1, 1.5, 2, 3, 4, 5, 6, 8` per decade rather than the decades alone: a dying contract
 * runs from about 30% to 400%, which is 1.1 decades, and decade ticks would label that
 * range three times. Thinned to every other tick when the range is wide enough to
 * produce more than ten, so a two-decade axis does not turn into a comb.
 *
 * The padding is multiplicative, because on a log axis that is what "a little air at
 * each end" means.
 */
const LOG_MULTIPLIERS = [1, 1.5, 2, 3, 4, 5, 6, 8] as const;

/**
 * `fallbackTarget` is not decoration. On an ordinary expiry the volatilities run from
 * about 29% to 35%, which is a fifth of a decade, and the decade multipliers put exactly
 * **one** tick in it — measured on the fixture, the log axis came back labelled `30%` and
 * nothing else. A log axis carries any tick values you give it, so below four decade
 * ticks this hands back the linear ones instead. The axis is still logarithmic; only the
 * labels stop being powers of ten.
 */
export function logTicks(lo: number, hi: number, fallbackTarget: number): number[] {
  if (!(lo > 0) || !(hi > lo)) return [];
  const ticks: number[] = [];
  for (let decade = Math.floor(Math.log10(lo)); decade <= Math.ceil(Math.log10(hi)); decade++) {
    const magnitude = 10 ** decade;
    for (const multiplier of LOG_MULTIPLIERS) {
      const value = Number((multiplier * magnitude).toPrecision(12));
      if (value >= lo && value <= hi) ticks.push(value);
    }
  }
  if (ticks.length < 4) return linearTicks(lo, hi, fallbackTarget);
  return ticks.length > 10 ? ticks.filter((_, i) => i % 2 === 0) : ticks;
}

export function logDomain(lo: number, hi: number, fraction: number): [number, number] {
  if (!(lo > 0) || !(hi > 0)) return [1, 10];
  const factor = 1 + fraction;
  return [lo / factor, hi * factor];
}

/**
 * How many decimal places a tick list needs to print without lying.
 *
 * Capped at two: these label a volatility axis, and a third decimal of a percentage is
 * below anything the solver claims.
 */
export function tickDecimals(ticks: readonly number[]): number {
  let places = 0;
  for (const tick of ticks) {
    if (!Number.isFinite(tick)) continue;
    const text = String(tick);
    const dot = text.indexOf(".");
    if (dot >= 0) places = Math.max(places, text.length - dot - 1);
  }
  return Math.min(2, places);
}
