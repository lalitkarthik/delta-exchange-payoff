"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  Label,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
  type XAxisTickContentProps,
  type YAxisTickContentProps,
} from "recharts";

import type { SmileMinute } from "@/lib/contract";
import { formatStrike } from "@/lib/format";
import {
  linearTicks,
  logDomain,
  logTicks,
  offsetTicks,
  paddedDomain,
  tickDecimals,
} from "@/lib/scale";
import {
  formatOffset,
  formatPercent,
  solvedPercents,
  toRows,
  unsolvedStrikes,
  type SmileDatum,
} from "@/lib/smile";

/**
 * The smile: implied volatility against strike, for one expiry at one minute.
 *
 * Four rendering decisions are load-bearing and none of them is a default:
 *
 * **One linear scale in strike, read twice.** The bottom axis labels it as a strike and
 * the top axis labels the same positions as an offset from the forward. Offset is a pure
 * shift — `strike - forward` — so one scale carries both, and both sets of ticks come out
 * evenly spaced. That is the whole reason the scale is linear in strike rather than in
 * log-moneyness: bending the axis would leave the offset ticks irregular, and an axis
 * whose ticks are 500 apart in one place and 380 apart in the next cannot be read off.
 *
 * **`type="linear"`, and it is not negotiable.** Recharts' `monotone` and `basis` draw a
 * spline through the points, which puts a number between two strikes in exactly the place
 * a reader would take one off. The segments here are scaffolding for the eye; the dots
 * are the data, and they are drawn.
 *
 * **`connectNulls` is left off.** An unsolved strike arrives as a point with a null
 * volatility, Recharts breaks the line there, and a dotted vertical rule marks the strike
 * so the break reads as a strike we could not solve rather than as a strike that does not
 * exist. Joining across would invent a number in the one place someone would read one.
 *
 * **Nothing wears Recharts' own styling.** Every tick is a custom component, the tooltip
 * is replaced outright, the grid and axis lines take palette tokens, and there is no
 * legend — one series needs no key, and the library's default one is grey Helvetica.
 * Where Recharts writes a colour of its own as an SVG presentation attribute — `#808080`
 * on every `Label`, `#ccc` on every `ReferenceLine` — the token is passed as a prop as
 * well as being set in the stylesheet. The class alone would win in the cascade, but it
 * would leave the grey sitting in the markup for the next person to copy.
 *
 * The curve is `--ink` rather than the amber. The amber marks the forward, which is this
 * screen's money row — the accent is spent on the reference the reader navigates by, and
 * a curve in the same colour would leave neither of them meaning anything.
 */

/** Roughly how many ticks each axis wants. Chosen for a 900-ish pixel plot. */
const STRIKE_TICK_TARGET = 8;
const OFFSET_TICK_TARGET = 8;
const IV_TICK_TARGET = 8;

/** A little air, so a point never sits on the frame. */
const X_PAD = 0.02;
const Y_PAD = 0.08;

export type VolScale = "linear" | "log";

export function SmileChart({
  minute,
  grid,
  scale,
  underlying,
  expiry,
}: {
  minute: SmileMinute;
  /**
   * Every strike this expiry listed anywhere in the response — see `strikeGrid`.
   *
   * Two things come out of drawing against the board rather than against the minute.
   * A **thin** minute breaks its line at the strikes it does not hold, instead of
   * drawing a segment straight across them; and the strike axis stops moving while the
   * scrubber does, so two adjacent minutes are comparable by eye rather than being
   * redrawn on two different scales.
   */
  grid?: readonly number[];
  scale: VolScale;
  underlying: string;
  expiry: string;
}) {
  const rows = useMemo(() => toRows(minute, grid), [minute, grid]);
  const forward = minute.forward;

  const model = useMemo(() => {
    const strikes = rows.map((row) => row.strike);
    const solved = solvedPercents(rows);

    const xDomain = paddedDomain(
      Math.min(...strikes),
      Math.max(...strikes),
      X_PAD,
    );
    const strikeTicks = linearTicks(xDomain[0], xDomain[1], STRIKE_TICK_TARGET);

    // Only meaningful when the minute has a forward. A minute whose parity regression
    // failed carries `forward: null`, and then there is no offset to label and no
    // reference to draw — the bottom axis stands alone rather than a zero being invented.
    const offsetPositions =
      forward === null ? [] : offsetTicks(xDomain[0], xDomain[1], forward, OFFSET_TICK_TARGET);

    // The log branch drops any non-positive volatility, which the solver does not
    // produce — but a log axis given a zero silently renders nothing at all, and a blank
    // chart is the worst failure mode on this screen.
    const positives = solved.filter((v) => v > 0);
    const useLog = scale === "log" && positives.length > 0;

    const yDomain: [number, number] = useLog
      ? logDomain(Math.min(...positives), Math.max(...positives), Y_PAD)
      : solved.length > 0
        ? paddedDomain(Math.min(...solved), Math.max(...solved), Y_PAD)
        : [0, 1];
    const ivTicks = useLog
      ? logTicks(yDomain[0], yDomain[1], IV_TICK_TARGET)
      : linearTicks(yDomain[0], yDomain[1], IV_TICK_TARGET);

    return {
      xDomain,
      strikeTicks,
      offsetPositions,
      yDomain,
      ivTicks,
      useLog,
      ivDecimals: tickDecimals(ivTicks),
      unsolved: unsolvedStrikes(rows),
      byStrike: new Map(rows.map((row) => [row.strike, row])),
    };
  }, [rows, forward, scale]);

  const strikeTick = (props: XAxisTickContentProps) => (
    <text
      className="chart-tick"
      x={Number(props.x)}
      y={Number(props.y)}
      dy={12}
      textAnchor="middle"
    >
      {formatStrike(Number(props.payload.value))}
    </text>
  );

  const offsetTick = (props: XAxisTickContentProps) => (
    <text
      className="chart-tick"
      x={Number(props.x)}
      y={Number(props.y)}
      dy={-5}
      textAnchor="middle"
    >
      {forward === null ? "" : formatOffset(Number(props.payload.value) - forward)}
    </text>
  );

  const ivTick = (props: YAxisTickContentProps) => (
    <text
      className="chart-tick"
      x={Number(props.x)}
      y={Number(props.y)}
      dx={-8}
      dy={4}
      textAnchor="end"
    >
      {formatPercent(Number(props.payload.value), model.ivDecimals)}
    </text>
  );

  /**
   * The whole readout, and the reason the hover exists.
   *
   * `iv_leg` is the field that earns its place here: it flips from put to call across the
   * forward because the volatility is always solved on the out-of-the-money side, and a
   * reader who does not know that reads the change in the curve's character as a break in
   * our arithmetic rather than as the convention working.
   *
   * The row is found by strike rather than taken from the payload, because Recharts drops
   * a series entry whose value is null — and the unsolved strike is precisely the point
   * whose tooltip has the most to say.
   */
  const tooltip = (props: TooltipContentProps) => {
    if (!props.active) return null;
    const fromPayload = props.payload?.[0]?.payload as SmileDatum | undefined;
    const row = fromPayload ?? model.byStrike.get(Number(props.label));
    if (!row) return null;

    return (
      <div className="chart-tip">
        <div className="chart-tip-head">{formatStrike(row.strike)}</div>
        <dl className="chart-tip-list">
          {/* Two different absences, and they are not interchangeable: the solver saw
              this strike and refused it, or this minute holds no row for it at all. */}
          <dt>IV</dt>
          <dd>
            {row.ivPct !== null
              ? formatPercent(row.ivPct, 2)
              : row.stored
                ? "not solved"
                : "not stored"}
          </dd>

          <dt>Offset</dt>
          <dd>{row.offset === null ? "no forward" : `${formatOffset(row.offset)} USD`}</dd>

          <dt>Leg</dt>
          <dd>{row.leg ?? "none"}</dd>

          {row.reason ? (
            <>
              <dt>Why</dt>
              <dd>{row.reason}</dd>
            </>
          ) : null}
        </dl>
      </div>
    );
  };

  return (
    <div className="plot">
      <ResponsiveContainer width="100%" height={440}>
        <LineChart data={rows} margin={{ top: 44, right: 26, bottom: 40, left: 4 }}>
          <CartesianGrid
            xAxisId="strike"
            yAxisId="iv"
            stroke="var(--line)"
            strokeDasharray="0"
          />

          <XAxis
            xAxisId="strike"
            type="number"
            dataKey="strike"
            orientation="bottom"
            domain={model.xDomain}
            ticks={model.strikeTicks}
            allowDataOverflow
            interval={0}
            height={34}
            tick={strikeTick}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value="STRIKE"
              position="insideBottom"
              offset={-24}
            />
          </XAxis>

          {/* The same scale, labelled as a shift. `dataKey` is still the strike: the
              positions are strikes and only the tick text subtracts the forward, which
              is what keeps the two axes exactly registered. */}
          <XAxis
            xAxisId="offset"
            type="number"
            dataKey="strike"
            orientation="top"
            domain={model.xDomain}
            ticks={model.offsetPositions}
            allowDataOverflow
            interval={0}
            height={34}
            tick={offsetTick}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value="OFFSET FROM FORWARD (USD)"
              position="insideTop"
              offset={-30}
            />
          </XAxis>

          <YAxis
            yAxisId="iv"
            type="number"
            dataKey="ivPct"
            scale={model.useLog ? "log" : "linear"}
            domain={model.yDomain}
            ticks={model.ivTicks}
            allowDataOverflow
            interval={0}
            width={68}
            tick={ivTick}
            tickLine={{ stroke: "var(--line-strong)" }}
            axisLine={{ stroke: "var(--line-strong)" }}
          >
            <Label
              className="chart-axis-title"
              fill="var(--ink-faint)"
              value={model.useLog ? "IMPLIED VOL % (LOG)" : "IMPLIED VOL %"}
              angle={-90}
              position="insideLeft"
              offset={14}
            />
          </YAxis>

          {/* Each strike the solver refused. Full height on purpose: the break claims
              nothing about where the volatility would have been. */}
          {model.unsolved.map((strike) => (
            <ReferenceLine
              key={`gap-${strike}`}
              xAxisId="strike"
              yAxisId="iv"
              x={strike}
              // Explicit rather than left to a stylesheet: Recharts puts `stroke="#ccc"`
              // on the line itself as a presentation attribute, and a grey that is not
              // in this palette must not be able to reach the screen at all.
              stroke="var(--ink-faint)"
              strokeWidth={1}
              strokeDasharray="2 5"
            />
          ))}

          {forward === null ? null : (
            <ReferenceLine
              xAxisId="strike"
              yAxisId="iv"
              x={forward}
              // `--accent`, not `--atm-line`. Measured on the rendered chart: the light
              // theme's `--atm-line` (#d99a1a) is **2.41:1** on `--surface`, under the
              // 3:1 a graphical object needs, and this is the reference the whole curve
              // is read against. `--accent` is 9.64:1 dark and 6.05:1 light, and it
              // matches the label beside it rather than being a second amber.
              stroke="var(--accent)"
              strokeWidth={1.5}
              strokeDasharray="5 4"
              label={
                <Label
                  className="chart-forward-label"
                  fill="var(--accent)"
                  value={`FWD ${formatStrike(forward)}`}
                  position="insideTopLeft"
                  offset={8}
                />
              }
            />
          )}

          {/*
            `filterNull={false}` is not a preference. Recharts' default drops any payload
            entry whose value is null, the wrapper then sees an empty payload and hides
            itself — so hovering the one strike the solver refused produced no readout at
            all. Measured on the fixture: the tooltip vanished between 79,200 and 79,400
            and came back nowhere. The unsolved strike is precisely the point whose hover
            has something to say, and `iv_reason` is the thing it says.
          */}
          <Tooltip
            content={tooltip}
            filterNull={false}
            // `--line-strong` measured 1.40:1 dark against the plot - a pointer cue nobody
            // can see is a pointer cue that was deleted. `--ink-faint` is 4.61:1 / 5.08:1.
            cursor={{ stroke: "var(--ink-faint)", strokeWidth: 1 }}
            isAnimationActive={false}
            wrapperStyle={{ outline: "none" }}
          />

          {/*
            `type="linear"` and no `connectNulls`. See the note at the head of this file:
            both are the point of the chart rather than a style preference.
          */}
          <Line
            xAxisId="strike"
            yAxisId="iv"
            type="linear"
            dataKey="ivPct"
            name={`${underlying} ${expiry}`}
            stroke="var(--ink)"
            strokeWidth={1.5}
            isAnimationActive={false}
            dot={{ r: 2.6, fill: "var(--ink)", strokeWidth: 0 }}
            activeDot={{ r: 5, fill: "var(--ink)", stroke: "var(--surface)", strokeWidth: 2 }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
