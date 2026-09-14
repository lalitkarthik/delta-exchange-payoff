/**
 * The structure chain's whole arithmetic, against literal chains.
 *
 * `lib/structures.ts` is the only place the screen computes anything; the component above
 * it chooses colours and headings. So this file is the feature's verification, not a
 * supplement to it — the claims below are the ones that, if wrong, put a plausible number
 * on screen that describes nothing:
 *
 *   - **A wing snaps to a listed strike, or the cell is empty.** A strangle drawn from a
 *     strike two columns away is a correct number under a heading that lies about it.
 *   - **`D = 0` is the straddle.** Both wings land on the row's own strike.
 *   - **A long structure is a debit.** Get the sign backwards and every cell inverts
 *     while the grid still looks right.
 *   - **Volatility is vega-weighted, and does not flip with direction.** A mean of two
 *     vols is a different number, and a short straddle is short the vol the long one is
 *     long.
 *   - **Ours, never Delta's.** A leg carrying Delta's greeks but no `computed` block
 *     contributes nothing to a Greek cell — and may still carry a price.
 *   - **`null` is not `0`.** An unquoted leg empties the cell; it does not zero it.
 */
import assert from "node:assert/strict";

import type { ChainResponse, ChainRow, Leg } from "@/lib/contract";
import {
  defaultStep,
  ivCoverage,
  peakValue,
  structureGrid,
  type Feature,
  type GridRequest,
} from "@/lib/structures";

let failures = 0;

function check(name: string, body: () => void): void {
  try {
    body();
    console.log(`  ok    ${name}`);
  } catch (err) {
    failures++;
    console.error(`  FAIL  ${name}`);
    console.error(`        ${err instanceof Error ? err.message : String(err)}`);
  }
}

/** A leg carrying only what this module reads, plus the fields it must not. Delta's own
 *  greeks are deliberately large and deliberately wrong: anything reading them instead of
 *  `computed` fails loudly rather than being off by a little. */
function leg({
  bid = null,
  ask = null,
  iv = null,
  delta = null,
  gamma = null,
  vega = null,
  theta = null,
  solved = true,
}: {
  bid?: number | null;
  ask?: number | null;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  vega?: number | null;
  theta?: number | null;
  /** `false` drops the whole `computed` block — the illiquid strike, ~40% of a board. */
  solved?: boolean;
}): Leg {
  return {
    symbol: "C-BTC-100000-040926",
    product_id: 1,
    bid,
    ask,
    mark: 999,
    bid_iv: 9.99,
    ask_iv: 9.99,
    mark_iv: 9.99,
    delta: 999,
    gamma: 999,
    theta: 999,
    vega: 999,
    rho: 999,
    oi: null,
    oi_value_usd: null,
    oi_change_usd_6h: null,
    tick_size: null,
    computed: solved
      ? { iv, iv_leg: "call", iv_reason: "", delta, gamma, vega, theta, rho: null }
      : null,
  };
}

/** Both sides listed and quoted, with the same numbers, so a test naming one field is not
 *  silently relying on another. */
function pair(strike: number, over: Parameters<typeof leg>[0] = {}): ChainRow {
  return { strike, call: leg(over), put: leg(over) };
}

function chain(rows: ChainRow[], over: Partial<ChainResponse> = {}): ChainResponse {
  return {
    underlying: "BTC",
    expiry: "04-09-2026",
    quote_currency: "USD",
    spot: 100_000,
    atm_strike: 100_000,
    fetched_at: "2026-09-04T09:21:04Z",
    rows,
    forward: 100_000,
    discount: 1,
    years_to_expiry: 0.01,
    forward_method: "F1",
    ...over,
  };
}

function req(over: Partial<GridRequest> = {}): GridRequest {
  return {
    offsetMin: 0,
    step: 1000,
    columns: 3,
    rowsEitherSide: 1,
    direction: 1,
    feature: "price" as Feature,
    ...over,
  };
}

/** Five evenly spaced strikes, every leg quoted 10/20 (mid 15) with a full greek block. */
function evenBoard(): ChainResponse {
  const over = { bid: 10, ask: 20, iv: 0.4, delta: 0.5, gamma: 0.0001, vega: 0.3, theta: -66 };
  return chain([
    pair(98_000, over),
    pair(99_000, over),
    pair(100_000, over),
    pair(101_000, over),
    pair(102_000, over),
  ]);
}

// --- the window ------------------------------------------------------------------

check("THE ROWS ARE COUNTED IN LISTED STRIKES, CENTRED ON THE ATM ONE", () => {
  const grid = structureGrid(evenBoard(), req({ rowsEitherSide: 1 }))!;
  assert.deepEqual(
    grid.rows.map((row) => row.strike),
    [99_000, 100_000, 101_000],
  );
  assert.equal(grid.atmStrike, 100_000);
});

check("A WINDOW WIDER THAN THE BOARD GIVES THE BOARD, NOT PADDING", () => {
  const grid = structureGrid(evenBoard(), req({ rowsEitherSide: 99 }))!;
  assert.equal(grid.rows.length, 5);
});

check("THE COLUMNS ARE OFFSET_MIN, +STEP, +STEP", () => {
  const grid = structureGrid(evenBoard(), req({ offsetMin: 0, step: 1000, columns: 3 }))!;
  assert.deepEqual(grid.offsets, [0, 1000, 2000]);
  const shifted = structureGrid(evenBoard(), req({ offsetMin: 1000, step: 1000, columns: 2 }))!;
  assert.deepEqual(shifted.offsets, [1000, 2000]);
});

// --- the snapping ----------------------------------------------------------------

check("D = 0 IS THE STRADDLE: both wings land on the row's own strike", () => {
  const grid = structureGrid(evenBoard(), req())!;
  const straddle = grid.rows[1]!.cells[0]!;
  assert.equal(grid.rows[1]!.strike, 100_000);
  assert.equal(straddle.callStrike, 100_000);
  assert.equal(straddle.putStrike, 100_000);
});

check("A STRANGLE TAKES THE CALL ABOVE AND THE PUT BELOW", () => {
  const grid = structureGrid(evenBoard(), req())!;
  const wide = grid.rows[1]!.cells[1]!; // D = 1000 off 100,000
  assert.equal(wide.callStrike, 101_000);
  assert.equal(wide.putStrike, 99_000);
});

check("A WING FURTHER THAN HALF A STEP AWAY IS REJECTED, NOT SUBSTITUTED", () => {
  // Strikes 1,000 apart asked for wings 500 out: the nearest listed strike is 500 away,
  // which is exactly half a step and allowed; 600 out would be 400 away and allowed too,
  // but a board with a hole is not.
  const holed = chain([pair(99_000), pair(100_000), pair(103_000)]);
  const grid = structureGrid(holed, req({ offsetMin: 1000, step: 1000, columns: 1 }))!;
  const cell = grid.rows.find((row) => row.strike === 100_000)!.cells[0]!;
  // The call wing wants 101,000; the nearest listed is 103,000, two steps away.
  assert.equal(cell.callStrike, null, "no strike within half a step of 101,000");
  assert.equal(cell.value, null);
  assert.equal(cell.unlisted, true, "hatched, not merely blank");
});

check("SNAPPING TIES GO TO THE LOWER STRIKE, AS THE ENGINE'S ATM LOOKUP DOES", () => {
  // Wing wants 100,500, which is exactly between 100,000 and 101,000.
  const board = chain([pair(100_000), pair(101_000)], { atm_strike: 100_000 });
  const grid = structureGrid(board, req({ offsetMin: 500, step: 1000, columns: 1 }))!;
  const cell = grid.rows.find((row) => row.strike === 100_000)!.cells[0]!;
  assert.equal(cell.callStrike, 100_000);
});

check("AN UNLISTED SIDE EMPTIES THE CELL AND IS MARKED UNLISTED", () => {
  const board = chain([
    { strike: 99_000, call: leg({ bid: 10, ask: 20 }), put: null },
    pair(100_000, { bid: 10, ask: 20 }),
    pair(101_000, { bid: 10, ask: 20 }),
  ]);
  const grid = structureGrid(board, req({ offsetMin: 1000, step: 1000, columns: 1 }))!;
  const cell = grid.rows.find((row) => row.strike === 100_000)!.cells[0]!;
  assert.equal(cell.putStrike, 99_000, "the strike is listed");
  assert.equal(cell.value, null, "but its put is not");
  assert.equal(cell.unlisted, true);
});

// --- the price -------------------------------------------------------------------

check("THE ARITHMETIC, BY HAND: a long straddle is minus the sum of two midpoints", () => {
  // mid(call) = (10+20)/2 = 15; mid(put) = 15; long = −30.
  const grid = structureGrid(evenBoard(), req({ feature: "price" }))!;
  assert.equal(grid.rows[1]!.cells[0]!.value, -30);
});

check("SHORT FLIPS THE SIGN AND NOTHING ELSE", () => {
  const long = structureGrid(evenBoard(), req({ feature: "price", direction: 1 }))!;
  const short = structureGrid(evenBoard(), req({ feature: "price", direction: -1 }))!;
  assert.equal(long.rows[1]!.cells[0]!.value, -30);
  assert.equal(short.rows[1]!.cells[0]!.value, 30);
  assert.equal(short.rows[1]!.cells[0]!.callStrike, long.rows[1]!.cells[0]!.callStrike);
});

check("A ONE-SIDED MARKET HAS NO MIDPOINT: the cell is empty, not half a price", () => {
  const board = chain([pair(100_000, { bid: 10, ask: null })]);
  const grid = structureGrid(board, req({ feature: "price", columns: 1 }))!;
  assert.equal(grid.rows[0]!.cells[0]!.value, null);
  assert.equal(grid.rows[0]!.cells[0]!.unlisted, false, "listed and paired, merely unquoted");
});

check("THE MARK IS NOT THE MIDPOINT: an unquoted leg carrying a mark is still empty", () => {
  // `leg()` sets mark 999 on every leg; a cell reading it would report 999-ish here.
  const board = chain([pair(100_000, { bid: null, ask: null })]);
  const grid = structureGrid(board, req({ feature: "price", columns: 1 }))!;
  assert.equal(grid.rows[0]!.cells[0]!.value, null);
});

// --- the greeks, ours never Delta's ----------------------------------------------

check("A GREEK IS THE SUM OF THE TWO LEGS, SIGNED BY DIRECTION", () => {
  // delta 0.5 on each leg — a synthetic figure, not a real straddle's — sums to 1.0.
  const long = structureGrid(evenBoard(), req({ feature: "delta" }))!;
  assert.equal(long.rows[1]!.cells[0]!.value, 1);
  const short = structureGrid(evenBoard(), req({ feature: "delta", direction: -1 }))!;
  assert.equal(short.rows[1]!.cells[0]!.value, -1);

  const gamma = structureGrid(evenBoard(), req({ feature: "gamma" }))!;
  assert.equal(gamma.rows[1]!.cells[0]!.value, 0.0002);
  const theta = structureGrid(evenBoard(), req({ feature: "theta" }))!;
  assert.equal(theta.rows[1]!.cells[0]!.value, -132);
});

check("THE UNDERLYING DOES NOT CHANGE THE ARITHMETIC: every figure is per one unit", () => {
  // Nothing here is scaled by a lot size. When `ChainResponse` grows a `contract_value`
  // this becomes the sharper claim that ETH's 0.01 moves nothing; until then it is the
  // weaker one that the underlying is not an input.
  const btc = structureGrid(evenBoard(), req({ feature: "vega" }))!;
  const eth = structureGrid(
    chain(evenBoard().rows, { underlying: "ETH" }),
    req({ feature: "vega" }),
  )!;
  assert.equal(eth.rows[1]!.cells[0]!.value, btc.rows[1]!.cells[0]!.value);
});

check("A LEG WITH NO COMPUTED BLOCK HAS NO GREEKS, AND DELTA'S ARE NOT USED", () => {
  const board = chain([pair(100_000, { bid: 10, ask: 20, solved: false })]);
  const grid = structureGrid(board, req({ feature: "delta", columns: 1 }))!;
  assert.equal(grid.rows[0]!.cells[0]!.value, null, "Delta's 999 must not reach the grid");
});

check("AN UNSOLVED STRIKE STILL HAS A PRICE: the two are separate absences", () => {
  const board = chain([pair(100_000, { bid: 10, ask: 20, solved: false })]);
  const priced = structureGrid(board, req({ feature: "price", columns: 1 }))!;
  assert.equal(priced.rows[0]!.cells[0]!.value, -30);
});

check("ONE SOLVED LEG IS NOT HALF A STRUCTURE: absence propagates through the sum", () => {
  const board = chain([
    {
      strike: 100_000,
      call: leg({ delta: 0.6, bid: 10, ask: 20 }),
      put: leg({ delta: null, bid: 10, ask: 20 }),
    },
  ]);
  const grid = structureGrid(board, req({ feature: "delta", columns: 1 }))!;
  assert.equal(grid.rows[0]!.cells[0]!.value, null, "0.6 reported as the pair's would understate");
});

// --- the volatility --------------------------------------------------------------

check("IV IS VEGA-WEIGHTED, NOT AVERAGED", () => {
  // (0.40×0.1 + 0.60×0.3) / 0.4 = 0.22/0.4 = 0.55. The plain mean would be 0.50.
  const board = chain([
    {
      strike: 100_000,
      call: leg({ iv: 0.4, vega: 0.1 }),
      put: leg({ iv: 0.6, vega: 0.3 }),
    },
  ]);
  const grid = structureGrid(board, req({ feature: "iv", columns: 1 }))!;
  // Binary floating point lands this a half-ulp under 0.55; the claim is the weighting,
  // not the last bit, and the plain mean it must not be is 0.05 away.
  assert.ok(Math.abs(grid.rows[0]!.cells[0]!.value! - 0.55) < 1e-12);
});

check("WITH NO VEGA TO WEIGH BY, THE PLAIN MEAN — and likewise at zero total vega", () => {
  const noVega = chain([
    { strike: 100_000, call: leg({ iv: 0.4 }), put: leg({ iv: 0.6 }) },
  ]);
  assert.equal(structureGrid(noVega, req({ feature: "iv", columns: 1 }))!.rows[0]!.cells[0]!.value, 0.5);

  const zeroVega = chain([
    { strike: 100_000, call: leg({ iv: 0.4, vega: 0 }), put: leg({ iv: 0.6, vega: 0 }) },
  ]);
  assert.equal(
    structureGrid(zeroVega, req({ feature: "iv", columns: 1 }))!.rows[0]!.cells[0]!.value,
    0.5,
    "never a division by zero reaching the screen as NaN",
  );
});

check("IV IS A DECIMAL FRACTION AND DOES NOT FLIP WITH DIRECTION", () => {
  const board = chain([pair(100_000, { iv: 0.4, vega: 0.3 })]);
  const long = structureGrid(board, req({ feature: "iv", columns: 1 }))!;
  const short = structureGrid(board, req({ feature: "iv", columns: 1, direction: -1 }))!;
  assert.equal(long.rows[0]!.cells[0]!.value, 0.4, "0.4, never 40");
  assert.equal(short.rows[0]!.cells[0]!.value, 0.4, "a short straddle is short the same vol");
});

check("ONE UNSOLVED SIDE LEAVES NO VOLATILITY AT ALL", () => {
  const board = chain([
    { strike: 100_000, call: leg({ iv: 0.4, vega: 0.3 }), put: leg({ iv: null, vega: 0.3 }) },
  ]);
  assert.equal(structureGrid(board, req({ feature: "iv", columns: 1 }))!.rows[0]!.cells[0]!.value, null);
});

// --- refusals, coverage, and the step --------------------------------------------

check("A BOARD WITH NO ATM STRIKE OR NO ROWS IS REFUSED WHOLE", () => {
  assert.equal(structureGrid(chain([]), req()), null, "nothing to anchor rows to");
  assert.equal(
    structureGrid(chain([pair(100_000)], { atm_strike: null }), req()),
    null,
    "no window without the money",
  );
});

check("COVERAGE COUNTS VALUED CELLS OUT OF PAIRED ONES, NOT OUT OF EVERY CELL", () => {
  const board = chain([
    pair(99_000, { bid: 10, ask: 20 }),
    pair(100_000, { bid: 10, ask: 20 }),
    { strike: 101_000, call: leg({ bid: null, ask: null }), put: leg({ bid: null, ask: null }) },
  ]);
  // One column, D = 0, three rows: two priced, one unquoted, none unlisted.
  const grid = structureGrid(board, req({ feature: "price", columns: 1, rowsEitherSide: 9 }))!;
  assert.deepEqual(ivCoverage(grid), { solved: 2, total: 3 });
});

check("A CELL WITH NO WING TO PRICE IS OUT OF THE DENOMINATOR", () => {
  const holed = chain([pair(100_000, { bid: 10, ask: 20 })]);
  // One strike on the board: D = 0 prices, D = 1000 has no wing either side.
  const grid = structureGrid(holed, req({ feature: "price", columns: 2, step: 1000 }))!;
  assert.deepEqual(ivCoverage(grid), { solved: 1, total: 1 });
});

check("THE PEAK IS THE LARGEST ABSOLUTE CELL, AND AN EMPTY GRID PEAKS AT ZERO", () => {
  const grid = structureGrid(evenBoard(), req({ feature: "price" }))!;
  assert.equal(peakValue(grid), 30);
  const blank = structureGrid(chain([pair(100_000)]), req({ feature: "price", columns: 1 }))!;
  assert.equal(peakValue(blank), 0, "never -Infinity, which would reach a style attribute");
});

check("THE DEFAULT STEP IS THE MODAL GAP, NOT THE MEAN OR THE MINIMUM", () => {
  // Gaps: 1000, 1000, 1000, 5000. Mean 2000, minimum 1000, mode 1000.
  const board = chain([pair(98_000), pair(99_000), pair(100_000), pair(101_000), pair(106_000)]);
  assert.equal(defaultStep(board), 1000);
  assert.equal(defaultStep(chain([pair(100_000)])), null, "one strike has no gap");
  assert.equal(defaultStep(chain([])), null);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
