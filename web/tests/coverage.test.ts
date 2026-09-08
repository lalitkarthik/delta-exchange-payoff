/**
 * The guard on issue #53: a thin minute breaks the line correctly but used to say
 * nothing about how thin it was, and drew a never-recorded strike identically to one the
 * solver declined.
 *
 * `strikeGrid` builds the board from every strike the whole day mentions; `toRows` lays
 * one minute against it and fills the strikes that minute did not store with
 * `stored: false`. That is right — see `lib/smile.ts` — but a reader standing on a thin
 * minute got only floating dots and a paragraph of prose, with no count of what the
 * minute held and no way to tell "never recorded" from "solver declined" without
 * hovering every dot. `smileCoverage` is the count; `notStoredStrikes` and
 * `unsolvedStrikes` are the two sets that must never overlap.
 *
 * **The seam is the pure one**, following `moneyness.test.ts`'s precedent: `toRows` and
 * `smileCoverage` are plain functions in `lib/smile.ts`, no component, no DOM, and no
 * runner beyond `node:assert/strict` is warranted for arithmetic over an array.
 *
 * This suite has a history of tests that assert something guaranteed by the code's own
 * construction — see `#46`/`#47`/`#49` and the note on that in the project's tickets. The
 * checks below are built to fail if the logic breaks: the exact strike identities are
 * asserted, not just the counts, and the "no grid" case checks the count is what it is
 * *because* `toRows` marked every row `stored: true`, not because nothing was measured.
 * Red-green verified by hand: reverting `smileCoverage`'s `!row.stored` guard to
 * `row.stored` flips `notStored`/`stored` and fails four of the checks below; restoring
 * it passes them again.
 */
import assert from "node:assert/strict";

import type { SmileMinute, SmilePoint } from "@/lib/contract";
import { notStoredStrikes, smileCoverage, toRows, unsolvedStrikes } from "@/lib/smile";

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

function point(strike: number, iv: number | null, reason: string | null = null): SmilePoint {
  return { strike, iv, iv_leg: iv === null ? null : "call", iv_reason: reason };
}

function minute(points: SmilePoint[]): SmileMinute {
  return {
    minute: "2026-09-07T21:16:00Z",
    forward: 78_000,
    discount: 0.99,
    years_to_expiry: 0.01,
    forward_method: "F1",
    model_version: "v1",
    points,
  };
}

console.log("coverage — the #53 per-minute board coverage guard");

check("a minute that stores and solves every board strike is full coverage", () => {
  const grid = [77_800, 78_000, 78_200];
  const m = minute(grid.map((strike) => point(strike, 0.5)));
  const rows = toRows(m, grid);
  const coverage = smileCoverage(rows);

  assert.deepEqual(coverage, { total: 3, stored: 3, notStored: 0, declined: 0, solved: 3 });
  assert.deepEqual(notStoredStrikes(rows), []);
  assert.deepEqual(unsolvedStrikes(rows), []);
});

check("THE GUARD: a thin minute counts never-recorded and solver-declined separately", () => {
  // The board lists five strikes. This minute stored rows for three of them — two
  // solved, one the solver declined — and never wrote a row at all for the other two.
  const grid = [77_400, 77_600, 77_800, 78_000, 78_200];
  const m = minute([
    point(77_600, 0.51),
    point(77_800, null, "bid/ask crossed"),
    point(78_000, 0.49),
  ]);
  const rows = toRows(m, grid);
  const coverage = smileCoverage(rows);

  assert.deepEqual(coverage, { total: 5, stored: 3, notStored: 2, declined: 1, solved: 2 });

  // Exact identities, not just counts: a coverage count that added the wrong strikes to
  // the wrong bucket would still pass a bare count assertion.
  assert.deepEqual(notStoredStrikes(rows), [77_400, 78_200]);
  assert.deepEqual(unsolvedStrikes(rows), [77_800]);
});

check("THE GUARD: never-recorded and solver-declined never share a strike", () => {
  const grid = [77_400, 77_600, 77_800, 78_000, 78_200];
  const m = minute([point(77_600, 0.51), point(77_800, null, "bid/ask crossed"), point(78_000, 0.49)]);
  const rows = toRows(m, grid);

  const neverRecorded = new Set(notStoredStrikes(rows));
  const declined = new Set(unsolvedStrikes(rows));
  for (const strike of neverRecorded) assert.equal(declined.has(strike), false, `${strike}`);
  // Between them they account for exactly the board's gap: total - stored.
  const coverage = smileCoverage(rows);
  assert.equal(neverRecorded.size + declined.size, coverage.notStored + coverage.declined);
});

check("the ticket's own thin minute: 20 of 24 board strikes stored, all 20 solved", () => {
  // `measured`, BTC 08-09-2026 at 2026-09-07T21:16:00Z (issue #53): the board is 400
  // apart below 78,400 and 200 apart above; this minute stores all 20 of its own strikes
  // and none of them fail to solve — the 4 missing are strikes another minute recorded
  // that this one, from before #51, never listed at all.
  const below = [76_800, 77_200, 77_600, 78_000];
  const above = Array.from({ length: 16 }, (_, i) => 78_400 + i * 200);
  const stored = [...below, ...above];
  // Strikes only a later minute stored — none of them already in `stored` above.
  const extraOnBoard = [76_400, 76_600, 78_200, 81_600];
  const grid = [...new Set([...stored, ...extraOnBoard])].sort((a, b) => a - b);
  assert.equal(stored.length, 20, "fixture sanity: 20 stored strikes");
  assert.equal(grid.length, 24, "fixture sanity: 24-strike board");

  const m = minute(stored.map((strike) => point(strike, 0.4)));
  const rows = toRows(m, grid);
  const coverage = smileCoverage(rows);

  assert.equal(coverage.total, 24);
  assert.equal(coverage.stored, 20);
  assert.equal(coverage.notStored, 4);
  assert.equal(coverage.declined, 0);
  assert.equal(coverage.solved, 20);
});

check("without a grid, toRows marks every row stored — nothing is ever notStored", () => {
  const m = minute([point(78_000, 0.5), point(78_200, null, "no book")]);
  const rows = toRows(m); // no grid argument: the minute's own points, unfilled

  const coverage = smileCoverage(rows);
  assert.equal(coverage.total, 2);
  assert.equal(coverage.notStored, 0);
  assert.equal(coverage.stored, 2);
  assert.equal(coverage.declined, 1);
  assert.equal(coverage.solved, 1);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
