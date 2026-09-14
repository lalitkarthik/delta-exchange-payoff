/**
 * What the board holds at each strike: gamma exposure, and open interest.
 *
 * Two screens, one module, because they are the same shape — a call figure and a put
 * figure at every strike, drawn as bars — and the parts that differ are two lines of
 * arithmetic. Two modules would be two copies of the walk over `rows`, the pairing, the
 * null rules and the ATM lookup.
 *
 * **Pure, and that is where the whole feature lives.** The client has no test runner for
 * components, so anything computed only inside one ships unverified. Everything here can
 * be asserted from `bun tests/exposure.test.ts` against a literal chain.
 *
 * ## The gamma is ours
 *
 * `leg.computed.gamma`, never `leg.gamma`. The second is Delta's own figure, fitted to
 * prices that are five seconds stale and quoted on a spot convention; the project's
 * oldest rule is that it is a reference column and never an input, and
 * `engine/tests/test_no_delta_inputs.py` pins it. The two are not blended — not even as
 * a fallback on a strike ours could not solve, which would make one axis a mixture of two
 * conventions and no label could say which strike came from which.
 *
 * Ours is the **Black-76 forward** gamma from `engine/src/deltapayoff/greeks.py`:
 * undiscounted, per one unit of the underlying, the second derivative with respect to the
 * **forward**. The dollar conversion below squares **spot**, not the forward. Within a day
 * the two agree to well under a percent, so the mismatch is immaterial at the resolution
 * of a bar chart — but it is stated rather than converted, because someone reading the
 * axis should know which gamma produced it.
 *
 * ## The dollar convention
 *
 * ```
 * gex(K) = (Γc·OIc − Γp·OIp) · contract_value · spot² · 0.01     [USD per 1% move]
 * ```
 *
 * Dealer-short: dealers are taken to be long calls and short puts, which is the published
 * convention and the one that gives the sign a meaning. `Γ` is per one unit of underlying
 * and `OI` is in **contracts**, so `contract_value` is what makes the product a quantity
 * of the underlying; `spot²` turns a second derivative into dollars and `0.01` makes it
 * per one percent rather than per one dollar.
 *
 * ## `null` is not `0`, here as everywhere
 *
 * A leg whose volatility did not solve carries no gamma at all — around 40% of listed
 * strikes are illiquid enough for that — and it contributes **nothing**, which is not the
 * same as contributing zero even though the sum is the same number. The difference is what
 * `gammaCoverage` reports, and the screen says it out loud: a total drawn from half the
 * board should not be read as a total.
 *
 * **`oi_value_usd` is never read.** `docs/storage.md` measured that the ticker channel's
 * `oi[1]` is Delta's `oi_change_usd_6h` and not a USD notional — it goes negative, which a
 * notional cannot — and `Leg.oi_value_usd` is still fed from that position on the
 * websocket path. It is a known bug wanting its own ticket, and nothing here touches it.
 * Open interest means contracts, which is `leg.oi` on both transports.
 */
import type { ChainResponse, Leg } from "./contract";

/** What one strike holds, on each side and net. `null` where the input was absent — a
 *  leg that is not listed, or one whose volatility did not solve — and never `0`, which
 *  on this board is a real zero: a listed strike nobody holds. */
export interface StrikeBar {
  strike: number;
  call: number | null;
  put: number | null;
  /** Calls less puts, counting an absent side as nothing. */
  net: number;
}

/** Per one percent, which is what makes a second derivative a number a desk can size. */
const ONE_PERCENT = 0.01;

function net(call: number | null, put: number | null): number {
  return (call ?? 0) - (put ?? 0);
}

/**
 * Gamma exposure by strike, in USD per 1% move — or `null` for the whole board when the
 * chain cannot support the arithmetic.
 *
 * **Refused rather than approximated when `spot` or `contract_value` is absent.** Both
 * are multipliers on every bar, so a missing one does not degrade the picture, it changes
 * its units — and a dollar axis whose dollars were invented is exactly the plausible
 * wrong screen this project keeps refusing to draw. `contract_value` is `null` only for
 * an underlying nobody has measured the lot for; `spot` is `null` only on a stored minute
 * with no `spot-bars` row.
 */
export function gexByStrike(chain: ChainResponse): StrikeBar[] | null {
  const { spot, contract_value: lot } = chain;
  if (spot === null || lot === null) return null;
  const scale = lot * spot * spot * ONE_PERCENT;
  return chain.rows.map((row) => {
    const call = dollarGamma(row.call, scale);
    const put = dollarGamma(row.put, scale);
    return { strike: row.strike, call, put, net: net(call, put) };
  });
}

/** One leg's gamma exposure, or `null` when either half of the product is missing. Ours,
 *  never Delta's — see this module's header. */
function dollarGamma(leg: Leg | null, scale: number): number | null {
  const gamma = leg?.computed?.gamma;
  const oi = leg?.oi;
  if (gamma === null || gamma === undefined) return null;
  if (oi === null || oi === undefined) return null;
  return gamma * oi * scale;
}

/**
 * Open interest by strike, in contracts.
 *
 * `net` is calls less puts, which is the same arithmetic as GEX and reads the same way —
 * positive where the board is call-heavy. This needs no `spot` and no lot: open interest
 * is a count, and the count is what the venue publishes.
 */
export function oiByStrike(chain: ChainResponse): StrikeBar[] {
  return chain.rows.map((row) => {
    const call = row.call?.oi ?? null;
    const put = row.put?.oi ?? null;
    return { strike: row.strike, call, put, net: net(call, put) };
  });
}

/** How much of the board the gamma figures actually cover: strikes with a computed gamma
 *  on either side, out of every strike listed. The screen prints this because a total
 *  summed over half a board is not a total, and nothing else on screen would say so. */
export function gammaCoverage(chain: ChainResponse): { solved: number; total: number } {
  const solved = chain.rows.filter(
    (row) => row.call?.computed?.gamma != null || row.put?.computed?.gamma != null,
  ).length;
  return { solved, total: chain.rows.length };
}

/** The running total across the board, ascending by strike — the curve whose sign change
 *  is the level below. */
export function cumulative(bars: StrikeBar[]): { strike: number; total: number }[] {
  let running = 0;
  return bars.map((bar) => {
    running += bar.net;
    return { strike: bar.strike, total: running };
  });
}

/**
 * The strike at which the cumulative total changes sign, or `null` when it never does.
 *
 * **Named for what it is.** This is not "the gamma flip price": that would be the spot at
 * which total exposure turns, and finding it means re-pricing every strike's gamma at a
 * range of spots, which this does not do and the payload could not support. It is the
 * strike where the running sum crosses zero, which is a fact about the board as it stands
 * — useful, and a smaller claim.
 *
 * The **first** crossing, ascending. A board can cross more than once and the lowest is
 * the one the price has to get through first.
 */
export function zeroCrossStrike(bars: StrikeBar[]): number | null {
  const totals = cumulative(bars);
  for (let i = 1; i < totals.length; i += 1) {
    const before = totals[i - 1]!.total;
    const here = totals[i]!.total;
    if (before === 0) return totals[i - 1]!.strike;
    if ((before < 0 && here > 0) || (before > 0 && here < 0)) return totals[i]!.strike;
    if (here === 0) return totals[i]!.strike;
  }
  return null;
}

/** The largest absolute figure on the board, for an axis that has to hold both signs
 *  symmetrically. `0` on an empty board rather than `-Infinity`. */
export function peak(bars: StrikeBar[]): number {
  return bars.reduce(
    (most, bar) => Math.max(most, Math.abs(bar.call ?? 0), Math.abs(bar.put ?? 0)),
    0,
  );
}

/** Every bar summed. Reported under the chart as the one figure for the whole expiry. */
export function total(bars: StrikeBar[]): number {
  return bars.reduce((sum, bar) => sum + bar.net, 0);
}

/* ------------------------------------------------------------------ many expiries
 *
 * The expiries panel checks more than one board at a time, and a checked board is
 * **summed into** the others rather than drawn beside them: the question these screens
 * answer is what the dealer's position is at a strike, and a strike carries whatever is
 * open against it whenever it expires. The engine serves one expiry per subscription, so
 * the summing happens here, on projections that have already been made — never on the
 * chains themselves, because two expiries at one strike are two different contracts with
 * two different volatilities and nothing about them should be averaged.
 */

/**
 * Several boards summed strike by strike.
 *
 * The union of every strike, ascending. A strike listed on one expiry and not another
 * contributes only where it is listed, which is the same rule as a leg contributing only
 * where it solved: `null + null` stays `null`, and `null + 3` is `3`. A column of
 * absences must not become a zero just because something else was present at that strike.
 */
export function sumBoards(boards: StrikeBar[][]): StrikeBar[] {
  const byStrike = new Map<number, { call: number | null; put: number | null }>();
  for (const board of boards) {
    for (const bar of board) {
      const held = byStrike.get(bar.strike) ?? { call: null, put: null };
      byStrike.set(bar.strike, {
        call: add(held.call, bar.call),
        put: add(held.put, bar.put),
      });
    }
  }
  return [...byStrike.entries()]
    .sort(([a], [b]) => a - b)
    .map(([strike, sides]) => ({
      strike,
      call: sides.call,
      put: sides.put,
      net: net(sides.call, sides.put),
    }));
}

/** `a + b` where absence is not zero: two absences stay absent, one absence yields the
 *  other. The asymmetry with `net` above is deliberate — that one is forming a difference
 *  and treats absence as nothing; this one is merging observations. */
function add(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  if (b === null) return a;
  return a + b;
}

/**
 * The `count` strikes either side of the money, and the money's own.
 *
 * The STRIKES control. Counted in **listed strikes, not points**, so the window holds the
 * same number of tradeable rows wherever the ladder thickens or thins — the same rule the
 * structure chain's row window follows. `null` for `count` is ALL and returns the board
 * unchanged; a board with no ATM strike is also returned unchanged, because there is
 * nothing to centre on and quietly showing the lowest strikes would be worse than showing
 * everything.
 */
export function strikeWindow(
  bars: StrikeBar[],
  atmStrike: number | null,
  count: number | null,
): StrikeBar[] {
  if (count === null || atmStrike === null || bars.length === 0) return bars;
  let centre = bars.findIndex((bar) => bar.strike === atmStrike);
  if (centre < 0) {
    centre = bars.reduce(
      (best, bar, i) =>
        Math.abs(bar.strike - atmStrike) < Math.abs(bars[best]!.strike - atmStrike) ? i : best,
      0,
    );
  }
  return bars.slice(Math.max(0, centre - count), centre + count + 1);
}

/**
 * What the board holds in total on each side, and the ratio between them.
 *
 * `pcr` is puts over calls — the published direction, so a number above 1 means more puts
 * are open than calls, as every desk reads it. `null` rather than `Infinity` when no calls
 * are open at all: a ratio with nothing underneath it is not a large ratio.
 */
export function sideTotals(bars: StrikeBar[]): {
  call: number;
  put: number;
  pcr: number | null;
} {
  let call = 0;
  let put = 0;
  for (const bar of bars) {
    call += bar.call ?? 0;
    put += bar.put ?? 0;
  }
  return { call, put, pcr: call === 0 ? null : put / call };
}

/** The strike carrying the most on one side — the wall the reference terminal badges.
 *  `null` on a board with nothing on that side at all, where "the largest" names nothing. */
export function peakStrike(bars: StrikeBar[], side: "call" | "put"): number | null {
  let best: number | null = null;
  let most = 0;
  for (const bar of bars) {
    const value = bar[side];
    if (value === null || value <= most) continue;
    most = value;
    best = bar.strike;
  }
  return best;
}

/** One ranked strike on the GEX board: where it is, what it holds, and the rank it was
 *  given. `rank` is 1-based so it can be read straight onto an `R1`/`S1` badge. */
export interface Level {
  readonly strike: number;
  readonly value: number;
  readonly rank: number;
}

/**
 * The strikes holding the most gamma either side, ranked by size.
 *
 * Positive net is **resistance** and negative is **support**, which is the published
 * reading of the dealer-short convention: where dealers are long gamma they sell into a
 * rally and buy a dip, so price is pinned. The top of each list is the wall.
 *
 * **Ranked by size, not by distance from spot**, and the lists are not filtered to one
 * side of it. A large positive strike below spot is still where the gamma is, and hiding
 * it because of where the price happens to be standing would make the panel a function of
 * two things while claiming to report one.
 */
export function levels(bars: StrikeBar[], count: number): { resistances: Level[]; supports: Level[] } {
  const positive = bars.filter((bar) => bar.net > 0).sort((a, b) => b.net - a.net);
  const negative = bars.filter((bar) => bar.net < 0).sort((a, b) => a.net - b.net);
  const take = (list: StrikeBar[]): Level[] =>
    list.slice(0, count).map((bar, i) => ({ strike: bar.strike, value: bar.net, rank: i + 1 }));
  return { resistances: take(positive), supports: take(negative) };
}

/**
 * Whole days from `now` to an expiry, for the DTE beside each entry in the panel.
 *
 * A string rearrangement into a UTC instant rather than `new Date("10-04-2026")`, which
 * parses as a **local** date — the off-by-one `lib/instrument.ts` avoids for the same
 * reason. Delta's expiries settle at 12:00 UTC, and that is the instant counted to:
 * counting to midnight would call an expiry that still has half a trading day left `0D`.
 *
 * Rounded down, and never below zero. An expiry that has already settled shows `0D`
 * rather than a negative, which would read as a date in the future written backwards.
 */
export function daysToExpiry(expiry: string, now: Date): number | null {
  const match = /^(\d{2})-(\d{2})-(\d{4})$/.exec(expiry);
  if (match === null) return null;
  const [, day, month, year] = match;
  const settles = Date.UTC(Number(year), Number(month) - 1, Number(day), 12, 0, 0);
  const days = Math.floor((settles - now.getTime()) / 86_400_000);
  return Math.max(0, days);
}
