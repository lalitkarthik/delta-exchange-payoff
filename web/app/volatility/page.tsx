"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import ThemeToggle from "@/components/ThemeToggle";
import { LINE_LABEL, SERIES_COLOUR, VolatilityChart } from "@/components/VolatilityChart";
import { UNDERLYINGS, type Underlying } from "@/lib/contract";
import { ENGINE_URL } from "@/lib/engine";
import {
  ESTIMATORS,
  ESTIMATOR_NOTE,
  IV_KEY,
  loadBounds,
  loadVolatility,
  type Alignment,
  type BoundsResponse,
  type Estimator,
  type LineKey,
  type VolatilitySeries,
} from "@/lib/volatility";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

const ALL_LINES: LineKey[] = [IV_KEY, ...ESTIMATORS];

const ALIGNMENT_LABEL: Record<Alignment, string> = {
  contemporaneous: "Contemporaneous",
  lag: "Lag-aligned",
};

const ALIGNMENT_NOTE: Record<Alignment, string> = {
  contemporaneous:
    "IV(t) beside RV(t). What every vendor draws — and at any point the two lines " +
    "describe windows 2N apart end to end, so the visible spread is not the premium.",
  lag:
    "IV(t) beside the movement that actually arrived over [t, t+N]. The honest pairing, " +
    "and the only one a premium may be read off. The last N has no answer yet.",
};

/**
 * Implied against realised, on one axis, at one lookback.
 *
 * **One control drives both sides.** The lookback sets the realised window *and* the
 * implied tenor, so the two lines always ask about the same length of time. Two controls
 * would make a ten-minute realised volatility against a thirty-day implied one
 * expressible, and the difference between them would look like a signal.
 *
 * **The slider is bounded by reality, and says what is binding it.** Lower by the
 * shortest listed expiry — under it there is no implied volatility to compare with —
 * upper by the smaller of the listed term structure and the history actually held. In
 * the first month of recording the third constraint binds hard, and a screen that could
 * not say so would read as broken every single time.
 *
 * **This screen does no arithmetic.** Both series arrive scaled to the window and
 * aligned; the chart turns numbers into coordinates and this file turns them into text.
 */
export default function VolatilityPage() {
  const [underlying, setUnderlying] = useState<Underlying>("BTC");
  const [interval, setInterval] = useState("1m");
  const [alignment, setAlignment] = useState<Alignment>("contemporaneous");
  const [lookback, setLookback] = useState<number | null>(null);

  const [bounds, setBounds] = useState<BoundsResponse | null>(null);
  const [series, setSeries] = useState<VolatilitySeries | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [visible, setVisible] = useState<Set<LineKey>>(
    () => new Set<LineKey>([IV_KEY, "log", "parkinson", "rogers_satchell"]),
  );

  /** The bounds first, always. A lookback cannot be chosen before it can be bounded. */
  useEffect(() => {
    let stale = false;
    setError(null);
    loadBounds(underlying, interval)
      .then((next) => {
        if (stale) return;
        setBounds(next);
        // Open on the middle of what is possible rather than on a constant. A default of
        // 30 would be refused on day one of recording and the screen would open on an
        // error the reader did nothing to cause.
        setLookback((current) => {
          if (!next.usable) return null;
          if (current !== null && current >= next.min_days && current <= next.max_days) {
            return current;
          }
          return Number(((next.min_days + next.max_days) / 2).toFixed(2));
        });
      })
      .catch((err) => {
        if (!stale) setError(message(err));
      });
    return () => {
      stale = true;
    };
  }, [underlying, interval]);

  const estimators = useMemo(
    () => ESTIMATORS.filter((key) => visible.has(key)),
    [visible],
  );

  const fetchSeries = useCallback(async () => {
    if (lookback === null || !bounds?.usable) return;
    setBusy(true);
    setError(null);
    try {
      setSeries(
        await loadVolatility({
          underlying,
          lookbackDays: lookback,
          interval,
          // Always ask for every estimator that is on. Asking only for the visible ones
          // keeps the payload honest about what it computed; it is also why toggling a
          // checkbox refetches rather than merely hiding a line that was already drawn.
          estimators: estimators.length > 0 ? estimators : (["log"] as Estimator[]),
          alignment,
        }),
      );
    } catch (err) {
      setSeries(null);
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }, [underlying, lookback, interval, estimators, alignment, bounds]);

  useEffect(() => {
    void fetchSeries();
  }, [fetchSeries]);

  const toggle = (key: LineKey) =>
    setVisible((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const offered = series?.valid_intervals ?? bounds?.intervals ?? ["1m"];

  /** The one line of text that turns an invisible decision into a visible one. */
  const provenance = (() => {
    if (!series || series.points.length === 0) return null;
    // The largest count across every point, not the last point's. In lag-aligned mode the
    // trailing N deliberately has no window and therefore no returns, so reading the last
    // point would report "0 returns per point" for a chart that is full of them.
    const counts = series.points.flatMap((point) =>
      series.estimators
        .map((key) => point.returns[key])
        .filter((count): count is number => typeof count === "number"),
    );
    const returns = counts.length > 0 ? Math.max(...counts) : 0;
    const step = series.step_seconds / series.interval_seconds;
    return (
      `${series.lookback_days}d window, ${interval} sampling, ` +
      `${returns.toLocaleString()} returns per point` +
      (step > 1 ? `, plotted every ${step} intervals` : "")
    );
  })();

  return (
    <div className="shell">
      <header className="header">
        <span className="brand">IV vs RV</span>

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
          <span className="stat-label">Sampling</span>
          <select
            className="picker-select"
            value={interval}
            onChange={(e) => setInterval(e.target.value)}
          >
            {offered.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <div className="stat lead">
          <span className="stat-label">Lookback N</span>
          <span className="stat-value">
            {lookback === null ? "—" : `${lookback} d`}
          </span>
        </div>

        <button
          type="button"
          className="refresh"
          onClick={() =>
            setAlignment((current) => (current === "lag" ? "contemporaneous" : "lag"))
          }
          title={ALIGNMENT_NOTE[alignment]}
        >
          {ALIGNMENT_LABEL[alignment]}
        </button>

        <span className="chip" title={`Reading ${ENGINE_URL}.`}>
          {busy ? "loading…" : provenance ? "ready" : "no data"}
        </span>

        <Link className="refresh" href="/">
          Chain →
        </Link>

        <ThemeToggle />
      </header>

      <main className="main">
        <section className="vol-controls">
          <div className="vol-slider">
            <input
              type="range"
              min={bounds?.min_days ?? 0}
              max={bounds?.max_days ?? 1}
              step={0.01}
              value={lookback ?? 0}
              disabled={!bounds?.usable}
              onChange={(e) => setLookback(Number(e.target.value))}
              aria-label="Lookback in days"
            />
            <input
              type="number"
              className="vol-number"
              min={bounds?.min_days}
              max={bounds?.max_days}
              step={0.01}
              value={lookback ?? ""}
              disabled={!bounds?.usable}
              onChange={(e) => {
                const next = Number(e.target.value);
                if (Number.isFinite(next)) setLookback(next);
              }}
              aria-label="Lookback in days"
            />
            <span className="stat-note">days — drives the RV window and the IV tenor</span>
          </div>

          <fieldset className="vol-legend">
            <legend className="sr-only">Lines to draw</legend>
            {ALL_LINES.map((key) => (
              <label
                key={key}
                className="vol-check"
                title={key === IV_KEY ? undefined : ESTIMATOR_NOTE[key as Estimator]}
              >
                <input
                  type="checkbox"
                  checked={visible.has(key)}
                  onChange={() => toggle(key)}
                />
                <span className="vol-swatch" style={{ background: SERIES_COLOUR[key] }} />
                {LINE_LABEL[key]}
              </label>
            ))}
          </fieldset>
        </section>

        {bounds && !bounds.usable ? (
          <p className="notice warn">
            No lookback works yet. {bounds.detail}
          </p>
        ) : null}

        {bounds?.usable ? (
          <p className="note">
            <strong>N ∈ [{bounds.min_days.toFixed(2)}, {bounds.max_days.toFixed(2)}] days</strong>{" "}
            — {bounds.detail}
          </p>
        ) : null}

        {error ? <p className="notice error">{error}</p> : null}

        {series ? (
          <>
            <div className="vol-chart-wrap">
              <VolatilityChart series={series} visible={visible} />
            </div>
            {provenance ? <p className="note vol-provenance">{provenance}</p> : null}
          </>
        ) : null}

        <p className="note">
          Both series are expressed <strong>over the lookback window</strong> and neither is
          annualised: the chart reads &ldquo;the market expected an 11.5% move over N days;
          it delivered 9%&rdquo;. Annualising is the convention and it hides N inside a
          constant, so two charts at different lookbacks would carry identical-looking axes
          while answering different questions. A year is 365 days everywhere here, because
          crypto trades weekends and this venue lists weekend expiries.{" "}
          <strong>The line breaks across a gap</strong> rather than joining over it — a
          minute with no arrivals produces no row, never a repeat of the previous close, so
          a break is a hole in the record and not a value of zero. In lag-aligned mode the
          most recent N days have no realised figure at all, because that window has not
          finished happening; that is drawn as an honest blank. The implied line is{" "}
          <strong>ATM only</strong> — interpolated between the two strikes bracketing the
          forward, and between the two expiries bracketing N in total variance rather than
          in volatility. It is <em>not</em> a DVOL equivalent and will disagree with a
          published DVOL print, because DVOL integrates the whole strike range and this
          tracks the level alone.
        </p>
      </main>
    </div>
  );
}
