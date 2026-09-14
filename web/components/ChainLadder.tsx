"use client";

import { useEffect, useRef, type KeyboardEvent } from "react";

import type { ChainResponse, ChainRow, Leg } from "@/lib/contract";
import {
  directionOf,
  priceMemoryOf,
  type Direction,
  type PriceMemory,
} from "@/lib/direction";
import {
  DASH,
  formatDelta,
  formatGamma,
  formatGreek,
  formatIv,
  formatOi,
  formatPrice,
  formatStrike,
} from "@/lib/format";
import { canonicalInstrument } from "@/lib/instrument";
import { heldAt } from "@/lib/legs";
import type { Direction as LegDirection, LegRequest } from "@/lib/payoff";

/**
 * Calls left, puts right, strikes down the middle — the sibling chain's table, wearing
 * the sibling chain's stylesheet (`payoff-project/web/components/ChainTable.tsx`).
 *
 * Calls read outward-in from the left and puts inward-out to the right:
 *
 *     OI  Rho  Theta  Vega  Gamma  Delta  IV  Bid  Ask  |  STRIKE  |  ...mirrored
 *
 * so the two columns either side of the strike are always the tradeable prices, and the
 * position-sized figures sit out at the edges. The Greeks read outward in order of how
 * often a desk looks at them: delta and the volatility nearest the prices, rho furthest
 * away. Nineteen columns is wide, and the wrapper scrolls horizontally rather than
 * shrinking the type — a dense numeric table that cannot be read is worse than one that
 * has to be scrolled.
 *
 * **The Greeks are named, not lettered.** The obvious headers are the Greek letters, and
 * they were - until rho and nu came back as "P" and "N". In this table's monospace face
 * ρ is very nearly a Latin p and ν very nearly a v, so two of the five columns were
 * unreadable and one reader asked what P and N meant. Θ and Δ survive the same test, but
 * a row of headers where three are words and two are glyphs is worse than five words. Mark price is not a column; it is in the
 * cell tooltip beside all three IVs, because a trader deals at the bid and the ask.
 *
 * **The IV and Δ columns are ours, not Delta's.** They used to be the venue's published
 * figures; since T8 the engine solves a volatility per strike from the out-of-the-money
 * leg's midpoint and computes Greeks from it. Delta's own numbers have not gone
 * anywhere — they are still in the payload and now read out in the cell tooltip, which
 * is what makes the comparison between the two possible at a glance.
 *
 * Delta republishes its figures every 5,001 ms while the book underneath moves every
 * 508 ms, so ours are up to **9.8x fresher**. Watching one against the other during a
 * fast move is the point of the whole project: theirs steps, ours moves underneath it.
 *
 * There are still two IV columns rather than the sibling's single centre one, and the
 * number in them is now the *same* on both sides — parity gives one volatility per
 * strike. The tooltip names the leg it was actually solved from so that the repetition
 * cannot be misread as two independent solves.
 *
 * **Bid and ask cells carry a direction arrow** — green up, red down — comparing this
 * push against the previous one. See `lib/direction.ts` for what that does and does not
 * mean; briefly, it samples the push and not the tick.
 *
 * Three rules this table must not break, and they are three different statements:
 *
 *  - **A missing side renders blank**, hatched. Never a zero, never a dash that could
 *    read as a price. Delta lists a call at a strike with no put; the row stays, its
 *    strike stays, and the absent half is visibly not a quote.
 *  - **A null field inside a present quote renders empty.** There is a contract there
 *    and the venue simply did not price this field of it, which is a smaller claim than
 *    a missing side and gets a smaller mark: nothing at all, on the normal ground.
 *  - **A real zero renders `0`.** Zero open interest is a fact; production ETH chains
 *    carry genuine zero-OI rows. Empty and zero mean opposite things.
 *
 * The in-the-money half carries a wash. It is measured against **spot**, because spot is
 * the only reference this project has — the sibling measures against its fitted Forward
 * and says so, but Delta publishes no forward and `docs/chain-contract.md` exposes none,
 * so inventing one to shade a table would be the worst possible reason to model.
 * `atm_strike` follows the same reference (the engine picks the listed strike nearest
 * spot), so the star and the wash agree.
 *
 * **P4 adds a tenth column a side, nearest the strike, when `onPick` is supplied.** The
 * B/S buttons take the position the diagram above shows Ask holding — the sibling
 * project's own placement, and the reason repeated at the buttons themselves — which
 * means "the two columns either side of the strike are always the tradeable prices" is
 * true only of a read-only ladder now; a pickable one reads pick, then ask, then bid,
 * outward from the strike on both sides. Nineteen columns becomes twenty-one. `onPick`
 * absent renders the ladder exactly as this comment already describes — nine a side,
 * not ten — which is what `tests/ladder-fingerprint.test.tsx` pins.
 */

/** Both comparisons strict, so a strike sitting exactly on spot is in the money on
 *  neither side. The boundary belongs to nobody, which is the honest answer for a
 *  contract with no intrinsic value either way. Exported so `tests/moneyness.test.ts`
 *  can pin it directly — the pure seam, per the following.test.ts pattern.
 *
 *  **`spot === null` is never in the money, on either side, at any strike.** #47: the
 *  historical route can answer a minute with nothing in `spot-bars`, and a coerced
 *  `strike < 0` used to wash every put in-the-money and no call — silently, on the one
 *  screen this project has for seeing where the money is. Not knowing where spot is
 *  renders as not knowing: no highlight beats a highlight at a spot that isn't real. */
export function inTheMoney(strike: number, spot: number | null, side: "call" | "put"): boolean {
  if (spot === null) return false;
  return side === "call" ? strike < spot : strike > spot;
}

/** OI, rho, theta, vega, gamma, delta, IV, bid, ask. Used by the header `colSpan`, by
 *  the hatched cells of an unlisted side, and nowhere else — one number, three readers. */
const COLUMNS_PER_SIDE = 9;

/**
 * One side of one row: five cells, in reading order for that side.
 *
 * `leg` is `null` when that side is not listed at all, and then every cell is hatched
 * and empty — computed before any moneyness, deliberately: shading an absence would be
 * a claim about a price that was never printed.
 */
function QuoteCells({
  leg,
  side,
  strike,
  spot,
  previous,
  underlying,
  expiry,
  quoteCurrency,
  onSelectContract,
  legs,
  onPick,
}: {
  leg: Leg | null;
  side: "call" | "put";
  strike: number;
  /** `null` on the historical route when `spot-bars` has no row for the minute —
   * `docs/historical-chain-contract.md`. `inTheMoney` above turns that into no
   * highlight on either side, never a wash at zero. */
  spot: number | null;
  previous: PriceMemory | null;
  underlying: string;
  expiry: string;
  /** `chain.quote_currency`, #60 (I1) — threaded down rather than re-read from a
   * module constant, so a second venue's chain needs no change here. */
  quoteCurrency: string;
  /** #46: opens the contract chart panel for this leg. `undefined` renders every cell
   * exactly as before — the ladder has one caller today, but nothing here should break
   * a second one that renders read-only. */
  onSelectContract?: (instrument: string) => void;
  /** P4: the strategy the B/S buttons read to decide which one is lit. Ignored — and
   * no buttons rendered at all — when `onPick` is absent, for the same reason
   * `onSelectContract`'s own comment gives. */
  legs?: LegRequest[];
  /** P4: builds or removes one leg. `undefined` renders every cell exactly as before
   * #46 left them — no picks column, nine cells per side, not ten. */
  onPick?: (instrument: string, direction: LegDirection) => void;
}) {
  const pickable = Boolean(onPick);

  if (leg === null) {
    const label = `No ${side} listed at this strike`;
    // One hatched cell per existing column on this side — never `itm`-washed, since
    // shading an absence would be a claim about a price that was never printed — plus
    // one more, distinctly classed, when the picks column is in play. There is nothing
    // to buy or sell on a side that is not listed at all, so it stays hatched too
    // rather than growing a live button; `class="picks blank"` (not `class="picks"`
    // alone) is what lets the DOM fingerprint test tell this cell apart from the
    // quoted branch's below and strip only what P4 actually added.
    return (
      <>
        {Array.from({ length: COLUMNS_PER_SIDE }, (_, i) => (
          <td key={i} className="blank" title={label} />
        ))}
        {pickable && <td key="picks" className="picks blank" title={label} />}
      </>
    );
  }

  const itm = inTheMoney(strike, spot, side) ? "itm" : "";
  const ours = leg.computed;

  // Everything the columns had no room for. Delta's three IVs and its own delta live
  // here now that the columns carry ours — the two sitting side by side is the
  // comparison this project exists to make, and it costs nothing to surface.
  //
  // The venue's floored `bid_iv` of 0.000005 on deep in-the-money calls surfaces here as
  // `0.0005%` — a real value it publishes, not a rendering fault. A null IV reads as a
  // dash in prose, where there is no empty cell to leave blank.
  const solvedFrom = ours?.iv_leg
    ? ` (solved on the ${ours.iv_leg})`
    : "";
  const whyNot = ours && ours.iv === null && ours.iv_reason
    ? ` · not solved: ${ours.iv_reason}`
    : "";
  const detail =
    `${leg.symbol} · mark ${formatPrice(leg.mark) || DASH}` +
    ` · ours IV ${formatIv(ours?.iv ?? null) || DASH}${solvedFrom}` +
    ` · Delta IV bid ${formatIv(leg.bid_iv) || DASH}` +
    ` · mark ${formatIv(leg.mark_iv) || DASH}` +
    ` · ask ${formatIv(leg.ask_iv) || DASH}` +
    ` · Delta Δ ${formatDelta(leg.delta) || DASH}` +
    whyNot;

  // #46/P4: every cell of a listed leg opens the same contract, and the same string
  // is what the B/S buttons trade — so it is built once per leg rather than per cell
  // or per button, regardless of how many of the ten columns want it.
  const instrument =
    onSelectContract || onPick
      ? canonicalInstrument(underlying, expiry, strike, side, quoteCurrency)
      : null;
  // **Gated on `onSelectContract` specifically, not on `instrument`.** P4 also needs
  // `instrument` computed when only `onPick` is supplied, and a caller that wired the
  // buttons but not the chart must not get "open the chart" wired onto every other
  // cell for free — that would call `onSelectContract` as a function when it is
  // `undefined`.
  const selectable = Boolean(onSelectContract) && instrument !== null;
  const selectLabel = selectable
    ? `Open the chart for ${side} ${formatStrike(strike)}`
    : undefined;

  // A plain `onClick` fires on the `mouseup` that ends a click-drag text selection too —
  // there is nothing else in this handler to tell "clicked" from "just finished
  // selecting a value to copy it" apart. Refusing to open the panel while the selection
  // this click is ending is non-empty is what keeps copying a bid or a Greek out of the
  // table from also swapping the chart underneath it.
  const select = selectable
    ? () => {
        if (window.getSelection()?.toString()) return;
        onSelectContract!(instrument!);
      }
    : undefined;
  const activate = selectable
    ? (event: KeyboardEvent<HTMLTableCellElement>) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        onSelectContract!(instrument!);
      }
    : undefined;
  const clickable = select ? "clickable" : "";

  // Spread onto every cell rather than passed as separate props: a `<td>` needs all
  // four — the click guard, the keyboard equivalent, and enough ARIA to be a control
  // rather than data — or none of them, never some, and a cell built without this would
  // be reachable by mouse and invisible to a keyboard.
  const activation = selectable
    ? {
        onClick: select,
        onKeyDown: activate,
        tabIndex: 0,
        role: "button" as const,
        "aria-label": selectLabel,
      }
    : {};

  const cell = (key: string, text: string) => (
    <td
      key={key}
      className={`num ${itm} ${clickable}`.trim()}
      title={detail}
      {...activation}
    >
      {text}
    </td>
  );

  /** A price cell, with an arrow when it moved since the previous push. */
  const priced = (key: "bid" | "ask") => {
    const moved: Direction = directionOf(previous, leg.symbol, key, leg[key]);
    return (
      <td
        key={key}
        className={`num ${itm} ${clickable} ${moved ? `moved-${moved}` : ""}`.trim()}
        title={detail}
        {...activation}
      >
        {formatPrice(leg[key])}
        {moved && (
          // `aria-hidden` because the arrow repeats no information a screen reader
          // needs: it is a redundant encoding of a change the value itself carries,
          // and announcing "up arrow" on every push would make the table unusable.
          <span className={`arrow arrow-${moved}`} aria-hidden="true">
            {moved === "up" ? "▲" : "▼"}
          </span>
        )}
      </td>
    );
  };

  // Written outward-in once, then reversed for the put side, so the order can only be
  // wrong in one place.
  const outwardIn = [
    cell("oi", formatOi(leg.oi)),
    cell("rho", formatGreek(ours?.rho ?? null)),
    cell("theta", formatGreek(ours?.theta ?? null)),
    cell("vega", formatGreek(ours?.vega ?? null)),
    cell("gamma", formatGamma(ours?.gamma ?? null)),
    cell("delta", formatDelta(ours?.delta ?? null)),
    cell("iv", formatIv(ours?.iv ?? null)),
    priced("bid"),
    priced("ask"),
  ];

  if (!pickable) {
    return <>{side === "call" ? outwardIn : [...outwardIn].reverse()}</>;
  }

  // P4: the one column nearest the strike, on both sides — the sibling project's own
  // placement (`convex-hedge-payoff/web/components/ChainTable.tsx`), and for the same
  // reason: a trader is already looking at the strike when the pointer is here.
  const bought = heldAt(legs ?? [], instrument!, 1);
  const sold = heldAt(legs ?? [], instrument!, -1);
  const strikeText = formatStrike(strike);
  const picks = (
    <td key="picks" className={`picks ${itm}`.trim()}>
      <span className="bs">
        <button
          type="button"
          className={`buy ${bought ? "on" : ""}`.trim()}
          aria-pressed={bought}
          title={bought ? "Bought — click to remove one lot" : `Buy ${side} ${strikeText}`}
          onClick={() => onPick!(instrument!, 1)}
        >
          B
        </button>
        <button
          type="button"
          className={`sell ${sold ? "on" : ""}`.trim()}
          aria-pressed={sold}
          title={sold ? "Sold — click to remove one lot" : `Sell ${side} ${strikeText}`}
          onClick={() => onPick!(instrument!, -1)}
        >
          S
        </button>
      </span>
    </td>
  );

  return (
    <>{side === "call" ? [...outwardIn, picks] : [picks, ...[...outwardIn].reverse()]}</>
  );
}

export function ChainLadder({
  chain,
  onSelectContract,
  legs,
  onPick,
}: {
  chain: ChainResponse;
  /** #46: called with a leg's canonical instrument string when a reader clicks it.
   * Optional so a caller that only wants a read-only ladder is unaffected. */
  onSelectContract?: (instrument: string) => void;
  /** P4: the strategy so far, read by every row's B/S buttons to decide which one is
   * lit. Unused — and, with `onPick` absent, unrendered — on a caller that passes
   * neither. */
  legs?: LegRequest[];
  /** P4: adds or removes one leg. `undefined` renders the ladder exactly as it stood
   * before this ticket — nine columns a side, not ten, and no buttons at all. */
  onPick?: (instrument: string, direction: LegDirection) => void;
}) {
  const pickable = Boolean(onPick);
  const columnsPerSide = COLUMNS_PER_SIDE + (pickable ? 1 : 0);
  const money = useRef<HTMLTableRowElement>(null);

  // The previous push's prices, so this one can be compared against them.
  //
  // **Keyed on the identity of `chain`, and that is load-bearing.** A naive
  // read-then-overwrite of a ref during render is defeated by StrictMode, which
  // double-invokes the render in development: the second pass reads back what the first
  // pass wrote, so every cell compares against itself and no arrow ever appears — in the
  // one environment where anyone would look at it. `reactStrictMode` is on in
  // `next.config.ts`.
  //
  // Holding both snapshots and only rolling them forward when the chain object actually
  // changes makes the render idempotent: run it twice on the same push and `previous`
  // is still the *previous* push. A new push parses a new object, so identity is exactly
  // the right trigger — and unlike `fetched_at` it cannot collide when two pushes land
  // inside the same second.
  const store = useRef<{
    source: ChainResponse;
    previous: PriceMemory | null;
    current: PriceMemory;
  } | null>(null);

  if (store.current === null || store.current.source !== chain) {
    store.current = {
      source: chain,
      previous: store.current?.current ?? null,
      current: priceMemoryOf(chain),
    };
  }
  const seen = store.current.previous;

  // Open on the money. A live BTC chain runs from far below spot to far above it and the
  // interesting strikes are in the middle, so a table scrolled to its top shows rows of
  // deep out-of-the-money puts — which reads as broken rather than as far away.
  //
  // On mount only. Re-centring on every Refresh would yank the view out from under
  // someone reading a wing. The page re-keys this component when the underlying or the
  // expiry changes, which is a different ladder and does deserve a fresh centring.
  useEffect(() => {
    money.current?.scrollIntoView({ block: "center" });
  }, []);

  if (chain.rows.length === 0) {
    return <p className="notice">The engine returned no strikes for this expiry.</p>;
  }

  return (
    <div className="chain-wrap">
      <table className="chain">
        <caption className="sr-only">
          {chain.underlying} option chain expiring {chain.expiry}, priced in {chain.quote_currency}.
          Calls on the left, strikes in
          the centre, puts on the right. The IV and delta columns are computed by this engine from
          the order book; the venue’s own figures are in each cell’s tooltip. A hatched cell
          means that side is not listed; an empty cell means the field could not be computed or
          was not priced; a zero means zero.
        </caption>
        <thead>
          <tr>
            <th className="side-head side-call" colSpan={columnsPerSide}>
              Calls
            </th>
            <th className="side-head">Strike</th>
            <th className="side-head side-put" colSpan={columnsPerSide}>
              Puts
            </th>
          </tr>
          <tr>
            <th>OI</th>
            <th title="Rho, per one percent. Computed here, not the venue&rsquo;s.">Rho</th>
            <th title="Theta, one calendar day. Computed here, not the venue&rsquo;s.">Theta</th>
            <th title="Vega, per volatility point. Computed here, not the venue&rsquo;s.">Vega</th>
            <th title="Gamma, scaled by 10,000 so it is readable. Computed here.">Gamma&nbsp;×10⁴</th>
            <th title="Delta, with respect to the forward. Computed here.">Delta</th>
            <th title="Implied volatility, solved from the out-of-the-money leg.">IV</th>
            <th title={`Bid, in ${chain.quote_currency}.`}>Bid&nbsp;({chain.quote_currency})</th>
            <th title={`Ask, in ${chain.quote_currency}.`}>Ask&nbsp;({chain.quote_currency})</th>
            {pickable && <th className="picks-head">Pick</th>}
            <th style={{ textAlign: "center" }}>Strike</th>
            {pickable && <th className="picks-head">Pick</th>}
            <th title={`Ask, in ${chain.quote_currency}.`}>Ask&nbsp;({chain.quote_currency})</th>
            <th title={`Bid, in ${chain.quote_currency}.`}>Bid&nbsp;({chain.quote_currency})</th>
            <th title="Implied volatility, solved from the out-of-the-money leg.">IV</th>
            <th title="Delta, with respect to the forward. Computed here.">Delta</th>
            <th title="Gamma, scaled by 10,000 so it is readable. Computed here.">Gamma&nbsp;×10⁴</th>
            <th title="Vega, per volatility point. Computed here, not the venue&rsquo;s.">Vega</th>
            <th title="Theta, one calendar day. Computed here, not the venue&rsquo;s.">Theta</th>
            <th title="Rho, per one percent. Computed here, not the venue&rsquo;s.">Rho</th>
            <th>OI</th>
          </tr>
        </thead>
        <tbody>
          {/* Rows arrive ascending by strike and are rendered in that order. */}
          {chain.rows.map((row: ChainRow) => {
            const atm = row.strike === chain.atm_strike;
            return (
              <tr key={row.strike} ref={atm ? money : undefined} className={atm ? "at-the-money" : ""}>
                <QuoteCells
                  leg={row.call}
                  side="call"
                  strike={row.strike}
                  spot={chain.spot}
                  previous={seen}
                  underlying={chain.underlying}
                  expiry={chain.expiry}
                  quoteCurrency={chain.quote_currency}
                  onSelectContract={onSelectContract}
                  legs={legs}
                  onPick={onPick}
                />
                <td className="strike">
                  {formatStrike(row.strike)}
                  {atm && " ★"}
                </td>
                <QuoteCells
                  leg={row.put}
                  side="put"
                  strike={row.strike}
                  spot={chain.spot}
                  previous={seen}
                  underlying={chain.underlying}
                  expiry={chain.expiry}
                  quoteCurrency={chain.quote_currency}
                  onSelectContract={onSelectContract}
                  legs={legs}
                  onPick={onPick}
                />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
