import { formatScaled, formatStrike } from "@/lib/format";
import type { PayoffPoint } from "@/lib/payoff";

/**
 * P&L at expiry on the readable grid, so exact figures are read rather than inferred
 * from the picture.
 *
 * **These rows are the engine's**, on the step it chose for this chain — `table` and
 * `curve.corners` are the same type on purpose, one quantity sampled twice, and they
 * agree wherever they share a price. Computing a grid here would make the table and the
 * chart two implementations of one quantity, free to drift.
 *
 * The row nearest the forward is marked, and that is the only decision this component
 * makes. The forward rather than spot, because the forward is the price the market
 * implies for the date this table is about — and it is `null` on a chain that could not
 * be fitted, in which case nothing is marked rather than the first row being marked by
 * accident.
 *
 * `factor` scales the **P&L column only**. The price column is a price of the
 * underlying; the lot size does not apply to it.
 */
export default function PayoffTable({
  table,
  forward,
  factor,
  unit,
}: {
  table: PayoffPoint[];
  forward: number | null;
  factor: number;
  unit: string;
}) {
  const nearest =
    forward === null
      ? null
      : table.reduce<PayoffPoint | null>(
          (best, row) =>
            best === null || Math.abs(row.price - forward) < Math.abs(best.price - forward)
              ? row
              : best,
          null,
        );

  return (
    <table className="grid payoff-table">
      <thead>
        <tr>
          <th>Price at expiry (USD)</th>
          <th>P&amp;L ({unit})</th>
        </tr>
      </thead>
      <tbody>
        {table.map((row) => (
          <tr key={row.price} className={row === nearest ? "here" : ""}>
            <td className="num">{formatStrike(row.price)}</td>
            <td className={`num ${row.pnl >= 0 ? "gain" : "loss"}`}>
              {formatScaled(row.pnl * factor)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
