/**
 * Types for `POST /analyse`, the payoff analysis.
 *
 * These mirror `docs/payoff-contract.md` field for field. That file is the authority;
 * if this file and it disagree, this file is wrong.
 *
 * It sits beside `contract.ts` rather than inside it for the reason `/smile`, `/bars`
 * and the two historical routes each got a document of their own rather than an
 * appendix to `/chain`'s: a second contract is a second subject.
 *
 * Three rules from the contract are load-bearing for every type here:
 *
 *   - Every decimal is a JSON `number` or `null`, never a string. Nothing in the web
 *     app parses a numeric string, so no field is typed `string | number`, and
 *     `assertPayoff` below refuses a payload that breaks it. **The rule binds the
 *     response**: the engine parses a request leniently, so `AnalyseRequest` is the one
 *     shape here that nothing guards.
 *   - **Unbounded is `null`** — never an infinity, never a large sentinel, never the
 *     string `"Unlimited"`. The screen renders the word; the wire carries the absence.
 *   - Every number is **per one unit of the underlying**. `contract_value` is echoed so
 *     the screen can multiply money and Greeks together at the very end. The engine
 *     never multiplies.
 */
import { ContractViolationError } from "./engine-errors";

/** Bought (`1`) or sold (`-1`). Never one signed quantity — the sign and the size are
 * separate fields, so nothing can render a negative quantity. */
export type Direction = 1 | -1;

/**
 * One leg as a client is allowed to describe it — the body of `POST /analyse`.
 *
 * `instrument` is the canonical string, built by `canonicalInstrument` in
 * `lib/instrument.ts` and by nothing else. The engine's
 * `events.instrument.Instrument.from_canonical` is the single validator and answers 400
 * naming the part that was wrong, so nothing here re-checks the shape.
 *
 * `entry_price` is optional and is USD per one unit of the underlying, on the same
 * scale as the ladder's `bid` and `ask`. Left off, the engine crosses the spread — buy
 * at the ask, sell at the bid. A leg with nothing quoted on its side and no price
 * supplied refuses the whole analysis, and the trader types one.
 */
export interface LegRequest {
  instrument: string;
  direction: Direction;
  /** A positive integer. At least one; the sign lives in `direction`. */
  quantity: number;
  entry_price?: number | null;
}

/**
 * The request. `legs` is ordered — the order the trader built them in, which is the
 * order the per-leg Greeks table is read in — and holds at least one leg.
 *
 * **No `as_of` means live.** Present, it is a stored minute, ISO 8601 UTC with second
 * precision and a `Z` suffix, the spelling `/chain/minutes` publishes. A link naming a
 * minute is a fixed set of numbers and never re-asks.
 */
export interface AnalyseRequest {
  legs: LegRequest[];
  as_of?: string | null;
}

/**
 * One leg's exposures, or a whole strategy's — the shape is the same either way.
 *
 * **Per one unit of the underlying**, with no `contract_value` in them. The conventions
 * are `docs/chain-contract.md`'s and are not all textbook: `delta` and `gamma`
 * undiscounted and with respect to the **forward**; `vega` and `rho` discounted and per
 * one percent; `theta` a one-calendar-day repricing on ACT/365, because crypto trades
 * weekends and a trading-day year overstates it by 1.456x here.
 */
export interface Greeks {
  delta: number;
  gamma: number;
  vega: number;
  theta: number;
  rho: number;
}

/**
 * One requested leg, echoed with the price it was actually entered at.
 *
 * `entry_price` is never `null` here: a leg with no quote on its side and none supplied
 * refuses the whole analysis rather than arriving priceless.
 *
 * `greeks` is `null` **exactly when `iv` is**. A leg with no volatility carries no
 * Greeks; five plausible numbers at some default sigma would describe nothing. The
 * Greeks are signed by `direction` and scaled by `quantity`; `entry_price` is not — it
 * is what one unit cost, so it stays comparable with the ladder's `bid` and `ask`.
 */
export interface AnalysedLeg {
  instrument: string;
  direction: Direction;
  quantity: number;
  entry_price: number;
  /** A decimal fraction — `0.3712` is 37.12%. The screen formats; the engine never
   * multiplies by 100. A property of the strike, not the leg. */
  iv: number | null;
  greeks: Greeks | null;
}

/** One `(price, pnl)` pair. `price` is the underlying's price at expiry, USD per one
 * unit; `pnl` is the strategy's profit or loss there. The curve's corners and the
 * payoff table are the same type because they are one quantity sampled twice. */
export interface PayoffPoint {
  price: number;
  pnl: number;
}

/** The range the engine suggests opening on: ±3 standard deviations from the
 * at-the-money volatility, widened to include every strike carrying a leg. A
 * **suggestion, not a clamp** — the corners and the two slopes describe the curve
 * everywhere, so zooming past it still reads exact values. `low < high`. */
export interface Window {
  low: number;
  high: number;
}

/**
 * The chart's line: P&L at expiry, as **corner points** rather than samples.
 *
 * A single-expiry payoff is piecewise linear, so the kinks plus the two end slopes are
 * exact, smaller on the wire and zoomable without limit. Drawing a straight segment
 * between two given points is rendering, not arithmetic, so the app's rule that it
 * computes nothing holds.
 *
 * `corners` is strictly ascending by `price`, and is a list of objects rather than two
 * parallel arrays: arrays that disagree in length do not raise on a chart, they draw
 * slightly wrong and nobody notices.
 *
 * `slope_left` and `slope_right` are the P&L per unit of underlying outside the first
 * and last corner. Zero on both for a capped structure.
 */
export interface Curve {
  corners: PayoffPoint[];
  slope_left: number;
  slope_right: number;
  window: Window;
}

/** The four numbers under the chart, and the ratio between two of them. */
export interface Metrics {
  /** `null` when unbounded — never an infinity and never a large sentinel. */
  max_profit: number | null;
  /** The worst outcome, **as a P&L** on the same axis as `PayoffPoint.pnl`, so it is
   * read straight off the chart. Negative on almost everything, and zero or positive on
   * a structure that cannot lose. `null` when unbounded. */
  max_loss: number | null;
  /** Every price at which P&L crosses zero, ascending. Empty when it never does. */
  breakevens: number[];
  /** Positive is **paid out** (a debit), negative is **received** (a credit). */
  net_premium: number;
  /** `max_profit` over the magnitude of `max_loss`. `null` when either side is
   * unbounded or when there is no loss to divide by: a ratio against unlimited has no
   * meaning, and a large number in its place would read as a good trade. */
  reward_risk: number | null;
}

/**
 * Everything about one strategy, in one response. Deliberately fat: splitting it would
 * mean several round trips carrying the same legs and recomputing the same curve, and
 * the trader would watch the numbers arrive after the chart they belong to.
 */
export interface AnalyseResponse {
  underlying: string;
  /** `DD-MM-YYYY`, `/chain`'s own spelling. One expiry per strategy. */
  expiry: string;
  /** ISO 8601 UTC, second precision, `Z`-suffixed. Always populated — a client that
   * named no minute is still told which one it got. */
  as_of: string;
  /** Delta's top-level `spot_price`. `greeks.spot` is never exposed. */
  spot: number | null;
  /** What the Greeks were priced against, and the discount fitted alongside it. `null`
   * together exactly when the chain could not be fitted, and then no leg carries an
   * `iv` or any Greeks either. The curve and the metrics survive that. */
  forward: number | null;
  discount: number | null;
  /** The lot-size multiplier, `0.001` on this venue. Echoed so the screen can multiply;
   * the engine never applies it. */
  contract_value: number;
  /** One row per requested leg, in the order they were sent. */
  legs: AnalysedLeg[];
  /** The strategy's exposure. Published **only when every leg carries Greeks** — a sum
   * over the legs that happened to solve describes a different position. */
  total_greeks: Greeks | null;
  curve: Curve;
  metrics: Metrics;
  /** The same quantity as `curve.corners`, on the readable grid a trader takes exact
   * figures off rather than inferring them from the picture. Strictly ascending. */
  table: PayoffPoint[];
}

/**
 * Every key on this contract whose value is a decimal or `null`, and never a string.
 *
 * Keyed by name rather than walked by path because the same names recur at four depths
 * — `price` and `pnl` on a corner and on a table row, the five Greeks per leg and once
 * for the total — and a path-shaped guard would have to be rewritten every time the
 * response grows a section.
 */
const NUMERIC_FIELDS: ReadonlySet<string> = new Set([
  "spot",
  "forward",
  "discount",
  "contract_value",
  "entry_price",
  "iv",
  "delta",
  "gamma",
  "vega",
  "theta",
  "rho",
  "price",
  "pnl",
  "slope_left",
  "slope_right",
  "low",
  "high",
  "max_profit",
  "max_loss",
  "net_premium",
  "reward_risk",
  "breakevens",
]);

function offences(value: unknown, path: string, found: string[]): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => offences(item, `${path}[${index}]`, found));
    return;
  }
  if (typeof value !== "object" || value === null) return;
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const here = path ? `${path}.${key}` : key;
    if (NUMERIC_FIELDS.has(key) && typeof item === "string") {
      found.push(`${here}=${JSON.stringify(item)}`);
    } else if (NUMERIC_FIELDS.has(key) && Array.isArray(item)) {
      // `breakevens` is a list of decimals rather than one, and a string inside it is
      // the same breach: recursion below would step past it, since a bare string is
      // neither an object nor an array.
      item.forEach((entry, index) => {
        if (typeof entry === "string") found.push(`${here}[${index}]=${JSON.stringify(entry)}`);
      });
    } else {
      offences(item, here, found);
    }
  }
}

/**
 * The contract guard: every decimal arrived as a number, or the engine is in breach.
 *
 * Delta sends decimals as strings and the engine converts once, at the boundary. If one
 * gets through, nothing downstream would fail — `"1240.0"` sorts and scales as text, so
 * the line draws in the wrong place and the metrics under it read as plausible figures.
 * `engine.ts`'s `assertNumeric` does the same job for `/chain` and for the same
 * reason; this one walks rather than lists, because the payoff response nests.
 *
 * `null` is never a breach. Unbounded is `null` and an absent observation is `null`,
 * and neither is a string.
 */
export function assertPayoff(response: AnalyseResponse): void {
  const found: string[] = [];
  offences(response, "", found);
  if (found.length > 0) {
    throw new ContractViolationError(
      `Engine sent decimals as strings, which docs/payoff-contract.md forbids. ` +
        `The web app will not parse them. Offending fields: ${found.slice(0, 6).join(", ")}` +
        (found.length > 6 ? ` (+${found.length - 6} more)` : ""),
    );
  }
}

/**
 * One entry of FastAPI's request-validation envelope.
 *
 * Mirrored because the shape is part of the contract, not an implementation detail of
 * the framework: `docs/payoff-contract.md` §Refusals fixes it as the envelope the
 * **schema** refusal arrives in, and names the fields a reader has to be shown.
 */
export interface ValidationEntry {
  type: string;
  /** `["body", "legs"]` — the path to the field that was wrong, outermost first. */
  loc: (string | number)[];
  msg: string;
}

/**
 * A refusal's `detail`, whichever of the **two envelopes** it arrived in, as one
 * sentence — or `null` when the body carries no `detail` at all.
 *
 * `docs/payoff-contract.md` §Refusals: seven of the eight refusals are **semantic** —
 * the request is well formed and the engine will not answer it — and travel as
 * FastAPI's default `{"detail": "..."}`, a string. The eighth is a **schema** breach,
 * caught by `AnalyseRequest` itself before the route is entered, so it arrives as
 * `{"detail": [{...}]}`, a **list**. That split lives on the type on purpose — the pure
 * core builds these models directly, so the type is what stops a core-side bug
 * producing an empty strategy — and the consequence is that a client reading `detail`
 * must expect either shape.
 *
 * `lib/contract.ts`'s `isEngineError` handles only the string, which is right for
 * `/chain`: none of its refusals are schema breaches. Widening it would loosen the
 * guard on every route to serve one, so the second shape is read here, beside the
 * contract that produces it.
 *
 * A list is rendered as `body.legs: List should have at least 1 item` rather than as
 * the raw object — a screen that prints `[object Object]` at a trader has told them
 * nothing, which is the whole failure this function exists to prevent.
 */
export function refusalDetail(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail) || detail.length === 0) return null;
  return detail
    .map((entry) => {
      const { loc, msg } = entry as Partial<ValidationEntry>;
      const where = Array.isArray(loc) ? loc.join(".") : "";
      const what = typeof msg === "string" ? msg : JSON.stringify(entry);
      return where ? `${where}: ${what}` : what;
    })
    .join("; ");
}
