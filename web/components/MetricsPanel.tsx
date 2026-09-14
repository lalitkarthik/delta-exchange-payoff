import { formatBound, formatRatio, formatScaled, formatStrike } from "@/lib/format";
import type { Metrics } from "@/lib/payoff";

/**
 * The five numbers beside the chart. `docs/payoff-contract.md` §metrics is the
 * authority for every one of them, and three of its conventions are easy to render
 * backwards into a perfectly plausible screen:
 *
 *   - **`max_loss` is a P&L on the same axis as the curve.** Negative on almost
 *     everything, so it is read straight off the chart rather than sign-flipped in the
 *     reader's head. It is not re-signed here, and a `0` means a strategy that cannot
 *     lose rather than one whose loss is unknown.
 *   - **`net_premium` positive is paid out** — a debit — and negative is received. The
 *     magnitude is printed with the word beside it, because "-1,400" alone is read as a
 *     loss about as often as it is read as a credit.
 *   - **`null` is unlimited**, and it is the word. `formatBound` owns that; nothing
 *     here substitutes a large number for an absent bound.
 *
 * **`factor` scales money and only money.** The breakevens are *prices of the
 * underlying*, not amounts, so the lot size does not touch them — multiplying a
 * breakeven by 0.001 would put it at 72.6 and the reader would have no way to tell.
 * `reward_risk` is a ratio and the factor cancels in it, so it is not scaled either;
 * `formatRatio`'s own docstring says so where it is defined.
 */
export default function MetricsPanel({
  metrics,
  factor,
  unit,
}: {
  metrics: Metrics;
  /** `1`, or `contract_value` when the per-contract toggle is on. */
  factor: number;
  /** What that factor makes the money columns mean — `USD` or `USD/contract`. */
  unit: string;
}) {
  const scaled = (value: number | null) => (value === null ? null : value * factor);

  /* Three states, not two. `Math.abs` below takes the sign off the number, so this word
     is carrying the whole distinction — and at exactly zero there is no distinction to
     carry. A costless structure labelled `0.00 debit` states something untrue about
     which way the money moved, which is precisely the failure the word exists to
     prevent. */
  const flow =
    metrics.net_premium < 0 ? "credit" : metrics.net_premium > 0 ? "debit" : null;

  /* A `<section>` around the table rather than a `<caption>` inside it, and the frame is
     on the section. `border-radius` on a `border-collapse: collapse` table is a no-op —
     the panel would read square beside the chart's rounded frame — and a `<caption>` is
     laid out outside the table's border box, so neither the corner nor the heading was
     going to sit where it looked like it would. `.legs-panel` on the chain screen is
     already a section, a heading and a body; this is that recipe, not a second one. */
  return (
    <section className="metrics-panel" aria-label="Metrics">
      <h2 className="metrics-title">At expiry — {unit}</h2>
      <table className="metrics">
      <tbody>
        <tr>
          <td>Max profit</td>
          <td className="num gain">{formatBound(scaled(metrics.max_profit))}</td>
        </tr>
        <tr>
          <td>Max loss</td>
          <td className="num loss">{formatBound(scaled(metrics.max_loss))}</td>
        </tr>
        <tr>
          <td>Breakevens</td>
          <td className="num" title="Prices of the underlying — the lot size does not apply.">
            {metrics.breakevens.length === 0
              ? "none"
              : metrics.breakevens.map((price) => formatStrike(price)).join("  ·  ")}
          </td>
        </tr>
        <tr>
          <td>Net premium</td>
          <td className="num">
            {formatScaled(Math.abs(metrics.net_premium) * factor)}
            {flow === null ? null : (
              <>
                {" "}
                <span className="metrics-note">{flow}</span>
              </>
            )}
          </td>
        </tr>
        <tr>
          <td>Reward / risk</td>
          <td className="num" title="max_profit over the magnitude of max_loss.">
            {formatRatio(metrics.reward_risk)}
          </td>
        </tr>
      </tbody>
      </table>
    </section>
  );
}
