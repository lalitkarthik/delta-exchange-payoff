"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { SmileChart, type ChartOverlay, type VolScale } from "@/components/SmileChart";
import ThemeToggle from "@/components/ThemeToggle";
import TimeScrubber from "@/components/TimeScrubber";
import {
  UNDERLYINGS,
  type ChainResponse,
  type SmileResponse,
  type Underlying,
} from "@/lib/contract";
import { ENGINE_URL, loadExpiries, loadSmile, type Source } from "@/lib/engine";
import { formatFetchedClock, formatLocalClock, formatSpot, localZoneLabel } from "@/lib/format";
import { LIVE_STATUS_LABEL, subscribeChain, type LiveStatus } from "@/lib/live";
import { smileMinuteFromChain } from "@/lib/livesmile";
import {
  NO_OVERLAYS,
  OVERLAYS,
  resolveOverlay,
  type OverlayId,
  type OverlayState,
} from "@/lib/overlay";
import {
  formatTimeToExpiry,
  hoursToExpiry,
  isDying,
  mergeStrikes,
  solvedPercents,
  strikeGrid,
  toRows,
} from "@/lib/smile";
import {
  buildTimeline,
  clampIndex,
  EMPTY_TIMELINE,
  indexOfStamp,
  lastIndex,
  withLiveMinute,
} from "@/lib/timeline";
import { viewQuery, type ViewRequest } from "@/lib/view";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * How long the URL waits behind the scrubber before it is rewritten.
 *
 * Not about history entries — `replaceState` never makes one, which is the acceptance
 * criterion, and that is true at any frequency. It is about the browsers: Firefox and
 * Safari both rate-limit the History API and throw once a page calls it too often in a
 * short window, and a drag across a day is several hundred calls in a couple of seconds.
 * A trailing timer means the URL is rewritten once the scrubber settles, which is also
 * the only moment anyone is going to copy it.
 */
const URL_SETTLE_MS = 200;

/**
 * How often the live curve is allowed to be redrawn.
 *
 * **A ladder and a smile are read differently and that is the whole argument.** The
 * chain screen takes every push, and it is right to: a ladder is read one cell at a
 * time, so a cell that changes under the eye is a cell that has news. A smile is read as
 * a *shape* — the tilt of the wings against the middle — and a shape has to hold still
 * long enough to be seen. At the stream's own rate the curve shimmers and the skew is
 * unreadable; at about a second it still reads as live and the shape settles between
 * frames.
 *
 * The sampler below is trailing, not leading: it publishes the newest push it holds when
 * the tick comes round and drops everything older, so the curve is never more than one
 * interval behind the stream and never renders a frame it is about to throw away. The
 * cost is that the first curve after a subscription waits up to one interval, which is
 * invisible beside the round trip that preceded it.
 */
const LIVE_REDRAW_MS = 1000;

/**
 * The volatility screen: a smile, a day to move it through, and everything a reader
 * needs to know how far to trust what is on the axis.
 *
 * **Everything the header says is read from the response.** The forward, the minute, the
 * clock the volatility is quoted on, which forward method produced it and which model
 * stamp is on the rows — none of it is hardcoded, because all of it can change between
 * one minute and the next and the forward convention alone is worth up to 3.9 vol points
 * on the axis below.
 *
 * **Two admissions are made loudly rather than quietly.** A response spanning two model
 * stamps says so instead of picking one, and an expiry inside a day of settlement carries
 * a warning naming what the spike is — measured at 07:38Z, the front expiry's median IV
 * was 62.5% and its maximum 400.5% at 4.4 hours out, against a median near 40% and a
 * maximum under 72% everywhere else on the board. That is vega collapsing, not our
 * arithmetic failing, and an unmarked 400% reads as the second thing.
 *
 * **The whole day arrives in one request and the scrubber is an index into it.** No
 * fetch is issued while time moves; `docs/smile-contract.md` sends the day precisely so
 * that none has to be. The two comparison overlays are a second index into the same
 * array, so switching one on costs no request either.
 *
 * **The right edge is live.** The store withholds the open minute by design, so the
 * newest sealed bar always trails the market. The chain stream fills that edge: the same
 * push the ladder renders, projected into a smile minute by `lib/livesmile.ts` and
 * sampled to about 1 Hz by `LIVE_REDRAW_MS`. Standing on the right edge follows it;
 * scrubbing off it pins a stored minute by stamp and stops following.
 *
 * **The view has an address.** Underlying, expiry and minute go into the URL in UTC, so
 * a link means the same curve in every timezone, and they come back out of it on load.
 * The clock on the scrubber is local because a control is read against a wall clock; the
 * identity of the view is not. `lib/view.ts` holds that boundary.
 *
 * A client component because it holds the selection, the scale toggle, the position in
 * time and the fetch. The route around it stays a server component, which is also what
 * reads the URL and hands the initial view down.
 */
export default function VolatilityScreen({ initial }: { initial: ViewRequest }) {
  const [underlying, setUnderlying] = useState<Underlying>(initial.underlying ?? "BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>(initial.expiry ?? "");

  const [smile, setSmile] = useState<SmileResponse | null>(null);
  const [source, setSource] = useState<Source>("engine");
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);

  /**
   * The minute the reader has asked for, as the store's own UTC stamp — or `null`,
   * which means "whatever the right edge is".
   *
   * The stamp rather than the index, because the stamp is what the URL carries and what
   * the store calls it; an index is a fact about one particular response and would mean
   * something different the moment the day grew by a minute.
   */
  const [wanted, setWanted] = useState<string | null>(initial.minute);

  /**
   * Linear, always, on first paint. The log axis is right on a dying contract and wrong
   * on every other one, and an axis that changed shape depending on which expiry you
   * picked would make two ordinary smiles incomparable by eye.
   */
  const [scale, setScale] = useState<VolScale>("linear");

  /**
   * Which comparison overlays are on.
   *
   * **Deliberately not reset when the underlying or the expiry changes.** An overlay is
   * a way of reading a curve, like the axis toggle beside it, not a fact about one
   * series — a reader comparing every expiry against an hour ago wants to keep
   * comparing. It is reset only by being switched off, which is a click, and it costs
   * nothing to carry across a series change because the new day arrives with its own
   * hour-ago minute in it.
   */
  const [overlayOn, setOverlayOn] = useState<OverlayState>(NO_OVERLAYS);

  /** The newest push the sampler has published. See `LIVE_REDRAW_MS`. */
  const [liveChain, setLiveChain] = useState<ChainResponse | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("connecting");
  const [liveDetail, setLiveDetail] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow earlier request landing after a newer one.
  const listRequest = useRef(0);
  const smileRequest = useRef(0);

  const loadExpiryList = useCallback(async (next: Underlying, wantedExpiry: string | null) => {
    const id = ++listRequest.current;
    setBusy(true);
    setError(null);
    try {
      const list = await loadExpiries(next);
      if (id !== listRequest.current) return;

      const available = list.data.expiries;
      setExpiries(available);

      const fallback =
        list.preferredExpiry && available.includes(list.preferredExpiry)
          ? list.preferredExpiry
          : (available[0] ?? "");
      const chosen =
        wantedExpiry && available.includes(wantedExpiry) ? wantedExpiry : fallback;
      setExpiry(chosen);
      if (!chosen) setError(`No expiries listed for ${next}.`);
    } catch (err) {
      if (id !== listRequest.current) return;
      setError(message(err));
    } finally {
      if (id === listRequest.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadExpiryList(underlying, expiry || null);
    // Keyed on the underlying alone: re-running this when the expiry changes would
    // refetch a list that cannot have changed and reset the dropdown under the reader.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying, loadExpiryList]);

  useEffect(() => {
    if (!expiry) return;
    const id = ++smileRequest.current;
    setSmile(null);
    setError(null);
    setBusy(true);

    void (async () => {
      try {
        const loaded = await loadSmile(underlying, expiry);
        if (id !== smileRequest.current) return;
        setSmile(loaded.data);
        setSource(loaded.source);
        setFallbackReason(loaded.fallbackReason ?? null);
      } catch (err) {
        if (id !== smileRequest.current) return;
        setError(message(err));
      } finally {
        if (id === smileRequest.current) setBusy(false);
      }
    })();
  }, [underlying, expiry]);

  /**
   * One live subscription per series, sampled down to `LIVE_REDRAW_MS`.
   *
   * **The same socket the chain screen reads, and the same object off it.** The stream
   * carries a chain; this screen draws a smile; `lib/livesmile.ts` is the projection
   * between them and carries the argument for doing it here rather than asking the
   * engine for a second payload or polling `/smile` on a timer. Nothing is recomputed —
   * the volatility this chart plots is the identical field of the identical push the
   * ladder prints.
   *
   * `latest` is a plain local rather than a ref because it belongs to this
   * subscription: it is created with the socket and dies with it, so a push that
   * arrived for the old expiry can never be published against the new one.
   */
  useEffect(() => {
    if (!expiry) return;
    setLiveChain(null);

    let latest: ChainResponse | null = null;
    const sampler = window.setInterval(() => {
      if (latest === null) return;
      setLiveChain(latest);
      latest = null;
    }, LIVE_REDRAW_MS);

    const stop = subscribeChain(underlying, expiry, {
      onChain: (chain) => {
        latest = chain;
      },
      onStatus: (next, detail) => {
        setLiveStatus(next);
        setLiveDetail(detail ?? null);
      },
    });

    return () => {
      window.clearInterval(sampler);
      stop();
    };
  }, [underlying, expiry]);

  /**
   * The live push as a minute of the smile, or `null`.
   *
   * The series is checked rather than assumed. The subscription is torn down and rebuilt
   * on every change of underlying or expiry, so a mismatch should be impossible — and
   * this screen puts two sources of the same expiry's curve on one axis, which is
   * exactly the place where "should be impossible" is worth one comparison.
   */
  const liveMinute = useMemo(() => {
    if (!liveChain) return null;
    if (liveChain.underlying !== underlying || liveChain.expiry !== expiry) return null;
    return smileMinuteFromChain(liveChain);
  }, [liveChain, underlying, expiry]);

  /**
   * The day laid out minute by minute, and the board's full strike list.
   *
   * Both are derived from the response and both are memoised on it, because both are
   * walked on every drag step and neither can change while the reader is dragging. The
   * live minute is then folded onto the right edge of each: `withLiveMinute` puts it one
   * position past the store's last sealed bar, and `mergeStrikes` adds any strike the
   * push lists that the stored day never did — returning the same array when there is
   * none, so a second's tick does not rebuild every series on the chart.
   */
  const storedTimeline = useMemo(() => (smile ? buildTimeline(smile) : EMPTY_TIMELINE), [smile]);
  const timeline = useMemo(
    () => withLiveMinute(storedTimeline, liveMinute),
    [storedTimeline, liveMinute],
  );

  const storedGrid = useMemo(() => (smile ? strikeGrid(smile) : []), [smile]);
  const grid = useMemo(
    () => mergeStrikes(storedGrid, liveMinute?.points ?? []),
    [storedGrid, liveMinute],
  );

  const last = lastIndex(timeline);
  const askedIndex = wanted === null ? -1 : indexOfStamp(timeline, wanted);
  /** No answer for what was asked means the right edge, which is the newest minute. */
  const index = askedIndex >= 0 ? askedIndex : last;

  /**
   * A link naming a minute this expiry's store does not reach at all — not a hole in the
   * middle of the day, which is a position you can stand on, but a stamp outside it. It
   * is said out loud rather than silently rounded to the nearest curve: quietly showing
   * a different minute than the one in the address bar is the same class of error as
   * drawing a line across a gap.
   */
  const unreachable = wanted !== null && askedIndex < 0 && last >= 0 ? wanted : null;

  const stamp = index >= 0 ? (timeline.stamps[index] ?? null) : null;
  const minute = index >= 0 ? (timeline.minutes[index] ?? null) : null;

  /** Standing on the live position — the right edge, following the stream. */
  const onLive = index >= 0 && index === timeline.liveIndex;

  const rows = useMemo(() => (minute ? toRows(minute, grid) : []), [minute, grid]);
  const solved = solvedPercents(rows);
  const hours = minute ? hoursToExpiry(minute) : null;
  const stamps = smile?.model_versions ?? [];

  /**
   * The overlays that are switched on, each resolved against the minute the scrubber is
   * standing on — never against the wall clock. `lib/overlay.ts` carries the argument
   * for both the offsets and the exact anchor. **No request is issued here or anywhere
   * below it**: an overlay is a second index into the day the client already holds.
   */
  const resolved = useMemo(
    () => OVERLAYS.filter((spec) => overlayOn[spec.id]).map((spec) => resolveOverlay(spec, stamp, timeline)),
    [overlayOn, stamp, timeline],
  );

  const drawn: ChartOverlay[] = [];
  const absent: typeof resolved = [];
  for (const overlay of resolved) {
    if (overlay.minute) drawn.push({ id: overlay.spec.id, label: overlay.spec.label, minute: overlay.minute });
    else absent.push(overlay);
  }

  const toggleOverlay = (id: OverlayId) =>
    setOverlayOn((current) => ({ ...current, [id]: !current[id] }));

  /**
   * The address bar follows the view; it never drives it after the first render.
   *
   * `window.history.replaceState` rather than a router push, for the reason the ticket
   * gives: a scrubber that filled the back button would be broken. Next's own docs make
   * the native call the supported way to do this, and it syncs the router without a
   * navigation. See `URL_SETTLE_MS` for why it waits.
   */
  useEffect(() => {
    if (!expiry || !stamp) return;
    const query = viewQuery(underlying, expiry, stamp);
    if (window.location.search === query) return;
    const timer = window.setTimeout(() => {
      window.history.replaceState(null, "", query);
    }, URL_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [underlying, expiry, stamp]);

  /** A different series is a different day; the position in the old one means nothing. */
  const pickUnderlying = (next: Underlying) => {
    setUnderlying(next);
    setWanted(null);
  };

  const pickExpiry = (next: string) => {
    setExpiry(next);
    setWanted(null);
  };

  /**
   * Where the scrubber puts the view, and the one place "following live" is spelled.
   *
   * **Standing on the right edge means following the right edge, not pinning the stamp
   * that happens to be there.** The live minute rolls over once a minute; pinning its
   * stamp would leave the reader looking at a curve that stopped updating the moment
   * they stepped onto it, and the screen would be stuck on a stale minute in exactly the
   * way the ticket forbids. `null` is already this component's spelling of "whatever the
   * right edge is", so the edge case is the absence of a case.
   *
   * Every other position pins by stamp, which is what makes scrubbing back and then
   * forward again land where it started even though the timeline grew underneath it.
   */
  const pickIndex = (next: number) => {
    const at = clampIndex(timeline, next);
    if (at < 0 || at === lastIndex(timeline)) {
      setWanted(null);
      return;
    }
    setWanted(timeline.stamps[at] ?? null);
  };

  return (
    <div className="shell">
      <header className="header">
        <div className="brand">DELTA</div>
        <h1 className="screen-title">Volatility</h1>

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

      <main className="main">
        <div className="tabs" role="tablist" aria-label="Volatility views">
          <button type="button" role="tab" className="tab" aria-selected="true">
            Smile
          </button>
          <button
            type="button"
            role="tab"
            className="tab"
            aria-selected="false"
            aria-disabled="true"
            disabled
          >
            IV vs RV <span className="tab-soon">soon</span>
          </button>
        </div>

        {error ? <p className="notice error">{error}</p> : null}

        {liveStatus === "error" ? (
          <p className="notice error">
            {liveDetail ?? "The live stream reported an error."} The stored day below is
            unaffected; only the right edge has stopped moving.
          </p>
        ) : null}

        {liveStatus === "closed" ? (
          <p className="notice warn">
            Lost the connection to the engine at <code>{ENGINE_URL}</code>. Retrying — the
            curve is the last minute that arrived and it is not moving.
          </p>
        ) : null}

        {source === "fixture" && !error ? (
          <p className="notice warn">
            Showing the committed smile fixture, not the store — a real capture of{" "}
            <strong>
              {storedTimeline.stamps.length} minute
              {storedTimeline.stamps.length === 1 ? "" : "s"}
            </strong>
            , not a day.{" "}
            {fallbackReason ?? `Set NEXT_PUBLIC_USE_FIXTURE=0 and start the engine for stored data.`}
          </p>
        ) : null}

        {unreachable ? (
          <p className="notice warn">
            The link asked for <strong>{unreachable}</strong>, which is outside the minutes
            stored for {underlying} {expiry}. Showing the most recent minute instead — the
            nearest curve is not the one you asked for, so it is not offered as one.
          </p>
        ) : null}

        {stamps.length > 1 ? (
          <p className="notice warn">
            This response spans <strong>{stamps.length} model stamps</strong> — the curves
            in this day were not all computed the same way. {stamps.join(" · ")}. The
            forward convention alone is worth up to 3.9 volatility points, and this screen
            plots nothing but volatility points.
          </p>
        ) : null}

        {minute && isDying(minute) && hours !== null ? (
          <p className="notice warn">
            <strong>Under a day to settlement — {formatTimeToExpiry(hours)}.</strong> Vega
            collapses as time to expiry goes to zero, so a one-tick price change moves the
            implied volatility by tens of points. A spike here is a{" "}
            <strong>dying contract, not a computation error</strong>. Measured at 07:38Z on
            the front expiry: median 62.5%, maximum 400.5% at 4.4 hours out, against a
            median near 40% and a maximum under 72% everywhere else on the board. Switch
            the volatility axis to LOG to read the at-the-money region.
          </p>
        ) : null}

        {/* The scrubber, above the plot: it changes which minute is drawn, so it sits
            with the chart rather than in the header with the figures it changes. */}
        {timeline.stamps.length > 0 && !error ? (
          <TimeScrubber timeline={timeline} index={index} onChange={pickIndex} />
        ) : null}

        {/* The chart's own controls, on their own row above the plot rather than in the
            header: they change how the chart is drawn and nothing about what the figures
            say. The overlay buttons carry their own swatch, so the control *is* the key
            — a legend under the plot would be the same information twice, in the place a
            reader looks last. */}
        <div className="plot-controls">
          <span className="stat-label">Compare</span>
          <div className="overlay-toggle" role="group" aria-label="Comparison overlays">
            {OVERLAYS.map((spec) => (
              <button
                key={spec.id}
                type="button"
                className="overlay-option"
                data-overlay={spec.id}
                aria-pressed={overlayOn[spec.id]}
                title={spec.title}
                onClick={() => toggleOverlay(spec.id)}
              >
                <span className="overlay-swatch" aria-hidden="true" />
                {spec.label}
              </button>
            ))}
          </div>

          <span className="plot-controls-gap" />

          <span className="stat-label">Vol axis</span>
          <div className="scale-toggle" role="group" aria-label="Volatility axis scale">
            {(["linear", "log"] as const).map((option) => (
              <button
                key={option}
                type="button"
                className="scale-option"
                aria-pressed={scale === option}
                onClick={() => setScale(option)}
              >
                {option === "linear" ? "LIN" : "LOG"}
              </button>
            ))}
          </div>
        </div>

        {/* An overlay that is on and has nothing to draw. Said in words, at the size of
            every other admission on this screen, because a series that renders as
            nothing and a series that renders as zero are the same picture and only one
            of them is true. */}
        {absent.map((overlay) => (
          <p key={overlay.spec.id} className="notice warn">
            <span className="overlay-swatch" data-overlay={overlay.spec.id} aria-hidden="true" />{" "}
            <strong>{overlay.spec.label} has nothing to draw.</strong> {overlay.absence} No
            line is drawn for it and none is implied: a flat line would claim the
            volatility was zero at that minute, and an overlay that quietly rendered as
            nothing would be indistinguishable from one that had.
          </p>
        ))}

        {renderPlot()}

        <p className="note">
          One point per strike, one strike per listed contract, at the minute the scrubber
          is standing on. <strong>Nothing here is fitted, smoothed or interpolated</strong>:
          the dots are the volatilities the engine solved and the segments between them are
          straight, because a spline would put a number between two strikes in exactly the
          place a reader would take one off. A dotted vertical rule is a strike that arrived
          with no solved volatility — the line breaks there and is never drawn through it,
          and it breaks the same way at a strike this minute stored no row for at all.
          Both x-axes are one linear scale in strike, read once as a strike and once as an
          offset from the forward, which is why their ticks are evenly spaced; the strike
          axis is the whole day&rsquo;s board, so it holds still while the scrubber moves.
          Hover any point for the strike, the volatility, its offset from the forward, the
          out-of-the-money leg it was solved on, the solver&rsquo;s reason when there is no
          number, and the same strike on any overlay that is switched on.{" "}
          <strong>
            The right edge is live and redraws about once a second
          </strong>{" "}
          — the same push the chain screen&rsquo;s ladder renders, sampled down because a
          shape has to hold still to be read while a ladder does not. Everything left of
          it is a sealed minute out of the store. The overlays are the same expiry an hour
          and a day earlier, anchored to the minute the scrubber is on rather than to the
          clock, drawn from minutes already in this response &mdash; switching one on asks
          the engine for nothing. An anchor the store holds no bar for draws no line and
          says so above.
        </p>
      </main>
    </div>
  );

  /**
   * Every state the store can legitimately be in, and none of them is an error page.
   *
   * `docs/smile-contract.md` makes absence a 200 with an empty `minutes`: an underlying
   * nobody has collected yet and a day nobody has lived through are both "nothing yet".
   * So the empty cases render as an explanation in the plot's own box, at the size the
   * chart would occupy, and the screen does not change shape when data arrives.
   *
   * **A position with no stored minute is one of those states, not an accident.** It
   * renders empty. Drawing the neighbouring minute's curve there would be the same error
   * as joining the line across a gap — a shape in a place where there is no shape — and
   * it is the one thing a scrubber makes easy to do by mistake.
   */
  function renderPlot() {
    if (error) return null;

    if (timeline.stamps.length === 0) {
      return (
        <section className="plot-empty" aria-label="Smile plot">
          <p>
            {busy || !smile
              ? "Reading the store…"
              : `No stored minutes for ${underlying} ${expiry}, and nothing on the stream yet.`}
            {smile && timeline.stamps.length === 0 ? (
              <>
                <br />
                Nothing has gone wrong — the store is answering &ldquo;nothing yet&rdquo;.
              </>
            ) : null}
          </p>
        </section>
      );
    }

    if (!minute) {
      return (
        <section className="plot-empty" aria-label="Smile plot">
          <p>
            No minute stored at{" "}
            <strong>
              {stamp ? formatLocalClock(stamp) : "—"} {stamp ? localZoneLabel(stamp) : ""}
            </strong>{" "}
            for {underlying} {expiry}.
            <br />
            {stamp} — the store wrote no bar here, so nothing is drawn. The curve either
            side of it belongs to another minute.
          </p>
        </section>
      );
    }

    if (rows.length === 0) {
      return (
        <section className="plot-empty" aria-label="Smile plot">
          <p>
            {underlying} {expiry} has a stored minute at{" "}
            {formatFetchedClock(minute.minute)} UTC with no strikes on it.
          </p>
        </section>
      );
    }

    if (solved.length === 0) {
      return (
        <section className="plot-empty" aria-label="Smile plot">
          <p>
            {rows.length} strikes at {formatFetchedClock(minute.minute)} UTC, and not one of
            them solved.
            <br />
            Every point carries a reason; there is no curve to draw.
          </p>
        </section>
      );
    }

    return (
      <>
        {minute.forward === null ? (
          <p className="notice warn">
            This minute has no fitted forward, so there is no offset axis and no reference
            line. The strikes and their volatilities are unchanged.
          </p>
        ) : null}
        <SmileChart
          // Re-keyed per series and per scale, and deliberately **not** per minute. A
          // different expiry is a different curve and a different axis is a different
          // chart, so both remount; a different minute is new data on the same chart, and
          // remounting the whole SVG on every drag step is the difference between a
          // scrubber that feels like dragging and one that stutters. Nothing animates
          // out of the last minute because nothing on this chart animates at all.
          key={`${underlying}:${expiry}:${scale}`}
          minute={minute}
          grid={grid}
          scale={scale}
          underlying={underlying}
          expiry={expiry}
          overlays={drawn}
        />
      </>
    );
  }
}
