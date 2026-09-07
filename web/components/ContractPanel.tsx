"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import ContractChart, { type LiveQuote } from "@/components/ContractChart";
import type { CandleMode } from "@/lib/contractChart";
import type { ChainResponse, ContractBar } from "@/lib/contract";
import { ENGINE_URL, loadContractBars } from "@/lib/engine";
import { parseCanonical } from "@/lib/instrument";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

const MODE_LABEL: Record<CandleMode, string> = { mid: "Mid", ltp: "Last trade" };

/**
 * The chart panel #46 adds to the chain page: opened by clicking a strike on
 * `ChainLadder`, closed from here, its contract carried in the page's URL by
 * `ChainScreen`.
 *
 * **This component fetches; it does not decide *which day*.** `date` arrives from
 * `ChainScreen`'s own `date` state — the day the whole page is anchored to, live or
 * historical — so a chart opened while scrubbing an old day shows that day's candles
 * rather than today's, which would either be empty or would silently show a different
 * day than the ladder the strike was clicked on.
 *
 * **Only the live edge feeds the last candle.** `liveChain`/`following` come from
 * `ChainScreen` unchanged; `ContractChart` is handed a live quote only when `following`
 * is true, so dragging the slider off the right edge stops moving the last candle in the
 * same instant it stops moving the ladder — one switch, not two.
 *
 * That switch is `following` and not `positionOf`'s `onLive`, which is the same switch
 * the ladder above chooses its own chain with. `onLive` additionally requires a push to
 * have been placed on the timeline, and taking the narrower of the two here would leave
 * the chart's last candle frozen while the ladder beside it moved — see the note over
 * `chain` in `ChainScreen`, and issue #49.
 */
export default function ContractPanel({
  instrument,
  date,
  liveChain,
  following,
  onClose,
}: {
  instrument: string;
  date: string;
  liveChain: ChainResponse | null;
  /** The ladder is following the stream, so the last candle should follow it too. */
  following: boolean;
  onClose: () => void;
}) {
  const [mode, setMode] = useState<CandleMode>("mid");
  const [bars, setBars] = useState<ContractBar[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const request = useRef(0);

  useEffect(() => {
    const id = ++request.current;
    setBars(null);
    setBusy(true);
    setError(null);
    // Reset to the default series on every new contract, so a reader who left the
    // previous one on "last trade" does not open the next expecting to see it — the
    // toggle is a lens on one contract, not a standing preference.
    setMode("mid");

    void (async () => {
      try {
        const loaded = await loadContractBars(instrument, date);
        if (id !== request.current) return;
        setBars(loaded.bars);
      } catch (err) {
        if (id !== request.current) return;
        setError(message(err));
      } finally {
        if (id === request.current) setBusy(false);
      }
    })();
  }, [instrument, date]);

  const parsed = useMemo(() => parseCanonical(instrument), [instrument]);

  const live: LiveQuote | null = useMemo(() => {
    if (!following || !liveChain || !parsed) return null;
    const row = liveChain.rows.find((r) => r.strike === parsed.strike);
    const leg = parsed.side === "call" ? row?.call : row?.put;
    if (!leg) return null;
    return { bid: leg.bid, ask: leg.ask };
  }, [following, liveChain, parsed]);

  return (
    <aside className="contract-panel" aria-label={`Chart for ${instrument}`}>
      <header className="contract-panel-header">
        <div className="contract-panel-title">
          <span className="stat-value">{instrument}</span>
          <span className="stat-note">{date}</span>
        </div>

        <div className="contract-panel-controls">
          {(Object.keys(MODE_LABEL) as CandleMode[]).map((key) => (
            <button
              key={key}
              type="button"
              className="refresh"
              aria-pressed={mode === key}
              data-active={mode === key ? "true" : undefined}
              onClick={() => setMode(key)}
            >
              {MODE_LABEL[key]}
            </button>
          ))}
          <button
            type="button"
            className="refresh"
            onClick={onClose}
            aria-label={`Close the chart for ${instrument}`}
          >
            Close
          </button>
        </div>
      </header>

      {error ? <p className="notice error">{error}</p> : null}

      {!error && busy && bars === null ? (
        <p className="notice">Reading the day&rsquo;s bars…</p>
      ) : null}

      {!error && bars !== null && bars.length === 0 ? (
        <p className="notice">
          No stored bars for {instrument} on {date}. The store wrote no row for any minute
          of this contract&rsquo;s day.
        </p>
      ) : null}

      {bars !== null && bars.length > 0 ? (
        <div className="contract-chart-wrap">
          <ContractChart bars={bars} mode={mode} live={live} />
        </div>
      ) : null}

      <p className="note">
        Candles are built from the stored bid/ask midpoint — what the book offered, not
        what printed. <strong>Last trade</strong> is the venue&rsquo;s own reference
        column, republished on a fixed cadence rather than only when a trade clears — a
        long flat stretch in it is not missing data, it is the same print holding.{" "}
        <strong>A gap in the candles is a stored gap</strong>: the store wrote no row for
        that minute, and none is invented to fill it. Bid and ask are drawn as lines over
        either candle series. While standing on the live edge, the last candle follows{" "}
        {ENGINE_URL}&rsquo;s current quote for this contract; leaving the live edge stops
        it exactly where the ladder itself stops.
      </p>
    </aside>
  );
}
