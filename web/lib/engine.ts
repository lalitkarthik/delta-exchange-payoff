/**
 * The only place the web app talks to the engine.
 *
 * Base URL is `NEXT_PUBLIC_ENGINE_URL`, defaulting to `http://localhost:8000`.
 * Nothing here does arithmetic and nothing here calls `parseFloat` — by the contract
 * every decimal already arrives as a JSON number. `assertNumeric` below enforces that
 * rather than papering over it: if a decimal arrives as a string the engine is in
 * breach and we say so loudly instead of quietly coercing.
 */
import {
  isEngineError,
  type ChainResponse,
  type ExpiriesResponse,
  type SmileResponse,
  type Underlying,
} from "./contract";
import { FIXTURE_CHAIN, fixtureChain, fixtureExpiries, fixtureSmile } from "./fixture";

export const ENGINE_URL = process.env.NEXT_PUBLIC_ENGINE_URL ?? "http://localhost:8000";

/** Set `NEXT_PUBLIC_USE_FIXTURE=1` to never touch the network. */
export const FORCE_FIXTURE = process.env.NEXT_PUBLIC_USE_FIXTURE === "1";

/** Where the data on screen actually came from. Shown in the header. */
export type Source = "engine" | "fixture";

export interface Loaded<T> {
  data: T;
  source: Source;
  /** Set when we fell back: the engine was unreachable and this is why. */
  fallbackReason?: string;
  /**
   * Only set on an expiries response served from the fixture: the one expiry the
   * fixture actually holds a chain for. The page opens on it so fixture mode shows a
   * real ladder rather than the front-month "no fixture" error. Against a live engine
   * this is always undefined and the page simply takes the front expiry.
   */
  preferredExpiry?: string;
}

/** The engine answered, but with an error status. Carries FastAPI's `detail`. */
export class EngineResponseError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
    this.name = "EngineResponseError";
  }
}

/** The engine could not be reached at all — not running, wrong port, CORS, DNS. */
export class EngineUnreachableError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause));
    this.name = "EngineUnreachableError";
  }
}

/** The engine answered 200 with a body the contract forbids. */
export class ContractViolationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractViolationError";
  }
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
 * Rule 3 of the brief, made enforceable. Every decimal in a chain is a JSON number
 * or `null`; a string here means the engine is violating the contract, and the right
 * move is to report it, not to parse it.
 */
const NUMERIC_LEG_FIELDS = [
  "bid",
  "ask",
  "mark",
  "bid_iv",
  "ask_iv",
  "mark_iv",
  "delta",
  "gamma",
  "theta",
  "vega",
  "rho",
  "oi",
  "oi_value_usd",
  "oi_change_usd_6h",
  "tick_size",
] as const;

function assertNumeric(chain: ChainResponse): void {
  const bad: string[] = [];
  const check = (label: string, value: unknown) => {
    if (typeof value === "string") bad.push(`${label}=${JSON.stringify(value)}`);
  };
  check("spot", chain.spot);
  check("atm_strike", chain.atm_strike);
  for (const row of chain.rows) {
    check(`rows[${row.strike}].strike`, row.strike);
    for (const [side, leg] of [
      ["call", row.call],
      ["put", row.put],
    ] as const) {
      if (!leg) continue;
      for (const field of NUMERIC_LEG_FIELDS) {
        check(`rows[${row.strike}].${side}.${field}`, leg[field]);
      }
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

/** The fixture only holds a chain for its own underlying, so only that one gets a hint. */
function fixturePreferred(underlying: Underlying): string | undefined {
  return underlying === FIXTURE_CHAIN.underlying ? FIXTURE_CHAIN.expiry : undefined;
}

export async function loadExpiries(underlying: Underlying): Promise<Loaded<ExpiriesResponse>> {
  if (FORCE_FIXTURE) {
    return {
      data: fixtureExpiries(underlying),
      source: "fixture",
      preferredExpiry: fixturePreferred(underlying),
    };
  }
  try {
    const data = await get<ExpiriesResponse>(`/expiries?underlying=${underlying}`);
    return { data, source: "engine" };
  } catch (err) {
    // An unreachable engine falls back to the fixture. An engine that answered
    // 400/404/502 gave a real answer, and that answer is surfaced, not hidden.
    if (err instanceof EngineUnreachableError) {
      return {
        data: fixtureExpiries(underlying),
        source: "fixture",
        fallbackReason: `Engine at ${ENGINE_URL} is unreachable (${err.message}).`,
        preferredExpiry: fixturePreferred(underlying),
      };
    }
    throw err;
  }
}

export async function loadChain(
  underlying: Underlying,
  expiry: string,
): Promise<Loaded<ChainResponse>> {
  if (FORCE_FIXTURE) {
    return { data: fixtureChain(underlying, expiry), source: "fixture" };
  }
  try {
    const data = await get<ChainResponse>(
      `/chain?underlying=${underlying}&expiry=${encodeURIComponent(expiry)}`,
    );
    assertNumeric(data);
    return { data, source: "engine" };
  } catch (err) {
    if (err instanceof EngineUnreachableError) {
      return {
        data: fixtureChain(underlying, expiry),
        source: "fixture",
        fallbackReason: `Engine at ${ENGINE_URL} is unreachable (${err.message}).`,
      };
    }
    throw err;
  }
}

/**
 * Rule 3 again, for the smile. `strike` and `iv` are the only decimals in the payload,
 * and `iv` is the one the whole screen plots — a string arriving there would sort and
 * scale as text and draw a plausible, wrong curve rather than fail.
 */
function assertSmileNumeric(smile: SmileResponse): void {
  const bad: string[] = [];
  for (const minute of smile.minutes) {
    for (const field of ["forward", "discount", "years_to_expiry"] as const) {
      if (typeof minute[field] === "string") bad.push(`${minute.minute}.${field}`);
    }
    for (const point of minute.points) {
      if (typeof point.strike === "string") bad.push(`${minute.minute}[?].strike`);
      if (typeof point.iv === "string") bad.push(`${minute.minute}[${point.strike}].iv`);
    }
  }
  if (bad.length > 0) {
    throw new ContractViolationError(
      `Engine sent decimals as strings, which docs/smile-contract.md forbids. ` +
        `The web app will not parse them. Offending fields: ${bad.slice(0, 6).join(", ")}` +
        (bad.length > 6 ? ` (+${bad.length - 6} more)` : ""),
    );
  }
}

/**
 * The whole stored day for one expiry, in one request.
 *
 * Same fallback rule as `loadChain`, and for the same reason: an engine that is not
 * running is a local condition the fixture can stand in for, while an engine that
 * answered 400 or 422 gave a real answer and that answer is surfaced rather than
 * papered over with a fixture that would look like a working screen.
 *
 * There is deliberately no 404 case. `docs/smile-contract.md` makes absence a 200 with
 * an empty `minutes`, so a 404 from this route means the engine is not the engine this
 * contract describes — and saying that out loud is more useful than a silent fixture.
 */
export async function loadSmile(
  underlying: Underlying,
  expiry: string,
): Promise<Loaded<SmileResponse>> {
  if (FORCE_FIXTURE) {
    return { data: fixtureSmile(underlying, expiry), source: "fixture" };
  }
  try {
    const data = await get<SmileResponse>(
      `/smile?underlying=${underlying}&expiry=${encodeURIComponent(expiry)}`,
    );
    assertSmileNumeric(data);
    return { data, source: "engine" };
  } catch (err) {
    if (err instanceof EngineUnreachableError) {
      return {
        data: fixtureSmile(underlying, expiry),
        source: "fixture",
        fallbackReason: `Engine at ${ENGINE_URL} is unreachable (${err.message}).`,
      };
    }
    throw err;
  }
}
