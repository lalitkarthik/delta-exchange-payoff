/**
 * Turning `ContractBar[]` into the series `lightweight-charts` draws — pure, and tested
 * by inspection rather than by a runner this project does not have (`docs/CLAUDE.md`'s
 * "a web test runner" is out of scope; `typecheck` and `build` are the gate). Kept apart
 * from `components/ContractChart.tsx` so the one behaviour this module exists to get
 * right — a gap stays a gap — is readable without the canvas wiring around it.
 *
 * **Never forward-fill, made visible on the time axis.** `/bars` already omits a minute
 * nobody quoted; this file's job is to turn that omission into a `WhitespaceData` point
 * rather than into two candles drawn edge to edge as if the minute between them never
 * existed. `lightweight-charts`' whitespace mechanism needs an explicit point for every
 * time on the axis that is not a real bar, so `minuteGrid` below walks every minute from
 * the first stored bar to the last and marks what `/bars` did not answer for.
 *
 * A **quoted minute whose own series never ticked** — `bars.QuoteBar`'s one-sided-tick
 * rule, or a minute `reference-bars` never fed — draws as whitespace too, independently
 * per series: `mid` can be a gap on a minute `bid` answers for, and `ltp` can be a gap on
 * every minute of a contract nobody has ever traded.
 */
import type {
  CandlestickData,
  LineData,
  UTCTimestamp,
  WhitespaceData,
} from "lightweight-charts";

import type { ContractBar } from "./contract";

export type CandleMode = "mid" | "ltp";

type Candle = CandlestickData<UTCTimestamp> | WhitespaceData<UTCTimestamp>;
type Line = LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp>;

/** `"2026-09-04T09:00:00Z"` to seconds since epoch, the unit `lightweight-charts` wants. */
export function toUnix(minute: string): UTCTimestamp {
  return Math.floor(Date.parse(minute) / 1000) as UTCTimestamp;
}

/** The current minute, floored — the bucket a live quote arriving right now belongs to.
 * Same spelling `/bars` returns, so a live-built point and a stored one compare equal. */
export function currentMinute(now: Date = new Date()): string {
  return new Date(Math.floor(now.getTime() / 60_000) * 60_000).toISOString().slice(0, 19) + "Z";
}

/**
 * Every minute from the first stored bar to the last, ascending, one per 60 seconds.
 *
 * The **domain**, not the data: a minute in this list may or may not have a bar behind
 * it, and every series below walks it independently to decide which is which. `[]` when
 * there are no bars at all — nothing to draw and nothing to leave a gap in.
 */
export function minuteGrid(bars: readonly ContractBar[]): string[] {
  if (bars.length === 0) return [];
  const start = Date.parse(bars[0]!.minute);
  const end = Date.parse(bars[bars.length - 1]!.minute);
  const stamps: string[] = [];
  for (let t = start; t <= end; t += 60_000) {
    stamps.push(new Date(t).toISOString().slice(0, 19) + "Z");
  }
  return stamps;
}

function byMinute(bars: readonly ContractBar[]): Map<string, ContractBar> {
  return new Map(bars.map((bar) => [bar.minute, bar] as const));
}

/**
 * The candlestick series for one mode, gaps as `WhitespaceData`.
 *
 * `mode` picks which of `/bars`' two OHLC quadruples this draws; the two never mix
 * within one candle, because a candle whose open came from the book and whose close
 * came from a trade print would describe a price that never existed — the same rule
 * `store.py`'s `BarAggregator` already applies to its own two channels.
 */
export function candleData(bars: readonly ContractBar[], mode: CandleMode): Candle[] {
  const rows = byMinute(bars);
  return minuteGrid(bars).map((minute): Candle => {
    const time = toUnix(minute);
    const bar = rows.get(minute);
    if (!bar) return { time };
    const [open, high, low, close] =
      mode === "mid"
        ? [bar.mid_open, bar.mid_high, bar.mid_low, bar.mid_close]
        : [bar.ltp_open, bar.ltp_high, bar.ltp_low, bar.ltp_close];
    if (open === null || high === null || low === null || close === null) return { time };
    return { time, open, high, low, close };
  });
}

/** One of the two line series drawn over the candles — the minute's OHLC **close**,
 * matching what `docs/bars-contract.md` and every other close-valued field in this
 * project mean by "the price at that minute". */
export function lineData(bars: readonly ContractBar[], side: "bid" | "ask"): Line[] {
  const rows = byMinute(bars);
  return minuteGrid(bars).map((minute): Line => {
    const time = toUnix(minute);
    const bar = rows.get(minute);
    const value = bar ? (side === "bid" ? bar.bid_close : bar.ask_close) : null;
    return value === null ? { time } : { time, value };
  });
}

/** One candle still being built from live quotes, kept client-side between pushes so
 * each push can extend it rather than replace it — `lightweight-charts`' `update()`
 * takes the whole bar, not a delta. */
export interface OpenCandle {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
}

/**
 * Fold one live price into the open candle for `time`.
 *
 * A new `time` — the live push has crossed into a new minute — starts a fresh candle
 * rather than extending the old one; `time` matching the current open candle extends
 * high/low/close in place, mirroring `bars._Series.update`'s own venue-clock discipline
 * as closely as a client driven by arrival order rather than a venue timestamp can.
 */
export function extendCandle(
  current: OpenCandle | null,
  time: UTCTimestamp,
  value: number,
): OpenCandle {
  if (current === null || current.time !== time) {
    return { time, open: value, high: value, low: value, close: value };
  }
  return {
    time,
    open: current.open,
    high: Math.max(current.high, value),
    low: Math.min(current.low, value),
    close: value,
  };
}
