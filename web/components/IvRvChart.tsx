"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  Label,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { IvRvTooltip } from "@/components/IvRvTooltip";
import {
  ESTIMATOR_LABEL,
  ESTIMATORS,
  IV_KEY,
  type Estimator,
  type LineKey,
  type VolatilitySeries,
} from "@/lib/ivrv";

/**
 * Implied against realised volatility, on the smile screen's own chart.
 *
 * **This was hand-rolled and no longer is.** The original file drew SVG paths directly,
 * for one reason: a missing minute produces no row — never-forward-fill is the project's
 * moral as well as its rule — and a line drawn straight through a hole asserts a value
 * nobody measured, in the one place a reader has no way to check. That argument does not
 * survive contact with `recharts`, which defaults `connectNulls` to **false**
 * (`recharts/lib/cartesian/Line.js`) and so breaks on a gap out of the box. What is left
 * is the cost of two charting approaches in one app, and a hover the hand-rolled one
 * never had.
 *
 * **The tick, the grid, the axis and the tooltip are `SmileChart`'s**, down to the CSS
 * class names. Two charts on one screen that style their axes differently read as two
 * screens, and the tabs are meant to be two views of one subject.
 *
 * **Implied is drawn with dots and realised without.** Not decoration: the implied side
 * cannot be backfilled — Delta's history carries no IV and no bid/ask — so it exists
 * only for the minutes this engine was running, which today is dozens against thousands.
 * A line style that suits a dense series renders a sparse one as almost nothing, and the
 * reader concludes the chart is broken rather than that the data is thin. Dots say
 * "these are the minutes there are".
 *
 * **Nothing here is domain arithmetic.** The engine sends both series already scaled to
 * the window and already aligned; this file turns numbers into coordinates.
 */

/**
 * One colour per line, from the palette in `globals.css`.
 *
 * Implied wears the accent because it is the line the other five are being compared
 * *against* — the eye should find it first. The two return estimators are deliberately
 * neighbouring hues: they overlap almost exactly on minute bars, and two nearly
 * identical colours make that overlap read as agreement rather than as one missing line.
 */
const COLOUR: Record<LineKey, string> = {
  iv: "var(--accent)",
  simple: "var(--series-a)",
  log: "var(--series-b)",
  parkinson: "var(--series-c)",
  garman_klass: "var(--series-d)",
  rogers_satchell: "var(--series-e)",
};

export const LINE_LABEL: Record<LineKey, string> = {
  iv: "Implied (ATM)",
  ...ESTIMATOR_LABEL,
};

/** One timestamp, flattened so Recharts can address each line by a `dataKey`. */
export interface ChartRow {
  at: number;
  label: string;
  iv: number | null;
  ivTenorDays: number | null;
  coverage: Partial<Record<Estimator, number>>;
  returns: Partial<Record<Estimator, number>>;
  [key: string]: unknown;
}

interface Props {
  series: VolatilitySeries;
  /** Which of the six to draw. Everything else stays out of the extent as well. */
  visible: Set<LineKey>;
}

/** A value the chart can plot: finite, and actually present. */
function plottable(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * A timestamp as the axis prints it.
 *
 * Date and time, because a lookback of weeks spans days and a chart labelled by
 * clock alone would repeat every label. In UTC, like every other instant in this app —
 * the store's minutes, the scrubber and the expiry stamps are all UTC, and one local
 * axis among them would silently shift the comparison by the reader's offset.
 */
function stamp(at: number): string {
  const date = new Date(at);
  const day = date.toISOString().slice(5, 10).replace("-", "/");
  const time = date.toISOString().slice(11, 16);
  return `${day} ${time}`;
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function IvRvChart({ series, visible }: Props) {
  const rows = useMemo<ChartRow[]>(
    () =>
      series.points.map((point) => {
        const row: ChartRow = {
          at: new Date(point.at).getTime(),
          label: point.at,
          iv: plottable(point.iv) ? point.iv : null,
          ivTenorDays: plottable(point.iv_tenor_days) ? point.iv_tenor_days : null,
          coverage: point.coverage as Partial<Record<Estimator, number>>,
          returns: point.returns as Partial<Record<Estimator, number>>,
        };
        for (const estimator of ESTIMATORS) {
          const value = point.rv[estimator];
          row[estimator] = plottable(value) ? value : null;
        }
        return row;
      }),
    [series],
  );

  // The extent covers only what is drawn, so hiding a series that ran an order of
  // magnitude above the rest reclaims the axis rather than leaving it stretched around
  // a line nobody can see. Zero-based, because these are standard deviations and an
  // axis that starts at the smallest value makes a 2% spread look like a collapse.
  const top = useMemo(() => {
    let highest = 0;
    for (const row of rows) {
      for (const key of visible) {
        const value = row[key];
        if (typeof value === "number" && value > highest) highest = value;
      }
    }
    return highest > 0 ? highest * 1.08 : 1;
  }, [rows, visible]);

  if (rows.length === 0) return null;

  return (
    <div className="plot">
      <ResponsiveContainer width="100%" height={440}>
        <LineChart data={rows} margin={{ top: 26, right: 26, bottom: 40, left: 4 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="0" />

          <XAxis
            type="number"
            dataKey="at"
            domain={["dataMin", "dataMax"]}
            scale="time"
            height={34}
            tickFormatter={stamp}
            minTickGap={56}
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value="TIME (UTC)"
              position="insideBottom"
              offset={-24}
            />
          </XAxis>

          <YAxis
            type="number"
            domain={[0, top]}
            width={68}
            tickFormatter={formatPercent}
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              // Not "over N days": only the realised side spans N. The implied side
              // spans its own expiry, which the hover reports per point.
              value="VOLATILITY OVER ITS HORIZON"
              angle={-90}
              position="insideLeft"
              offset={14}
            />
          </YAxis>

          <Tooltip
            content={(props) => <IvRvTooltip {...props} visible={visible} />}
            filterNull={false}
            // `--line-strong` measured 1.40:1 dark against the plot — a pointer cue
            // nobody can see is a pointer cue that was deleted. `--ink-faint` is 4.61:1.
            cursor={{ stroke: "var(--ink-faint)", strokeWidth: 1 }}
            isAnimationActive={false}
            wrapperStyle={{ outline: "none" }}
          />

          {/* Realised first, so implied paints over it: the accent line is the subject
              and the five estimators are the ground it is read against. */}
          {ESTIMATORS.filter((estimator) => visible.has(estimator)).map((estimator) => (
            <Line
              key={estimator}
              type="linear"
              dataKey={estimator}
              name={LINE_LABEL[estimator]}
              stroke={COLOUR[estimator]}
              strokeWidth={1.5}
              isAnimationActive={false}
              dot={false}
              activeDot={{ r: 4, strokeWidth: 0 }}
            />
          ))}

          {visible.has(IV_KEY) ? (
            <Line
              type="linear"
              dataKey="iv"
              name={LINE_LABEL.iv}
              stroke={COLOUR.iv}
              strokeWidth={2}
              isAnimationActive={false}
              // Dots, because this series is sparse by construction and a bare line
              // through a handful of points renders as nothing at all. See the note at
              // the head of this file.
              dot={{ r: 3, fill: COLOUR.iv, strokeWidth: 0 }}
              activeDot={{
                r: 5,
                fill: COLOUR.iv,
                stroke: "var(--surface)",
                strokeWidth: 2,
              }}
            />
          ) : null}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export { COLOUR as SERIES_COLOUR };
export type { Estimator };
