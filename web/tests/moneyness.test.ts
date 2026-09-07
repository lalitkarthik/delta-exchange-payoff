/**
 * The guard on issue #47: a null spot must not wash the ladder.
 *
 * `ChainResponse.spot` is reachable as `null` on the historical route — a minute with
 * nothing in `spot-bars` legitimately has none, `docs/historical-chain-contract.md`.
 * `ChainLadder.tsx`'s `inTheMoney` used to type `spot` as a plain `number` and compare
 * `strike < spot` / `strike > spot` directly. JavaScript coerces a `null` spot to `0`,
 * so every put strike (all positive) read `strike > 0` as true and every call strike
 * read `strike < 0` as false: every put washed in-the-money, no call ever did — the
 * ladder rendering backwards the ticket describes, silently, with nothing on screen to
 * say the comparison was meaningless.
 *
 * **The seam is the pure one**, following `following.test.ts`'s precedent: no DOM test
 * runner exists here and none is warranted for one comparison function. `inTheMoney` is
 * exported from `ChainLadder.tsx` for exactly this — importing the component module at
 * the top level touches no DOM (`useEffect`/`useRef` are only invoked when the
 * component itself renders), so a plain `node:assert` check on the function is honest
 * and complete for what broke: the direction of the wash, not the render.
 *
 * No highlight is the fix's answer to "what does the ladder show when it does not know
 * where the money is" — a highlight at zero is not honest, so `spot === null` must
 * return `false` on both sides, for every strike, including a strike of exactly `0`.
 */
import assert from "node:assert/strict";

import { inTheMoney } from "@/components/ChainLadder";

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

console.log("moneyness / inTheMoney — the #47 null-spot guard");

check("THE GUARD: a null spot is never in the money on the put side", () => {
  // The exact failure mode: `strike > null` coerces to `strike > 0`, true for every
  // positive strike, washing every put in-the-money with nothing to say why.
  assert.equal(inTheMoney(78000, null, "put"), false);
});

check("THE GUARD: a null spot is never in the money on the call side", () => {
  assert.equal(inTheMoney(1, null, "call"), false);
});

check("THE GUARD: holds at every strike, not just the obvious ones", () => {
  for (const strike of [-1, 0, 1, 77_500, 1_000_000]) {
    assert.equal(inTheMoney(strike, null, "call"), false, `call @ ${strike}`);
    assert.equal(inTheMoney(strike, null, "put"), false, `put @ ${strike}`);
  }
});

check("a real spot still shades a call below it", () => {
  assert.equal(inTheMoney(77000, 77500, "call"), true);
  assert.equal(inTheMoney(78000, 77500, "call"), false);
});

check("a real spot still shades a put above it", () => {
  assert.equal(inTheMoney(78000, 77500, "put"), true);
  assert.equal(inTheMoney(77000, 77500, "put"), false);
});

check("a strike exactly on spot is in the money on neither side", () => {
  assert.equal(inTheMoney(77500, 77500, "call"), false);
  assert.equal(inTheMoney(77500, 77500, "put"), false);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
