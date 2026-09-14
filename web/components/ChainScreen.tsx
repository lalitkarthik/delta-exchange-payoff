"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ChainLadder } from "@/components/ChainLadder";
import ContractPanel from "@/components/ContractPanel";
import LegsPanel from "@/components/LegsPanel";
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
import {
  ENGINE_URL,
  commandFeed,
  type FeedCommand,
  loadChainAt,
  loadChainMinutes,
  loadExpiries,
} from "@/lib/engine";
import { looksCanonical } from "@/lib/instrument";
import { addLeg, dropFirst, heldAt } from "@/lib/legs";
import { LegsUrlError, analyseHref, decodeLegs, encodeLegs, syncedUrl } from "@/lib/legs-url";
import {
  feedBadge,
  LIVE_STATUS_LABEL,
  subscribeChain,
  type FeedStatus,
  type LiveStatus,
} from "@/lib/live";
import { formatFetchedAt, formatFetchedClock, formatSpot } from "@/lib/format";
import type { Direction as LegDirection, LegRequest } from "@/lib/payoff";
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
 * P4: the strategy the link that opened this page named, decoded exactly once.
 *
 * One `try` rather than two computing the same thing independently — this file used
 * to seed `legs` and `legsError` from two separate `useState` initialisers that each
 * called `decodeLegs(initialLegsParam)` on their own, agreeing only because nothing
 * had yet edited one without the other. A malformed `legs=` is never treated as "no
 * strategy": `decodeLegs` throws naming the part that was wrong, and `error` here
 * carries that message for `ChainScreen` to show loudly, rather than quietly starting
 * the reader on an empty position their link did not ask for.
 */
function decodeInitialLegs(param: string | null): { legs: LegRequest[]; error: string | null } {
  try {
    return { legs: decodeLegs(param), error: null };
  } catch (err) {
    return { legs: [], error: err instanceof LegsUrlError ? err.message : message(err) };
  }
}

/**
 * `?underlying=BTC&expiry=04-09-2026` live, `&minute=...` added standing anywhere else,
 * `&instrument=...` added whenever the chart panel is open, `&legs=...` added whenever
 * the strategy is non-empty. `lib/view.ts`'s `viewQuery` was not reused: that one always
 * writes a minute, and "live" here is the absence of one rather than a stamp that
 * happens to be the newest.
 */
function chainQuery(
  underlying: Underlying,
  expiry: string,
  minute: string | null,
  instrument: string | null,
  legs: LegRequest[],
): string {
  const params = new URLSearchParams({ underlying, expiry });
  if (minute) params.set("minute", minute);
  if (instrument) params.set("instrument", instrument);
  const encoded = encodeLegs(legs);
  if (encoded) params.set("legs", encoded);
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
 * it is not.** `following` — from `positionOf` — is the one switch: true subscribes
 * `/ws/chain` and tears down any historical fetch, false does the opposite. Releasing
 * the slider back onto the right edge is nothing more than `wanted` returning to `null`,
 * which is already this codebase's spelling of "follow whatever the right edge is".
 *
 * **`following`, and not `positionOf`'s `onLive`, and the difference is load-bearing.**
 * `onLive` asks whether a live push has been placed on the timeline; `following` asks
 * whether the reader has pinned a historical minute. The first cannot be true until a
 * socket has already delivered something, so a subscription gated on it never opens —
 * no socket, no push, no live index, no socket. That was issue #49, and it left this,
 * the app's primary screen, showing "Connecting to the engine…" for ever on any load
 * without a `minute=` in the URL. Every switch on this screen that decides between the
 * live ladder and a stored one now reads `following`; see the note over `chain` for the
 * labels, which have to agree with it or lie quietly.
 */
export default function ChainScreen({
  initial,
  initialInstrument,
  initialLegsParam,
}: {
  initial: ViewRequest;
  /** #46: the canonical instrument string the URL carried on load, or `null`. Read by
   * `app/page.tsx` separately from `initial` — see that file's comment on why. */
  initialInstrument: string | null;
  /**
   * P4: the raw `legs=` query value, undecoded. Decoded here rather than in
   * `app/page.tsx` — unlike `initialInstrument`'s lenient `looksCanonical` gate, a
   * malformed strategy link is not treated as absent (`lib/legs-url.ts`'s whole reason
   * to exist), so decoding it has to happen somewhere that can show the reader a loud
   * notice rather than silently rendering an empty panel. A server component has no
   * such notice to show into.
   */
  initialLegsParam: string | null;
}) {
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

  /** The venue feed's own state, #40 — distinct from `liveStatus` above, which is this
   * browser's socket to the engine. `null` until the engine's first `feed` message. */
  const [feedStatus, setFeedStatus] = useState<FeedStatus | null>(null);

  const [historicalChain, setHistoricalChain] = useState<HistoricalChain | null>(null);
  const [historicalWaiting, setHistoricalWaiting] = useState(false);
  const [historicalBusy, setHistoricalBusy] = useState(false);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * P4: `legs` and `legsError` both read off this one state rather than each holding
   * an independent copy — see `decodeInitialLegs`'s own comment for the bug this
   * replaced. `error` clears itself the moment the reader makes their own edit
   * (`pickLeg`, below): once they hold a strategy they built, a stale complaint about
   * the link that opened the page no longer describes what is on screen.
   */
  const [legsState, setLegsState] = useState(() => decodeInitialLegs(initialLegsParam));
  const legs = legsState.legs;
  const legsError = legsState.error;

  /**
   * A strategy cleared out from under the reader without a click of their own —
   * switching underlying or expiry, `pickUnderlying`/`pickExpiry` below — gets one
   * line saying so, the same "never silently" rule `legsError` keeps for a malformed
   * link. Cleared on the next pick or the next switch, so it never survives to
   * describe a clearing that is no longer the most recent thing that happened.
   */
  const [legsClearedNotice, setLegsClearedNotice] = useState<string | null>(null);

  /* The feed controls' in-flight guard. The ref is what makes a double-click a no-op —
     state alone would let a second click through before React re-rendered the first. */
  const [feedCommandPending, setFeedCommandPending] = useState(false);
  const feedCommandInFlight = useRef(false);

  /** #46: the contract chart panel. `null` means closed. Seeded from the URL so a link
   * carrying a contract opens straight onto its chart, as `docs/bars-contract.md`'s own
   * user story asks for. */
  const [panelInstrument, setPanelInstrument] = useState<string | null>(initialInstrument);

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

      // A link naming an expiry this underlying no longer lists — the common case is a
      // contract's expiry rolling off Delta's board — falls back to a different one
      // silently here, and a chart panel seeded from that same link names a contract on
      // the expiry that was asked for, not the one just substituted. Closing it is the
      // same call `pickExpiry` already makes for a reader-driven change; this is the
      // page doing it for a change nobody asked for in this session.
      if (wanted && chosen !== wanted) setPanelInstrument(null);

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

  const { index, stamp, following, unreachable } = positionOf(timeline, wanted);

  // The live subscription: open unless the reader has pinned a historical minute.
  // Dragging off the right edge tears the socket down; releasing back onto it reopens
  // one.
  //
  // **`following`, never `onLive`.** `onLive` says a push has already been placed on the
  // timeline, which cannot be true before a socket exists — gating the socket on it is
  // the deadlock of #49, and it left the primary screen on "Connecting to the engine…"
  // for ever. What should be asked here is whether the reader wants the stream, and that
  // is answerable on the first render.
  useEffect(() => {
    if (!expiry || !following) return;
    setLiveChain(null);
    setFeedStatus(null);
    return subscribeChain(underlying, expiry, {
      onChain: setLiveChain,
      onStatus: (next, detail) => {
        setLiveStatus(next);
        setLiveStatusDetail(detail ?? null);
      },
      onFeed: setFeedStatus,
    });
  }, [underlying, expiry, following]);

  // The historical read: one request per stored minute the slider stands on.
  useEffect(() => {
    if (following || !stamp) {
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
  }, [underlying, expiry, following, stamp]);

  // The address bar follows the slider and the chart panel; it never drives either
  // after the first render.
  //
  // `following` rather than `onLive`, so a page that is following the stream but has not
  // yet been sent a push writes no `minute=` — a link copied in that first second must
  // mean "live", which is what the reader is looking at, not the last sealed minute.
  useEffect(() => {
    if (!expiry) return;
    // **`syncedUrl`, never `chainQuery` unwrapped.** Follow-up to P4 (#5): this effect
    // used to call `chainQuery(...)` unconditionally, so a malformed `?legs=` link was
    // erased by this very settle timer before the reader could copy the broken text
    // back out — diagnosed correctly in P4's review, but the remedy applied there
    // (clearing the notice once the reader made an edit) stopped the notice going
    // stale without stopping the rewrite that caused it. `syncedUrl` is the shared
    // gate `lib/legs-url.ts` now carries for exactly this — see its own comment for
    // the invariant and the rest of this history — and it is what `AnalyseScreen`
    // already routes its own query builder through, via `syncedAnalyseHref`.
    const query = syncedUrl(legsError, () =>
      chainQuery(underlying, expiry, following ? null : stamp, panelInstrument, legs),
    );
    if (query === null) return;
    if (window.location.search === query) return;
    const timer = window.setTimeout(() => {
      window.history.replaceState(null, "", query);
    }, URL_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [underlying, expiry, following, stamp, panelInstrument, legs, legsError]);

  const pickUnderlying = (next: Underlying) => {
    setUnderlying(next);
    setWanted(null);
    // A contract from the old underlying's chain has no row on the new one; open panel,
    // wrong ladder underneath it is worse than a closed one.
    setPanelInstrument(null);
    // Every leg names an instrument on the old underlying's chain — #2's "one expiry
    // per strategy" refusal is about two expiries of the *same* underlying and does not
    // even apply across two different underlyings, so there is no series here to carry.
    // Said out loud (review round 1, minor #7) rather than left to be noticed by its
    // absence: this screen's organising rule is that a leg is never dropped quietly.
    if (legs.length > 0) {
      setLegsClearedNotice(
        `Switched to ${next} — the strategy named ${legs.length === 1 ? "a contract" : "contracts"} ` +
          `on the previous underlying's chain, which does not follow across underlyings.`,
      );
    }
    setLegsState({ legs: [], error: null });
  };

  const pickExpiry = (next: string) => {
    setExpiry(next);
    setWanted(null);
    setPanelInstrument(null);
    // A leg names its own expiry inside its instrument string; picking a different one
    // here would leave the strategy pointed at contracts no longer on this ladder.
    if (legs.length > 0) {
      setLegsClearedNotice(
        `Switched to expiry ${next} — the strategy named ${legs.length === 1 ? "a contract" : "contracts"} ` +
          `on the previous expiry, which does not follow across expiries.`,
      );
    }
    setLegsState({ legs: [], error: null });
  };

  /**
   * B and S both add and remove, depending on what is already held — `lib/legs.ts`'s
   * `heldAt`/`addLeg`/`dropFirst`, the same pure functions the DOM fingerprint test
   * pins. The button lights when the contract beside it is in the strategy, so clicking
   * a lit one has to take it back off; it removes **one lot**, decrementing a leg held
   * at quantity 2 or more before ever deleting it outright (review round 1, important
   * #1) — a two-lot position built by clicking B twice comes off one lot at a time.
   *
   * Clears both notices: a reader who has just built or edited a leg is not still
   * looking at a complaint about the link that opened the page, or a note about a
   * strategy that got cleared several clicks ago.
   */
  const pickLeg = (instrument: string, direction: LegDirection) => {
    setLegsClearedNotice(null);
    setLegsState((current) => ({
      legs: heldAt(current.legs, instrument, direction)
        ? dropFirst(current.legs, instrument, direction)
        : addLeg(current.legs, instrument, direction),
      error: null,
    }));
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

  const runFeedCommand = async (command: FeedCommand) => {
    if (feedStatus === null || feedCommandInFlight.current) return;
    feedCommandInFlight.current = true;
    setFeedCommandPending(true);
    setError(null);
    try {
      await commandFeed(feedStatus.adapter, command);
    } catch (err) {
      setError(message(err));
    } finally {
      feedCommandInFlight.current = false;
      setFeedCommandPending(false);
    }
  };

  /**
   * Which of the two ladders is on screen — and, below, the one predicate every label
   * about it is read from.
   *
   * **The chain and the words describing it must come off the same switch.** Choosing
   * the chain by `following` and labelling it by `onLive` would put live figures under a
   * historical clock in the window between a push arriving and the timeline placing it,
   * and a wrong label is worse than a missing ladder because nothing on screen announces
   * it. So the header's LIVE/UTC, the connection chip, the stream notices and the chart
   * panel's live quote all read `following` too. The `unreachable` and `historicalWaiting`
   * notices are the only ones where the two predicates cannot differ — both require a
   * pinned `wanted`, and `following` collapses to `onLive` there — and they are spelled
   * the same way regardless, so no reader of this file has to work that out.
   */
  const chain = following ? liveChain : historicalChain;

  /** `null` while standing on a stored minute: there is no subscription then, and
   * `feedStatus` is whatever the live feed last reported before the reader pinned a
   * minute — showing it would describe a connection this screen is no longer using. */
  const badge = following ? feedBadge(feedStatus) : null;

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
            {following ? "LIVE" : chain ? formatFetchedClock(chain.fetched_at) : "—"}{" "}
            <span className="stat-note">{following ? "" : "UTC"}</span>
          </span>
        </div>

        <span
          className="chip"
          title={liveStatusDetail ?? `Streaming from ${ENGINE_URL}.`}
        >
          {following ? LIVE_STATUS_LABEL[liveStatus] : "history"}
        </span>

        {/* The venue feed's own state, #40 — beside the chip above and never merged
            into it. That chip is this browser's socket to the engine; this badge is
            the engine's socket to Delta, and the two are shown side by side because
            they can disagree: the chip can read "live" while this reads "reconnecting".
            Absent entirely for `connected` and whenever there is nothing to report. */}
        {badge ? (
          <span className="feed-badge" data-state={badge.state} title={badge.reason || undefined}>
            {badge.label}
          </span>
        ) : null}

        {following && feedStatus ? (
          <div className="feed-controls" role="group" aria-label="Feed controls">
            <button
              type="button"
              className="feed-control"
              onClick={() => void runFeedCommand("pause")}
              disabled={feedCommandPending || feedStatus.state === "stopped"}
            >
              Pause
            </button>
            <button
              type="button"
              className="feed-control"
              onClick={() => void runFeedCommand("resume")}
              disabled={
                feedCommandPending ||
                feedStatus.state !== "stopped" ||
                feedStatus.reason !== "paused"
              }
            >
              Resume
            </button>
            <button
              type="button"
              className="feed-control"
              onClick={() => void runFeedCommand("reconnect")}
              disabled={feedCommandPending || feedStatus.state === "stopped"}
            >
              Reconnect
            </button>
          </div>
        ) : null}

        <RecordingToggle />

        <ThemeToggle />
      </header>

      <main className="main">
        {following && liveStatus === "error" ? (
          <p className="notice error">
            {liveStatusDetail ?? "The live stream reported an error."}
          </p>
        ) : null}

        {following && liveStatus === "closed" ? (
          <p className="notice warn">
            Lost the connection to the engine at <code>{ENGINE_URL}</code>. Retrying — the
            figures below are the last ones that arrived, and they are not moving.
          </p>
        ) : null}

        {!following && unreachable ? (
          <p className="notice warn">
            The link asked for <strong>{unreachable}</strong>, which this day&rsquo;s
            store does not reach. Showing the closest minute the slider can stand on.
          </p>
        ) : null}

        {!following && historicalWaiting ? (
          <p className="notice">
            No stored quotes for {underlying} expiring {expiry} at {stamp}. The store
            wrote no row for this minute — never a neighbouring one shown in its place.
          </p>
        ) : null}

        {error ? <p className="notice error">{error}</p> : null}

        {legsError ? (
          <p className="notice error">
            The strategy link could not be read: {legsError}. Starting with no legs
            rather than guessing which ones were meant.
          </p>
        ) : null}

        {legsClearedNotice ? <p className="notice">{legsClearedNotice}</p> : null}

        {/*
          The ladder (or its status notice) in the growing column, the legs panel and
          the #46 chart in a fixed-width one beside it — **unconditionally paired**, so
          the chart panel's own visibility stays exactly what it was before this ticket
          (driven only by `panelInstrument`, never by whether the ladder has finished
          loading). Nesting the aside inside the `chain ?` branch below would make a
          chart opened from a stale link disappear the instant the reader dragged the
          slider onto a minute the ladder is still fetching — a regression this ticket
          does not intend and the DOM fingerprint test does not reach, since it renders
          `ChainLadder` alone.
        */}
        <div className="chain-body">
          <div className="chain-main">
            {chain ? (
              <ChainLadder
                key={`${chain.underlying}:${chain.expiry}`}
                chain={chain}
                onSelectContract={setPanelInstrument}
                legs={legs}
                onPick={pickLeg}
              />
            ) : error || (following && liveStatus === "error") || historicalWaiting ? null : (
              <p className="notice">
                {historicalBusy
                  ? "Reading the stored ladder…"
                  : following && liveStatus === "waiting"
                    ? "Connected. Waiting for the first quotes on this expiry…"
                    : "Connecting to the engine…"}
              </p>
            )}
          </div>

          {legs.length > 0 || panelInstrument ? (
            <aside className="chain-side">
              {legs.length > 0 ? (
                <LegsPanel legs={legs} href={analyseHref(legs, following ? null : stamp)} />
              ) : null}
              {panelInstrument ? (
                <ContractPanel
                  key={panelInstrument}
                  instrument={panelInstrument}
                  date={date}
                  liveChain={liveChain}
                  following={following}
                  onClose={() => setPanelInstrument(null)}
                />
              ) : null}
            </aside>
          ) : null}
        </div>

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
