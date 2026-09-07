"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ChainLadder } from "@/components/ChainLadder";
import RecordingToggle from "@/components/RecordingToggle";
import ThemeToggle from "@/components/ThemeToggle";
import TimeScrubber from "@/components/TimeScrubber";
import { chainTimeline, withLiveEdge } from "@/lib/chainTimeline";
import {
  type ChainResponse,
  type HistoricalChain,
  UNDERLYINGS,
  type Underlying,
} from "@/lib/contract";
import { ENGINE_URL, loadChainAt, loadChainMinutes, loadExpiries } from "@/lib/engine";
import { LIVE_STATUS_LABEL, subscribeChain, type LiveStatus } from "@/lib/live";
import { formatFetchedAt, formatFetchedClock, formatSpot } from "@/lib/format";
import { positionOf } from "@/lib/position";
import { clampIndex, lastIndex } from "@/lib/timeline";
import type { ViewRequest } from "@/lib/view";

/** Same rate limit `VolatilityScreen` waits on before rewriting the address bar, and
 * the same reason: Firefox and Safari throttle `history.replaceState` and a drag is
 * hundreds of calls in a couple of seconds. */
const URL_SETTLE_MS = 200;

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** `YYYY-MM-DD`, UTC — the store's own partition spelling. */
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/**
 * `?underlying=BTC&expiry=04-09-2026` live, `&minute=...` added standing anywhere else.
 * `lib/view.ts`'s `viewQuery` was not reused: that one always writes a minute, and
 * "live" here is the absence of one rather than a stamp that happens to be the newest.
 */
function chainQuery(underlying: Underlying, expiry: string, minute: string | null): string {
  const params = new URLSearchParams({ underlying, expiry });
  if (minute) params.set("minute", minute);
  return `?${params.toString().replace(/%3A/g, ":")}`;
}

/**
 * One header row of figures, the ladder, and — new in this ticket — a slider standing
 * on any stored minute of the day.
 *
 * **The slider follows the volatility screen's scrubber exactly** rather than inventing
 * a second idiom: `lib/chainTimeline.ts` wraps the stored-minutes list in the same
 * `Timeline` `TimeScrubber` and `lib/position.ts` already understand, so both are reused
 * unchanged. What differs from the smile screen is what a position *carries* — a smile
 * response holds the whole day; a ladder is heavy enough that this screen fetches one
 * minute at a time (`docs/design/lld/historical-read-path.md`), over `/chain/at`.
 *
 * **Standing on the right edge means the socket is open; standing anywhere else means
 * it is not.** `onLive` — from `positionOf`, the same field the volatility screen reads
 * — is the one switch: true subscribes `/ws/chain` and tears down any historical fetch,
 * false does the opposite. Releasing the slider back onto the right edge is nothing
 * more than `wanted` returning to `null`, which is already this codebase's spelling of
 * "follow whatever the right edge is".
 */
export default function ChainScreen({ initial }: { initial: ViewRequest }) {
  const [underlying, setUnderlying] = useState<Underlying>(initial.underlying ?? "BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>(initial.expiry ?? "");

  /** The minute a reader asked for, or `null` for "the right edge, whatever that is".
   * Never derived from the wall clock — see `lib/position.ts`. */
  const [wanted, setWanted] = useState<string | null>(initial.minute ?? null);

  /** The day the slider's domain is read for. Computed once at mount, from the linked
   * minute when there was one — a deep link into a past day has to ask the store about
   * that day, not about today. Not kept in step with the wall clock afterwards: a
   * session crossing midnight UTC is not a case this screen handles. */
  const [date] = useState<string>(() => initial.minute?.slice(0, 10) ?? todayUtc());

  const [minutes, setMinutes] = useState<string[]>([]);

  const [liveChain, setLiveChain] = useState<ChainResponse | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("connecting");
  const [liveStatusDetail, setLiveStatusDetail] = useState<string | null>(null);

  const [historicalChain, setHistoricalChain] = useState<HistoricalChain | null>(null);
  const [historicalWaiting, setHistoricalWaiting] = useState(false);
  const [historicalBusy, setHistoricalBusy] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow earlier request landing after a newer one.
  const expiryRequest = useRef(0);
  const minutesRequest = useRef(0);
  const ladderRequest = useRef(0);

  const loadExpiryList = useCallback(async (next: Underlying, wanted: string | null) => {
    const id = ++expiryRequest.current;
    setBusy(true);
    setError(null);

    try {
      const list = await loadExpiries(next);
      if (id !== expiryRequest.current) return;

      const available = list.data.expiries;
      setExpiries(available);

      const fallback =
        list.preferredExpiry && available.includes(list.preferredExpiry)
          ? list.preferredExpiry
          : (available[0] ?? "");
      const chosen = wanted && available.includes(wanted) ? wanted : fallback;
      setExpiry(chosen);

      if (!chosen) setError(`No expiries listed for ${next}.`);
    } catch (err) {
      if (id !== expiryRequest.current) return;
      setError(message(err));
    } finally {
      if (id === expiryRequest.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadExpiryList(underlying, expiry || null);
    // Deliberately keyed on the underlying alone — see `page.tsx`'s sibling comment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying, loadExpiryList]);

  // The slider's domain: every stored minute of `date`, for this series.
  useEffect(() => {
    if (!expiry) return;
    const id = ++minutesRequest.current;
    void (async () => {
      try {
        const loaded = await loadChainMinutes(underlying, expiry, date);
        if (id !== minutesRequest.current) return;
        setMinutes(loaded.minutes);
      } catch {
        // The ladder still works without a domain to scrub; the slider simply has
        // nothing to mark. Not surfaced as a page-level error over a control nobody
        // may be using yet.
      }
    })();
  }, [underlying, expiry, date]);

  const storedTimeline = useMemo(() => chainTimeline(minutes), [minutes]);
  const timeline = useMemo(
    () => withLiveEdge(storedTimeline, liveStatus === "live" ? liveChain?.fetched_at ?? null : null),
    [storedTimeline, liveStatus, liveChain],
  );

  const { index, stamp, onLive, unreachable } = positionOf(timeline, wanted);

  // The live subscription: open only while standing on the right edge. Dragging off it
  // tears the socket down; releasing back onto it reopens one.
  useEffect(() => {
    if (!expiry || !onLive) return;
    setLiveChain(null);
    return subscribeChain(underlying, expiry, {
      onChain: setLiveChain,
      onStatus: (next, detail) => {
        setLiveStatus(next);
        setLiveStatusDetail(detail ?? null);
      },
    });
  }, [underlying, expiry, onLive]);

  // The historical read: one request per stored minute the slider stands on.
  useEffect(() => {
    if (onLive || !stamp) {
      // Bumped even though nothing is fetched here: it retires any request already in
      // flight, so a slow historical read landing after the reader has moved back to
      // live cannot overwrite the state this branch just cleared.
      ++ladderRequest.current;
      setHistoricalChain(null);
      setHistoricalWaiting(false);
      setHistoricalBusy(false);
      return;
    }
    const id = ++ladderRequest.current;
    setHistoricalBusy(true);
    void (async () => {
      try {
        const loaded = await loadChainAt(underlying, expiry, stamp);
        if (id !== ladderRequest.current) return;
        if (loaded.type === "waiting") {
          setHistoricalChain(null);
          setHistoricalWaiting(true);
        } else {
          setHistoricalChain(loaded.data);
          setHistoricalWaiting(false);
        }
      } catch (err) {
        if (id !== ladderRequest.current) return;
        setError(message(err));
      } finally {
        if (id === ladderRequest.current) setHistoricalBusy(false);
      }
    })();
  }, [underlying, expiry, onLive, stamp]);

  // The address bar follows the slider; it never drives it after the first render.
  useEffect(() => {
    if (!expiry) return;
    const query = chainQuery(underlying, expiry, onLive ? null : stamp);
    if (window.location.search === query) return;
    const timer = window.setTimeout(() => {
      window.history.replaceState(null, "", query);
    }, URL_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [underlying, expiry, onLive, stamp]);

  const pickUnderlying = (next: Underlying) => {
    setUnderlying(next);
    setWanted(null);
  };

  const pickExpiry = (next: string) => {
    setExpiry(next);
    setWanted(null);
  };

  /** Where the scrubber puts the view. Standing on the right edge is spelled `null` —
   * "whatever the right edge is" — exactly as `VolatilityScreen.pickIndex` spells it,
   * and for the same reason: pinning a stamp there would stop following live the
   * moment a reader stepped onto the edge. */
  const pickIndex = (next: number) => {
    const at = clampIndex(timeline, next);
    if (at < 0 || at === lastIndex(timeline)) {
      setWanted(null);
      return;
    }
    setWanted(timeline.stamps[at] ?? null);
  };

  const chain = onLive ? liveChain : historicalChain;

  return (
    <div className="shell">
      <header className="header">
        <div className="brand">DELTA</div>

        <label className="picker">
          <span className="stat-label">Underlying</span>
          <select
            className="picker-select"
            value={underlying}
            onChange={(e) => pickUnderlying(e.target.value as Underlying)}
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
            onChange={(e) => pickExpiry(e.target.value)}
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

        <div className="stat lead">
          <span className="stat-label">Spot</span>
          <span className="stat-value">{chain ? formatSpot(chain.spot) : "—"}</span>
        </div>

        <div className="stat">
          <span className="stat-label">ATM strike</span>
          <span className="stat-value">
            {chain ? formatSpot(chain.atm_strike) : "—"}{" "}
            <span className="stat-note">(★)</span>
          </span>
        </div>

        {/* Which of the two this row is: "live" or the minute the slider stands on. */}
        <div className="stat">
          <span className="stat-label">Showing</span>
          <span
            className="stat-value"
            title={chain ? formatFetchedAt(chain.fetched_at) : undefined}
          >
            {onLive ? "LIVE" : chain ? formatFetchedClock(chain.fetched_at) : "—"}{" "}
            <span className="stat-note">{onLive ? "" : "UTC"}</span>
          </span>
        </div>

        <span
          className="chip"
          title={liveStatusDetail ?? `Streaming from ${ENGINE_URL}.`}
        >
          {onLive ? LIVE_STATUS_LABEL[liveStatus] : "history"}
        </span>

        <RecordingToggle />

        <ThemeToggle />
      </header>

      <main className="main">
        {onLive && liveStatus === "error" ? (
          <p className="notice error">
            {liveStatusDetail ?? "The live stream reported an error."}
          </p>
        ) : null}

        {onLive && liveStatus === "closed" ? (
          <p className="notice warn">
            Lost the connection to the engine at <code>{ENGINE_URL}</code>. Retrying — the
            figures below are the last ones that arrived, and they are not moving.
          </p>
        ) : null}

        {!onLive && unreachable ? (
          <p className="notice warn">
            The link asked for <strong>{unreachable}</strong>, which this day&rsquo;s
            store does not reach. Showing the closest minute the slider can stand on.
          </p>
        ) : null}

        {!onLive && historicalWaiting ? (
          <p className="notice">
            No stored quotes for {underlying} expiring {expiry} at {stamp}. The store
            wrote no row for this minute — never a neighbouring one shown in its place.
          </p>
        ) : null}

        {error ? <p className="notice error">{error}</p> : null}

        {chain ? (
          <ChainLadder key={`${chain.underlying}:${chain.expiry}`} chain={chain} />
        ) : error || (onLive && liveStatus === "error") || historicalWaiting ? null : (
          <p className="notice">
            {historicalBusy
              ? "Reading the stored ladder…"
              : onLive && liveStatus === "waiting"
                ? "Connected. Waiting for the first quotes on this expiry…"
                : "Connecting to the engine…"}
          </p>
        )}

        <TimeScrubber timeline={timeline} index={index} onChange={pickIndex} />

        <p className="note">
          A hatched half means that side is not listed at this strike. An empty cell means the
          venue did not price that field of a contract that does exist. A printed <code>0</code>{" "}
          is a real zero — open interest of exactly zero is common and is shown as such. The IV
          and Δ columns are <strong>computed here</strong>, not the venue&rsquo;s: the volatility is
          solved from the out-of-the-money leg&rsquo;s midpoint against a forward recovered from
          every paired strike, so both sides of a row share one figure. Hovering any cell gives
          the mark price, our volatility and the leg it came from, and Delta&rsquo;s own bid, mark
          and ask IV beside it. A bid or ask that moved since the last update carries a green or
          red arrow — that compares one update to the previous one, not every trade in between.
          The in-the-money half is washed, measured against spot. The slider stands on any
          stored minute of the day; the right edge is live.
        </p>
      </main>
    </div>
  );
}
