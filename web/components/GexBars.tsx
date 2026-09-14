"use client";

import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Label,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { levels, type Level, type StrikeBar } from "@/lib/exposure";
import { formatStrike } from "@/lib/format";

/** How many strikes each side of the panel names. Four, because that is as many levels as
 *  a reader holds at once and the fifth is never the one that matters. */
export const LEVEL_COUNT = 4;

/**
 * Net gamma exposure by strike, drawn sideways.
 *
 * **Strike runs up the left and exposure runs across**, which is the reference terminal's
 * layout and the right one for this board: a reader is looking for *levels* — where the
 * walls are relative to where the price is standing — and a level is a horizontal line on
 * a price axis, the same way it is on every chart they have ever read. The OI board keeps
 * strike along the bottom because it is being read as a distribution rather than as a set
 * of levels.
 *
 * **One bar per strike, and it is the net.** Calls and puts are not drawn separately here:
 * the dealer-short convention already collapses them into one signed number, and drawing
 * the two sides beside it would invite the net to be read as a third series rather than as
 * what the other two mean. The hover carries the split.
 *
 * **Green is positive and red is negative**, and unlike the OI board that is not a
 * relabelling of the palette: `--up` and `--down` name a sign here, which is what they
 * name everywhere else in the app.
 *
 * The flip line and the level badges are drawn from `lib/exposure.ts`, which is where the
 * ranking rule lives and where it is tested.
 */
export default function GexBars({
  bars,
  spot,
  flip,
  format,
  axisTitle,
}: {
  bars: StrikeBar[];
  spot: number | null;
  /** The strike where the running total changes sign — see `ExposureScreen`'s note on
   *  what this is and, more importantly, what it is not. */
  flip: number | null;
  format: (value: number | null) => string;
  axisTitle: string;
}) {
  if (bars.length === 0) return <p className="notice">No strikes on this board yet.</p>;

  // Ascending strike puts the low strikes at the bottom, as a price axis is always read.
  const rows = [...bars].reverse();
  const { resistances, supports } = levels(bars, LEVEL_COUNT);
  const badge = new Map<number, string>();
  for (const level of resistances) badge.set(level.strike, `R${level.rank}`);
  for (const level of supports) badge.set(level.strike, `S${level.rank}`);

  return (
    <div className="plot">
      <div className="chart-legend">
        <span className="chart-key up">+GEX</span>
        <span className="chart-key down">−GEX</span>
        <span className="chart-key spot">Spot</span>
        <span className="chart-key flip">Flip</span>
      </div>
      <ResponsiveContainer width="100%" height={Math.max(420, rows.length * 22 + 90)}>
        <ComposedChart
          layout="vertical"
          data={rows}
          margin={{ top: 16, right: 96, bottom: 44, left: 8 }}
        >
          <CartesianGrid stroke="var(--line)" strokeDasharray="2 4" horizontal={false} />

          <XAxis
            type="number"
            tickFormatter={(value: number) => format(value)}
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
            height={34}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value={axisTitle.toUpperCase()}
              position="insideBottom"
              offset={-26}
            />
          </XAxis>

          <YAxis
            type="category"
            dataKey="strike"
            width={72}
            interval={0}
            tickFormatter={(value: number) => formatStrike(value)}
            tick={{ fill: "var(--ink-faint)", fontSize: 10 }}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value="STRIKE"
              angle={-90}
              position="insideLeft"
              style={{ textAnchor: "middle" }}
            />
          </YAxis>

          {/* Zero is the line both signs are read against, so it is drawn rather than left
              to be inferred from where the bars stop. */}
          <ReferenceLine x={0} stroke="var(--line-strong)" />

          {spot === null ? null : (
            <ReferenceLine y={nearestOf(bars, spot)} stroke="var(--accent)" strokeDasharray="5 4">
              <Label
                className="chart-badge"
                value={`SPOT ${formatStrike(spot)}`}
                position="right"
                fill="var(--accent)"
              />
            </ReferenceLine>
          )}

          {flip === null ? null : (
            <ReferenceLine y={flip} stroke="var(--overlay-24h)" strokeDasharray="5 4">
              <Label
                className="chart-badge"
                value={`FLIP ${formatStrike(flip)}`}
                position="left"
                fill="var(--overlay-24h)"
              />
            </ReferenceLine>
          )}

          <Tooltip cursor={{ fill: "var(--row-hover)" }} content={<GexTooltip format={format} />} />

          <Bar dataKey="net" isAnimationActive={false}>
            {rows.map((bar) => (
              <Cell
                key={bar.strike}
                fill={bar.net >= 0 ? "var(--up)" : "var(--down)"}
                fillOpacity={badge.has(bar.strike) ? 1 : 0.55}
              />
            ))}
            <LabelList dataKey="strike" content={<LevelTag badge={badge} />} />
          </Bar>
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * The `R1`/`S2` tag at the end of a ranked bar, and nothing on any other.
 *
 * Recharts calls a `LabelList`'s content once per bar, so the filter is here: every bar is
 * asked and only a ranked one answers. The tag sits at the bar's own end — inside for a
 * positive, which grows right, and outside for a negative, which grows left — so it never
 * crosses the zero line into the other sign's half.
 */
function LevelTag(props: {
  badge?: Map<number, string>;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  value?: number;
}) {
  const { badge, x = 0, y = 0, width = 0, height = 0, value } = props;
  const text = value === undefined ? undefined : badge?.get(value);
  if (!text) return null;
  const positive = width >= 0;
  const w = 20;
  // Recharts gives a negative width for a bar drawn leftwards; `x` is still its left edge.
  const left = positive ? x + width - w - 3 : x + 3;
  return (
    <g>
      <rect
        x={left}
        y={y + height / 2 - 7}
        width={w}
        height={14}
        rx={2}
        fill="var(--bg)"
        stroke={positive ? "var(--up)" : "var(--down)"}
      />
      <text
        className="chart-badge-text"
        x={left + w / 2}
        y={y + height / 2 + 4}
        textAnchor="middle"
        fill={positive ? "var(--up)" : "var(--down)"}
      >
        {text}
      </text>
    </g>
  );
}

/** The listed strike closest to a price. A category axis places a reference line on a
 *  category, so a line asked for at an arbitrary price would not be drawn at all. */
function nearestOf(bars: StrikeBar[], price: number): number {
  return bars.reduce((best, bar) =>
    Math.abs(bar.strike - price) < Math.abs(best.strike - price) ? bar : best,
  ).strike;
}

/** The hover. The net is what the bar is; the split is what the net is made of, and it is
 *  shown because the two sides can be large and nearly cancelling, which the bar cannot
 *  say on its own. */
function GexTooltip({
  active,
  payload,
  format,
}: {
  active?: boolean;
  payload?: { payload: StrikeBar }[];
  format: (value: number | null) => string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const bar = payload[0]!.payload;
  return (
    <div className="chart-tip">
      <div className="chart-tip-head">Strike {formatStrike(bar.strike)}</div>
      <dl className="chart-tip-list">
        <dt>Net GEX</dt>
        <dd className={bar.net >= 0 ? "gain" : "loss"}>{format(bar.net)}</dd>
        <dt>Call</dt>
        <dd>{format(bar.call)}</dd>
        <dt>Put</dt>
        <dd>{format(bar.put)}</dd>
      </dl>
    </div>
  );
}

export type { Level };
