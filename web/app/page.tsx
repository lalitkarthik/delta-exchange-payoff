"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ChainLadder } from "@/components/ChainLadder";
import ThemeToggle from "@/components/ThemeToggle";
import { UNDERLYINGS, type ChainResponse, type Underlying } from "@/lib/contract";
import { ENGINE_URL, loadChain, loadExpiries, type Source } from "@/lib/engine";
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
 */
export default function Page() {
  const [underlying, setUnderlying] = useState<Underlying>("BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>("");

  const [chain, setChain] = useState<ChainResponse | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow earlier request landing after a newer one.
  const requestId = useRef(0);

  /**
   * One fetch pass: expiries, then the chain for the chosen expiry. Called on mount,
   * when the underlying changes, when the expiry changes, and when Refresh is clicked.
   * There is no interval, no websocket and no revalidation anywhere in this file —
   * data moves only when the user asks for it.
   */
  const load = useCallback(async (next: Underlying, wanted: string | null) => {
    const id = ++requestId.current;
    setBusy(true);
    setError(null);

    try {
      const list = await loadExpiries(next);
      if (id !== requestId.current) return;

      const available = list.data.expiries;
      setExpiries(available);

      // Keep the user's expiry if the new underlying still lists it. Otherwise take the
      // front expiry — except in fixture mode, where `preferredExpiry` names the one
      // expiry a chain was actually captured for.
      const fallback =
        list.preferredExpiry && available.includes(list.preferredExpiry)
          ? list.preferredExpiry
          : (available[0] ?? "");
      const chosen = wanted && available.includes(wanted) ? wanted : fallback;
      setExpiry(chosen);

      if (!chosen) {
        setChain(null);
        setSource(list.source);
        setFallbackReason(list.fallbackReason ?? null);
        setError(`No expiries listed for ${next}.`);
        return;
      }

      const res = await loadChain(next, chosen);
      if (id !== requestId.current) return;

      setChain(res.data);
      setSource(res.source);
      setFallbackReason(res.fallbackReason ?? list.fallbackReason ?? null);
    } catch (err) {
      if (id !== requestId.current) return;
      setChain(null);
      setSource(null);
      setFallbackReason(null);
      setError(message(err));
    } finally {
      if (id === requestId.current) setBusy(false);
    }
  }, []);

  // Load once on mount. The empty dependency list is the point: no polling.
  useEffect(() => {
    void load("BTC", null);
  }, [load]);

  const onUnderlying = (next: Underlying) => {
    setUnderlying(next);
    void load(next, expiry || null);
  };

  const onExpiry = (next: string) => {
    setExpiry(next);
    void load(underlying, next);
  };

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

        <div className="stat">
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

        {/* Always on screen, whichever way it reads: a fixture must never be mistaken
            for live market data, and the only way to guarantee that is to name the
            source every time rather than only when it is the surprising one. */}
        <span
          className="chip"
          title={
            source === "fixture"
              ? "The committed fixture, not the venue. Start the engine for live quotes."
              : `Live from the engine at ${ENGINE_URL}.`
          }
        >
          source · {source ?? "—"}
        </span>

        <button
          type="button"
          className="refresh"
          onClick={() => void load(underlying, expiry || null)}
          disabled={busy}
        >
          {busy ? "Refreshing…" : "Refresh"}
        </button>

        {/* Last, and pushed right by `margin-left: auto`: it changes how the figures look
            and never what they say, so it must not sit among them competing for the eye. */}
        <ThemeToggle />
      </header>

      <main className="main">
        {fallbackReason ? (
          <p className="notice warn">
            {fallbackReason} Showing the committed fixture instead. Engine base URL is{" "}
            <code>{ENGINE_URL}</code>.
          </p>
        ) : null}

        {error ? <p className="notice error">{error}</p> : null}

        {chain ? (
          // Re-keyed per series: a different underlying or expiry is a different ladder
          // and earns a fresh centring on the money. A Refresh keeps the key, so the
          // view stays where the reader left it.
          <ChainLadder key={`${chain.underlying}:${chain.expiry}`} chain={chain} />
        ) : error ? null : (
          <p className="notice">Loading the chain…</p>
        )}

        <p className="note">
          A hatched half means that side is not listed at this strike. An empty cell means the
          venue did not price that field of a contract that does exist. A printed <code>0</code>{" "}
          is a real zero — open interest of exactly zero is common and is shown as such. The IV
          column is the mark IV, per side, because Delta prices the call and the put separately;
          hovering any cell gives the mark price and bid, mark and ask IV. The in-the-money half
          is washed, measured against spot. Data changes only on load and on Refresh.
        </p>
      </main>
    </div>
  );
}
