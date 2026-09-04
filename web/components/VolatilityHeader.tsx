"use client";

import ThemeToggle from "@/components/ThemeToggle";
import { LIVE_REDRAW_MS } from "@/hooks/useLiveSmile";
import { UNDERLYINGS, type SmileMinute, type Underlying } from "@/lib/contract";
import { ENGINE_URL, type Source } from "@/lib/engine";
import { formatFetchedClock, formatSpot } from "@/lib/format";
import { LIVE_STATUS_LABEL, type LiveStatus } from "@/lib/live";
import { formatTimeToExpiry } from "@/lib/smile";

/**
 * The header strip: the two series pickers, the four figures the current minute is read
 * by, the model stamp, and the two chips saying where each half of the screen came from.
 *
 * **Everything here is read from the response.** The forward, the minute, the clock the
 * volatility is quoted on, which forward method produced it and which model stamp is on
 * the rows — none of it is hardcoded, because all of it can change between one minute and
 * the next and the forward convention alone is worth up to 3.9 vol points on the axis
 * below.
 */
export default function VolatilityHeader({
  underlying,
  onPickUnderlying,
  expiries,
  expiry,
  onPickExpiry,
  busy,
  minute,
  stamp,
  hours,
  stamps,
  source,
  fallbackReason,
  liveStatus,
  liveDetail,
}: {
  underlying: Underlying;
  onPickUnderlying: (next: Underlying) => void;
  expiries: readonly string[];
  expiry: string;
  onPickExpiry: (next: string) => void;
  busy: boolean;
  /** The minute the scrubber is standing on, or `null` where the store holds none. */
  minute: SmileMinute | null;
  /** That minute's UTC stamp — present even when no minute was stored there. */
  stamp: string | null;
  hours: number | null;
  /** `model_versions`, as the response listed them. A list, never one picked out of it. */
  stamps: readonly string[];
  source: Source;
  fallbackReason: string | null;
  liveStatus: LiveStatus;
  liveDetail: string | null;
}) {
  return (
    <header className="header">
      <div className="brand">DELTA</div>
      <h1 className="screen-title">Volatility</h1>

      <label className="picker">
        <span className="stat-label">Underlying</span>
        <select
          className="picker-select"
          value={underlying}
          onChange={(e) => onPickUnderlying(e.target.value as Underlying)}
          disabled={busy}
        >
          {UNDERLYINGS.map((u) => (
            <option key={u} value={u}>
              {u}
            </option>
          ))}
        </select>
      </label>

      <label className="picker">
        <span className="stat-label">Expiry</span>
        <select
          className="picker-select"
          value={expiry}
          onChange={(e) => onPickExpiry(e.target.value)}
          disabled={busy || expiries.length === 0}
        >
          {expiries.length === 0 ? <option value="">—</option> : null}
          {expiries.map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
      </label>

      {/* The one amber figure. The forward is what the offset axis and the reference
          line are both read off, so it is this screen's spot. */}
      <div className="stat lead">
        <span className="stat-label">Forward</span>
        <span className="stat-value">
          {minute?.forward != null ? formatSpot(minute.forward) : "—"}
        </span>
      </div>

      {/* The position, in UTC, and it stays UTC even when the position is empty — this
          is the minute's identity and the thing the URL carries. The scrubber below
          says the same instant in the reader's own zone. */}
      <div className="stat">
        <span className="stat-label">Minute</span>
        <span className="stat-value">
          {stamp ? formatFetchedClock(stamp) : "—"} <span className="stat-note">UTC</span>
        </span>
      </div>

      <div className="stat">
        <span className="stat-label">To expiry</span>
        <span className="stat-value">{hours === null ? "—" : formatTimeToExpiry(hours)}</span>
      </div>

      <div className="stat">
        <span className="stat-label">Forward method</span>
        <span className="stat-value stat-small">{minute?.forward_method ?? "—"}</span>
      </div>

      {/* Read from the response, never hardcoded — and reported as a count when the
          response spans more than one, because picking one would be a claim the data
          does not support. The banner below names both. */}
      <div className="stat">
        <span className="stat-label">Model</span>
        <span className="stat-value stat-small" title={stamps.join("  ·  ") || undefined}>
          {stamps.length === 0 ? "—" : stamps.length === 1 ? stamps[0] : `${stamps.length} stamps`}
        </span>
      </div>

      {/* Two chips, because this screen has two sources and they fail separately: the
          stored day arrived over HTTP and does not change, the live edge arrives over
          a socket and can stop without the figures on screen looking any different. */}
      <span className="chip" title={fallbackReason ?? `Read from ${ENGINE_URL}/smile.`}>
        {source === "fixture" ? "fixture" : "stored"}
      </span>

      <span
        className="chip"
        title={liveDetail ?? `Streaming from ${ENGINE_URL}/ws/chain, sampled at ${LIVE_REDRAW_MS} ms.`}
      >
        {LIVE_STATUS_LABEL[liveStatus]}
      </span>

      <ThemeToggle />
    </header>
  );
}
