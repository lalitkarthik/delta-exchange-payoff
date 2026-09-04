"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { SmileChart, type VolScale } from "@/components/SmileChart";
import ThemeToggle from "@/components/ThemeToggle";
import { UNDERLYINGS, type SmileResponse, type Underlying } from "@/lib/contract";
import { ENGINE_URL, loadExpiries, loadSmile, type Source } from "@/lib/engine";
import { formatFetchedClock, formatSpot } from "@/lib/format";
import {
  formatTimeToExpiry,
  hoursToExpiry,
  isDying,
  latestMinute,
  solvedPercents,
  toRows,
} from "@/lib/smile";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * The volatility screen: a smile, and everything a reader needs to know how far to trust
 * it.
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
 * **The whole day arrives in one request.** `minutes` is the entire store for this
 * expiry; this screen shows the last of them. The scrubber that walks the rest is #20,
 * and it is an index into this same array rather than another fetch.
 *
 * A client component because it holds the selection, the scale toggle and the fetch. The
 * route around it stays a server component so the page keeps its metadata.
 */
export default function VolatilityScreen() {
  const [underlying, setUnderlying] = useState<Underlying>("BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>("");

  const [smile, setSmile] = useState<SmileResponse | null>(null);
  const [source, setSource] = useState<Source>("engine");
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);

  /**
   * Linear, always, on first paint. The log axis is right on a dying contract and wrong
   * on every other one, and an axis that changed shape depending on which expiry you
   * picked would make two ordinary smiles incomparable by eye.
   */
  const [scale, setScale] = useState<VolScale>("linear");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow earlier request landing after a newer one.
  const listRequest = useRef(0);
  const smileRequest = useRef(0);

  const loadExpiryList = useCallback(async (next: Underlying, wanted: string | null) => {
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
      const chosen = wanted && available.includes(wanted) ? wanted : fallback;
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

  const minute = smile ? latestMinute(smile) : null;
  const rows = minute ? toRows(minute) : [];
  const solved = solvedPercents(rows);
  const hours = minute ? hoursToExpiry(minute) : null;
  const stamps = smile?.model_versions ?? [];

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
            onChange={(e) => setUnderlying(e.target.value as Underlying)}
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
            onChange={(e) => setExpiry(e.target.value)}
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

        <div className="stat">
          <span className="stat-label">Minute</span>
          <span className="stat-value">
            {minute ? formatFetchedClock(minute.minute) : "—"}{" "}
            <span className="stat-note">UTC</span>
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

        <span className="chip" title={fallbackReason ?? `Read from ${ENGINE_URL}/smile.`}>
          {source === "fixture" ? "fixture" : "stored"}
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

        {source === "fixture" && !error ? (
          <p className="notice warn">
            Showing the committed smile fixture, not the store.{" "}
            {fallbackReason ?? `Set NEXT_PUBLIC_USE_FIXTURE=0 and start the engine for stored data.`}
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

        {/* The axis control, on its own row above the plot rather than in the header:
            it changes how the chart is drawn and nothing about what the figures say. */}
        <div className="plot-controls">
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

        {renderPlot()}

        <p className="note">
          One point per strike, one strike per listed contract, at the latest minute this
          expiry was stored. <strong>Nothing here is fitted, smoothed or interpolated</strong>:
          the dots are the volatilities the engine solved and the segments between them are
          straight, because a spline would put a number between two strikes in exactly the
          place a reader would take one off. A dotted vertical rule is a strike that arrived
          with no solved volatility — the line breaks there and is never drawn through it.
          Both x-axes are one linear scale in strike, read once as a strike and once as an
          offset from the forward, which is why their ticks are evenly spaced. Hover any
          point for the strike, the volatility, its offset from the forward, the
          out-of-the-money leg it was solved on, and the solver&rsquo;s reason when there is
          no number.
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
   */
  function renderPlot() {
    if (error) return null;

    if (!smile || !minute) {
      return (
        <section className="plot-empty" aria-label="Smile plot">
          <p>
            {busy || !smile
              ? "Reading the store…"
              : `No stored minutes for ${underlying} ${expiry}.`}
            {smile && !minute ? (
              <>
                <br />
                Nothing has gone wrong — the store is answering &ldquo;nothing yet&rdquo;.
              </>
            ) : null}
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
          // Re-keyed per series and per scale: a different expiry is a different curve and
          // a different axis is a different chart, and neither should animate out of the
          // last one.
          key={`${underlying}:${expiry}:${minute.minute}:${scale}`}
          minute={minute}
          scale={scale}
          underlying={underlying}
          expiry={expiry}
        />
      </>
    );
  }
}
