/**
 * The GEX and OI screens' whole arithmetic, against literal chains.
 *
 * `lib/exposure.ts` is the only place either screen computes anything; the components
 * above it choose colours and axes. So this file is the feature's verification, not a
 * supplement to it — the claims below are the ones that, if wrong, put a plausible
 * number on screen that describes nothing:
 *
 *   - **The sign is the dealer-short convention.** A put-heavy strike is negative. Get
 *     this backwards and every reading of the chart inverts while it still looks right.
 *   - **The dollars are worked by hand here.** `Γ·OI·lot·S²·0.01` is five multiplications
 *     and no test above this file would catch one of them being dropped.
 *   - **Ours, never Delta's.** A leg carrying Delta's `gamma` but no `computed.gamma`
 *     contributes nothing. This is the project's oldest rule and the one a "sensible
 *     fallback" breaks most quietly.
 *   - **A missing multiplier refuses the board.** No spot, no lot, no dollars — not a
 *     chart in unstated units.
 *   - **`oi_value_usd` is never read.** It is a six-hour change wearing a notional's
 *     name (`docs/storage.md`), and it is on the same leg as the field that is correct.
 */
import assert from "node:assert/strict";

import type { ChainResponse, ChainRow, Leg } from "@/lib/contract";
import {
  cumulative,
  daysToExpiry,
  gammaCoverage,
  gexByStrike,
  levels,
  oiByStrike,
  peak,
  peakStrike,
  sideTotals,
  strikeWindow,
  sumBoards,
  total,
  zeroCrossStrike,
} from "@/lib/exposure";

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

/** A leg carrying only what this module reads, plus the two fields it must not. */
function leg({
  gamma,
  oi,
  venueGamma = 999,
}: {
  gamma: number | null;
  oi: number | null;
  venueGamma?: number | null;
}): Leg {
  return {
    symbol: "C-BTC-77000-040926",
    product_id: 1,
    bid: null,
    ask: null,
    mark: null,
    bid_iv: null,
    ask_iv: null,
    mark_iv: null,
    delta: null,
    // Delta's own gamma, deliberately large and deliberately wrong: anything that reads
    // it instead of `computed.gamma` fails loudly rather than being off by a little.
    gamma: venueGamma,
    theta: null,
    vega: null,
    rho: null,
    oi,
    // A six-hour change wearing a notional's name. Nothing may read it.
    oi_value_usd: 123_456_789,
    oi_change_usd_6h: null,
    tick_size: null,
    computed:
      gamma === null
        ? null
        : { iv: 0.4, iv_leg: "call", iv_reason: "", delta: null, gamma, vega: null, theta: null, rho: null },
  };
}

function chain(rows: ChainRow[], over: Partial<ChainResponse> = {}): ChainResponse {
  return {
    underlying: "BTC",
    expiry: "04-09-2026",
    quote_currency: "USD",
    contract_value: 0.001,
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

// --- the dollar convention ------------------------------------------------------

check("THE ARITHMETIC, BY HAND: gamma x OI x lot x spot^2 x 1%", () => {
  // 0.0001 x 2,000 x 0.001 x 100,000^2 x 0.01 = 20,000
  const bars = gexByStrike(chain([{ strike: 100_000, call: leg({ gamma: 0.0001, oi: 2000 }), put: null }]));
  assert.ok(bars);
  assert.equal(bars[0]!.call, 20_000);
  assert.equal(bars[0]!.put, null, "an unlisted side is absent, not zero");
  assert.equal(bars[0]!.net, 20_000);
});

check("DEALER-SHORT: calls add, puts subtract, and a put-heavy strike is negative", () => {
  const bars = gexByStrike(
    chain([
      {
        strike: 100_000,
        call: leg({ gamma: 0.0001, oi: 1000 }),
        put: leg({ gamma: 0.0001, oi: 3000 }),
      },
    ]),
  );
  assert.ok(bars);
  assert.equal(bars[0]!.call, 10_000);
  assert.equal(bars[0]!.put, 30_000, "the put's own bar is its size, not its sign");
  assert.equal(bars[0]!.net, -20_000, "net is where the convention lives");
});

check("THE LOT IS APPLIED: ETH's 0.01 is ten times BTC's 0.001, and nothing else moves", () => {
  const rows = [{ strike: 100_000, call: leg({ gamma: 0.0001, oi: 2000 }), put: null }];
  const btc = gexByStrike(chain(rows))!;
  const eth = gexByStrike(chain(rows, { contract_value: 0.01 }))!;
  assert.equal(eth[0]!.net, btc[0]!.net * 10);
});

// --- ours, never Delta's --------------------------------------------------------

check("A LEG WITH NO COMPUTED GAMMA CONTRIBUTES NOTHING, AND DELTA'S IS NOT USED", () => {
  const bars = gexByStrike(
    chain([
      // Delta published a gamma for this strike and our solver declined it. The bar is
      // absent, not 999 x OI.
      { strike: 99_000, call: leg({ gamma: null, oi: 5000, venueGamma: 0.5 }), put: null },
      { strike: 100_000, call: leg({ gamma: 0.0001, oi: 2000 }), put: null },
    ]),
  );
  assert.ok(bars);
  assert.equal(bars[0]!.call, null);
  assert.equal(bars[0]!.net, 0);
  assert.equal(bars[1]!.call, 20_000);
  assert.equal(total(bars), 20_000, "and the board's total is only what we solved");
});

check("A STRIKE WITH NO OPEN INTEREST FIGURE CONTRIBUTES NOTHING EITHER", () => {
  const bars = gexByStrike(chain([{ strike: 100_000, call: leg({ gamma: 0.0001, oi: null }), put: null }]));
  assert.ok(bars);
  assert.equal(bars[0]!.call, null);
});

check("ZERO OPEN INTEREST IS A REAL ZERO, NOT AN ABSENCE", () => {
  const bars = gexByStrike(chain([{ strike: 100_000, call: leg({ gamma: 0.0001, oi: 0 }), put: null }]));
  assert.ok(bars);
  assert.equal(bars[0]!.call, 0, "a listed strike nobody holds is 0, and 0 is not null");
});

// --- a missing multiplier refuses the board -------------------------------------

check("NO SPOT, NO DOLLARS: the whole board is refused rather than drawn in no units", () => {
  const rows = [{ strike: 100_000, call: leg({ gamma: 0.0001, oi: 2000 }), put: null }];
  assert.equal(gexByStrike(chain(rows, { spot: null })), null);
  assert.equal(gexByStrike(chain(rows, { contract_value: null })), null);
  assert.ok(gexByStrike(chain(rows)), "and with both it draws");
});

check("OPEN INTEREST NEEDS NEITHER: a count is a count", () => {
  const bars = oiByStrike(
    chain([{ strike: 100_000, call: leg({ gamma: null, oi: 40 }), put: leg({ gamma: null, oi: 100 }) }], {
      spot: null,
      contract_value: null,
    }),
  );
  assert.equal(bars[0]!.call, 40);
  assert.equal(bars[0]!.put, 100);
  assert.equal(bars[0]!.net, -60);
});

check("OPEN INTEREST IS `oi`, AND `oi_value_usd` IS NEVER READ", () => {
  // Every leg above carries oi_value_usd = 123,456,789. If any of these figures were
  // built from it the numbers would be astronomical rather than the counts asserted.
  const bars = oiByStrike(chain([{ strike: 100_000, call: leg({ gamma: null, oi: 7 }), put: null }]));
  assert.equal(bars[0]!.call, 7);
  assert.equal(total(bars), 7);
});

// --- what the screen has to say out loud ----------------------------------------

check("COVERAGE COUNTS THE STRIKES THAT CARRY A COMPUTED GAMMA, NOT THE LEGS", () => {
  const response = chain([
    { strike: 99_000, call: leg({ gamma: null, oi: 1 }), put: leg({ gamma: null, oi: 1 }) },
    // One solved side is enough for the strike to have contributed something.
    { strike: 100_000, call: leg({ gamma: 0.0001, oi: 1 }), put: leg({ gamma: null, oi: 1 }) },
    { strike: 101_000, call: leg({ gamma: 0.0001, oi: 1 }), put: leg({ gamma: 0.0001, oi: 1 }) },
  ]);
  assert.deepEqual(gammaCoverage(response), { solved: 2, total: 3 });
});

check("THE RUNNING TOTAL, AND THE FIRST STRIKE WHERE IT CHANGES SIGN", () => {
  const bars = [
    { strike: 90_000, call: null, put: 100, net: -100 },
    { strike: 95_000, call: null, put: 50, net: -50 },
    { strike: 100_000, call: 400, put: null, net: 400 },
    { strike: 105_000, call: 10, put: null, net: 10 },
  ];
  assert.deepEqual(
    cumulative(bars).map((point) => point.total),
    [-100, -150, 250, 260],
  );
  assert.equal(zeroCrossStrike(bars), 100_000);
});

check("A BOARD THAT NEVER CHANGES SIGN HAS NO CROSSING, AND SAYS SO", () => {
  assert.equal(
    zeroCrossStrike([
      { strike: 90_000, call: 10, put: null, net: 10 },
      { strike: 95_000, call: 20, put: null, net: 20 },
    ]),
    null,
  );
  assert.equal(zeroCrossStrike([]), null, "and neither does an empty one");
});

check("THE PEAK IS THE LARGEST BAR EITHER SIDE, AND AN EMPTY BOARD PEAKS AT ZERO", () => {
  assert.equal(
    peak([
      { strike: 90_000, call: 10, put: 70, net: -60 },
      { strike: 95_000, call: 40, put: null, net: 40 },
    ]),
    70,
  );
  assert.equal(peak([]), 0, "never -Infinity, which would reach an axis attribute");
});


// --- many expiries, and the panels read off the board ----------------------------

/** A bar, written out. These tests are about the folds above, not about a chain. */
function bar(strike: number, call: number | null, put: number | null) {
  return { strike, call, put, net: (call ?? 0) - (put ?? 0) };
}

check("BOARDS SUM STRIKE BY STRIKE, OVER THE UNION OF THEIR STRIKES", () => {
  const summed = sumBoards([
    [bar(99_000, 10, 5), bar(100_000, 20, 8)],
    [bar(100_000, 3, 2), bar(101_000, 7, 1)],
  ]);
  assert.deepEqual(
    summed.map((b) => [b.strike, b.call, b.put, b.net]),
    [
      [99_000, 10, 5, 5],
      [100_000, 23, 10, 13],
      [101_000, 7, 1, 6],
    ],
  );
});

check("AN ABSENCE SUMMED WITH AN ABSENCE STAYS ABSENT, AND NEVER BECOMES ZERO", () => {
  const summed = sumBoards([[bar(100_000, null, null)], [bar(100_000, null, 4)]]);
  assert.equal(summed[0]!.call, null, "no expiry listed a call here");
  assert.equal(summed[0]!.put, 4, "one did list a put, and it stands alone");
});

check("SUMMING ONE BOARD IS THAT BOARD, AND SUMMING NONE IS EMPTY", () => {
  const one = [bar(100_000, 10, 5)];
  assert.deepEqual(sumBoards([one]), one);
  assert.deepEqual(sumBoards([]), []);
});

check("THE STRIKE WINDOW IS COUNTED IN LISTED STRIKES, CENTRED ON THE MONEY", () => {
  const board = [98, 99, 100, 101, 102].map((k) => bar(k * 1000, 1, 1));
  assert.deepEqual(
    strikeWindow(board, 100_000, 1).map((b) => b.strike),
    [99_000, 100_000, 101_000],
  );
  assert.equal(strikeWindow(board, 100_000, null).length, 5, "ALL is the whole board");
  assert.equal(strikeWindow(board, null, 1).length, 5, "nothing to centre on: show it all");
  assert.equal(strikeWindow(board, 100_000, 99).length, 5, "wider than the board is the board");
});

check("THE WINDOW CENTRES ON THE NEAREST STRIKE WHEN THE ATM ONE IS NOT ON THE BOARD", () => {
  // A summed board need not carry the ATM strike of any one expiry.
  const board = [98, 99, 101, 102].map((k) => bar(k * 1000, 1, 1));
  assert.deepEqual(
    strikeWindow(board, 100_000, 1).map((b) => b.strike),
    [98_000, 99_000, 101_000],
  );
});

check("THE TOTALS AND THE PUT/CALL RATIO, PUTS OVER CALLS", () => {
  const board = [bar(99_000, 10, 20), bar(100_000, 30, null)];
  assert.deepEqual(sideTotals(board), { call: 40, put: 20, pcr: 0.5 });
  assert.equal(
    sideTotals([bar(99_000, 0, 5)]).pcr,
    null,
    "a ratio with nothing underneath is not a large ratio",
  );
  assert.deepEqual(sideTotals([]), { call: 0, put: 0, pcr: null });
});

check("THE WALL IS THE LARGEST BAR ON ITS OWN SIDE", () => {
  const board = [bar(99_000, 10, 40), bar(100_000, 70, 5)];
  assert.equal(peakStrike(board, "call"), 100_000);
  assert.equal(peakStrike(board, "put"), 99_000);
  assert.equal(peakStrike([bar(99_000, null, null)], "call"), null, "nothing there to be largest");
});

check("LEVELS RANK BY SIZE: POSITIVE IS RESISTANCE, NEGATIVE IS SUPPORT", () => {
  const board = [
    bar(23_500, 0, 320),   // net -320
    bar(23_700, 0, 218),   // net -218
    bar(24_000, 712, 0),   // net +712
    bar(24_200, 560, 0),   // net +560
    bar(24_400, 0, 0),     // net 0 — neither, and in neither list
  ];
  const { resistances, supports } = levels(board, 4);
  assert.deepEqual(
    resistances.map((l) => [l.rank, l.strike, l.value]),
    [
      [1, 24_000, 712],
      [2, 24_200, 560],
    ],
  );
  assert.deepEqual(
    supports.map((l) => [l.rank, l.strike, l.value]),
    [
      [1, 23_500, -320],
      [2, 23_700, -218],
    ],
    "supports rank by magnitude, so the most negative is S1",
  );
});

check("LEVELS ARE NOT FILTERED BY WHERE SPOT HAPPENS TO BE STANDING", () => {
  // A large positive strike below the money is still where the gamma is.
  const board = [bar(20_000, 900, 0), bar(24_000, 100, 0)];
  assert.equal(levels(board, 4).resistances[0]!.strike, 20_000);
});

check("DTE COUNTS TO THE 12:00 UTC SETTLEMENT, NOT TO MIDNIGHT, AND NEVER GOES NEGATIVE", () => {
  // 13-04-2026 settles at 12:00Z. From 10-04 09:15 IST (03:45Z) that is 3 days and 8¼
  // hours, which is 3D — the reference terminal's own reading of the same pair.
  assert.equal(daysToExpiry("13-04-2026", new Date("2026-04-10T03:45:00Z")), 3);
  assert.equal(
    daysToExpiry("13-04-2026", new Date("2026-04-13T06:00:00Z")),
    0,
    "expiry morning is 0D, not 1D: half a trading day is not a day",
  );
  assert.equal(
    daysToExpiry("13-04-2026", new Date("2026-04-20T00:00:00Z")),
    0,
    "already settled reads 0D, never a negative wearing a future date",
  );
  assert.equal(daysToExpiry("not-a-date", new Date()), null);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
