/**
 * Types for the engine's HTTP interface.
 *
 * These mirror `docs/chain-contract.md` and `docs/smile-contract.md` field for field.
 * Those files are the authority; if this file and a contract disagree, this file is wrong.
 *
 * Two rules from the contract are load-bearing for every type here:
 *
 *   - Every decimal is a JSON `number` or `null`, never a string. Nothing in the web
 *     app parses a numeric string, so no field is typed `string | number`.
 *   - `null` is absence, not zero. Optional numbers are `number | null`, never
 *     `number | undefined`, so a missing quote cannot be silently coerced to 0.
 */

/** The only two underlyings in scope. XAUT and single-names are deliberately excluded. */
export type Underlying = "BTC" | "ETH";

export const UNDERLYINGS: readonly Underlying[] = ["BTC", "ETH"] as const;

/**
 * An expiry as the engine and Delta both spell it: `DD-MM-YYYY`.
 * Never reformatted anywhere in the stack — it is passed straight back as a query param.
 */
export type ExpiryDate = string;

/** `GET /expiries?underlying=BTC` */
export interface ExpiriesResponse {
  underlying: Underlying;
  /** Ascending by date. */
  expiries: ExpiryDate[];
}

/**
 * What the engine computed for one leg, as opposed to what Delta published.
 *
 * Kept as its own object rather than as prefixed fields so the boundary between the
 * venue's numbers and ours is visible in the payload. Nothing here replaces anything on
 * `Leg`; every field there is still Delta's own figure.
 *
 * `iv` is a property of the **strike**, not of the leg. Put-call parity gives both sides
 * one volatility and the engine recovers it from whichever side is out of the money, so
 * the same number appears on both legs of a row. `iv_leg` names the side it came from,
 * so that repetition cannot be misread as two independent solves.
 *
 * **The Greek conventions are not all textbook** and are documented in the engine's
 * `greeks.py`: `delta` and `gamma` are undiscounted, `vega` and `rho` are discounted and
 * quoted per one percent, and `theta` is a one-calendar-day repricing rather than the
 * analytic derivative. They are carried unchanged from the sibling project's verified
 * implementation rather than converted, because a convention the desk does not use is
 * one that has to be undone at every boundary.
 */
export interface ComputedLeg {
  /** Decimal fraction, as everywhere else. `null` when the strike could not be solved. */
  iv: number | null;
  /** `"call"` or `"put"` — the out-of-the-money side this strike's `iv` came from. */
  iv_leg: string | null;
  /** Empty when solved; otherwise the solver's own account of why it stopped. */
  iv_reason: string;
  /** With respect to the **forward**, not to spot. Delta's `delta` is a spot delta. */
  delta: number | null;
  gamma: number | null;
  vega: number | null;
  theta: number | null;
  rho: number | null;
}

/**
 * One side of one strike — a call or a put.
 *
 * `symbol` and `product_id` always come through. Every quote, vol and Greek may be
 * `null`: roughly 40% of listed strikes are illiquid and carry no bid at all.
 */
export interface Leg {
  symbol: string;
  product_id: number;
  bid: number | null;
  ask: number | null;
  mark: number | null;
  /** Decimal fraction. 0.3701 is 37.01%. The engine never multiplies by 100. */
  bid_iv: number | null;
  /** Decimal fraction. */
  ask_iv: number | null;
  /** Decimal fraction. */
  mark_iv: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  rho: number | null;
  /** Open interest in **contracts**, on both transports. */
  oi: number | null;
  /** The USD notional. REST carries it; the websocket does not, so it is null live. */
  oi_value_usd: number | null;
  /** How the notional moved over six hours. May be negative. Both transports. */
  oi_change_usd_6h: number | null;
  tick_size: number | null;
  /** Ours. `null` on a chain that has not been through the engine's enrichment. */
  computed: ComputedLeg | null;
}

/**
 * One rung of the ladder. Either side may be `null` when only one of the pair is
 * listed — the row still exists and still shows its strike.
 */
export interface ChainRow {
  strike: number;
  call: Leg | null;
  put: Leg | null;
}

/** `GET /chain?underlying=BTC&expiry=04-09-2026` */
export interface ChainResponse {
  underlying: Underlying;
  expiry: ExpiryDate;
  /** Delta's top-level `spot_price`. `greeks.spot` is deliberately not exposed. */
  spot: number;
  /** The listed strike closest to spot. A lookup, not a model. */
  atm_strike: number;
  /** ISO 8601, UTC, e.g. "2026-09-01T09:21:04Z". */
  fetched_at: string;
  /** Ascending by strike. */
  rows: ChainRow[];
  /**
   * The forward every `computed` figure on this chain was priced against, recovered by
   * parity regression across all paired strikes — not spot, and not assumed. `null` when
   * the chain could not be fitted, in which case no leg carries a volatility either.
   */
  forward: number | null;
  /** The discount factor fitted alongside the forward. */
  discount: number | null;
  /** ACT/365. The clock the volatility and the Greeks are both quoted on. */
  years_to_expiry: number | null;
  /** Which method produced the forward. `"F1"` is the parity regression. */
  forward_method: string | null;
}

/**
 * One strike's implied volatility at one minute. `docs/smile-contract.md`.
 *
 * **One point per strike, not per leg.** The store's grain is the contract, so a paired
 * strike holds two rows carrying the same number; put-call parity gives the strike one
 * volatility and the engine solves it on whichever side is out of the money. `iv_leg`
 * names that side, which is why the pair is not two independent solves — and why the
 * hover has to show it: the leg flips from put to call across the forward, and a reader
 * who does not know that reads the change in the curve's character as a break in the
 * arithmetic.
 *
 * **An unsolved strike arrives as a point with a null `iv`, never as a missing point.**
 * The line breaks there and is never drawn across it. Dropping the point would join two
 * neighbours through a gap and let a reader take a number off a segment that describes
 * nothing.
 *
 * No Greeks. They are stored beside these rows and deliberately not served.
 */
export interface SmilePoint {
  strike: number;
  /** Decimal fraction. 0.3189 is 31.89%. `null` when the strike could not be solved. */
  iv: number | null;
  /** `"call"` or `"put"`. `null` exactly when `iv` is. */
  iv_leg: string | null;
  /** `null` when solved — not `""`. `/chain`'s `ComputedLeg.iv_reason` spells it the
   * other way, and the two are deliberately not unified: this one is the store's. */
  iv_reason: string | null;
}

/**
 * One sealed minute of one expiry: the chain-level numbers, then the curve.
 *
 * `forward` is what the offset axis and the reference line are both read off, so it
 * arrives per minute rather than being inferred from spot — measured, spot-as-forward
 * disagrees with the fitted forward by up to 1.626 vol points.
 */
export interface SmileMinute {
  /** ISO 8601 UTC, second precision — `"2026-09-04T09:00:00Z"`. Never a local time. */
  minute: string;
  /** The forward this minute's volatilities were solved against. */
  forward: number | null;
  discount: number | null;
  /** ACT/365. The clock this minute's volatility is quoted on. */
  years_to_expiry: number | null;
  /** `"F1"`, `"F1+assumed-rate"` or `"F2"`. See `docs/chain-contract.md`. */
  forward_method: string | null;
  /** The stamp on this minute's rows, read from the data rather than hardcoded. */
  model_version: string | null;
  /** Ascending by strike. */
  points: SmilePoint[];
}

/**
 * `GET /smile?underlying=BTC&expiry=04-09-2026` — the whole stored day for one expiry.
 *
 * One request, not one per minute: measured, the entire store for one expiry reads in
 * 6.8 ms against 4.5 ms for a single minute, so scrubbing is an index into `minutes` and
 * an overlay is a second index into the same array. Nothing here is fetched again while
 * the scrubber moves.
 *
 * `model_versions` is a **list** because the model can change mid-day. A response
 * spanning two stamps reports both, and the header says so rather than putting two
 * differently computed curves on one axis in silence.
 *
 * An empty `minutes` is a 200, not an error: an underlying nobody has collected yet and
 * a day nobody has lived through are both "nothing yet".
 */
export interface SmileResponse {
  underlying: Underlying;
  expiry: ExpiryDate;
  /** Every distinct stamp in this response, ascending. Empty when there are no rows. */
  model_versions: string[];
  /** Ascending by minute. */
  minutes: SmileMinute[];
}

/** FastAPI's default error shape. */
export interface EngineError {
  detail: string;
}

/** Type guard for the error body, used to surface `detail` rather than a bare status. */
export function isEngineError(value: unknown): value is EngineError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { detail?: unknown }).detail === "string"
  );
}
