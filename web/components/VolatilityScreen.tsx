"use client";

import { useEffect, useMemo, useState } from "react";

import PlotControls from "@/components/PlotControls";
import SmileNote from "@/components/SmileNote";
import SmileNotices from "@/components/SmileNotices";
import SmilePlot from "@/components/SmilePlot";
import TimeScrubber from "@/components/TimeScrubber";
import VolatilityHeader from "@/components/VolatilityHeader";
import { useLiveSmile } from "@/hooks/useLiveSmile";
import { useSmileDay } from "@/hooks/useSmileDay";
import type { Underlying } from "@/lib/contract";
import {
  NO_OVERLAYS,
  resolveOverlays,
  splitOverlays,
  type OverlayId,
  type OverlayState,
} from "@/lib/overlay";
import { positionOf } from "@/lib/position";
import { hoursToExpiry, mergeStrikes, strikeGrid } from "@/lib/smile";
import { type VolScale } from "@/lib/smilemodel";
import {
  buildTimeline,
  clampIndex,
  EMPTY_TIMELINE,
  lastIndex,
  withLiveMinute,
} from "@/lib/timeline";
import { viewQuery, type ViewRequest } from "@/lib/view";

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
 * The volatility screen: a smile, a day to move it through, and everything a reader
 * needs to know how far to trust what is on the axis.
 *
 * This file holds the state and the layout and nothing else. Each of the jobs it used to
 * do in line has its own file now: `hooks/useSmileDay.ts` fetches the day,
 * `hooks/useLiveSmile.ts` follows the stream, `lib/position.ts` turns the reader's
 * request into a position, `lib/overlay.ts` resolves the comparisons, and
 * `VolatilityHeader`, `SmileNotices`, `PlotControls` and `SmilePlot` draw the four parts.
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
 * sampled to about 1 Hz — see `hooks/useLiveSmile.ts`. Standing on the right edge
 * follows it;
 * scrubbing off it pins a stored minute by stamp and stops following.
 *
 * **The view has an address.** Underlying, expiry and minute go into the URL in UTC, so
 * a link means the same curve in every timezone, and they come back out of it on load.
 * The clock on the scrubber is local because a control is read against a wall clock; the
 * identity of the view is not. `lib/view.ts` holds that boundary.
 *
 * A client component because it holds the selection, the scale toggle and the position
 * in time. The route around it stays a server component, which is also what reads the
 * URL and hands the initial view down.
 */
export default function VolatilityScreen({ initial }: { initial: ViewRequest }) {
  const [underlying, setUnderlying] = useState<Underlying>(initial.underlying ?? "BTC");
  /**
   * The stored day, and the expiry it belongs to. The list request is what settles which
   * expiry is being read, so both live behind one hook — see `hooks/useSmileDay.ts`.
   */
  const day = useSmileDay(underlying, initial.expiry ?? "");
  const { expiries, expiry, smile, source, fallbackReason, busy, error } = day;

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

  const live = useLiveSmile(underlying, expiry);
  const liveMinute = live.minute;

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

  const { index, stamp, minute, unreachable } = positionOf(timeline, wanted);

  const hours = minute ? hoursToExpiry(minute) : null;
  const stamps = smile?.model_versions ?? [];

  /**
   * The overlays that are switched on, each resolved against the minute the scrubber is
   * standing on — never against the wall clock. `lib/overlay.ts` carries the argument
   * for both the offsets and the exact anchor. **No request is issued here or anywhere
   * below it**: an overlay is a second index into the day the client already holds.
   */
  const resolved = useMemo(
    () => resolveOverlays(overlayOn, stamp, timeline),
    [overlayOn, stamp, timeline],
  );

  const { drawn, absent } = splitOverlays(resolved);

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
    day.setExpiry(next);
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
      <VolatilityHeader
        underlying={underlying}
        onPickUnderlying={pickUnderlying}
        expiries={expiries}
        expiry={expiry}
        onPickExpiry={pickExpiry}
        busy={busy}
        minute={minute}
        stamp={stamp}
        hours={hours}
        stamps={stamps}
        source={source}
        fallbackReason={fallbackReason}
        liveStatus={live.status}
        liveDetail={live.detail}
      />

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

        <SmileNotices
          error={error}
          liveStatus={live.status}
          liveDetail={live.detail}
          source={source}
          fallbackReason={fallbackReason}
          storedMinutes={storedTimeline.stamps.length}
          unreachable={unreachable}
          underlying={underlying}
          expiry={expiry}
          stamps={stamps}
          minute={minute}
          hours={hours}
        />

        {/* The scrubber, above the plot: it changes which minute is drawn, so it sits
            with the chart rather than in the header with the figures it changes. */}
        {timeline.stamps.length > 0 && !error ? (
          <TimeScrubber timeline={timeline} index={index} onChange={pickIndex} />
        ) : null}

        <PlotControls
          overlayOn={overlayOn}
          onToggleOverlay={toggleOverlay}
          scale={scale}
          onPickScale={setScale}
        />

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

        <SmilePlot
          error={error}
          busy={busy}
          hasResponse={smile !== null}
          timeline={timeline}
          underlying={underlying}
          expiry={expiry}
          stamp={stamp}
          minute={minute}
          grid={grid}
          scale={scale}
          overlays={drawn}
        />

        <SmileNote />
      </main>
    </div>
  );
}
