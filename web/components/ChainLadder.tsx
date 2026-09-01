import type { ChainResponse, ChainRow, Leg } from "@/lib/contract";
import { DASH, formatDelta, formatIv, formatOi, formatPrice, formatStrike } from "@/lib/format";

const SIDE_COLUMNS = ["Bid", "Ask", "Mark", "IV", "Δ", "OI"] as const;

/**
 * One side of one row. `leg` is `null` when that side is not listed at all — the row
 * still renders, with every cell of the absent side showing a dash. Dropping the row
 * would silently hide a strike that exists.
 */
function SideCells({ leg, side, itm }: { leg: Leg | null; side: "call" | "put"; itm: boolean }) {
  const cls = `num ${side}${itm ? " itm" : ""}`;

  if (leg === null) {
    return (
      <>
        {SIDE_COLUMNS.map((col) => (
          <td key={col} className={`${cls} absent`} title={`No ${side} listed at this strike`}>
            {DASH}
          </td>
        ))}
      </>
    );
  }

  // Each cell is independently nullable. The formatters return the dash for null, so
  // a real 0 still prints as `0.00` / `0` and stays distinguishable from missing data.
  //
  // The IV column shows mark IV. All three IVs go in the cell's tooltip, which is where
  // the venue's floored bid_iv (0.000005, i.e. "0.0005%") surfaces — that is a real
  // value the venue reports on deep in-the-money calls, not a rendering fault.
  const ivTitle =
    `${leg.symbol} · bid IV ${formatIv(leg.bid_iv)}` +
    ` · mark IV ${formatIv(leg.mark_iv)} · ask IV ${formatIv(leg.ask_iv)}`;

  const cells: { key: string; text: string; empty: boolean; title?: string }[] = [
    { key: "bid", text: formatPrice(leg.bid), empty: leg.bid === null },
    { key: "ask", text: formatPrice(leg.ask), empty: leg.ask === null },
    { key: "mark", text: formatPrice(leg.mark), empty: leg.mark === null },
    { key: "iv", text: formatIv(leg.mark_iv), empty: leg.mark_iv === null, title: ivTitle },
    { key: "delta", text: formatDelta(leg.delta), empty: leg.delta === null },
    // Open interest: `null` is a dash, but a genuine 0 prints as 0.
    { key: "oi", text: formatOi(leg.oi), empty: leg.oi === null },
  ];

  return (
    <>
      {cells.map((c) => (
        <td
          key={c.key}
          className={`${cls}${c.empty ? " empty" : ""}`}
          title={c.title ?? (c.empty ? `${leg.symbol}: no ${c.key}` : leg.symbol)}
        >
          {c.text}
        </td>
      ))}
    </>
  );
}

function LadderRow({ row, spot, atm }: { row: ChainRow; spot: number; atm: boolean }) {
  return (
    <tr className={atm ? "atm" : undefined}>
      <SideCells leg={row.call} side="call" itm={row.strike < spot} />
      <th scope="row" className="strike">
        {formatStrike(row.strike)}
        {atm ? <span className="atm-badge">ATM</span> : null}
      </th>
      <SideCells leg={row.put} side="put" itm={row.strike > spot} />
    </tr>
  );
}

export function ChainLadder({ chain }: { chain: ChainResponse }) {
  if (chain.rows.length === 0) {
    return <p className="notice">The engine returned no strikes for this expiry.</p>;
  }

  return (
    <div className="ladder-scroll">
      <table className="ladder">
        <caption className="sr-only">
          {chain.underlying} option chain expiring {chain.expiry}. Calls on the left, strikes in
          the centre, puts on the right. An em dash means no data; a zero means zero.
        </caption>
        <thead>
          <tr className="group-row">
            <th colSpan={SIDE_COLUMNS.length} scope="colgroup" className="group call">
              Calls
            </th>
            <th className="group strike-group" scope="col">
              Strike
            </th>
            <th colSpan={SIDE_COLUMNS.length} scope="colgroup" className="group put">
              Puts
            </th>
          </tr>
          <tr className="col-row">
            {SIDE_COLUMNS.map((c) => (
              <th key={`c-${c}`} scope="col" className="call">
                {c}
              </th>
            ))}
            <th scope="col" className="strike-group" />
            {SIDE_COLUMNS.map((c) => (
              <th key={`p-${c}`} scope="col" className="put">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {/* Rows arrive ascending by strike and are rendered in that order. */}
          {chain.rows.map((row) => (
            <LadderRow
              key={row.strike}
              row={row}
              spot={chain.spot}
              atm={row.strike === chain.atm_strike}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
