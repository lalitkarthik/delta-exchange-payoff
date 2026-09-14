/**
 * P4: the ladder grows a B and an S beside every call and put.
 *
 * Two things this file has to prove, and they are different claims:
 *
 *   1. `lib/legs.ts` — `heldAt`, `addLeg`, `dropFirst` — is the pure logic the buttons
 *      read and call. No DOM in it, tested the way `moneyness.test.ts` tests
 *      `inTheMoney`: plain functions, plain assertions.
 *   2. **A DOM fingerprint of the ladder, before and after.** `ChainLadder` is rendered
 *      with `react-dom/server`'s `renderToStaticMarkup` — a pure function from a React
 *      element to an HTML string, no browser, no jsdom, nothing this workspace does not
 *      already have — against a small fixed `ChainResponse`. The fingerprint pins every
 *      existing cell's text *before* the buttons existed; adding two buttons per side
 *      must not move a single one of those assertions, or the ladder changed in more
 *      ways than the ticket asked for.
 *
 * **The seam is the pure one**, following `moneyness.test.ts`'s and `feed.test.ts`'s
 * precedent — no DOM test runner exists in this workspace and this ticket does not
 * warrant adding one. `renderToStaticMarkup` is the one piece of React machinery that
 * fits that rule: it is itself a pure function, so importing it costs nothing this repo
 * does not already ship (`react-dom` is a direct dependency).
 */
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { ChainLadder } from "@/components/ChainLadder";
import type { ChainResponse, ComputedLeg } from "@/lib/contract";
import { addLeg, dropFirst, heldAt } from "@/lib/legs";
import type { LegRequest } from "@/lib/payoff";

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

console.log("legs / heldAt, addLeg, dropFirst — the ladder's button logic");

const CALL = "DELTA-BTC-20260904-77000-C-USD";
const PUT = "DELTA-BTC-20260904-77000-P-USD";

check("an empty strategy holds nothing", () => {
  assert.equal(heldAt([], CALL, 1), false);
});

check("a leg is held only under its own instrument and direction", () => {
  const legs: LegRequest[] = [{ instrument: CALL, direction: 1, quantity: 1 }];
  assert.equal(heldAt(legs, CALL, 1), true);
  assert.equal(heldAt(legs, CALL, -1), false, "bought is not sold");
  assert.equal(heldAt(legs, PUT, 1), false, "a different instrument is not held");
});

check("addLeg appends a one-lot leg with no entry price", () => {
  const legs = addLeg([], CALL, 1);
  assert.deepEqual(legs, [{ instrument: CALL, direction: 1, quantity: 1 }]);
});

check("addLeg increments an existing matching leg's quantity, not a second leg — Important #1", () => {
  // Reviewed fix: a two-lot position built by clicking B twice must be ONE leg at
  // quantity 2, not two one-lot legs — a `legs=` link naming `…:B2` has to survive a
  // click-click-click-click round trip identically to one built the other way, and
  // #6's in-place quantity editing needs one leg per contract to edit.
  const once = addLeg([], CALL, 1);
  const twice = addLeg(once, CALL, 1);
  assert.equal(twice.length, 1, "one leg, not two");
  assert.equal(twice[0]!.quantity, 2);
});

check("addLeg does not increment a leg held the other way — bought and sold stay separate", () => {
  const bought = addLeg([], CALL, 1);
  const both = addLeg(bought, CALL, -1);
  assert.equal(both.length, 2);
  assert.equal(both[0]!.quantity, 1);
  assert.equal(both[1]!.quantity, 1);
});

check("dropFirst removes exactly one matching leg, not the whole position", () => {
  // Two separate one-lot legs sharing an instrument and direction can still arrive off
  // a decoded URL (`decodeLegs` never merges fragments), even though `addLeg` itself no
  // longer produces this shape — so `dropFirst` still has to handle it: remove the
  // first, leave the second lit.
  const twoLot = [
    { instrument: CALL, direction: 1 as const, quantity: 1 },
    { instrument: CALL, direction: 1 as const, quantity: 1 },
  ];
  const after = dropFirst(twoLot, CALL, 1);
  assert.equal(after.length, 1);
  assert.equal(heldAt(after, CALL, 1), true, "one lot is still held");
});

check("dropFirst decrements a quantity-2 leg by one lot rather than deleting it — Important #1", () => {
  // THE BUG: a `legs=…:B2` link decoded straight (never having been through `addLeg`)
  // used to lose both lots on one click, because `dropFirst` spliced the whole leg out
  // without ever consulting `quantity`. It must come off one lot at a time, same as the
  // sibling's `dropFirst` (`convex-hedge-payoff/web/lib/positions.ts:41-60`) — the
  // difference from that file is deliberate: the sibling can repair a wrongly-split
  // position from its Legs strip, and this ladder has no such strip, so `dropFirst`
  // itself has to be the thing that never loses more than one lot.
  const legs: LegRequest[] = [
    { instrument: CALL, direction: 1, quantity: 2 },
    { instrument: PUT, direction: -1, quantity: 1 },
  ];
  const after = dropFirst(legs, CALL, 1);
  assert.equal(after.length, 2, "the leg survives with one lot removed, not deleted outright");
  assert.equal(after[0]!.quantity, 1);
  assert.equal(after[1]!.instrument, PUT, "the other leg is untouched");
});

check("dropFirst removes a quantity-1 leg entirely — nothing left to decrement to", () => {
  const legs: LegRequest[] = [
    { instrument: CALL, direction: 1, quantity: 1 },
    { instrument: PUT, direction: -1, quantity: 1 },
  ];
  const after = dropFirst(legs, CALL, 1);
  assert.equal(after.length, 1);
  assert.equal(after[0]!.instrument, PUT);
});

check("the full click cycle: B, B, S, S returns to empty, by way of one 2x leg", () => {
  let legs = addLeg([], CALL, 1);
  legs = addLeg(legs, CALL, 1);
  assert.equal(legs.length, 1);
  assert.equal(legs[0]!.quantity, 2);
  legs = dropFirst(legs, CALL, 1);
  assert.equal(legs.length, 1);
  assert.equal(legs[0]!.quantity, 1, "one lot removed, one lot lit and held");
  assert.equal(heldAt(legs, CALL, 1), true);
  legs = dropFirst(legs, CALL, 1);
  assert.equal(legs.length, 0);
  assert.equal(heldAt(legs, CALL, 1), false);
});

check("dropFirst is a no-op when nothing matches", () => {
  const legs: LegRequest[] = [{ instrument: PUT, direction: -1, quantity: 1 }];
  assert.equal(dropFirst(legs, CALL, 1), legs);
});

console.log("\nChainLadder — a DOM fingerprint of the ladder, before and after the buttons");

function computed(overrides: Partial<ComputedLeg>): ComputedLeg {
  return {
    iv: 0.3712,
    iv_leg: "call",
    iv_reason: "",
    delta: 0.5231,
    gamma: 0.000312,
    vega: 0.3,
    theta: -5.5,
    rho: 0.12,
    ...overrides,
  };
}

/**
 * Two strikes: 77,000 with both sides quoted, so every quoted column carries a
 * distinct, recognisable value; 78,000 with **only a call** — the pre-existing
 * `leg === null` branch this ticket's `blankColumns`/`COLUMNS_PER_SIDE` edit touches,
 * exercised here because a wrong hatched-cell count there misaligns every column of
 * the table and the fingerprint exists specifically to catch that. Deliberately not
 * exported: this fixture exists for this file alone, the way `payoff.test.ts`'s
 * `worked()` exists for its own.
 */
function fixture(): ChainResponse {
  return {
    underlying: "BTC",
    expiry: "04-09-2026",
    quote_currency: "USD",
    contract_value: 0.001,
    spot: 77500,
    atm_strike: 77000,
    fetched_at: "2026-09-04T09:21:00Z",
    forward: 77600,
    discount: 0.9996,
    years_to_expiry: 0.01,
    forward_method: "F1",
    rows: [
      {
        strike: 77000,
        call: {
          symbol: "C-BTC-77000-040926",
          product_id: 1,
          bid: 1200,
          ask: 1250,
          mark: 1225,
          bid_iv: 0.37,
          ask_iv: 0.372,
          mark_iv: 0.371,
          delta: 0.52,
          gamma: 0.0003,
          theta: -5,
          vega: 0.3,
          rho: 0.12,
          oi: 100,
          oi_value_usd: null,
          oi_change_usd_6h: null,
          tick_size: 0.5,
          computed: computed({}),
        },
        put: {
          symbol: "P-BTC-77000-040926",
          product_id: 2,
          bid: 980.5,
          ask: 1005.25,
          mark: 992,
          bid_iv: 0.36,
          ask_iv: 0.362,
          mark_iv: 0.361,
          delta: -0.41,
          gamma: 0.00012,
          theta: -3.25,
          vega: 0.2,
          rho: -0.05,
          oi: 50,
          oi_value_usd: null,
          oi_change_usd_6h: null,
          tick_size: 0.5,
          computed: computed({
            iv: 0.3599,
            iv_leg: "put",
            delta: -0.4123,
            gamma: 0.000123,
            vega: 0.2,
            theta: -3.25,
            rho: -0.05,
          }),
        },
      },
      {
        strike: 78000,
        call: {
          symbol: "C-BTC-78000-040926",
          product_id: 3,
          bid: 700,
          ask: 730,
          mark: 715,
          bid_iv: 0.35,
          ask_iv: 0.352,
          mark_iv: 0.351,
          delta: 0.41,
          gamma: 0.00025,
          theta: -4.1,
          vega: 0.28,
          rho: 0.09,
          oi: 20,
          oi_value_usd: null,
          oi_change_usd_6h: null,
          tick_size: 0.5,
          computed: computed({
            iv: 0.353,
            iv_leg: "call",
            delta: 0.4102,
            gamma: 0.000251,
            vega: 0.28,
            theta: -4.1,
            rho: 0.09,
          }),
        },
        // No put listed at all — the hatched branch. See the fixture's own docstring
        // for why this row exists.
        put: null,
      },
    ],
  };
}

/**
 * The exact `renderToStaticMarkup` output of the ladder **before this ticket touched
 * it** — rendered from `git show 971ca4b:web/components/ChainLadder.tsx` (the commit
 * immediately before P4's own) against `fixture()`, captured with `JSON.stringify` to
 * survive the trip into this file byte-exact (a hand-retyped `&nbsp;` silently became a
 * plain space once already — see the fix report), then pasted here as a literal. Not
 * regenerated from the current component: that would make this test agree with
 * whatever `ChainLadder.tsx` does today by construction, which is the one thing a
 * fingerprint must not do.
 */
const BEFORE_HTML =
  "<div class=\"chain-wrap\"><table class=\"chain\"><caption class=\"sr-only\">BTC option chain expiring 04-09-2026, priced in USD. Calls on the left, strikes in the centre, puts on the right. The IV and delta columns are computed by this engine from the order book; the venue’s own figures are in each cell’s tooltip. A hatched cell means that side is not listed; an empty cell means the field could not be computed or was not priced; a zero means zero.</caption><thead><tr><th class=\"side-head side-call\" colSpan=\"9\">Calls</th><th class=\"side-head\">Strike</th><th class=\"side-head side-put\" colSpan=\"9\">Puts</th></tr><tr><th>OI</th><th title=\"Rho, per one percent. Computed here, not the venue’s.\">Rho</th><th title=\"Theta, one calendar day. Computed here, not the venue’s.\">Theta</th><th title=\"Vega, per volatility point. Computed here, not the venue’s.\">Vega</th><th title=\"Gamma, scaled by 10,000 so it is readable. Computed here.\">Gamma ×10⁴</th><th title=\"Delta, with respect to the forward. Computed here.\">Delta</th><th title=\"Implied volatility, solved from the out-of-the-money leg.\">IV</th><th title=\"Bid, in USD.\">Bid (USD)</th><th title=\"Ask, in USD.\">Ask (USD)</th><th style=\"text-align:center\">Strike</th><th title=\"Ask, in USD.\">Ask (USD)</th><th title=\"Bid, in USD.\">Bid (USD)</th><th title=\"Implied volatility, solved from the out-of-the-money leg.\">IV</th><th title=\"Delta, with respect to the forward. Computed here.\">Delta</th><th title=\"Gamma, scaled by 10,000 so it is readable. Computed here.\">Gamma ×10⁴</th><th title=\"Vega, per volatility point. Computed here, not the venue’s.\">Vega</th><th title=\"Theta, one calendar day. Computed here, not the venue’s.\">Theta</th><th title=\"Rho, per one percent. Computed here, not the venue’s.\">Rho</th><th>OI</th></tr></thead><tbody><tr class=\"at-the-money\"><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">100</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">0.12</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">-5.50</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">0.30</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">3.12</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">0.523</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">37.12%</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">1,200.00</td><td class=\"num itm\" title=\"C-BTC-77000-040926 · mark 1,225.00 · ours IV 37.12% (solved on the call) · Delta IV bid 37.00% · mark 37.10% · ask 37.20% · Delta Δ 0.520\">1,250.00</td><td class=\"strike\">77,000 ★</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">1,005.25</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">980.50</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">35.99%</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">-0.412</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">1.23</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">0.20</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">-3.25</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">-0.05</td><td class=\"num\" title=\"P-BTC-77000-040926 · mark 992.00 · ours IV 35.99% (solved on the put) · Delta IV bid 36.00% · mark 36.10% · ask 36.20% · Delta Δ -0.410\">50</td></tr><tr class=\"\"><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">20</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">0.09</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">-4.10</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">0.28</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">2.51</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">0.410</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">35.30%</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">700.00</td><td class=\"num\" title=\"C-BTC-78000-040926 · mark 715.00 · ours IV 35.30% (solved on the call) · Delta IV bid 35.00% · mark 35.10% · ask 35.20% · Delta Δ 0.410\">730.00</td><td class=\"strike\">78,000</td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td><td class=\"blank\" title=\"No put listed at this strike\"></td></tr></tbody></table></div>";

check("THE BEFORE PIN: the untouched ladder matches the literal captured pre-ticket", () => {
  const html = renderToStaticMarkup(<ChainLadder chain={fixture()} />);
  assert.equal(html, BEFORE_HTML);
});

/**
 * Undoes every trace of the one new column this ticket adds — the quoted branch's
 * `td.picks` cells, the hatched branch's `td.picks.blank` cells (`class="picks
 * blank"`, distinct from an ordinary hatched cell's bare `class="blank"` precisely so
 * this regex can tell the two apart and strip only the one P4 added), the two
 * `th.picks-head` header cells, and the `colSpan` each side's banner grew by one to
 * cover it — so what is left can be compared against `BEFORE_HTML` byte for byte.
 * Nothing else this ticket touches would match any of these four patterns, which is
 * what makes the comparison a fingerprint of everything *else*.
 */
function withoutPicks(html: string): string {
  return html
    .replace(/<td class="picks[^"]*"[^>]*>.*?<\/td>/g, "")
    .replace(/<th class="picks-head">Pick<\/th>/g, "")
    .replace(/colSpan="10"/g, 'colSpan="9"');
}

check("THE FINGERPRINT: with the two new buttons stripped out, the ladder is unchanged", () => {
  const html = renderToStaticMarkup(
    <ChainLadder chain={fixture()} legs={[]} onPick={() => {}} />,
  );
  assert.equal(withoutPicks(html), BEFORE_HTML);
});

check("no legs held renders six unlit buttons — none at all on the hatched put", () => {
  // Six, not four: the 77,000 row is both-sided (four buttons) and the 78,000 row is
  // call-only (two buttons, none on the hatched put — there is nothing to buy or sell
  // where nothing is listed).
  const html = renderToStaticMarkup(
    <ChainLadder chain={fixture()} legs={[]} onPick={() => {}} />,
  );
  const pressed = [...html.matchAll(/aria-pressed="(true|false)"/g)].map((m) => m[1]);
  assert.deepEqual(pressed, ["false", "false", "false", "false", "false", "false"]);
});

check('a held leg lights its button, aria-pressed="true", and only that one', () => {
  // Must match `canonicalInstrument("BTC", "04-09-2026", 77000, "call", "USD")` exactly
  // — `DELTA-BTC-20260904-77000-C-USD` — or this would pass for the wrong reason.
  const held: LegRequest[] = [
    { instrument: "DELTA-BTC-20260904-77000-C-USD", direction: 1, quantity: 1 },
  ];
  const html = renderToStaticMarkup(
    <ChainLadder chain={fixture()} legs={held} onPick={() => {}} />,
  );
  const pressed = [...html.matchAll(/aria-pressed="(true|false)"/g)].map((m) => m[1]);
  assert.equal(pressed.filter((p) => p === "true").length, 1);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
