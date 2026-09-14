"use client";

import { DASH, formatDelta, formatGamma, formatGreek, formatIv, formatPrice, formatStrike } from "@/lib/format";
import { peakValue, type Cell, type Feature, type StructureGrid as Grid } from "@/lib/structures";

/**
 * The structure grid: one row per strike, one column per wing width.
 *
 * A matrix, which is why it is not one of the tables already here. `ChainLadder` is
 * fixed-column — nine fields a side, written once and reversed — and the exposure screens
 * draw charts; neither shell holds a table whose columns are data. So this borrows their
 * idioms rather than their code: the `.grid` recipe, the ladder's at-the-money marking,
 * the ladder's hatch for a cell that is not there, and `lib/format.ts` for every number.
 *
 * **Nothing is computed here.** Every value arrives from `lib/structures.ts`, which is
 * where the feature's verification lives. What is left is the choice of formatter per
 * feature and the tint, and the tint is the only arithmetic in the file: an opacity
 * proportional to `|value| / peak`, which is a shading rather than a claim.
 *
 * **Three cell states, told apart on purpose.** A cell whose wings could not be resolved
 * to listed strikes is **hatched**, the ladder's own mark for a contract that does not
 * exist. A cell that resolved but has no number — an unquoted leg, an unsolved
 * volatility — is **blank**. A real zero prints as a zero. That is the same three-way
 * split the ladder makes, and the reason `null` is never rendered as `0` anywhere here.
 */
export default function StructureGrid({
  grid,
  feature,
}: {
  grid: Grid;
  feature: Feature;
}) {
  const peak = peakValue(grid);
  return (
    <div className="structures-scroll">
      <table className="grid structures">
        <thead>
          <tr>
            {/* The reference terminal's own heading: rows are K, columns are D. */}
            <th scope="col">K \ D</th>
            {grid.offsets.map((offset) => (
              <th key={offset} scope="col">
                {formatStrike(offset)}
                {offset === 0 ? <span className="structures-sub">straddle</span> : null}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.rows.map((row) => (
            <tr key={row.strike} className={row.strike === grid.atmStrike ? "at-the-money" : ""}>
              <th scope="row">{formatStrike(row.strike)}</th>
              {row.cells.map((cell, i) => (
                <StructureCell
                  key={grid.offsets[i]}
                  cell={cell}
                  feature={feature}
                  peak={peak}
                  rowStrike={row.strike}
                  offset={grid.offsets[i]!}
                />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StructureCell({
  cell,
  feature,
  peak,
  rowStrike,
  offset,
}: {
  cell: Cell;
  feature: Feature;
  peak: number;
  rowStrike: number;
  offset: number;
}) {
  if (cell.unlisted) {
    return (
      <td
        className="num blank"
        title={`No pair listed within half a step of ${formatStrike(rowStrike)} ± ${formatStrike(offset)}`}
      />
    );
  }
  const { value } = cell;
  if (value === null) return <td className="num" />;

  /* The tint is a shading of one colour by size, not a heatmap of many: the reader is
     being told which way the number points and roughly how far, and a second hue would
     imply a scale nobody defined. `peak` is 0 only on a grid with nothing in it, and the
     guard keeps a division by zero out of a style attribute. */
  const weight = peak > 0 ? Math.abs(value) / peak : 0;
  return (
    <td
      className={`num ${value > 0 ? "gain" : value < 0 ? "loss" : ""}`}
      style={{ "--tint": weight.toFixed(3) } as React.CSSProperties}
      title={`${formatStrike(cell.callStrike!)} call / ${formatStrike(cell.putStrike!)} put`}
    >
      {format(feature, value)}
    </td>
  );
}

/**
 * One formatter per feature, all from `lib/format.ts`.
 *
 * Gamma is scaled by 10,000 and volatility by 100 inside those functions — the ladder's
 * own conventions, so a gamma read here and a gamma read there are the same number. The
 * headings say so; nothing is scaled twice and nothing is scaled here.
 */
function format(feature: Feature, value: number): string {
  switch (feature) {
    case "price":
      return formatPrice(value);
    case "iv":
      return formatIv(value);
    case "delta":
      return formatDelta(value);
    case "gamma":
      return formatGamma(value);
    default:
      return formatGreek(value);
  }
}

/** What each feature's numbers are, for the line above the grid. The units are not
 *  decoration: a vega per one percent and a theta per calendar day are this project's
 *  conventions rather than the textbook ones, and nothing else on screen says so. */
export const FEATURE_LABEL: Record<Feature, { code: string; band: string }> = {
  price: { code: "PRICE", band: `Net premium, USD per unit ${DASH} bid/ask midpoint of both legs` },
  iv: { code: "IV", band: "Implied volatility, vega-weighted across the two strikes" },
  delta: { code: "Δ", band: "Delta, summed across both legs, with respect to the forward" },
  gamma: { code: "Γ", band: "Gamma ×10⁴, summed across both legs" },
  vega: { code: "ν", band: "Vega, summed, per one volatility point" },
  theta: { code: "Θ", band: "Theta, summed, one calendar day" },
};
