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
}

export const EMPTY_TIMELINE: Timeline = {
  stamps: [],
  minutes: [],
  gaps: [],
  storedCount: 0,
  emptyCount: 0,
  gridded: true,
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

function ungridded(minutes: readonly SmileMinute[]): Timeline {
  return {
    stamps: minutes.map((m) => m.minute),
    minutes: [...minutes],
    gaps: [],
    storedCount: minutes.length,
    emptyCount: 0,
    gridded: false,
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
  const gaps: Gap[] = [];
  let storedCount = 0;
  let run = 0;

  for (let i = 0; i < span; i++) {
    const stamp = stampOf(startMs + i * MINUTE_MS);
    const minute = byStamp.get(stamp) ?? null;
    stamps.push(stamp);
    minutes.push(minute);
    if (minute) {
      storedCount++;
      if (run > 0) gaps.push({ start: i - run, length: run });
      run = 0;
    } else {
      run++;
    }
  }
  if (run > 0) gaps.push({ start: span - run, length: run });

  // A stored minute the grid could not place would be a contract violation — the stamps
  // are minute-aligned and inside the span by construction. If one ever is, the grid is
  // abandoned rather than shipped with a minute the reader cannot reach.
  if (storedCount !== stored.length) return ungridded(stored);

  return {
    stamps,
    minutes,
    gaps,
    storedCount,
    emptyCount: span - storedCount,
    gridded: true,
  };
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
