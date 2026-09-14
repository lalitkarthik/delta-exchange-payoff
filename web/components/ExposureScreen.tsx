"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import GexBars, { LEVEL_COUNT } from "@/components/GexBars";
import OiBars from "@/components/OiBars";
import ThemeToggle from "@/components/ThemeToggle";
import { UNDERLYINGS, type ChainResponse, type Underlying } from "@/lib/contract";
import { ENGINE_URL, loadExpiries } from "@/lib/engine";
import {
  daysToExpiry,
  levels,
  sideTotals,
  strikeWindow,
  sumBoards,
  zeroCrossStrike,
  type StrikeBar,
} from "@/lib/exposure";
import { formatFetchedClock, formatRatio, formatSpot, formatStrike } from "@/lib/format";
import { LIVE_STATUS_LABEL, subscribeChain, type LiveStatus } from "@/lib/live";
import type { ViewRequest } from "@/lib/view";

/** The STRIKES control. `null` is ALL. Counted either side of the money, so `15` draws
 *  thirty-one rows on a board that has them. */
const WINDOWS: (number | null)[] = [5, 10, 15, 20, 25, null];

/**
 * The shell both exposure boards wear: the pickers, the expiries panel, one subscription
 * per checked expiry, and whichever chart the tab asks for.
 *
 * **One shell, two boards.** OI and GEX ask the engine the same question — the live chain
 * for one underlying and one expiry — and differ in what they make of the answer and in
 * how that answer is best drawn. The projection arrives as a prop, so the fetching, the
 * pickers, the sockets, the status chips and the empty states exist once.
 *
 * **Many expiries, summed.** The panel on the right checks any number of them, and a
 * checked board is added into the others strike by strike. That is what the question
 * "what is open at this strike" means: a strike carries whatever is written against it
 * whenever it expires, and a reader looking for a wall is looking for the total. One
 * socket is opened per checked expiry — the engine's fan-out makes that a queue each
 * rather than a connection each, which is the whole reason `DeltaFeed` holds one socket
 * to the venue — and `lib/exposure.ts` does the summing on projections rather than on
 * chains, because two expiries at one strike are two different contracts and nothing
 * about their volatilities should be averaged.
 *
 * **Live only, and there is no scrubber.** Both boards are read for where the market is
 * standing right now; the stored day would need an engine route that joins gamma from
 * table C to open interest from table B, which is a separate ticket and not needed to see
 * the shape. The URL therefore carries the underlying and the checked expiries and no
 * minute — a minute in this address would promise a fixed board the screen does not have.
 *
 * **A projection may refuse the whole board**, and that is the state this shell is most
 * careful about. GEX in dollars needs `spot` and `contract_value`; without either, the
 * axis would be in units nobody could name, so `lib/exposure.ts` returns `null` and the
 * shell says which multiplier is missing instead of drawing an unlabelled chart. This is
 * the same rule as a leg with no volatility carrying no Greeks.
 */
export default function ExposureScreen({
  initial,
  initialExpiries,
  board,
  title,
  project,
  refusal,
  axisTitle,
  format,
  note,
  coverage,
}: {
  initial: ViewRequest;
  /** Every expiry the link checked. `parseExpiries` has already dropped the malformed. */
  initialExpiries: string[];
  /** Which chart, and which tab is lit. */
  board: "oi" | "gex";
  title: string;
  /** Turns one live chain into bars, or refuses it. */
  project: (chain: ChainResponse) => StrikeBar[] | null;
  /** Why a refusal happened, in one sentence, given the chain that was refused. */
  refusal: (chain: ChainResponse) => string;
  axisTitle: string;
  format: (value: number | null) => string;
  note: React.ReactNode;
  /** The coverage line, or `null` when the projection has nothing to qualify. */
  coverage?: (chains: ChainResponse[]) => string | null;
}) {
  const [underlying, setUnderlying] = useState<Underlying>(initial.underlying ?? "BTC");
  const [expiries, setExpiries] = useState<string[]>([]);
  const [checked, setChecked] = useState<string[]>(
    initialExpiries.length > 0 ? initialExpiries : initial.expiry ? [initial.expiry] : [],
  );
  const [chains, setChains] = useState<Record<string, ChainResponse>>({});
  const [status, setStatus] = useState<Record<string, LiveStatus>>({});
  const [detail, setDetail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [window_, setWindow] = useState<number | null>(15);

  /*
   * The expiry list, and the choice it constrains.
   *
   * `useSmileDay`'s rule, kept: a URL naming an expiry the engine does not list has to be
   * resolved against the list before anything is subscribed, or the panel shows one set
   * of ticks while the chart draws another. Anything checked that is no longer listed is
   * dropped rather than carried — an expiry that has settled is not a board.
   */
  useEffect(() => {
    let live = true;
    loadExpiries(underlying)
      .then(({ data, preferredExpiry }) => {
        if (!live) return;
        setExpiries(data.expiries);
        setError(null);
        setChecked((current) => {
          const kept = current.filter((e) => data.expiries.includes(e));
          if (kept.length > 0) return kept;
          if (preferredExpiry && data.expiries.includes(preferredExpiry)) return [preferredExpiry];
          return data.expiries[0] ? [data.expiries[0]] : [];
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

  /*
   * One subscription per checked expiry.
   *
   * Keyed on the joined list so checking a box adds a socket and unchecking one closes it,
   * rather than tearing down and rebuilding every subscription on each change. Each
   * expiry's chain and status are held under its own key, so a board still warming up does
   * not blank the ones already arriving — and the chart draws what it has.
   */
  const series = checked.join(",");
  useEffect(() => {
    const wanted = series ? series.split(",") : [];
    setChains({});
    setStatus({});
    const stops = wanted.map((expiry) =>
      subscribeChain(underlying, expiry, {
        onChain: (chain) => setChains((held) => ({ ...held, [expiry]: chain })),
        onStatus: (next, why) => {
          setStatus((held) => ({ ...held, [expiry]: next }));
          if (why) setDetail(why);
        },
      }),
    );
    return () => {
      for (const stop of stops) stop();
    };
  }, [underlying, series]);

  // The address bar follows the pickers. `replaceState` rather than a router push, for
  // the reason every screen here gives: changing an expiry must not fill the back button.
  useEffect(() => {
    const params = new URLSearchParams({ underlying });
    if (checked.length > 0) params.set("expiry", checked.join(","));
    const href = `${globalThis.location.pathname}?${params.toString()}`;
    if (`${globalThis.location.pathname}${globalThis.location.search}` !== href) {
      globalThis.history.replaceState(null, "", href);
    }
  }, [underlying, checked]);

  const held = useMemo(
    () => checked.map((expiry) => chains[expiry]).filter((c): c is ChainResponse => !!c),
    [checked, chains],
  );
  /* The nearest checked expiry leads: it is the one whose spot and forward the header
     shows, and on a live board every checked expiry reports the same spot anyway. */
  const lead = held[0] ?? null;

  const { bars, refused } = useMemo(() => {
    if (held.length === 0) return { bars: null, refused: null };
    const boards: StrikeBar[][] = [];
    for (const chain of held) {
      const projected = project(chain);
      if (projected === null) return { bars: null, refused: chain };
      boards.push(projected);
    }
    return { bars: sumBoards(boards), refused: null };
  }, [held, project]);

  const shown = useMemo(
    () => (bars ? strikeWindow(bars, lead?.atm_strike ?? null, window_) : null),
    [bars, lead, window_],
  );

  const totals = shown ? sideTotals(shown) : null;
  const ranked = shown && board === "gex" ? levels(shown, LEVEL_COUNT) : null;
  const flip = shown && board === "gex" ? zeroCrossStrike(shown) : null;
  const coverageLine = coverage && held.length > 0 ? coverage(held) : null;
  const worst = overallStatus(checked, status);
  const now = useNow();

  return (
    // No `.app` wrapper and no rail: `app/layout.tsx` owns both, so navigating between
    // screens does not tear down the chain websocket.
    <main className="screen">
      <header className="header">
        <div className="brand">DELTA</div>
        <h1 className="screen-title">{title}</h1>

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

        <div className="picker">
          <span className="stat-label">Strikes</span>
          <div className="scale-toggle" role="group" aria-label="Strikes either side of the money">
            {WINDOWS.map((count) => (
              <button
                key={count ?? "all"}
                type="button"
                className="scale-option"
                aria-pressed={window_ === count}
                onClick={() => setWindow(count)}
              >
                {count ?? "ALL"}
              </button>
            ))}
          </div>
        </div>

        <div className="stat lead">
          <span className="stat-label">Spot</span>
          <span className="stat-value">{formatSpot(lead?.spot ?? null)}</span>
        </div>

        <div className="stat">
          <span className="stat-label">As of</span>
          <span className="stat-value">
            {lead ? formatFetchedClock(lead.fetched_at) : "—"}{" "}
            <span className="stat-note">UTC</span>
          </span>
        </div>

        <span className="chip" title={detail ?? `Streaming from ${ENGINE_URL}/ws/chain.`}>
          {LIVE_STATUS_LABEL[worst]}
        </span>

        <ThemeToggle />
      </header>

      {/* The two boards are two routes, so the tabs are links: both URLs stay shareable
          and the rail keeps both entries, while the strip reads as one screen with two
          views. The checked expiries travel across, because a tab is a change of view
          and not a change of subject. */}
      <nav className="board-tabs" aria-label="Board">
        {(["oi", "gex"] as const).map((which) => (
          <Link
            key={which}
            className="tab"
            href={`/${which}?underlying=${underlying}${checked.length ? `&expiry=${checked.join(",")}` : ""}`}
            aria-selected={board === which}
            role="tab"
          >
            {which.toUpperCase()}
          </Link>
        ))}
      </nav>

      <div className="board">
        <div className="board-main">
          <div className="stat-band">
            <Stat label="Spot" value={formatSpot(lead?.spot ?? null)} lead />
            <Stat label="Forward" value={formatSpot(lead?.forward ?? null)} />
            {board === "oi" ? (
              <>
                <Stat label="Call tot" value={format(totals?.call ?? null)} tone="down" />
                <Stat label="Put tot" value={format(totals?.put ?? null)} tone="up" />
                <Stat label="PCR" value={formatRatio(totals?.pcr ?? null)} />
              </>
            ) : (
              <Stat
                label="Gamma flip"
                value={flip === null ? "—" : formatStrike(flip)}
                tone="flip"
              />
            )}
          </div>

          {coverageLine ? <p className="board-coverage">{coverageLine}</p> : null}
          {error ? <p className="notice error">{error}</p> : null}

          {held.length === 0 ? (
            <p className="notice">
              {checked.length === 0
                ? "No expiry is checked. Pick one on the right."
                : worst === "error"
                  ? (detail ?? "The engine could not be reached.")
                  : "Connecting to the engine…"}
            </p>
          ) : refused !== null ? (
            // A refusal, not an empty chart. The projection could not be made in the units
            // the axis claims, and saying which multiplier is missing is the whole answer.
            <p className="notice">{refusal(refused)}</p>
          ) : board === "oi" ? (
            <OiBars
              bars={shown!}
              spot={lead?.spot ?? null}
              atmStrike={lead?.atm_strike ?? null}
              format={format}
              axisTitle={axisTitle}
            />
          ) : (
            <GexBars
              bars={shown!}
              spot={lead?.spot ?? null}
              flip={flip}
              format={format}
              axisTitle={axisTitle}
            />
          )}

          <p className="note">{note}</p>
        </div>

        <aside className="board-side">
          <h2 className="side-title">Expiries</h2>
          <ul className="expiry-list">
            {expiries.map((expiry) => {
              const dte = now === null ? null : daysToExpiry(expiry, now);
              return (
                <li key={expiry}>
                  <label className={`expiry-row ${checked.includes(expiry) ? "on" : ""}`}>
                    <input
                      type="checkbox"
                      checked={checked.includes(expiry)}
                      onChange={(e) =>
                        setChecked((current) =>
                          e.target.checked
                            ? // Kept in the engine's own order — ascending by date — so the
                              // lead expiry is the nearest one however they were ticked.
                              expiries.filter((x) => x === expiry || current.includes(x))
                            : current.filter((x) => x !== expiry),
                        )
                      }
                    />
                    <span className="expiry-date">{expiry}</span>
                    <span className="expiry-dte">{dte === null ? "" : `${dte}D`}</span>
                  </label>
                </li>
              );
            })}
            {expiries.length === 0 ? <li className="expiry-empty">—</li> : null}
          </ul>

          {ranked ? (
            <>
              <LevelList title="Resistances" levels={ranked.resistances} tone="up" format={format} />
              <LevelList title="Supports" levels={ranked.supports} tone="down" format={format} />
            </>
          ) : null}
        </aside>
      </div>
    </main>
  );
}

/** One figure in the band under the tabs. */
function Stat({
  label,
  value,
  lead,
  tone,
}: {
  label: string;
  value: string;
  lead?: boolean;
  tone?: "up" | "down" | "flip";
}) {
  return (
    <div className={`stat ${lead ? "lead" : ""}`}>
      <span className="stat-label">{label}</span>
      <span className={`stat-value ${tone ? `tone-${tone}` : ""}`}>{value}</span>
    </div>
  );
}

/**
 * The ranked strikes either side, as the panel reads them.
 *
 * The top of each list is the wall and is named as one; the rest are numbered. The figure
 * is carried beside the strike because a rank on its own says which is largest and not by
 * how much, and two levels within a percent of each other are not a hierarchy.
 */
function LevelList({
  title,
  levels: list,
  tone,
  format,
}: {
  title: string;
  levels: { strike: number; value: number; rank: number }[];
  tone: "up" | "down";
  format: (value: number | null) => string;
}) {
  if (list.length === 0) return null;
  const code = tone === "up" ? "R" : "S";
  return (
    <>
      <h2 className="side-title">{title}</h2>
      <ul className="level-list">
        {list.map((level) => (
          <li key={level.strike} className={`level level-${tone}`}>
            <span className="level-rank">{`${code}${level.rank}`}</span>
            <span className="level-name">
              {level.rank === 1 ? (tone === "up" ? "Call wall" : "Put wall") : title.slice(0, -1)}
            </span>
            <span className="level-strike">{formatStrike(level.strike)}</span>
            <span className="level-value">{format(level.value)}</span>
          </li>
        ))}
      </ul>
    </>
  );
}

/**
 * One status for several sockets: the worst any of them is in.
 *
 * A board summed from three expiries is only as live as its least live subscription, and
 * a chip reading LIVE while one of the three is reconnecting would be claiming a total
 * that is short a third of itself. The order is the severity order, worst first.
 */
function overallStatus(checked: string[], status: Record<string, LiveStatus>): LiveStatus {
  const order: LiveStatus[] = ["error", "closed", "connecting", "waiting", "live"];
  for (const state of order) {
    if (checked.some((expiry) => status[expiry] === state)) return state;
  }
  return "connecting";
}

/**
 * Today, refreshed hourly, for the DTE beside each expiry.
 *
 * Not `new Date()` read during render: that is a different value on the server and in the
 * browser, which React reports as a hydration mismatch. It is set in an effect, so the
 * first paint carries no DTE and the second carries the real one — a blank count for one
 * frame is better than a count that is wrong in a way nothing would catch.
 */
function useNow(): Date | null {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), 3_600_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}
