"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ChainLadder } from "@/components/ChainLadder";
import { UNDERLYINGS, type ChainResponse, type Underlying } from "@/lib/contract";
import { ENGINE_URL, loadChain, loadExpiries, type Source } from "@/lib/engine";
import { formatFetchedAt, formatSpot } from "@/lib/format";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

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
    <main>
      <header className="head">
        <div className="head-title">
          <h1>Option chain</h1>
          <p className="sub">
            Delta Exchange · quotes and Greeks as the venue publishes them. No calculation of our
            own.
          </p>
        </div>

        <div className="controls">
          <div className="seg" role="group" aria-label="Underlying">
            {UNDERLYINGS.map((u) => (
              <button
                key={u}
                type="button"
                className={u === underlying ? "on" : undefined}
                aria-pressed={u === underlying}
                onClick={() => onUnderlying(u)}
                disabled={busy}
              >
                {u}
              </button>
            ))}
          </div>

          <label className="field">
            <span>Expiry</span>
            <select
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

          <button
            type="button"
            className="refresh"
            onClick={() => void load(underlying, expiry || null)}
            disabled={busy}
          >
            {busy ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      <section className="facts" aria-label="Chain summary">
        <div className="fact">
          <span className="k">Underlying</span>
          <span className="v">{chain ? chain.underlying : underlying}</span>
        </div>
        <div className="fact">
          <span className="k">Expiry</span>
          <span className="v">{chain ? chain.expiry : expiry || "—"}</span>
        </div>
        <div className="fact">
          <span className="k">Spot</span>
          <span className="v">{chain ? formatSpot(chain.spot) : "—"}</span>
        </div>
        <div className="fact">
          <span className="k">ATM strike</span>
          <span className="v">{chain ? formatSpot(chain.atm_strike) : "—"}</span>
        </div>
        <div className="fact wide">
          <span className="k">Fetched at</span>
          <span className="v">{chain ? formatFetchedAt(chain.fetched_at) : "—"}</span>
        </div>
        <div className="fact">
          <span className="k">Source</span>
          <span className="v">
            {source === null ? (
              "—"
            ) : (
              <span className={`badge ${source}`}>{source === "engine" ? "engine" : "fixture"}</span>
            )}
          </span>
        </div>
      </section>

      {fallbackReason ? (
        <p className="notice warn">
          {fallbackReason} Showing the committed fixture instead. Engine base URL is{" "}
          <code>{ENGINE_URL}</code>.
        </p>
      ) : null}

      {error ? <p className="notice error">{error}</p> : null}

      {chain ? (
        <ChainLadder chain={chain} />
      ) : error ? null : (
        <p className="notice">Loading the chain…</p>
      )}

      <footer className="foot">
        An em dash (—) means no data, and is never a zero. A printed <code>0</code> is a real
        zero — open interest of exactly zero is common and is shown as such. IV is the mark IV,
        shown as a percentage; the engine sends it as a decimal fraction, and hovering a cell
        gives bid, mark and ask IV. Data changes only on load and on Refresh.
      </footer>
    </main>
  );
}
