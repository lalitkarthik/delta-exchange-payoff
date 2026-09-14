/**
 * `lib/legs-url.ts` — the Strategy's URL codec, pure in both directions.
 *
 * Two properties matter more than the spelling of the format. A leg that survives a
 * copy-paste into a fresh tab is the whole feature (P4's "reload and find the same leg
 * still selected"); and a malformed link must fail loudly, because the alternative is
 * dropping a leg silently and showing a trader a chart of a position they did not build.
 *
 * **The seam is the pure one**, following `payoff.test.ts`'s and `moneyness.test.ts`'s
 * precedent: no DOM, no fetch, no engine, `node:assert/strict` plus this repo's own
 * `check()` harness. `bun run test`.
 */
import assert from "node:assert/strict";

import type { LegRequest } from "@/lib/payoff";
import { LegsUrlError, decodeLegs, encodeLegs, syncedUrl } from "@/lib/legs-url";

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

console.log("legs-url / encodeLegs, decodeLegs — the Strategy's URL codec");

const CALL = "DELTA-BTC-20260904-77000-C-USD";
const PUT = "DELTA-BTC-20260904-76000-P-USD";

check("a single bought leg, quantity 1, no entry price, survives the round trip", () => {
  const legs: LegRequest[] = [{ instrument: CALL, direction: 1, quantity: 1 }];
  assert.deepEqual(decodeLegs(encodeLegs(legs)), legs);
});

check("a sold leg with quantity and an entry price survives the round trip", () => {
  const legs: LegRequest[] = [{ instrument: PUT, direction: -1, quantity: 2, entry_price: 326.7 }];
  assert.deepEqual(decodeLegs(encodeLegs(legs)), legs);
});

check("two legs survive, in order — the order the Greeks table is read in", () => {
  const legs: LegRequest[] = [
    { instrument: CALL, direction: 1, quantity: 1, entry_price: 1240.0 },
    { instrument: PUT, direction: -1, quantity: 1, entry_price: 326.7 },
  ];
  const decoded = decodeLegs(encodeLegs(legs));
  assert.deepEqual(decoded, legs);
  assert.equal(decoded[0]!.instrument, CALL);
});

check("an empty strategy encodes to an empty string and back", () => {
  assert.equal(encodeLegs([]), "");
  assert.deepEqual(decodeLegs(""), []);
  assert.deepEqual(decodeLegs(null), []);
  assert.deepEqual(decodeLegs(undefined), []);
});

check("B and S rather than 1 and -1, because a URL is read by people", () => {
  const legs: LegRequest[] = [{ instrument: CALL, direction: 1, quantity: 1 }];
  assert.equal(encodeLegs(legs), `${CALL}:B1`);
});

// Minor #5: `encodeLegs` used `${leg.entry_price}` — plain JS number stringification —
// which switches to exponential notation outside roughly 1e-6..1e21. The old decoder's
// price group, `\d+(?:\.\d+)?`, could not read that shape back, so the encoder could
// silently produce a fragment its own decoder rejected. Unreachable with a real USD
// premium on a 0.5 tick, but this module is what Tasks 5 and 6 import for editable
// prices, so the round trip has to hold for whatever a `number` can be.
const AWKWARD_PRICES = [1e-7, 1e21, 0.00000001, 123456789012345];

for (const price of AWKWARD_PRICES) {
  check(`an entry price of ${price} survives the round trip`, () => {
    const legs: LegRequest[] = [{ instrument: CALL, direction: 1, quantity: 1, entry_price: price }];
    const decoded = decodeLegs(encodeLegs(legs));
    assert.equal(decoded.length, 1);
    assert.equal(decoded[0]!.entry_price, price);
  });
}

// Minor #6: each case asserts the *distinguishing* text of its failure, not merely
// that some `LegsUrlError` was thrown — an instrument failure has to read differently
// from a direction failure, which has to read differently from a quantity failure, or
// a trader staring at "cannot read ... as a leg" has no way to know which of the six
// parts to go fix.
const MALFORMED: Array<[string, string, RegExp]> = [
  ["a missing ':' separator", "DELTA-BTC-20260904-77000-C-USDB1", /missing ':'/],
  [
    "a five-part pre-I1 instrument with no currency",
    "DELTA-BTC-20260904-77000-C:B1",
    /is not a canonical instrument string/,
  ],
  [
    "an instrument with an unknown right",
    "DELTA-BTC-20260904-77000-X-USD:B1",
    /is not a canonical instrument string/,
  ],
  ["a missing direction letter", `${CALL}:1`, /expected <B\|S><quantity>/],
  ["an unknown direction letter", `${CALL}:X1`, /expected <B\|S><quantity>/],
  ["a zero quantity", `${CALL}:B0`, /quantity must be a positive integer/],
  ["a negative quantity", `${CALL}:B-2`, /expected <B\|S><quantity>/],
  ["a fractional quantity", `${CALL}:B1.5`, /expected <B\|S><quantity>/],
  ["a truncated entry price", `${CALL}:B1@`, /expected <B\|S><quantity>/],
  ["a non-numeric entry price", `${CALL}:B1@abc`, /entry_price must be a finite number/],
  ["one good leg and one broken", `${CALL}:B1,garbage`, /missing ':'/],
];

for (const [name, encoded, expected] of MALFORMED) {
  check(`rejects ${name}`, () => {
    try {
      decodeLegs(encoded);
      assert.fail(`expected a LegsUrlError for ${JSON.stringify(encoded)}`);
    } catch (err) {
      assert.ok(err instanceof LegsUrlError, "wrong error type");
      assert.match(
        (err as LegsUrlError).message,
        expected,
        `expected ${expected} in "${(err as LegsUrlError).message}"`,
      );
    }
  });
}

check("the error names the fragment that failed, not just 'invalid'", () => {
  try {
    decodeLegs(`${CALL}:B1,garbage`);
    assert.fail("expected a LegsUrlError");
  } catch (err) {
    assert.ok(err instanceof LegsUrlError, "wrong error type");
    assert.match(err.message, /garbage/);
  }
});

check("never returns a partial strategy — one broken fragment, nothing decoded", () => {
  let decoded: unknown = "not assigned";
  try {
    decoded = decodeLegs(`${CALL}:B1,garbage`);
  } catch {
    /* expected */
  }
  assert.equal(decoded, "not assigned");
});

console.log("\nlegs-url / syncedUrl — the address bar never eats a malformed link");

// Follow-up to P4 (#5): the chain screen's own URL-sync effect built its query
// unconditionally — `chainQuery(...)`, no `legsError` in sight — so a malformed
// `?legs=` link was destroyed by the ~200ms settle timer before the reader could copy
// the broken text back out. `syncedAnalyseHref` already fixed this on the analyse
// screen (P6); `syncedUrl` is the general gate underneath it, so `ChainScreen`'s own
// query builder can be routed through the same rule rather than reinventing it.

check("THE BUG, REPRODUCED: a malformed link's query is still rewritten, unconditionally", () => {
  // This is exactly today's defect, expressed directly: a caller's own URL builder,
  // handed a parse failure, must come back `null` — not the built string — or the
  // 200ms settle timer erases the text the reader needs to fix their link.
  const query = syncedUrl(
    'cannot read "DELTA-BTC:X" as a leg: bad quantity',
    () => "?underlying=BTC&expiry=04-09-2026",
  );
  assert.equal(query, null, "no rewrite at all while a parse failure is on screen");
});

check("no legsError: the built query is returned unchanged", () => {
  const query = syncedUrl(null, () => "?underlying=BTC&expiry=04-09-2026");
  assert.equal(query, "?underlying=BTC&expiry=04-09-2026");
});

check("build is never called while a parse failure is on screen", () => {
  let calls = 0;
  syncedUrl("cannot read ... as a leg: bad quantity", () => {
    calls++;
    return "?should-not-be-built";
  });
  assert.equal(calls, 0, "the caller's URL builder must not even run");
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
