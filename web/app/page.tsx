"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ChainLadder } from "@/components/ChainLadder";
import RecordingToggle from "@/components/RecordingToggle";
import ThemeToggle from "@/components/ThemeToggle";
import { UNDERLYINGS, type ChainResponse, type Underlying } from "@/lib/contract";
import { ENGINE_URL, loadExpiries } from "@/lib/engine";
import { LIVE_STATUS_LABEL, subscribeChain, type LiveStatus } from "@/lib/live";
import { formatFetchedAt, formatFetchedClock, formatSpot } from "@/lib/format";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * One header row of figures, then the ladder, in the sibling chain screen's shell.
 *
 * The header holds what belongs to the *minute*: which series, spot, the strike the
 * star is on, when the numbers were taken, and where they came from. The Underlying and
 * Expiry dropdowns are shaped as figures rather than as controls, so they sit in that
 * row without pulling the eye off it — see `.picker` in `globals.css`.
 *
 * Nothing invented. There is no forward, no basis and no session IV, because Delta
 * publishes none and `docs/chain-contract.md` exposes none; the sibling shows all three
 * because it fits them from the option prices, which this project does not do.
 *
 * **The chain arrives over a websocket and there is no Refresh button.** The engine holds
 * one connection to Delta and pushes the complete chain once a second; this page never
 * asks for it. `loadExpiries` is still a REST call, because the dropdown needs its list
 * once and it does not change while you watch.
 */
export default function Page() {
  const [underlying, setUnderlying] = useState<Underlying>("BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>("");

  const [chain, setChain] = useState<ChainResponse | null>(null);
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [statusDetail, setStatusDetail] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow earlier expiries request landing after a newer one.
  const requestId = useRef(0);

  /**
   * The expiry list, over REST. Called on mount and when the underlying changes.
   *
   * Only the list. The chain itself is not fetched anywhere in this file — the effect
   * below subscribes to it, and data arrives without anyone asking.
   */
  const loadExpiryList = useCallback(async (next: Underlying, wanted: string | null) => {
    const id = ++requestId.current;
    setBusy(true);
    setError(null);

    try {
      const list = await loadExpiries(next);
      if (id !== requestId.current) return;

      const available = list.data.expiries;
      setExpiries(available);

      // Keep the user's expiry if the new underlying still lists it, otherwise take the
      // front one. `preferredExpiry` names the expiry the committed fixture holds a
      // chain for, and only ever appears when the engine was unreachable.
      const fallback =
        list.preferredExpiry && available.includes(list.preferredExpiry)
          ? list.preferredExpiry
          : (available[0] ?? "");
      const chosen = wanted && available.includes(wanted) ? wanted : fallback;
      setExpiry(chosen);

      if (!chosen) setError(`No expiries listed for ${next}.`);
    } catch (err) {
      if (id !== requestId.current) return;
      setError(message(err));
    } finally {
      if (id === requestId.current) setBusy(false);
    }
  }, []);

  // The expiry list, once per underlying.
  useEffect(() => {
    void loadExpiryList(underlying, expiry || null);
    // Deliberately keyed on the underlying alone: re-running this when the expiry
    // changes would refetch a list that cannot have changed and reset the dropdown.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying, loadExpiryList]);

  /**
   * One live subscription, torn down and rebuilt whenever the series changes.
   *
   * The cleanup is what makes switching expiry safe: without it, the old socket keeps
   * pushing the old expiry's chain and the two interleave on screen.
   */
  useEffect(() => {
    if (!expiry) return;
    setChain(null);
    return subscribeChain(underlying, expiry, {
      onChain: setChain,
      onStatus: (next, detail) => {
        setStatus(next);
        setStatusDetail(detail ?? null);
      },
    });
  }, [underlying, expiry]);

  const onUnderlying = (next: Underlying) => setUnderlying(next);
  const onExpiry = (next: string) => setExpiry(next);

  return (
    <div className="shell">
      <header className="header">
        <div className="brand">DELTA</div>

        <label className="picker">
          <span className="stat-label">Underlying</span>
          <select
            className="picker-select"
            value={underlying}
            onChange={(e) => onUnderlying(e.target.value as Underlying)}
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
            onChange={(e) => onExpiry(e.target.value)}
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

        <div className="stat">
          <span className="stat-label">Fetched</span>
          <span
            className="stat-value"
            title={chain ? formatFetchedAt(chain.fetched_at) : undefined}
          >
            {chain ? formatFetchedClock(chain.fetched_at) : "—"}{" "}
            <span className="stat-note">UTC</span>
          </span>
        </div>

        {/* Always on screen. A stalled socket and a live one show the same numbers,
            and the only difference is whether they are still moving — so the state of
            the connection is named every time rather than only when it is bad. */}
        <span className="chip" title={statusDetail ?? `Streaming from ${ENGINE_URL}.`}>
          {LIVE_STATUS_LABEL[status]}
        </span>

        {/* Whether the day is being captured, read from the engine on every poll. It
            belongs among the figures rather than off with the theme control: it is a
            fact about the data, and the one fact on this row a reader can change. */}
        <RecordingToggle />

        {/* Last, and pushed right by `margin-left: auto`: it changes how the figures look
            and never what they say, so it must not sit among them competing for the eye. */}
        <ThemeToggle />
      </header>

      <main className="main">
        {status === "error" ? (
          <p className="notice error">
            {statusDetail ?? "The live stream reported an error."}
          </p>
        ) : null}

        {status === "closed" ? (
          <p className="notice warn">
            Lost the connection to the engine at <code>{ENGINE_URL}</code>. Retrying — the
            figures below are the last ones that arrived, and they are not moving.
          </p>
        ) : null}

        {error ? <p className="notice error">{error}</p> : null}

        {chain ? (
          // Re-keyed per series: a different underlying or expiry is a different ladder
          // and earns a fresh centring on the money. A push of new prices keeps the key,
          // so the view stays where the reader left it while the numbers change under it.
          <ChainLadder key={`${chain.underlying}:${chain.expiry}`} chain={chain} />
        ) : error || status === "error" ? null : (
          <p className="notice">
            {status === "waiting"
              ? "Connected. Waiting for the first quotes on this expiry…"
              : "Connecting to the engine…"}
          </p>
        )}

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
          The in-the-money half is washed, measured against spot. Figures update continuously
          from the venue; the clock above is when the engine last rebuilt the ladder.
        </p>
      </main>
    </div>
  );
}
