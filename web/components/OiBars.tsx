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

import { peakStrike, type StrikeBar } from "@/lib/exposure";
import { formatStrike } from "@/lib/format";

/**
 * Open interest by strike: a put bar and a call bar side by side, and the two walls named.
 *
 * **Both sides are drawn upwards.** The previous version mirrored puts below the axis,
 * which reads the net at a glance and hides the comparison this screen is actually for:
 * how much is stacked at a strike against how much is stacked at the next one. Grouped
 * bars put the two counts on one scale and one baseline, which is the reading the walls
 * depend on. Signed exposure is the GEX board's job and it keeps its signed axis.
 *
 * **Puts are green and calls are red, which is the opposite of this app's own tokens.**
 * `--call` is green and `--put` is red on the ladder, where the colour names a *contract
 * type*. Here it names a *market structure*: open put interest below the price is where
 * dealers defend, and open call interest above it is where they cap — support and
 * resistance, the reading every terminal that draws this board uses. The two conventions
 * are both right about different things, so the legend names the series rather than
 * leaving the colour to carry it, and the note under the chart says which is which.
 *
 * **The walls are the largest bar on each side, badged in place.** `MAX PUT OI` and
 * `MAX CALL OI` are drawn as labels on the bars themselves rather than in a side panel,
 * because a wall is a fact about one bar and pointing at it is the whole message.
 *
 * Nothing wears Recharts' own styling, per `SmileChart`: every colour is a palette token
 * passed as a prop, there is no default legend, and the tooltip is replaced outright.
 * The library writes `#808080` into SVG presentation attributes where it is not told
 * otherwise, and a class alone would win in the cascade while leaving the grey in the
 * markup for the next person to copy.
 */
export default function OiBars({
  bars,
  spot,
  atmStrike,
  format,
  axisTitle,
}: {
  bars: StrikeBar[];
  spot: number | null;
  atmStrike: number | null;
  format: (value: number | null) => string;
  axisTitle: string;
}) {
  if (bars.length === 0) return <p className="notice">No strikes on this board yet.</p>;

  const maxCall = peakStrike(bars, "call");
  const maxPut = peakStrike(bars, "put");

  return (
    <div className="plot">
      <div className="chart-legend">
        <span className="chart-key put">Put OI</span>
        <span className="chart-key call">Call OI</span>
        <span className="chart-key spot">Spot</span>
      </div>
      <ResponsiveContainer width="100%" height={560}>
        <ComposedChart data={bars} margin={{ top: 34, right: 24, bottom: 44, left: 8 }}>
          <CartesianGrid stroke="var(--line)" strokeDasharray="2 4" vertical={false} />

          {/* Categorical rather than numeric: a listed board is not evenly spaced in
              strike, and a numeric axis would leave bars of different widths claiming
              different sizes. */}
          <XAxis
            dataKey="strike"
            interval="preserveStartEnd"
            minTickGap={26}
            height={34}
            tickFormatter={(value: number) => formatStrike(value)}
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value="STRIKE"
              position="insideBottom"
              offset={-26}
            />
          </XAxis>

          <YAxis
            width={72}
            tickFormatter={(value: number) => format(value)}
            tick={{ fill: "var(--ink-faint)", fontSize: 11 }}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value={axisTitle.toUpperCase()}
              angle={-90}
              position="insideLeft"
              style={{ textAnchor: "middle" }}
            />
          </YAxis>

          {spot === null ? null : (
            <ReferenceLine
              x={nearestOf(bars, spot)}
              stroke="var(--accent)"
              strokeDasharray="5 4"
            >
              <Label
                className="chart-badge"
                value={`SPOT ${formatStrike(spot)}`}
                position="top"
                fill="var(--accent)"
              />
            </ReferenceLine>
          )}

          <Tooltip
            cursor={{ fill: "var(--row-hover)" }}
            content={<OiTooltip format={format} atmStrike={atmStrike} />}
          />

          {/* Put first, so the green sits left of the red at every strike and the pair is
              read in the same order the legend and the totals are written in. */}
          <Bar dataKey="put" fill="var(--up)" isAnimationActive={false}>
            {bars.map((bar) => (
              <Cell key={bar.strike} fillOpacity={bar.strike === maxPut ? 1 : 0.62} />
            ))}
            <LabelList dataKey="strike" content={<Wall at={maxPut} text="MAX PUT OI" tone="up" />} />
          </Bar>
          <Bar dataKey="call" fill="var(--down)" isAnimationActive={false}>
            {bars.map((bar) => (
              <Cell key={bar.strike} fillOpacity={bar.strike === maxCall ? 1 : 0.62} />
            ))}
            <LabelList
              dataKey="strike"
              content={<Wall at={maxCall} text="MAX CALL OI" tone="down" />}
            />
          </Bar>
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * The badge over the tallest bar on one side, and nothing over any other.
 *
 * Recharts calls a `LabelList`'s content once per bar, so the filter is here rather than
 * in the data: every bar is asked and only the wall answers. Drawn as a rect plus text
 * rather than a plain label because it has to stay legible over whatever is behind it.
 */
function Wall(props: {
  at?: number | null;
  text?: string;
  tone?: "up" | "down";
  x?: number;
  y?: number;
  width?: number;
  value?: number;
}) {
  const { at, text = "", tone = "up", x = 0, y = 0, width = 0, value } = props;
  if (at === null || at === undefined || value !== at) return null;
  const w = text.length * 6.2 + 12;
  const centre = x + width / 2;
  return (
    <g>
      <rect
        x={centre - w / 2}
        y={y - 20}
        width={w}
        height={15}
        rx={2}
        fill={tone === "up" ? "var(--up)" : "var(--down)"}
      />
      <text
        className="chart-badge-text"
        x={centre}
        y={y - 9}
        textAnchor="middle"
        fill="var(--bg)"
      >
        {text}
      </text>
    </g>
  );
}

/** The listed strike closest to a price — the bars stand on strikes, so a reference line
 *  drawn at an arbitrary price would float between two of them on a categorical axis. */
function nearestOf(bars: StrikeBar[], price: number): number {
  return bars.reduce((best, bar) =>
    Math.abs(bar.strike - price) < Math.abs(best.strike - price) ? bar : best,
  ).strike;
}

/** The hover, replaced outright. Both sides at their own size, which is what a reader
 *  asking what is at this strike is asking. */
function OiTooltip({
  active,
  payload,
  format,
  atmStrike,
}: {
  active?: boolean;
  payload?: { payload: StrikeBar }[];
  format: (value: number | null) => string;
  atmStrike: number | null;
}) {
  if (!active || !payload || payload.length === 0) return null;
  const bar = payload[0]!.payload;
  return (
    <div className="chart-tip">
      <div className="chart-tip-head">
        Strike {formatStrike(bar.strike)}
        {atmStrike !== null && bar.strike === atmStrike ? " · ATM" : ""}
      </div>
      <dl className="chart-tip-list">
        <dt className="key-up">Put OI</dt>
        <dd>{format(bar.put)}</dd>
        <dt className="key-down">Call OI</dt>
        <dd>{format(bar.call)}</dd>
      </dl>
    </div>
  );
}
