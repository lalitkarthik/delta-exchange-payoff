/**
 * P6: the analyse tab stops being a printout.
 *
 * Two seams, the ones `ladder-fingerprint.test.tsx` established for the chain's own
 * buttons: **the pure edit** (`lib/leg-edit.ts` — what a typed character makes of a
 * strategy) and **a DOM fingerprint** of the screen that carries it, rendered through
 * `renderToStaticMarkup` with no browser, no network and no clock.
 *
 * The claims that matter:
 *
 *   - **An edit round-trips through the address bar.** Editing a quantity and pasting
 *     the link into a fresh tab has to reproduce the edit, so the edit and
 *     `lib/legs-url.ts` are asserted together rather than separately.
 *   - **A rejected edit returns the array it was given.** Not an equal copy — the same
 *     one. The screen re-asks `POST /analyse` whenever the legs change identity, so a
 *     half-typed `"1e"` producing a fresh array would fire a request per keystroke for
 *     a strategy nobody changed.
 *   - **The editor survives a refusal.** A leg with no quote on its side refuses the
 *     whole analysis (`docs/payoff-contract.md` §Refusals, the last row), and typing a
 *     price is the only thing that recovers it. An error message that replaced the
 *     inputs would leave the trader with the question and no way to answer it.
 */
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import AnalyseView from "@/components/AnalyseView";
import {
  appendLeg,
  toggleDirection,
  commitEntryPrice,
  commitInstrument,
  commitQuantity,
  removeLeg,
  withEntryPrice,
  withInstrument,
  withQuantity,
} from "@/lib/leg-edit";
import { decodeLegs, encodeLegs, syncedAnalyseHref } from "@/lib/legs-url";
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

const CALL = "DELTA-BTC-20260904-77000-C-USD";
const PUT = "DELTA-BTC-20260904-74000-P-USD";

function one(): LegRequest[] {
  return [{ instrument: CALL, direction: 1, quantity: 1 }];
}

console.log("leg-edit / withQuantity, withEntryPrice, withInstrument, removeLeg");

check("EDITING A QUANTITY: two of these, and the rest of the leg untouched", () => {
  assert.deepEqual(withQuantity(one(), 0, "2"), [
    { instrument: CALL, direction: 1, quantity: 2 },
  ]);
});

check("an edited quantity survives the address bar into a fresh tab", () => {
  // The whole acceptance criterion, end to end and without a browser: edit, encode,
  // and read it back the way a pasted link is read.
  const edited = withQuantity(one(), 0, "3");
  assert.deepEqual(decodeLegs(encodeLegs(edited)), edited);
  assert.equal(encodeLegs(edited), `${CALL}:B3`);
});

check("a quantity that is not a positive integer leaves the strategy alone", () => {
  const legs = one();
  for (const typed of ["", " ", "0", "-1", "1.5", "two", "1e"]) {
    assert.equal(withQuantity(legs, 0, typed), legs, `"${typed}" changed the strategy`);
  }
});

check("EDITING A PRICE: what if I were filled at 900", () => {
  assert.deepEqual(withEntryPrice(one(), 0, "900"), [
    { instrument: CALL, direction: 1, quantity: 1, entry_price: 900 },
  ]);
});

check("clearing the price hands it back to the book, rather than pinning it to zero", () => {
  // Absent is not zero: absent means the engine crosses the spread — buy at the ask,
  // sell at the bid — and the field must have a way back to that.
  const priced = withEntryPrice(one(), 0, "900");
  const cleared = withEntryPrice(priced, 0, "");
  assert.deepEqual(cleared, one());
  assert.equal("entry_price" in cleared[0]!, false, "absent, not null and not 0");
  assert.equal(encodeLegs(cleared), `${CALL}:B1`, "and no @ in the link either");
});

check("a price of zero is a real zero and is kept", () => {
  assert.equal(withEntryPrice(one(), 0, "0")[0]!.entry_price, 0);
});

check("a price that is not a finite number, or is negative, leaves the strategy alone", () => {
  const legs = one();
  for (const typed of ["-1", "abc", "1.2.3", "Infinity"]) {
    assert.equal(withEntryPrice(legs, 0, typed), legs, `"${typed}" changed the strategy`);
  }
});

check("EDITING A CONTRACT: a different strike, and the quantity and price ride along", () => {
  const priced = withEntryPrice(withQuantity(one(), 0, "2"), 0, "900");
  assert.deepEqual(withInstrument(priced, 0, PUT), [
    { instrument: PUT, direction: 1, quantity: 2, entry_price: 900 },
  ]);
});

check("a contract that is not a canonical string leaves the strategy alone", () => {
  const legs = one();
  for (const typed of ["", "77000C", "DELTA-BTC-20260904-77000-C", "banana"]) {
    assert.equal(withInstrument(legs, 0, typed), legs, `"${typed}" changed the strategy`);
  }
});

check("REMOVING: the named leg goes and the others keep their order", () => {
  const two: LegRequest[] = [
    { instrument: CALL, direction: 1, quantity: 1 },
    { instrument: PUT, direction: -1, quantity: 2 },
  ];
  assert.deepEqual(removeLeg(two, 0), [{ instrument: PUT, direction: -1, quantity: 2 }]);
  assert.deepEqual(removeLeg(two, 1), [{ instrument: CALL, direction: 1, quantity: 1 }]);
  assert.deepEqual(removeLeg(two, 5), two);
  assert.deepEqual(removeLeg(one(), 0), [], "and the last one can go too");
});

check("an index nobody holds is not an edit", () => {
  const legs = one();
  assert.equal(withQuantity(legs, 3, "2"), legs);
  assert.equal(withEntryPrice(legs, -1, "900"), legs);
  assert.equal(withInstrument(legs, 9, PUT), legs);
});

check("AN ACCEPTED EDIT THAT CHANGES NOTHING IS NOT AN EDIT — fix round 1, minor 3", () => {
  // Identity is the signal: a fresh array restarts the subscription, blanks the chart to
  // "Analysing…" and fires a request for a strategy nobody changed. Tabbing out of a
  // field, or re-committing "2" as "02", must not do that.
  const legs = withEntryPrice(withQuantity(one(), 0, "2"), 0, "900");
  assert.equal(withQuantity(legs, 0, "2"), legs, "the same quantity");
  assert.equal(withQuantity(legs, 0, "02"), legs, "the same quantity, spelled differently");
  assert.equal(withEntryPrice(legs, 0, "900"), legs, "the same price");
  assert.equal(withEntryPrice(legs, 0, " 900 "), legs, "the same price, with the spaces");
  assert.equal(withInstrument(legs, 0, CALL), legs, "the same contract");
  const bare = one();
  assert.equal(withEntryPrice(bare, 0, ""), bare, "clearing a price nobody ever set");
});

check("a quantity is decimal digits, not everything Number will read — fix round 1, minor 7", () => {
  // `Number` reads "1e3" as 1000 and "0x10" as 16, both positive integers. The URL codec
  // spells a quantity `(\d+)` and nothing else, so a quantity this accepted but that
  // could not survive a round trip would be a strategy the link cannot carry.
  const legs = one();
  for (const typed of ["1e3", "0x10", "+2", "2.0"]) {
    assert.equal(withQuantity(legs, 0, typed), legs, `"${typed}" was read as a quantity`);
  }
  assert.equal(withQuantity(legs, 0, " 2 ")[0]!.quantity, 2, "surrounding space is still fine");
});

console.log("\nleg-edit / commit* — what the field must show once the edit has been judged");

check("A REJECTED EDIT DOES NOT STAY IN THE FIELD: the value in force comes back", () => {
  // The inputs are uncontrolled, so nothing re-renders them when an edit is refused and
  // the DOM keeps the typed text — a field reading 0 beside metrics, a curve and a URL
  // that all describe a quantity of 1. `text` is what the field must be re-seeded with.
  const held = one();
  const rejected = commitQuantity(held, 0, "0");
  assert.equal(rejected.legs, held, "the strategy is untouched, and identically so");
  assert.equal(rejected.text, "1", "and the field shows the quantity in force, not the 0");

  const priced = withEntryPrice(one(), 0, "900");
  const badPrice = commitEntryPrice(priced, 0, "1.2.3");
  assert.deepEqual(badPrice.legs, priced);
  assert.equal(badPrice.text, "900");

  const badContract = commitInstrument(one(), 0, "banana");
  assert.deepEqual(badContract.legs, one());
  assert.equal(badContract.text, CALL);
});

check("an accepted edit asks for no re-seed — the field already says what the leg says", () => {
  const good = commitQuantity(one(), 0, "2");
  assert.equal(good.legs[0]!.quantity, 2);
  assert.equal(good.text, "2", "identical to what was typed, so nothing is re-seeded");

  const cleared = commitEntryPrice(withEntryPrice(one(), 0, "900"), 0, "");
  assert.equal("entry_price" in cleared.legs[0]!, false);
  assert.equal(cleared.text, "", "an empty price field stays empty");

  const moved = commitInstrument(one(), 0, PUT);
  assert.equal(moved.legs[0]!.instrument, PUT);
  assert.equal(moved.text, PUT);
});

console.log("\nlegs-url / syncedAnalyseHref — the address bar never eats a malformed link");

check("A MALFORMED LINK IS NEVER OVERWRITTEN: the text needed to fix it stays in the bar", () => {
  // The whole reason `decodeLegs` throws rather than dropping a fragment is that the
  // reader is told which part was wrong — and the part that was wrong is in their
  // address bar. A rewrite 200 ms later replaces it with `?legs=`, leaves the notice on
  // screen describing text nobody can see any more, and turns a reload into "this link
  // names no legs". The same defect was caught on the chain screen in P4.
  assert.equal(
    syncedAnalyseHref([], null, 'cannot read "DELTA-BTC:X" as a leg: bad quantity'),
    null,
    "no rewrite at all while a parse failure is on screen",
  );
  assert.equal(
    syncedAnalyseHref(one(), null, 'cannot read "..." as a leg: bad quantity'),
    null,
    "not even when some legs did decode — the link is still the reader's only copy",
  );
  assert.equal(syncedAnalyseHref(one(), null, null), `/analyse?legs=${CALL}:B1`);
  assert.equal(
    syncedAnalyseHref(one(), "2026-09-04T09:21:00Z", null),
    `/analyse?legs=${CALL}:B1&minute=2026-09-04T09:21:00Z`,
    "and a stored tab keeps its minute in the link it writes",
  );
  assert.equal(syncedAnalyseHref([], null, null), "/analyse?legs=", "an emptied strategy is a fact");
});

console.log("\nAnalyseView — the editable screen, and the chip that says which tab it is");

/** The worked response of `docs/payoff-contract.md`, as `analyse.test.ts` copies it. */
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
        instrument: CALL,
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

check("every leg gets a quantity, a price and a contract to type in", () => {
  const html = renderToStaticMarkup(
    <AnalyseView {...IDLE} legs={withEntryPrice(one(), 0, "900")} analysis={worked()} />,
  );
  assert.match(html, new RegExp(`value="${CALL}"`), "the contract, as typed text");
  assert.match(html, /aria-label="Quantity"/);
  assert.match(html, /aria-label="Entry price"/);
  assert.match(html, /value="900"/, "the price that was typed, not the one from the book");
  assert.match(html, /aria-label="Remove leg"/);
});

check("A REFUSAL LEAVES THE INPUTS: an unquoted leg can still be given a price", () => {
  const html = renderToStaticMarkup(
    <AnalyseView
      {...IDLE}
      legs={one()}
      problem={`no ask for ${CALL} and no entry_price was supplied`}
    />,
  );
  assert.match(html, /no ask for/, "the refusal is on screen");
  assert.match(html, /aria-label="Entry price"/, "and so is the way to answer it");
});

check("THE AS-OF CHIP, LIVE: it says live, and when it last heard back", () => {
  const html = renderToStaticMarkup(
    <AnalyseView
      {...IDLE}
      legs={one()}
      analysis={worked()}
      minute={null}
      receivedAt="2026-09-04T09:21:07Z"
    />,
  );
  assert.match(html, />live(<!-- -->)? \u00b7 updated 09:21:07</, "the chip's own text");
});

check("THE AS-OF CHIP, STORED: it names the minute and says it will not re-ask", () => {
  const html = renderToStaticMarkup(
    <AnalyseView
      {...IDLE}
      legs={one()}
      analysis={worked()}
      minute="2026-09-04T09:21:00Z"
      receivedAt="2026-09-04T09:30:00Z"
    />,
  );
  assert.match(html, /stored minute/);
  assert.match(html, /2026-09-04 09:21:00/, "the minute it is pinned to");
  assert.doesNotMatch(html, /09:30:00/, "when we asked is not what it is as of");
  assert.doesNotMatch(html, />live</, "and it must never claim to be live");
});

check("REMOVING THE LAST LEG: a readable empty state, not a crash and not a stale curve", () => {
  const html = renderToStaticMarkup(<AnalyseView {...IDLE} legs={removeLeg(one(), 0)} />);
  assert.match(html, /no legs/i);
  assert.doesNotMatch(html, /<polyline/, "nothing left to draw");
  assert.doesNotMatch(html, /aria-label="Quantity"/, "and nothing left to edit");
});

check("THE DIRECTION TOGGLE: B becomes S, and the price of the old side does not follow", () => {
  const priced = withEntryPrice(one(), 0, "900");
  const flipped = toggleDirection(priced, 0);
  assert.equal(flipped[0]!.direction, -1);
  assert.equal(flipped[0]!.entry_price, undefined, "that fill was on the other side of the spread");
  assert.equal(toggleDirection(flipped, 0)[0]!.direction, 1, "and back again");
  const legs = one();
  assert.equal(toggleDirection(legs, 7), legs, "no such leg, and the same array back");
});

check("ADDING A LEG: the last one copied at a single lot, with no price", () => {
  const added = appendLeg(withEntryPrice(one(), 0, "900"));
  assert.equal(added.length, 2);
  assert.equal(added[1]!.instrument, added[0]!.instrument);
  assert.equal(added[1]!.quantity, 1);
  assert.equal(added[1]!.entry_price, undefined);
  const empty: LegRequest[] = [];
  assert.equal(appendLeg(empty), empty, "nothing to copy, and the same array back");
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
