/**
 * The day as positions in time: what the scrubber moves along.
 *
 * Its own file rather than a section of `smile.ts` because it is a different subject.
 * `smile.ts` turns one minute into rows to draw; this turns a whole response into the
 * sequence of minutes a reader can stand on, and into the places where the store holds
 * nothing at all.
 *
 * **The positions are every minute between the first stored one and the last, not the
 * stored ones alone.** That is the load-bearing decision here and it comes straight out
 * of `docs/smile-contract.md`: a minute the store never wrote is a real minute with no
 * bar, and a scrubber indexed only over the minutes that exist would silently close
 * those holes up — 233 of them in the 791-minute span this was built against
 * (`measured`, BTC 25-09-2026, live engine, 2026-09-04T11:50Z). Closing them up is the
 * same error as joining the curve across an unsolved strike: it puts something where
 * there is nothing, and it makes the acceptance criterion "a minute with no data renders
 * as empty" unreachable, because you could never land on one.
 *
 * Nothing here formats, renders or knows about a timezone. The stamps are the store's
 * own UTC keys, unchanged, because they are also what the URL carries.
 */
import type { SmileMinute, SmileResponse } from "./contract";

const MINUTE_MS = 60_000;

/**
 * How many positions the grid will build before it gives up on being a grid.
 *
 * Two days. The endpoint returns every stored minute for an expiry, and today that is
 * part of one day — but the store accumulates, and an expiry a month old would span
 * 43,200 minutes. A range input with 43,200 steps is not a control, and building the
 * array on every response would be work nobody asked for. Past this the timeline falls
 * back to the stored minutes alone and says so through `gridded`, so the scrubber still
 * reaches every stored minute; only the holes stop being positions.
 */
export const MAX_POSITIONS = 2880;

/** A contiguous run of positions the store holds nothing for. */
export interface Gap {
  /** Index of the first empty position. */
  start: number;
  /** How many positions in the run. Never zero. */
  length: number;
}

export interface Timeline {
  /** One UTC stamp per position, ascending. The store's own spelling, unchanged. */
  stamps: readonly string[];
  /** Same length as `stamps`. `null` at a position the store holds no minute for. */
  minutes: readonly (SmileMinute | null)[];
  /** The empty runs, ascending. Empty when every position has a minute. */
  gaps: readonly Gap[];
  /** Positions that carry a stored minute. */
  storedCount: number;
  /** Positions that do not. `stamps.length - storedCount`. */
  emptyCount: number;
  /**
   * `false` when the span was too wide to lay out minute by minute and the positions are
   * the stored minutes alone. The scrubber still reaches every stored minute; there are
   * simply no empty positions to land on, and nothing to mark. See `MAX_POSITIONS`.
   */
  gridded: boolean;
  /**
   * The position carrying the live minute, or `-1` when the stream has sent nothing.
   *
   * **It is always the last position when it exists**, which `withLiveMinute` enforces —
   * the live minute is the right edge by definition, and a "live" position sitting
   * anywhere else would mean the store had sealed a minute ahead of the stream. That
   * invariant is what lets the screen spell "follow the live curve" as "stand on the
   * right edge" rather than as a second piece of state that can disagree with the first.
   */
  liveIndex: number;
}

export const EMPTY_TIMELINE: Timeline = {
  stamps: [],
  minutes: [],
  gaps: [],
  storedCount: 0,
  emptyCount: 0,
  gridded: true,
  liveIndex: -1,
};

/** `2026-09-04T11:50:00Z` — the only spelling the store, the contract and the URL use. */
const STAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

export function isMinuteStamp(value: string): boolean {
  return STAMP.test(value);
}

const PAD = (n: number) => String(n).padStart(2, "0");

/** Milliseconds back to the contract's spelling. Never `toISOString`, which adds millis. */
function stampOf(ms: number): string {
  const d = new Date(ms);
  return (
    `${d.getUTCFullYear()}-${PAD(d.getUTCMonth() + 1)}-${PAD(d.getUTCDate())}T` +
    `${PAD(d.getUTCHours())}:${PAD(d.getUTCMinutes())}:${PAD(d.getUTCSeconds())}Z`
  );
}

/** The minute an instant belongs to, in the store's spelling. `null` if unparseable. */
export function minuteOfInstant(iso: string): string | null {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return stampOf(Math.floor(ms / MINUTE_MS) * MINUTE_MS);
}

/**
 * A stamp moved by whole minutes, in the same spelling. `null` if unparseable.
 *
 * This is what anchors a comparison overlay: `−1h` is the scrubber's own minute minus
 * sixty, not the wall clock minus sixty. Arithmetic on the stamp rather than on a
 * `Date` in the reader's zone, because a UTC minute has no daylight saving in it and a
 * local one does.
 */
export function shiftStamp(stamp: string, minutes: number): string | null {
  const ms = Date.parse(stamp);
  if (!Number.isFinite(ms)) return null;
  return stampOf(ms + minutes * MINUTE_MS);
}

/** The empty runs and the two counts, read off a positions array. */
function scan(minutes: readonly (SmileMinute | null)[]): {
  gaps: Gap[];
  storedCount: number;
  emptyCount: number;
} {
  const gaps: Gap[] = [];
  let storedCount = 0;
  let run = 0;
  for (let i = 0; i < minutes.length; i++) {
    if (minutes[i]) {
      storedCount++;
      if (run > 0) gaps.push({ start: i - run, length: run });
      run = 0;
    } else {
      run++;
    }
  }
  if (run > 0) gaps.push({ start: minutes.length - run, length: run });
  return { gaps, storedCount, emptyCount: minutes.length - storedCount };
}

function ungridded(minutes: readonly SmileMinute[]): Timeline {
  return {
    stamps: minutes.map((m) => m.minute),
    minutes: [...minutes],
    gaps: [],
    storedCount: minutes.length,
    emptyCount: 0,
    gridded: false,
    liveIndex: -1,
  };
}

/**
 * The response as a walkable line of minutes.
 *
 * `minutes` ascends by contract, so the ends are the ends. A minute whose stamp the
 * grid cannot place — an unparseable date, or one outside the span, neither of which the
 * contract permits — is not dropped: the fallback below keeps every stored minute
 * reachable rather than quietly losing one.
 */
export function buildTimeline(response: SmileResponse): Timeline {
  const stored = response.minutes;
  if (stored.length === 0) return EMPTY_TIMELINE;

  const first = stored[0];
  const last = stored[stored.length - 1];
  if (!first || !last) return EMPTY_TIMELINE;

  const startMs = Date.parse(first.minute);
  const endMs = Date.parse(last.minute);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) {
    return ungridded(stored);
  }

  const span = Math.floor((endMs - startMs) / MINUTE_MS) + 1;
  if (span > MAX_POSITIONS) return ungridded(stored);

  const byStamp = new Map<string, SmileMinute>();
  for (const minute of stored) byStamp.set(minute.minute, minute);

  const stamps: string[] = [];
  const minutes: (SmileMinute | null)[] = [];

  for (let i = 0; i < span; i++) {
    const stamp = stampOf(startMs + i * MINUTE_MS);
    stamps.push(stamp);
    minutes.push(byStamp.get(stamp) ?? null);
  }

  const counted = scan(minutes);

  // A stored minute the grid could not place would be a contract violation — the stamps
  // are minute-aligned and inside the span by construction. If one ever is, the grid is
  // abandoned rather than shipped with a minute the reader cannot reach.
  if (counted.storedCount !== stored.length) return ungridded(stored);

  return { stamps, minutes, ...counted, gridded: true, liveIndex: -1 };
}

/**
 * The stored day with the live minute standing one position past its right edge.
 *
 * **The live minute is not a stored minute and this is where the difference is kept.**
 * `docs/smile-contract.md` deliberately withholds the open minute — "a bar that has not
 * sealed is not yet a bar" — so the store's right edge always trails the stream. This
 * puts the stream's minute immediately after it, as one more position.
 *
 * **The minutes in between are not laid out as empty positions, and that is the decision
 * in this function.** Everywhere else, a `null` position means "the store wrote no bar
 * at this minute", and the scrubber marks it as a hole. The minutes between the last
 * sealed bar and the live push are not that: they are minutes this screen has *not
 * asked about*. The day was read once, in one request, and the response is as of the
 * moment it was read. Filling that stretch with nulls would let the screen claim an
 * outage the store does not have, and the claim would grow by a minute a minute while a
 * session stayed open. The step from the last stored minute to the live one is therefore
 * one position wide however long it is; both ends name their own minute on the clock and
 * in the header, so nothing on screen misstates the time.
 *
 * Three placements, and the third is a contradiction rather than a shape:
 *
 *   - Past the stored edge: appended, and it is the last position.
 *   - Equal to the last stored stamp: the store sealed the minute the stream is still
 *     inside. The live snapshot replaces the sealed bar — both describe that minute and
 *     the live one is the later observation of it.
 *   - **Before** the stored edge: the store would be ahead of the stream, which cannot
 *     happen while both read the same feed. The timeline is returned untouched rather
 *     than growing a position out of order, and the screen has no live curve until the
 *     stream catches up.
 *
 * Returns the timeline unchanged, by reference, whenever there is nothing to add — so a
 * memo keyed on it does not recompute the whole day sixty times a minute.
 */
export function withLiveMinute(timeline: Timeline, live: SmileMinute | null): Timeline {
  if (!live) return timeline;

  const liveMs = Date.parse(live.minute);
  if (!Number.isFinite(liveMs)) return timeline;

  if (timeline.stamps.length === 0) {
    return {
      stamps: [live.minute],
      minutes: [live],
      gaps: [],
      storedCount: 1,
      emptyCount: 0,
      gridded: timeline.gridded,
      liveIndex: 0,
    };
  }

  const lastStamp = timeline.stamps[timeline.stamps.length - 1];
  if (lastStamp === undefined) return timeline;
  const lastMs = Date.parse(lastStamp);
  if (!Number.isFinite(lastMs) || liveMs < lastMs) return timeline;

  const minutes = [...timeline.minutes];
  const stamps = [...timeline.stamps];
  if (liveMs === lastMs) {
    minutes[minutes.length - 1] = live;
  } else {
    stamps.push(live.minute);
    minutes.push(live);
  }

  return { ...timeline, stamps, minutes, ...scan(minutes), liveIndex: minutes.length - 1 };
}

/**
 * The last minute the store answered for, which is not the same as the last position
 * once the live minute is standing past it.
 *
 * An overlay anchor beyond this stamp is not a hole in the store — it is a minute this
 * screen's one request predates. `lib/overlay.ts` uses it to tell those two apart.
 */
export function storedThrough(timeline: Timeline): string | null {
  for (let i = timeline.stamps.length - 1; i >= 0; i--) {
    if (i !== timeline.liveIndex && timeline.minutes[i]) return timeline.stamps[i] ?? null;
  }
  return null;
}

/** The position of a UTC stamp, or `-1` when this timeline does not contain it. */
export function indexOfStamp(timeline: Timeline, stamp: string): number {
  return timeline.stamps.indexOf(stamp);
}

/** The last position — the right edge, which is the most recent minute available. */
export function lastIndex(timeline: Timeline): number {
  return timeline.stamps.length - 1;
}

/** Clamped into the timeline, so a step off either end stays on the end. */
export function clampIndex(timeline: Timeline, index: number): number {
  if (timeline.stamps.length === 0) return -1;
  if (!Number.isFinite(index)) return lastIndex(timeline);
  return Math.min(Math.max(Math.round(index), 0), lastIndex(timeline));
}
