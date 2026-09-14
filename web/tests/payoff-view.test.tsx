/**
 * P5: what the analyse screen actually puts on the page.
 *
 * Two seams, the same two `ladder-fingerprint.test.tsx` uses and for the same reasons:
 * the pure formatting rules as plain functions, and the components rendered with
 * `react-dom/server`'s `renderToStaticMarkup` — itself a pure function from an element
 * to an HTML string, so no browser, no jsdom, and nothing this workspace does not
 * already ship.
 *
 * **The worked strategy is the short strangle of `payoff-chart.test.ts`**, and every
 * expected figure below is derived from its six lines by hand rather than from the
 * component's own helpers:
 *
 *     Sell 1 × 74,000 put at 800     Sell 1 × 80,000 call at 600
 *     Credit 1,400 — so net_premium is −1,400, a credit received
 *     Max profit +1,400 between the strikes; max loss unbounded on both sides
 *     Breakevens 72,600 and 81,400
 *
 * With `contract_value = 0.001`, one contract of that is a credit of **$1.40** and a
 * maximum profit of **$1.40** — which is the whole reason the toggle exists.
 */
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import GreeksTable from "@/components/GreeksTable";
import MetricsPanel from "@/components/MetricsPanel";
import PayoffChart from "@/components/PayoffChart";
import PayoffTable from "@/components/PayoffTable";
import { formatBound, formatRatio, formatScaled } from "@/lib/format";
import type { AnalysedLeg, Curve, Metrics } from "@/lib/payoff";

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

console.log("format / the three rules this screen adds");

check("an unbounded max profit is the word, never an infinity and never a big number", () => {
  assert.equal(formatBound(null), "unlimited");
  assert.doesNotMatch(formatBound(null), /Infinity|∞|9999/);
});

check("a bounded one is a figure, and zero is a figure rather than an absence", () => {
  assert.equal(formatBound(1400), "1,400.00");
  // `max_loss` of 0 is a strategy that cannot lose; `null` is one that can lose
  // everything. They must not look alike.
  assert.equal(formatBound(0), "0.00");
});

check("a reward:risk against an unlimited side says so rather than printing a number", () => {
  assert.equal(formatRatio(null), "n/a");
  assert.equal(formatRatio(1.75), "1.75");
});

check("a per-contract Greek stays visibly nonzero instead of rounding to 0.00", () => {
  // Delta 0.5231 per unit is 0.0005231 per contract. Two decimals would print `0.00`
  // for the whole column — the same lie `formatIv` refuses to tell about a floored
  // volatility, and the reason `formatGamma` scales rather than truncates.
  assert.equal(formatScaled(0.0005231), "0.000523");
  assert.equal(formatScaled(-1400), "-1,400.00");
  // A real zero is a real zero.
  assert.equal(formatScaled(0), "0.00");
  assert.equal(formatScaled(null), "");
});

/** The short strangle's metrics, as the engine would publish them. */
const METRICS: Metrics = {
  max_profit: 1400,
  max_loss: null,
  breakevens: [72600, 81400],
  net_premium: -1400,
  reward_risk: null,
};

console.log("\nMetricsPanel — the five numbers, and the two that can be absent");

check("an unbounded max loss renders the word, and no infinity reaches the markup", () => {
  const html = renderToStaticMarkup(<MetricsPanel metrics={METRICS} factor={1} unit="USD" />);
  assert.match(html, /unlimited/);
  assert.doesNotMatch(html, /Infinity|NaN|∞/);
});

check("a credit says credit — net_premium negative is money received", () => {
  const html = renderToStaticMarkup(<MetricsPanel metrics={METRICS} factor={1} unit="USD" />);
  assert.match(html, /1,400\.00/);
  assert.match(html, /credit/);
  assert.doesNotMatch(html, /debit/);
});

check("a debit says debit, and the sign is not flipped in the reader's head", () => {
  const debit: Metrics = { ...METRICS, net_premium: 1240, max_loss: -1240 };
  const html = renderToStaticMarkup(<MetricsPanel metrics={debit} factor={1} unit="USD" />);
  assert.match(html, /debit/);
  // `max_loss` is a P&L on the same axis as the curve: negative, read straight off
  // the chart rather than sign-flipped.
  assert.match(html, /-1,240\.00/);
});

check("both breakevens are listed, ascending", () => {
  const html = renderToStaticMarkup(<MetricsPanel metrics={METRICS} factor={1} unit="USD" />);
  assert.ok(html.indexOf("72,600") < html.indexOf("81,400"), "ascending");
});

check("THE MULTIPLIER: per contract, 1,400 of credit is 1.40 of credit", () => {
  const html = renderToStaticMarkup(<MetricsPanel metrics={METRICS} factor={0.001} unit="USD/contract" />);
  assert.match(html, /1\.40/);
  assert.doesNotMatch(html, /1,400\.00/);
  assert.match(html, /USD\/contract/, "the unit says which");
});

check("a zero-cost structure is neither a credit nor a debit, and says so", () => {
  // `Math.abs` takes the sign off the number, so the word beside it is carrying the
  // whole distinction — and at exactly zero there is no distinction to carry. A costless
  // structure reading `0.00 debit` states something untrue about which way money moved.
  const costless: Metrics = { ...METRICS, net_premium: 0 };
  const html = renderToStaticMarkup(<MetricsPanel metrics={costless} factor={1} unit="USD" />);
  assert.doesNotMatch(html, /debit/);
  assert.doesNotMatch(html, /credit/);
  assert.match(html, />0\.00</);
});

console.log("\nGreeksTable — per leg, then the total, and the unit in the header");

/** The two legs of the strangle, already signed by direction and scaled by quantity
 * the way `docs/payoff-contract.md` says the engine publishes them. */
const LEGS: AnalysedLeg[] = [
  {
    instrument: "DELTA-BTC-20260904-74000-P-USD",
    direction: -1,
    quantity: 1,
    entry_price: 800,
    iv: 0.3712,
    greeks: { delta: 0.41, gamma: 0.0000312, vega: -0.3, theta: 40, rho: -0.05 },
  },
  {
    instrument: "DELTA-BTC-20260904-80000-C-USD",
    direction: -1,
    quantity: 2,
    entry_price: 600,
    iv: 0.3489,
    greeks: { delta: -0.62, gamma: -0.00004, vega: -0.8, theta: 90, rho: -0.11 },
  },
];

check("one row per leg in the order they were sent, with side and quantity", () => {
  const html = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={null} factor={1} unit="USD" />,
  );
  assert.ok(html.indexOf("74,000") < html.indexOf("80,000"), "in the order sent");
  assert.match(html, /2×/, "the quantity is shown, not folded into the Greek");
});

check("a leg with no volatility shows no Greeks — not five plausible zeros", () => {
  const unfitted: AnalysedLeg[] = [{ ...LEGS[0]!, iv: null, greeks: null }];
  const html = renderToStaticMarkup(
    <GreeksTable legs={unfitted} total={null} factor={1} unit="USD" />,
  );
  assert.doesNotMatch(html, /0\.00/, "a default sigma's Greeks would describe nothing");
});

check("the total is published only when the engine sent one", () => {
  const total = { delta: -0.21, gamma: -0.00002, vega: -1.1, theta: 130, rho: -0.16 };
  const withTotal = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={total} factor={1} unit="USD" />,
  );
  assert.match(withTotal, /Total/);
  const without = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={null} factor={1} unit="USD" />,
  );
  assert.doesNotMatch(without, /Total/);
});

check("THE MULTIPLIER: per contract a delta of 0.41 reads 0.00041, and the header says so", () => {
  const html = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={null} factor={0.001} unit="USD/contract" />,
  );
  assert.match(html, /0\.00041/, "a thousandth of the per-underlying figure");
  assert.match(html, /USD\/contract/);
});


check("gamma is scaled by ten thousand — the ladder's own convention, said in the header", () => {
  // `formatGamma`'s reason, unchanged: gamma is orders of magnitude smaller than the
  // other four, and a column of `0.00` claiming there is no convexity anywhere is the
  // same lie `formatIv` refuses to tell about a floored volatility. One convention for
  // one quantity across both screens, so a reader moving between them meets no surprise.
  const html = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={null} factor={1} unit="USD" />,
  );
  assert.match(html, /Γ ×10⁴/, "the header carries the scale");
  // 0.0000312 × 10,000 = 0.312 — and the ladder prints 0.31 for the same figure.
  assert.match(html, />0\.31</);
});

check("THE DEFAULT STATE: a per-contract gamma reads as a number, not as 3.12e-8", () => {
  const html = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={null} factor={0.001} unit="USD/contract" />,
  );
  // 0.0000312 × 0.001 × 10,000 = 0.000312. Without the ×10⁴ this was 3.12e-8, rendered
  // in scientific notation in the state the screen opens in.
  assert.match(html, />0\.000312</);
  assert.doesNotMatch(html, /e-\d/, "no scientific notation in a column of exposures");
});

check("the total's gamma is scaled the same way, or the row disagrees with the rows above it", () => {
  const total = { delta: -0.21, gamma: -0.0000456, vega: -1.1, theta: 130, rho: -0.16 };
  const html = renderToStaticMarkup(
    <GreeksTable legs={LEGS} total={total} factor={1} unit="USD" />,
  );
  // -0.0000456 × 10,000 = -0.456 → -0.46
  assert.match(html, />-0\.46</);
});

console.log("\nPayoffTable — the readable grid, on the same axis as the chart");

check("every row the engine sent is a row, with its P&L", () => {
  const html = renderToStaticMarkup(
    <PayoffTable
      table={[
        { price: 72000, pnl: -600 },
        { price: 77000, pnl: 1400 },
      ]}
      forward={77609.4}
      factor={1}
      unit="USD"
    />,
  );
  assert.match(html, /72,000/);
  assert.match(html, /-600\.00/);
  assert.match(html, /1,400\.00/);
});

check("THE MULTIPLIER: per contract the P&L column scales and the header names the unit", () => {
  const html = renderToStaticMarkup(
    <PayoffTable
      table={[{ price: 77000, pnl: 1400 }]}
      forward={77609.4}
      factor={0.001}
      unit="USD/contract"
    />,
  );
  assert.match(html, /1\.40/);
  assert.match(html, /USD\/contract/);
});

console.log("\nPayoffChart — corners, two rays, the forward and every breakeven");

/** The strangle again, framed as the engine frames it. */
const STRANGLE: Curve = {
  corners: [
    { price: 70000, pnl: -2600 },
    { price: 74000, pnl: 1400 },
    { price: 80000, pnl: 1400 },
    { price: 84000, pnl: -2600 },
  ],
  slope_left: 1,
  slope_right: -1,
  window: { low: 70000, high: 84000 },
};

check("the line is drawn through all four corners, at the coordinates worked by hand", () => {
  const html = renderToStaticMarkup(
    <PayoffChart curve={STRANGLE} forward={77609.4} breakevens={[72600, 81400]} factor={1} unit="USD" />,
  );
  // The gradient fill is a `<polygon>` drawn first; the line itself is the polyline.
  const points = /<polyline[^>]*points="([^"]+)"/.exec(html);
  assert.ok(points, "a polyline is drawn");
  /*
   * The exact coordinates, not a count of them — a count leaves the component's own
   * composition of span, vertical fit and frame unpinned, which is the half the pure
   * geometry tests cannot reach.
   *
   * Worked from the frame constants and the strangle's four corners:
   *   vertical span   P&L runs -2,600..1,400 and zero is already inside, so the
   *                   height is 4,000 and the 8% pad is 320: -2,920..1,720, range 4,640
   *   x per price     880 / 14,000 = 0.0628571    y per P&L   320 / 4,640 = 0.0689655
   *   74,000          4,000  x 0.0628571 = 251.43   +1,400 -> (1,720-1,400) x 0.0689655 = 22.07
   *   80,000          10,000 x 0.0628571 = 628.57   -2,600 -> (1,720+2,600) x 0.0689655 = 297.93
   */
  assert.equal(points![1], "0,297.93 251.43,22.07 628.57,22.07 880,297.93");
});

check("the forward and both breakevens are marked", () => {
  const html = renderToStaticMarkup(
    <PayoffChart curve={STRANGLE} forward={77609.4} breakevens={[72600, 81400]} factor={1} unit="USD" />,
  );
  assert.match(html, /class="payoff-forward"/);
  assert.equal([...html.matchAll(/class="payoff-breakeven"/g)].length, 2);
});

check("THE MULTIPLIER: the P&L axis carries the unit, the price axis never does", () => {
  const html = renderToStaticMarkup(
    <PayoffChart
      curve={STRANGLE}
      forward={77609.4}
      breakevens={[72600, 81400]}
      factor={0.001}
      unit="USD/contract"
    />,
  );
  assert.match(html, /USD\/contract/);
  /*
   * Two separate claims, and the alternation this replaced proved neither: it passed if
   * *either* an axis tick or a breakeven label happened to be unscaled.
   *
   * The price axis is untouched. `linearTicks(70,000, 84,000, 6)` steps by 2,500, so the
   * ticks are 70,000 / 72,500 / ... / 82,500 — and they are still those prices, not
   * 70.00 and 82.50.
   */
  assert.match(html, />70,000</, "the first price tick is a price");
  assert.match(html, />82,500</, "and so is the last");
  /*
   * The P&L axis *is* scaled. `linearTicks(-2,920, 1,720, 5)` steps by 1,000, so the
   * ticks are -2,000 / -1,000 / 0 / 1,000 per underlying — and -2.00 per contract.
   */
  assert.match(html, />-2\.00</, "the P&L axis carries the lot size");
  assert.doesNotMatch(html, />-2,000\.00</, "and does not carry both at once");
});

check("no non-finite number ever reaches an attribute", () => {
  const html = renderToStaticMarkup(
    <PayoffChart curve={STRANGLE} forward={null} breakevens={[]} factor={1} unit="USD" />,
  );
  assert.doesNotMatch(html, /NaN|Infinity/);
  assert.doesNotMatch(html, /class="payoff-forward"/, "no forward, no marker");
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
