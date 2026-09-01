"use client";

import { useEffect, useRef } from "react";

import type { ChainResponse, ChainRow, Leg } from "@/lib/contract";
import {
  DASH,
  formatDelta,
  formatIv,
  formatOi,
  formatPrice,
  formatStrike,
} from "@/lib/format";

/**
 * Calls left, puts right, strikes down the middle — the sibling chain's table, wearing
 * the sibling chain's stylesheet (`payoff-project/web/components/ChainTable.tsx`).
 *
 * Calls read outward-in from the left and puts inward-out to the right:
 *
 *     OI  Δ  IV  Bid  Ask  |  STRIKE  |  Ask  Bid  IV  Δ  OI
 *
 * so the two columns either side of the strike are always the tradeable prices, and the
 * position-sized figures sit out at the edges. Mark price is not a column; it is in the
 * cell tooltip beside all three IVs, because a trader deals at the bid and the ask.
 *
 * **One deliberate deviation from the sibling: IV is per side, not shared.** The sibling
 * carries a single centre IV column, because it *solves* one volatility per strike from
 * the out-of-the-money leg. This project solves nothing — Delta publishes `mark_iv`
 * separately for the call and the put, and the two genuinely differ: 28.19% against
 * 27.58% on the at-the-money strike of a live BTC chain. Collapsing them into one figure
 * would invent a number the venue never sent, so there are two IV columns.
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
 */

/** Both comparisons strict, so a strike sitting exactly on spot is in the money on
 *  neither side. The boundary belongs to nobody, which is the honest answer for a
 *  contract with no intrinsic value either way. */
function inTheMoney(strike: number, spot: number, side: "call" | "put"): boolean {
  return side === "call" ? strike < spot : strike > spot;
}

/**
 * One side of one row: five cells, in reading order for that side.
 *
 * `leg` is `null` when that side is not listed at all, and then every cell is hatched
 * and empty — computed before any moneyness, deliberately: shading an absence would be
 * a claim about a price that was never printed.
 */
function QuoteCells({ leg, side, strike, spot }: {
  leg: Leg | null;
  side: "call" | "put";
  strike: number;
  spot: number;
}) {
  if (leg === null) {
    const label = `No ${side} listed at this strike`;
    return (
      <>
        <td className="blank" title={label} />
        <td className="blank" title={label} />
        <td className="blank" title={label} />
        <td className="blank" title={label} />
        <td className="blank" title={label} />
      </>
    );
  }

  const itm = inTheMoney(strike, spot, side) ? "itm" : "";

  // Everything the columns had no room for. The venue's floored `bid_iv` of 0.000005 on
  // deep in-the-money calls surfaces here as `0.0005%` — a real value it publishes, not
  // a rendering fault. A null IV reads as a dash in prose, where there is no empty cell
  // to leave blank.
  const detail =
    `${leg.symbol} · mark ${formatPrice(leg.mark) || DASH}` +
    ` · IV bid ${formatIv(leg.bid_iv) || DASH}` +
    ` · mark ${formatIv(leg.mark_iv) || DASH}` +
    ` · ask ${formatIv(leg.ask_iv) || DASH}`;

  const cell = (key: string, text: string) => (
    <td key={key} className={`num ${itm}`} title={detail}>
      {text}
    </td>
  );

  // Written outward-in once, then reversed for the put side, so the order can only be
  // wrong in one place.
  const outwardIn = [
    cell("oi", formatOi(leg.oi)),
    cell("delta", formatDelta(leg.delta)),
    cell("iv", formatIv(leg.mark_iv)),
    cell("bid", formatPrice(leg.bid)),
    cell("ask", formatPrice(leg.ask)),
  ];

  return <>{side === "call" ? outwardIn : [...outwardIn].reverse()}</>;
}

export function ChainLadder({ chain }: { chain: ChainResponse }) {
  const money = useRef<HTMLTableRowElement>(null);

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
          {chain.underlying} option chain expiring {chain.expiry}. Calls on the left, strikes in
          the centre, puts on the right. A hatched cell means that side is not listed; an empty
          cell means the venue did not price that field; a zero means zero.
        </caption>
        <thead>
          <tr>
            <th className="side-head side-call" colSpan={5}>
              Calls
            </th>
            <th className="side-head">Strike</th>
            <th className="side-head side-put" colSpan={5}>
              Puts
            </th>
          </tr>
          <tr>
            <th>OI</th>
            <th>Δ</th>
            <th>IV</th>
            <th>Bid</th>
            <th>Ask</th>
            <th style={{ textAlign: "center" }}>Strike</th>
            <th>Ask</th>
            <th>Bid</th>
            <th>IV</th>
            <th>Δ</th>
            <th>OI</th>
          </tr>
        </thead>
        <tbody>
          {/* Rows arrive ascending by strike and are rendered in that order. */}
          {chain.rows.map((row: ChainRow) => {
            const atm = row.strike === chain.atm_strike;
            return (
              <tr key={row.strike} ref={atm ? money : undefined} className={atm ? "at-the-money" : ""}>
                <QuoteCells leg={row.call} side="call" strike={row.strike} spot={chain.spot} />
                <td className="strike">
                  {formatStrike(row.strike)}
                  {atm && " ★"}
                </td>
                <QuoteCells leg={row.put} side="put" strike={row.strike} spot={chain.spot} />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
