/**
 * The comparison overlays: the same expiry's curve, some fixed distance back in time.
 *
 * ## Why these two offsets and not the reference terminal's four
 *
 * The sibling terminal offers **previous close**, **day open**, **previous expiry** and
 * an **N-average**. Three of those are equity furniture. BTC options trade continuously
 * — there is no bell, so there is no close and no open, and a control labelled "PREV
 * CLOSE" on this market would be a control with no definition behind it. A time-anchored
 * offset has an exact meaning on a continuous market and does the same job: it answers
 * "how has this curve moved", which is the only question the close and the open were
 * ever standing in for.
 *
 * Previous expiry is not dropped, only deferred: it is a real comparison on this market,
 * and it becomes available once an expiry has been captured through settlement. None has
 * been yet, so there is nothing to draw and no control is offered for it.
 *
 * ## Anchored to the scrubber's minute, never to the wall clock
 *
 * `−1h` means "an hour before the minute you are standing on". Scrub back to 03:00 and
 * the overlay draws 02:00. Anchoring to `Date.now()` instead would put 03:00 beside
 * whatever the clock happened to say, which is a comparison nobody could explain and
 * which would silently change while the reader stared at a curve they had frozen.
 *
 * ## The anchor is exact, and a miss is said out loud
 *
 * The lookup is by stamp with no tolerance: the minute an hour back, or nothing.
 * `measured` on the live store at 2026-09-04T12:33Z (BTC 25-09-2026, 590 stored minutes
 * across an 834-minute span), an exact `−1h` anchor lands on a stored minute **64.4%**
 * of the time; `−24h` lands **0%** of the time, because the store begins at
 * 2026-09-03T22:40Z and has not yet lived a full day.
 *
 * A tolerance would raise the first figure and it is deliberately not taken. The primary
 * curve on this same chart already refuses to draw a neighbouring minute at a position
 * the store holds nothing for — `VolatilityScreen.renderPlot` says so in as many words —
 * and an overlay that quietly substituted 11:18 for 11:19 would put two curves on one
 * axis obeying two different rules about a missing minute. The absence is reported with
 * the minute that was asked for, so it reads as a fact about the store rather than as a
 * series that failed to render.
 *
 * Three absences are distinguished, because a reader can act on the difference: before
 * the store began, past the edge of the day this screen read, and a hole inside it.
 * Only the middle one is fixed by reloading, and only the first is fixed by waiting.
 *
 * Nothing here fetches. Both overlays are a second index into the array the day already
 * arrived in — see `docs/smile-contract.md` §"The day, not the minute".
 */
import type { SmileMinute } from "./contract";
import { indexOfStamp, shiftStamp, storedThrough, type Timeline } from "./timeline";

export type OverlayId = "h1" | "d1";

export interface OverlaySpec {
  id: OverlayId;
  /** What the control says. U+2212, not a hyphen: this is a minus sign. */
  label: string;
  /** How far back the anchor sits, in whole minutes. */
  minutesBack: number;
  /** The control's `title`, spelling the offset out in words. */
  title: string;
}

export const OVERLAYS: readonly OverlaySpec[] = [
  {
    id: "h1",
    label: "−1h",
    minutesBack: 60,
    title: "The same expiry sixty minutes before the minute on the scrubber.",
  },
  {
    id: "d1",
    label: "−24h",
    minutesBack: 1440,
    title: "The same expiry twenty-four hours before the minute on the scrubber.",
  },
];

/** Which overlays are switched on. Both off is the first paint. */
export type OverlayState = Record<OverlayId, boolean>;

export const NO_OVERLAYS: OverlayState = { h1: false, d1: false };

export interface ResolvedOverlay {
  spec: OverlaySpec;
  /** The UTC minute this overlay asked for. `null` when there is no minute to anchor to. */
  anchor: string | null;
  /** The curve to draw, or `null` — in which case `absence` says why there is none. */
  minute: SmileMinute | null;
  /**
   * Why nothing is drawn, in the reader's terms. `null` when there is a curve.
   *
   * A series that renders as nothing and a series that renders as zero look the same on
   * an axis, and only one of them is true — so the absent case carries a sentence rather
   * than a blank.
   */
  absence: string | null;
}

/**
 * One overlay, resolved against the day the client already holds.
 *
 * `stamp` is the scrubber's current minute. Passing `null` — no position at all — gives
 * back an overlay with nothing to say rather than one that failed.
 */
export function resolveOverlay(
  spec: OverlaySpec,
  stamp: string | null,
  timeline: Timeline,
): ResolvedOverlay {
  if (stamp === null) {
    return { spec, anchor: null, minute: null, absence: "There is no minute to compare against." };
  }

  const anchor = shiftStamp(stamp, -spec.minutesBack);
  if (anchor === null) {
    return { spec, anchor: null, minute: null, absence: `${stamp} is not a minute this screen can shift.` };
  }

  const at = indexOfStamp(timeline, anchor);
  const minute = at >= 0 ? (timeline.minutes[at] ?? null) : null;
  if (minute) return { spec, anchor, minute, absence: null };

  // Three different absences, and a reader can act on the difference between them: one
  // resolves by waiting, one resolves by reloading, and one never resolves at all. The
  // stamps are fixed-width and zero-padded, so comparing them as strings orders them as
  // instants.
  const first = timeline.stamps[0];
  const through = storedThrough(timeline);

  if (first !== undefined && anchor < first) {
    return {
      spec,
      anchor,
      minute: null,
      absence: `Nothing stored at ${anchor} — this expiry's store begins at ${first}.`,
    };
  }

  if (through !== null && anchor > through) {
    return {
      spec,
      anchor,
      minute: null,
      // The day arrived in one request and is as of the moment it was read. A minute
      // past that edge is not a hole; it is a minute the screen has not asked about, and
      // saying "the store wrote no bar" about it would be a claim we cannot support.
      absence:
        `${anchor} is past the day this screen read, which ends at ${through}. ` +
        `The store may well hold it — reload to read the day again.`,
    };
  }

  return { spec, anchor, minute: null, absence: `The store wrote no bar at ${anchor}.` };
}
