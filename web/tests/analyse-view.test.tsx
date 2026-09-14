/**
 * P5: the analyse screen as a whole — one analysis, or one reason there is not.
 *
 * `AnalyseView` is the screen with the fetching taken out of it: it is handed the state
 * and renders it, which is what makes the whole page reachable from
 * `renderToStaticMarkup` without a browser, a network or a clock. `AnalyseScreen` is the
 * client container that produces that state; the request it makes is pinned in
 * `analyse.test.ts` against a stubbed `fetch`.
 *
 * The two claims that matter most here are the ones a plausible-looking screen would
 * hide:
 *
 *   - **Both refusal envelopes render readably.** `docs/payoff-contract.md` §Refusals
 *     has seven semantic refusals arriving as a string and one schema refusal arriving
 *     as a list, and a screen that printed `[object Object]` at a trader — or crashed —
 *     would have told them nothing about a request the engine deliberately declined.
 *   - **The unit toggle is on by default and says which unit it is showing.** Money and
 *     Greeks together; `contract_value` is 0.001, so the difference between the two
 *     states is a factor of a thousand and nothing on an untagged column would say so.
 */
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import AnalyseView from "@/components/AnalyseView";
import { unitFactor, unitLabel } from "@/lib/format";
import type { AnalyseResponse, LegRequest } from "@/lib/payoff";

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

/** The short strangle, as `/analyse` would answer it. Hand-built from the same six
 * lines as `payoff-chart.test.ts`; see that file's header. */
function strangle(): AnalyseResponse {
  return {
    underlying: "BTC",
    expiry: "04-09-2026",
    as_of: "2026-09-04T09:21:00Z",
    spot: 77543.0,
    forward: 77609.4,
    discount: 0.99961,
    contract_value: 0.001,
    legs: [
      {
        instrument: "DELTA-BTC-20260904-74000-P-USD",
        direction: -1,
        quantity: 1,
        entry_price: 800,
        iv: 0.3712,
        greeks: { delta: 0.41, gamma: 0.00002, vega: -0.3, theta: 40, rho: -0.05 },
      },
      {
        instrument: "DELTA-BTC-20260904-80000-C-USD",
        direction: -1,
        quantity: 1,
        entry_price: 600,
        iv: 0.3489,
        greeks: { delta: -0.62, gamma: -0.00004, vega: -0.8, theta: 90, rho: -0.11 },
      },
    ],
    total_greeks: { delta: -0.21, gamma: -0.00002, vega: -1.1, theta: 130, rho: -0.16 },
    curve: {
      corners: [
        { price: 70000, pnl: -2600 },
        { price: 74000, pnl: 1400 },
        { price: 80000, pnl: 1400 },
        { price: 84000, pnl: -2600 },
      ],
      slope_left: 1,
      slope_right: -1,
      window: { low: 70000, high: 84000 },
    },
    metrics: {
      max_profit: 1400,
      max_loss: null,
      breakevens: [72600, 81400],
      net_premium: -1400,
      reward_risk: null,
    },
    table: [
      { price: 72000, pnl: -600 },
      { price: 77000, pnl: 1400 },
    ],
  };
}

/** The two legs of the strangle above, as the *request* they were built from — what the
 *  reader is editing, which is not the same object as the engine's echo of them. */
const STRANGLE_LEGS: LegRequest[] = [
  { instrument: "DELTA-BTC-20260904-74000-P-USD", direction: -1, quantity: 1 },
  { instrument: "DELTA-BTC-20260904-80000-C-USD", direction: -1, quantity: 1 },
];

const IDLE = {
  legs: [] as LegRequest[],
  analysis: null,
  problem: null,
  legsError: null,
  busy: false,
  minute: null,
  receivedAt: null,
  onLegsChange: () => {},
};

console.log("format / unitFactor and unitLabel — the toggle, as one rule");

check("on means per contract, which is contract_value, and the label says so", () => {
  assert.equal(unitFactor(true, 0.001), 0.001);
  assert.equal(unitLabel(true), "USD/contract");
});

check("off means per one unit of the underlying, which is no multiplier at all", () => {
  assert.equal(unitFactor(false, 0.001), 1);
  assert.equal(unitLabel(false), "USD");
});

check("ETH's lot size is the response's, never a constant of this app's", () => {
  // 0.01 for ETH, 0.001 for BTC — the engine echoes it and the screen multiplies.
  assert.equal(unitFactor(true, 0.01), 0.01);
});

console.log("\nAnalyseView — the whole screen, without a browser");

check("a semantic refusal is shown in full, and no chart is drawn beside it", () => {
  const html = renderToStaticMarkup(
    <AnalyseView
      {...IDLE}
      legs={STRANGLE_LEGS}
      problem="the legs span two expiries: 04-09-2026 and 11-09-2026"
    />,
  );
  assert.match(html, /span two expiries/);
  assert.match(html, /11-09-2026/);
  assert.doesNotMatch(html, /<polyline/, "a curve here would be the previous strategy's");
});

check("the schema refusal's sentence renders as a sentence, never as [object Object]", () => {
  // What `refusalDetail` makes of FastAPI's validation list — see `analyse.test.ts`.
  const html = renderToStaticMarkup(
    <AnalyseView
      {...IDLE}
      problem="body.legs: List should have at least 1 item after validation, not 0"
    />,
  );
  assert.match(html, /at least 1 item/);
  assert.doesNotMatch(html, /\[object Object\]/);
});

check("a malformed strategy link says which part was wrong, and starts nothing", () => {
  const html = renderToStaticMarkup(
    <AnalyseView {...IDLE} legsError={'cannot read "DELTA-BTC:X" as a leg: bad quantity'} />,
  );
  assert.match(html, /cannot read/);
  assert.match(html, /bad quantity/);
});

check("a link with no legs asks for some rather than drawing a flat line at zero", () => {
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} />);
  assert.match(html, /no legs/i);
  assert.doesNotMatch(html, /<polyline/);
});

check("an analysis draws the curve, the metrics and the three tabs", () => {
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} legs={STRANGLE_LEGS} analysis={strangle()} />);
  assert.match(html, /<polyline/, "the curve");
  assert.match(html, /unlimited/, "max loss, as a word");
  assert.equal([...html.matchAll(/role="tab"/g)].length, 3);
  assert.match(html, /aria-selected="true"[^>]*>P&amp;L/, "P&L is the tab it opens on");
});

check("THE DEFAULT: the toggle is on, so every money figure is per contract", () => {
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} legs={STRANGLE_LEGS} analysis={strangle()} />);
  assert.match(html, /USD\/contract/, "the unit is named where the figures are");
  // 1,400 per underlying is 1.40 per contract, and the per-underlying figure must not
  // be on screen at the same time claiming to be the same thing.
  assert.match(html, /1\.40/);
  assert.doesNotMatch(html, /1,400\.00/);
  assert.match(html, /checked=""|checked/, "the checkbox is checked");
});

check("the 1,000x note is on screen, because the ladder behind it disagrees", () => {
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} legs={STRANGLE_LEGS} analysis={strangle()} />);
  assert.match(html, /1,000/);
});

check("what it was priced against is shown — forward, discount, spot, and the minute", () => {
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} legs={STRANGLE_LEGS} analysis={strangle()} />);
  assert.match(html, /77,609\.40|77609\.4/, "the forward");
  assert.match(html, /77,543\.00|77543/, "spot");
  assert.match(html, /2026-09-04/, "the minute it is as of");
});

check("an unfitted chain still draws the curve — a P&L at expiry needs no model", () => {
  const unfitted = strangle();
  unfitted.forward = null;
  unfitted.discount = null;
  unfitted.total_greeks = null;
  unfitted.legs = unfitted.legs.map((leg) => ({ ...leg, iv: null, greeks: null }));
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} legs={STRANGLE_LEGS} analysis={unfitted} />);
  assert.match(html, /<polyline/);
  assert.doesNotMatch(html, /NaN|Infinity/);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
