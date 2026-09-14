"use client";

import { useEffect, useRef, useState } from "react";

import StructureGrid, { FEATURE_LABEL } from "@/components/StructureGrid";
import ThemeToggle from "@/components/ThemeToggle";
import { UNDERLYINGS, type ChainResponse, type Underlying } from "@/lib/contract";
import { ENGINE_URL, loadExpiries } from "@/lib/engine";
import { formatFetchedClock, formatSpot } from "@/lib/format";
import { LIVE_STATUS_LABEL, subscribeChain, type LiveStatus } from "@/lib/live";
import {
  defaultStep,
  FEATURES,
  ivCoverage,
  structureGrid,
  type Direction,
  type Feature,
} from "@/lib/structures";
import type { ViewRequest } from "@/lib/view";

/**
 * The structure chain screen: state, controls, and nothing else.
 *
 * `VolatilityScreen` is the precedent for what lives in a screen and what does not: state
 * and layout here, arithmetic in `lib/structures.ts`. The two pickers, `loadExpiries`, one
 * `subscribeChain`, the status chip and the `replaceState` URL sync are that screen's, kept
 * line for line where they could be.
 *
 * **Live only, and there is no scrubber.** The board is read for where the market stands
 * now, and pinning it to a stored minute would need a read path this screen does not have.
 * So the URL carries the underlying and the expiry and no minute — a minute in this
 * address would promise a fixed board the screen does not have.
 *
 * **The grid's shape is not in the URL either.** Offset, step, columns and rows are how
 * someone is looking at a board rather than which board it is, and a link that pinned
 * them would arrive wrong on a series with different spacing. The step seeds itself from
 * the ladder — `defaultStep` — and then belongs to the reader.
 */
export default function StructureScreen({ initial }: { initial: ViewRequest }) {
  const [underlying, setUnderlying] = useState<Underlying>(initial.underlying ?? "BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>(initial.expiry ?? "");
  const [chain, setChain] = useState<ChainResponse | null>(null);
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [detail, setDetail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [offsetMin, setOffsetMin] = useState(0);
  const [step, setStep] = useState<number | null>(null);
  const [columns, setColumns] = useState(5);
  const [rowsEitherSide, setRowsEitherSide] = useState(10);
  const [direction, setDirection] = useState<Direction>(1);
  const [feature, setFeature] = useState<Feature>("price");

  /* The expiry list, and the choice it constrains. `useSmileDay`'s rule, kept: a URL
     naming an expiry the engine does not list is resolved against the list before
     anything is subscribed, or the dropdown shows one expiry while the grid draws
     another. */
  useEffect(() => {
    let live = true;
    loadExpiries(underlying)
      .then(({ data, preferredExpiry }) => {
        if (!live) return;
        setExpiries(data.expiries);
        setError(null);
        setExpiry((current) => {
          if (current && data.expiries.includes(current)) return current;
          if (preferredExpiry && data.expiries.includes(preferredExpiry)) return preferredExpiry;
          return data.expiries[0] ?? "";
        });
      })
      .catch((err: unknown) => {
        if (!live) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      live = false;
    };
  }, [underlying]);

  /* The board itself. Cleared on a series change rather than left up: a grid drawn for
     one expiry and labelled with another is this feature's characteristic failure. */
  useEffect(() => {
    if (!expiry) return;
    setChain(null);
    return subscribeChain(underlying, expiry, {
      onChain: setChain,
      onStatus: (next, why) => {
        setStatus(next);
        setDetail(why ?? null);
      },
    });
  }, [underlying, expiry]);

  /*
   * The step seeds itself from the ladder, once per series.
   *
   * A BTC ladder is spaced in thousands and an ETH one in tens, so a step carried across
   * a change of underlying draws a grid of empty columns and looks like an outage. It is
   * seeded from the first chain of each series and then left alone — the ref, not the
   * state, is what makes "then left alone" true across the once-a-second pushes.
   */
  const seeded = useRef<string>("");
  useEffect(() => {
    const series = `${underlying}/${expiry}`;
    if (!chain || seeded.current === series) return;
    const suggested = defaultStep(chain);
    if (suggested === null) return;
    seeded.current = series;
    setStep(suggested);
  }, [chain, underlying, expiry]);

  // The address bar follows the pickers. `replaceState` rather than a router push, for
  // the reason every screen here gives: changing an expiry must not fill the back button.
  useEffect(() => {
    const params = new URLSearchParams({ underlying });
    if (expiry) params.set("expiry", expiry);
    const href = `${window.location.pathname}?${params.toString()}`;
    if (`${window.location.pathname}${window.location.search}` !== href) {
      window.history.replaceState(null, "", href);
    }
  }, [underlying, expiry]);

  const grid =
    chain && step !== null
      ? structureGrid(chain, { offsetMin, step, columns, rowsEitherSide, direction, feature })
      : null;
  const coverage = grid ? ivCoverage(grid) : null;

  return (
    // No `.app` wrapper and no rail: `app/layout.tsx` owns both, so navigating between
    // screens does not tear down the chain websocket.
    <main className="screen">
      <header className="header">
        <div className="brand">DELTA</div>
        <h1 className="screen-title">Straddle / strangle</h1>

        <label className="picker">
          <span className="stat-label">Underlying</span>
          <select
            className="picker-select"
            value={underlying}
            onChange={(e) => setUnderlying(e.target.value as Underlying)}
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
            onChange={(e) => setExpiry(e.target.value)}
            disabled={expiries.length === 0}
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
          <span className="stat-value">{formatSpot(chain?.spot ?? null)}</span>
        </div>

        <div className="stat">
          <span className="stat-label">Forward</span>
          <span className="stat-value">{formatSpot(chain?.forward ?? null)}</span>
        </div>

        <div className="stat">
          <span className="stat-label">As of</span>
          <span className="stat-value">
            {chain ? formatFetchedClock(chain.fetched_at) : "—"}{" "}
            <span className="stat-note">UTC</span>
          </span>
        </div>

        <span className="chip" title={detail ?? `Streaming from ${ENGINE_URL}/ws/chain.`}>
          {LIVE_STATUS_LABEL[status]}
        </span>

        <ThemeToggle />
      </header>

      <div className="structures-controls">
        <NumberBox label="Offset min" value={offsetMin} min={0} onChange={setOffsetMin} />
        <NumberBox label="Step" value={step ?? 0} min={1} onChange={setStep} />
        <NumberBox label="Columns" value={columns} min={1} max={24} onChange={setColumns} />
        <NumberBox label="Rows ±ATM" value={rowsEitherSide} min={0} max={80} onChange={setRowsEitherSide} />

        <div className="picker">
          <span className="stat-label">Direction</span>
          <div className="scale-toggle" role="group" aria-label="Direction">
            {([1, -1] as Direction[]).map((d) => (
              <button
                key={d}
                type="button"
                className="scale-option"
                aria-pressed={direction === d}
                onClick={() => setDirection(d)}
              >
                {d === 1 ? "Long" : "Short"}
              </button>
            ))}
          </div>
        </div>

        <div className="picker">
          <span className="stat-label">Feature</span>
          <div role="tablist" aria-label="Feature" className="structures-features">
            {FEATURES.map((f) => (
              <button
                key={f}
                type="button"
                role="tab"
                className="tab"
                aria-selected={feature === f}
                onClick={() => setFeature(f)}
              >
                {FEATURE_LABEL[f].code}
              </button>
            ))}
          </div>
        </div>
      </div>

      <h2 className="structures-band">
        {FEATURE_LABEL[feature].band}
        {coverage && coverage.total > 0 ? (
          <span className="structures-coverage">
            {coverage.solved === coverage.total
              ? `all ${coverage.total} structures carry a figure`
              : `${coverage.solved} of ${coverage.total} structures carry a figure — the rest are blank`}
          </span>
        ) : null}
      </h2>

      {error ? <p className="notice error">{error}</p> : null}

      {chain === null ? (
        <p className="notice">
          {status === "error"
            ? (detail ?? "The engine could not be reached.")
            : "Connecting to the engine…"}
        </p>
      ) : grid === null ? (
        // A refusal, not an empty grid. Every row is placed relative to the money, so a
        // chain with no at-the-money strike has no window to draw and saying so is the
        // whole answer.
        <p className="notice">
          This chain carries no at-the-money strike, and every row of the grid is placed
          relative to it. Nothing is drawn rather than a window picked at random.
        </p>
      ) : (
        <StructureGrid grid={grid} feature={feature} />
      )}

      <p className="note">
        <strong>Each cell is one structure.</strong> The row is the strike it is built
        around and the column is how far the wings sit from it, so the <code>0</code>{" "}
        column is the straddle — a call and a put at the same strike — and every column
        right of it is a strangle widening symmetrically. A wing is resolved to the{" "}
        <em>nearest listed strike</em>, ties to the lower; one that would land further than
        half a step from where the heading says is left hatched rather than drawn, because
        a real strangle under the wrong column heading is worse than a gap.{" "}
        <strong>The volatility and the Greeks are ours, not the venue&rsquo;s.</strong>{" "}
        Every figure is built from the volatility this engine solved out of the order book;
        Delta publishes its own on the same payload and none of it is read here. A strike
        whose volatility did not solve carries no Greeks at all, so its cell is blank —
        which is not zero, and the count above says how many.{" "}
        <strong>The price is a midpoint, not a fill.</strong> It is{" "}
        <code>−(mid(call) + mid(put))</code> for a long structure — negative because buying
        it is cash out — where a midpoint needs both a bid and an ask; nobody trades there,
        and it is used because it is the same number the volatility was solved from, so the
        price and the vol describe one board. Everything is per{" "}
        <em>one unit of the underlying</em>: the lot size is on the payload and is
        deliberately not applied.
      </p>
    </main>
  );
}

/** One control. A number input rather than a slider: these are typed once and then read,
 *  and a slider would make an exact step something to aim at. An empty or unparseable box
 *  is ignored rather than reset, so a half-typed number does not redraw the grid at 4. */
function NumberBox({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max?: number;
  onChange: (next: number) => void;
}) {
  return (
    <label className="picker">
      <span className="stat-label">{label}</span>
      <input
        className="picker-select structures-number"
        type="number"
        value={value}
        min={min}
        max={max}
        step={1}
        onChange={(e) => {
          const next = e.target.valueAsNumber;
          if (!Number.isFinite(next)) return;
          if (next < min) return;
          if (max !== undefined && next > max) return;
          onChange(next);
        }}
      />
    </label>
  );
}
