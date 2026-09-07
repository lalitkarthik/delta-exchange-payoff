/**
 * The guard on issue #49: a screen that has never been sent a live push must still be
 * allowed to ask for one.
 *
 * **This file exists because the bug it pins was invisible to every check this repo
 * had.** `tsc` and `next build` both pass on a deadlock — a circular data dependency is
 * well-typed — and the engine's pytest suite cannot see a React effect at all. The bug
 * shipped in `bca676f` and left the app's primary screen showing "Connecting to the
 * engine…" for ever on any load without a `minute=` in the URL. Nothing failed. That is
 * the gap this closes.
 *
 * **The seam is the pure one, deliberately.** There is no browser test runner here and
 * #49 did not ask for one; adding React Testing Library, a DOM and a bundler config to
 * assert on a `useEffect` would be a larger change than the fix. What can be pinned
 * honestly without any of that is the predicate the effect is gated on, and that is
 * where the bug actually lived: `positionOf` derived only `onLive`, the screen gated the
 * subscription on it, and `onLive` cannot be true before a push has arrived. So the
 * assertions below are about `following` versus `onLive` on timelines in the states a
 * real session passes through. They would all have failed on the old shape.
 *
 * What this does *not* cover, said plainly rather than left to be assumed: that
 * `ChainScreen` still reads `following` and not `onLive`. A rename or a careless revert
 * in the component would pass this file. The browser observation on the ticket is what
 * covers that, and it has to be redone if the subscription effect is touched.
 *
 * No runner and no dependency: `node:assert/strict` is already typed by the `@types/node`
 * this project installs, and bun runs a TypeScript file directly. `bun run test`.
 */
import assert from "node:assert/strict";

import { chainTimeline, withLiveEdge } from "@/lib/chainTimeline";
import { positionOf } from "@/lib/position";
import { EMPTY_TIMELINE } from "@/lib/timeline";

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

/** Three consecutive stored minutes — a day the store has sealed and the stream has not
 * yet spoken about, which is precisely the state every fresh load starts in. */
const STORED = ["2026-09-07T12:00:00Z", "2026-09-07T12:01:00Z", "2026-09-07T12:02:00Z"];

console.log("timeline / following — the #49 deadlock guard");

check("a stored day with no live push has no live position", () => {
  const timeline = withLiveEdge(chainTimeline(STORED), null);
  assert.equal(timeline.liveIndex, -1, "liveIndex must stay -1 until a push arrives");
});

check("THE GUARD: a timeline that has never seen a live push still permits following", () => {
  const timeline = withLiveEdge(chainTimeline(STORED), null);
  const at = positionOf(timeline, null);

  // The whole bug in two lines. `onLive` is false — correctly, nothing live has
  // arrived — and the subscription must open anyway, or it never will.
  assert.equal(at.onLive, false, "no push has arrived, so onLive is false");
  assert.equal(at.following, true, "the reader asked for no minute, so the socket must open");
});

check("THE GUARD, cold: an empty store still permits following", () => {
  // A day the store holds nothing for at all — a fresh expiry, or the first minutes of a
  // UTC day. There is not even a right edge to stand on, and the socket must still open.
  const at = positionOf(EMPTY_TIMELINE, null);
  assert.equal(at.onLive, false);
  assert.equal(at.following, true, "an empty day must not lock the stream out");
});

check("a live push arriving makes the right edge live, and following stays true", () => {
  const timeline = withLiveEdge(chainTimeline(STORED), "2026-09-07T12:03:00Z");
  const at = positionOf(timeline, null);
  assert.equal(timeline.liveIndex, 3, "the push stands one position past the stored edge");
  assert.equal(at.onLive, true);
  assert.equal(at.following, true);
});

check("pinning a stored minute stops following, so the socket closes", () => {
  const timeline = withLiveEdge(chainTimeline(STORED), "2026-09-07T12:03:00Z");
  const at = positionOf(timeline, "2026-09-07T12:01:00Z");
  assert.equal(at.onLive, false);
  assert.equal(at.following, false, "a pinned historical minute must tear the socket down");
});

check("pinning a stored minute stops following even before any push", () => {
  const timeline = withLiveEdge(chainTimeline(STORED), null);
  const at = positionOf(timeline, "2026-09-07T12:01:00Z");
  assert.equal(at.following, false, "no push must not turn a historical link into a live one");
});

check("a link naming the live minute's own stamp follows it", () => {
  // The reason `following` is not simply `wanted === null`. A URL can carry the stamp a
  // push has just put on the right edge, and arriving that way is standing on the live
  // edge as surely as arriving with no query at all.
  const timeline = withLiveEdge(chainTimeline(STORED), "2026-09-07T12:03:00Z");
  const at = positionOf(timeline, "2026-09-07T12:03:00Z");
  assert.equal(at.onLive, true);
  assert.equal(at.following, true, "a deep link onto the live minute must keep following it");
});

if (failures > 0) {
  console.error(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
