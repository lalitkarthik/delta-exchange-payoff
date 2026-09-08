/**
 * The guard on issue #40: the ladder header's feed badge, and the one thing it must
 * never be confused with.
 *
 * `docs/live-chain-contract.md` adds a fourth websocket message, `feed`, carrying the
 * venue connection's own state — distinct from `LiveStatus`, which is this browser's
 * socket to *the engine*. The two fail independently (the ticket's own "what to
 * notice": the browser's socket can read `live` while the venue feed reads
 * `reconnecting`), and the failure mode this file is against is a badge built off the
 * wrong one of the two, or a badge that shows for `connected` when the whole point of
 * the badge is to say "this is not simply fine".
 *
 * **The seam is the pure one**, following `moneyness.test.ts`'s precedent: `feedBadge`
 * is a plain function from `FeedStatus | null` to what the header should render, with
 * no DOM and no socket in it, so what actually changed under #40 — a mapping from five
 * states to a shown-or-not label — can be pinned without a browser test runner this
 * repo does not have.
 *
 * **What this does *not* cover, stated plainly rather than left to be assumed:** that
 * `ChainScreen.tsx` calls `feedBadge(feedStatus)` and not `feedBadge` built off
 * `liveStatus`, and that it clears `feedStatus` when the subscription tears down. A
 * component that read the wrong variable, or read `LiveStatus`'s values coerced into a
 * `FeedState`-shaped object, could still pass every check in this file. That has to be
 * read off the component and, ultimately, watched in the browser — this file only pins
 * the one function both would have to go through.
 *
 * No runner, no new dependency: `node:assert/strict` and `bun tests/feed.test.ts`,
 * exactly as `following.test.ts` and `moneyness.test.ts` already run.
 */
import assert from "node:assert/strict";

import { feedBadge, type FeedState, type FeedStatus } from "@/lib/live";

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

function status(state: FeedState, reason = ""): FeedStatus {
  return { adapter: "DELTA", state, since: "2026-09-08T09:00:00Z", reason };
}

console.log("live / feedBadge — the #40 badge guard");

check("no feed message yet renders no badge", () => {
  assert.equal(feedBadge(null), null);
});

check("THE GUARD: a connected feed renders no badge", () => {
  // The whole point of the badge is to say "the venue feed is not simply fine". A
  // badge that showed for `connected` too would be noise on every normal page load.
  assert.equal(feedBadge(status("connected")), null);
});

check("THE GUARD: every other state renders a badge, none of them null", () => {
  // The inverse of the check above, and the one a careless edit to the exclusion list
  // would break in the opposite direction — e.g. excluding `stopped` instead of
  // `connected`, which would silence the loudest state this badge can show.
  const rest: FeedState[] = ["connecting", "degraded", "reconnecting", "stopped"];
  for (const state of rest) {
    const badge = feedBadge(status(state));
    assert.notEqual(badge, null, `${state} must render a badge`);
    assert.equal(badge?.state, state);
  }
});

check("the reason travels through unchanged, for the hover title", () => {
  const badge = feedBadge(status("degraded", "stale"));
  assert.equal(badge?.reason, "stale");
});

check("an empty reason travels through as an empty string, not a placeholder", () => {
  // `main.py` sends `""` when the controller's own reason was empty; inventing a
  // placeholder here would put words on screen the engine never said.
  const badge = feedBadge(status("stopped", ""));
  assert.equal(badge?.reason, "");
});

check("each state gets its own label, not one label reused across states", () => {
  const rest: FeedState[] = ["connecting", "degraded", "reconnecting", "stopped"];
  const labels = rest.map((state) => feedBadge(status(state))?.label);
  assert.equal(new Set(labels).size, labels.length, `labels collided: ${labels.join(", ")}`);
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
