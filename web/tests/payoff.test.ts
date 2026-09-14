/**
 * The `/analyse` mirror's one piece of behaviour: the contract guard.
 *
 * `docs/payoff-contract.md` promises every decimal as a JSON number or `null`, and the
 * web app never calls `parseFloat`. A string arriving where a number belongs would not
 * throw on a chart — `"1240.0"` sorts and scales as text, so the line would draw in the
 * wrong place and the metrics under it would read as plausible figures. `assertPayoff`
 * is what turns that into a loud failure, and this is the test that it actually bites
 * rather than passing because it looked at nothing.
 *
 * The seam is the pure one, following `moneyness.test.ts`: no DOM, no fetch, no engine.
 */
import assert from "node:assert/strict";

import { ContractViolationError } from "@/lib/engine";
import { assertPayoff, type AnalyseResponse } from "@/lib/payoff";

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

/** `docs/payoff-contract.md`'s worked response, copied out of the document by hand. */
function worked(): AnalyseResponse {
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
        instrument: "DELTA-BTC-20260904-77000-C-USD",
        direction: 1,
        quantity: 1,
        entry_price: 1240.0,
        iv: 0.3712,
        greeks: { delta: 0.5231, gamma: 0.0000312, vega: 0.4118, theta: -66.58, rho: 0.129 },
      },
    ],
    total_greeks: { delta: 0.5231, gamma: 0.0000312, vega: 0.4118, theta: -66.58, rho: 0.129 },
    curve: {
      corners: [
        { price: 74000.0, pnl: -1240.0 },
        { price: 77000.0, pnl: -1240.0 },
        { price: 81000.0, pnl: 2760.0 },
      ],
      slope_left: 0.0,
      slope_right: 1.0,
      window: { low: 74000.0, high: 81000.0 },
    },
    metrics: {
      max_profit: null,
      max_loss: -1240.0,
      breakevens: [78240.0],
      net_premium: 1240.0,
      reward_risk: null,
    },
    table: [{ price: 74000.0, pnl: -1240.0 }],
  };
}

/** Reach into the response the way only a breaching engine could. */
function breached(mutate: (body: Record<string, unknown>) => void): AnalyseResponse {
  const body = worked() as unknown as Record<string, unknown>;
  mutate(body);
  return body as unknown as AnalyseResponse;
}

console.log("payoff / assertPayoff — the /analyse contract guard");

check("the document's own worked response passes untouched", () => {
  assertPayoff(worked());
});

check("a top-level decimal sent as a string is refused", () => {
  assert.throws(
    () => assertPayoff(breached((body) => (body.forward = "77609.4"))),
    ContractViolationError,
  );
});

check("a decimal nested inside a leg's Greeks is refused", () => {
  assert.throws(
    () =>
      assertPayoff(
        breached((body) => {
          const legs = body.legs as Record<string, unknown>[];
          (legs[0]!.greeks as Record<string, unknown>).delta = "0.5231";
        }),
      ),
    ContractViolationError,
  );
});

check("a decimal inside the curve's corners is refused", () => {
  assert.throws(
    () =>
      assertPayoff(
        breached((body) => {
          const curve = body.curve as Record<string, unknown>;
          (curve.corners as Record<string, unknown>[])[1]!.pnl = "-1240.0";
        }),
      ),
    ContractViolationError,
  );
});

check("a decimal inside the payoff table is refused", () => {
  assert.throws(
    () =>
      assertPayoff(
        breached((body) => {
          (body.table as Record<string, unknown>[])[0]!.price = "74000.0";
        }),
      ),
    ContractViolationError,
  );
});

check("the message names the offending field, so the breach can be found", () => {
  try {
    assertPayoff(breached((body) => (body.contract_value = "0.001")));
    assert.fail("expected a ContractViolationError");
  } catch (err) {
    assert.ok(err instanceof ContractViolationError, "wrong error type");
    assert.match(err.message, /contract_value/);
  }
});

check("a breakeven sent as a string is refused, list and all", () => {
  assert.throws(
    () =>
      assertPayoff(
        breached((body) => {
          const metrics = body.metrics as Record<string, unknown>;
          metrics.breakevens = ["78240.0"];
        }),
      ),
    ContractViolationError,
  );
});

check("null is not a violation — unbounded and absent are both legitimate", () => {
  assertPayoff(
    breached((body) => {
      body.spot = null;
      body.forward = null;
      body.discount = null;
    }),
  );
});

check("a string where a string belongs is not a violation", () => {
  assertPayoff(breached((body) => (body.expiry = "11-09-2026")));
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
