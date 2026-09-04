/**
 * Where the screen is standing in the day: the minute a reader asked for, resolved
 * against the timeline that actually arrived.
 *
 * One file because it is one question with four answers that must agree — the index the
 * scrubber sits at, the stamp the URL carries, the minute the chart draws, and whether
 * the position is the live right edge. Computing them separately is how three of them
 * end up describing one minute and the fourth another.
 *
 * Pure, like the rest of `lib/`: it takes the timeline and the request and returns the
 * position, so the screen holds the request and nothing else.
 */
import type { SmileMinute } from "./contract";
import { indexOfStamp, lastIndex, type Timeline } from "./timeline";

export interface Position {
  /** The position on the timeline, or `-1` when the timeline is empty. */
  index: number;
  /** The UTC stamp at that position, or `null` when there is no position. */
  stamp: string | null;
  /** The stored minute there, or `null` — a hole is a position you can stand on. */
  minute: SmileMinute | null;
  /** Standing on the live position — the right edge, following the stream. */
  onLive: boolean;
  /**
   * A link naming a minute this expiry's store does not reach at all — not a hole in the
   * middle of the day, which is a position you can stand on, but a stamp outside it. It
   * is said out loud rather than silently rounded to the nearest curve: quietly showing
   * a different minute than the one in the address bar is the same class of error as
   * drawing a line across a gap.
   */
  unreachable: string | null;
}

/**
 * `wanted` is the reader's request as the store's own UTC stamp, or `null`, which means
 * "whatever the right edge is".
 *
 * The stamp rather than the index, because the stamp is what the URL carries and what
 * the store calls it; an index is a fact about one particular response and would mean
 * something different the moment the day grew by a minute.
 */
export function positionOf(timeline: Timeline, wanted: string | null): Position {
  const last = lastIndex(timeline);
  const askedIndex = wanted === null ? -1 : indexOfStamp(timeline, wanted);
  /** No answer for what was asked means the right edge, which is the newest minute. */
  const index = askedIndex >= 0 ? askedIndex : last;

  return {
    index,
    stamp: index >= 0 ? (timeline.stamps[index] ?? null) : null,
    minute: index >= 0 ? (timeline.minutes[index] ?? null) : null,
    onLive: index >= 0 && index === timeline.liveIndex,
    unreachable: wanted !== null && askedIndex < 0 && last >= 0 ? wanted : null,
  };
}
