/**
 * Types and loaders for `/volatility` and `/volatility/bounds`.
 *
 * These mirror `VolatilitySeries` and `BoundsResponse` in the engine's `volatility.py`
 * field for field, on the same terms as `contract.ts` mirrors `docs/chain-contract.md`:
 * that side is the authority, and if the two disagree this file is wrong.
 *
 * The two rules that shape every type here are the chain contract's:
 *
 *   - **Every decimal is a JSON `number` or `null`, never a string.** Nothing in the web
 *     app parses a numeric string. `assertNumericSeries` enforces it rather than papering
 *     over it.
 *   - **`null` is absence, not zero.** A realised volatility of `null` means the window
 *     could not be measured; a `0` would mean the market did not move, which is a claim.
 *     They must never render the same way, and on the chart they do not: `null` breaks
 *     the line and `0` draws a point on the axis.
 */
import { ENGINE_URL, EngineResponseError, EngineUnreachableError, ContractViolationError } from "./engine";
import { isEngineError, type Underlying } from "./contract";

/** The five estimators, by the key the engine answers to. */
export const ESTIMATORS = [
  "simple",
  "log",
  "parkinson",
  "garman_klass",
  "rogers_satchell",
] as const;

export type Estimator = (typeof ESTIMATORS)[number];

/**
 * What each line is called on screen, and the one-line reason it exists.
 *
 * Yang-Zhang and GARCH are absent by decision rather than oversight: Yang-Zhang's whole
 * contribution over Rogers-Satchell is an overnight term, which needs a market that
 * closes, and this one never does. GARCH is a fitted model producing a forecast, which
 * makes it the same kind of object as implied volatility rather than the same kind as
 * realised.
 */
export const ESTIMATOR_LABEL: Record<Estimator, string> = {
  simple: "Simple returns",
  log: "Log returns",
  parkinson: "Parkinson",
  garman_klass: "Garman–Klass",
  rogers_satchell: "Rogers–Satchell",
};

export const ESTIMATOR_NOTE: Record<Estimator, string> = {
  simple: "Standard deviation of (Cₜ − Cₜ₋₁)/Cₜ₋₁. Should sit on top of the log line.",
  log: "Standard deviation of ln(Cₜ/Cₜ₋₁). Close-to-close: ignores everything inside the bar.",
  parkinson: "From the high–low range. Sees a bar that moved and came back; assumes zero drift.",
  garman_klass: "Parkinson's range plus the open–close term. Also assumes zero drift.",
  rogers_satchell: "Unbiased under a trend — it separates drift from movement.",
};

/** The IV line is not an estimator, but it is a checkbox like the others. */
export const IV_KEY = "iv" as const;

export type LineKey = Estimator | typeof IV_KEY;

export const ALIGNMENTS = ["contemporaneous", "lag"] as const;
export type Alignment = (typeof ALIGNMENTS)[number];

/** One timestamp, both series, and what each was computed from. */
export interface VolPoint {
  at: string;
  /** Constant-maturity ATM implied volatility at the lookback. `null` where unsolved. */
  iv: number | null;
  /** Per estimator. The **key is always present**; the value may be `null`. */
  rv: Partial<Record<Estimator, number | null>>;
  /** Returns (or bars, for the range estimators) behind each realised figure. */
  returns: Partial<Record<Estimator, number>>;
  /** `returns / expected`. Below one, the window had holes in it. */
  coverage: Partial<Record<Estimator, number>>;
}

export interface LookbackBounds {
  min_days: number;
  max_days: number;
  /** `"history"`, `"term_structure"`, `"observations"` or `"none"`. */
  binding: string;
  detail: string;
}

export interface VolatilitySeries {
  underlying: Underlying;
  lookback_days: number;
  interval_seconds: number;
  /** Seconds between successive points. Above `interval_seconds` when the range is long. */
  step_seconds: number;
  alignment: Alignment;
  estimators: Estimator[];
  valid_intervals: string[];
  bounds: LookbackBounds;
  points: VolPoint[];
  /** Which table the realised series in `points` was computed from. The engine
   * chooses; this file never infers it from the rest of the payload. */
  realised_source: "index-bars" | "spot-bars";
  /** How many of `points` carry a usable realised figure — at least one requested
   * estimator is non-null. Supplied by the engine; never recounted here. */
  realised_points: number;
  /** How many of `points` carry a non-null `iv`. Supplied by the engine. */
  implied_points: number;
}

export interface BoundsResponse extends LookbackBounds {
  usable: boolean;
  intervals: string[];
}

async function get<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${ENGINE_URL}${path}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch (cause) {
    throw new EngineUnreachableError(cause);
  }

  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new EngineResponseError(res.status, `${res.status} ${res.statusText}: body was not JSON`);
  }
  if (!res.ok) {
    throw new EngineResponseError(
      res.status,
      isEngineError(body) ? body.detail : `${res.status} ${res.statusText}`,
    );
  }
  return body as T;
}

/**
 * The contract's third rule, made enforceable on this payload too.
 *
 * A decimal arriving as a string would either be parsed here — putting arithmetic on the
 * wrong side of the contract — or rendered verbatim and quietly wrong. Neither is
 * acceptable, so it is reported instead.
 */
function assertNumericSeries(series: VolatilitySeries): void {
  const bad: string[] = [];
  const check = (label: string, value: unknown) => {
    if (typeof value === "string") bad.push(`${label}=${JSON.stringify(value)}`);
  };
  check("lookback_days", series.lookback_days);
  check("interval_seconds", series.interval_seconds);
  check("bounds.min_days", series.bounds.min_days);
  check("bounds.max_days", series.bounds.max_days);
  for (const point of series.points) {
    check(`points[${point.at}].iv`, point.iv);
    for (const key of Object.keys(point.rv) as Estimator[]) {
      check(`points[${point.at}].rv.${key}`, point.rv[key]);
      check(`points[${point.at}].returns.${key}`, point.returns[key]);
      check(`points[${point.at}].coverage.${key}`, point.coverage[key]);
    }
  }
  if (bad.length > 0) {
    throw new ContractViolationError(
      `Engine sent decimals as strings, which docs/chain-contract.md forbids. ` +
        `The web app will not parse them. Offending fields: ${bad.slice(0, 6).join(", ")}` +
        (bad.length > 6 ? ` (+${bad.length - 6} more)` : ""),
    );
  }
}

export async function loadBounds(
  underlying: Underlying,
  interval: string,
): Promise<BoundsResponse> {
  return get<BoundsResponse>(
    `/volatility/bounds?underlying=${underlying}&interval=${encodeURIComponent(interval)}`,
  );
}

export async function loadVolatility(params: {
  underlying: Underlying;
  lookbackDays: number;
  interval: string;
  estimators: Estimator[];
  alignment: Alignment;
}): Promise<VolatilitySeries> {
  const query = new URLSearchParams({
    underlying: params.underlying,
    lookback_days: String(params.lookbackDays),
    interval: params.interval,
    estimators: params.estimators.join(","),
    alignment: params.alignment,
  });
  const series = await get<VolatilitySeries>(`/volatility?${query}`);
  assertNumericSeries(series);
  return series;
}
