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
} from "recharts";

import { ivTickFor, offsetTickFor, strikeTick } from "@/components/SmileAxisTicks";
import { SmileTooltip } from "@/components/SmileTooltip";
import type { SmileMinute } from "@/lib/contract";
import { formatStrike } from "@/lib/format";
import {
  OVERLAY_DASH,
  OVERLAY_STROKE,
  OVERLAY_WIDTH,
  type ChartOverlay,
} from "@/lib/overlay";
import { smileChartModel, type VolScale } from "@/lib/smilemodel";
import { OVERLAY_COLUMN, toChartRows } from "@/lib/smile";

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
 * **Nothing wears Recharts' own styling.** Every tick is a custom component
 * (`SmileAxisTicks`), the tooltip is replaced outright (`SmileTooltip`), the grid and axis lines take palette tokens, and there is no
 * legend — one series needs no key, and the library's default one is grey Helvetica.
 * Where Recharts writes a colour of its own as an SVG presentation attribute — `#808080`
 * on every `Label`, `#ccc` on every `ReferenceLine` — the token is passed as a prop as
 * well as being set in the stylesheet. The class alone would win in the cascade, but it
 * would leave the grey sitting in the markup for the next person to copy.
 *
 * The curve is `--ink` rather than the amber. The amber marks the forward, which is this
 * screen's money row — the accent is spent on the reference the reader navigates by, and
 * a curve in the same colour would leave neither of them meaning anything.
 *
 * **The comparison overlays take neither of those two roles.** Two more amber things
 * would leave the forward meaning nothing, and two more warm greys would be
 * indistinguishable from the curve at a glance, so the overlays are the only two hues on
 * this screen that are not already spoken for: a cyan for `−1h` and a violet for `−24h`.
 * They are told apart from the primary and from each other by **hue and by dash
 * together**, never by hue alone — the primary is solid, `−1h` is long-dashed and `−24h`
 * is dotted, so the three remain separable where hue does not survive, which includes
 * every red-green deficiency and a printout. `OVERLAY_DASH` in `lib/overlay.ts` carries
 * the patterns and `--overlay-1h` / `--overlay-24h` in `globals.css` carry the measured
 * ratios.
 *
 * **The Recharts children below are not split into components of their own.** Recharts
 * finds its axes, its reference lines and its series by inspecting the *type* of each
 * direct child of `LineChart`; a wrapper component in between is not recognised and the
 * axis silently does not exist. So what could leave this file has — the layout
 * arithmetic to `lib/smilemodel.ts`, the ticks and the hover to their own components —
 * and the JSX tree stays whole because splitting it would break the chart rather than
 * clarify it.
 *
 * **Overlays get no dots.** The dots are what says "this is a data point you may read a
 * number off", and the number this screen is for is the one on the curve for the minute
 * the scrubber is standing on. The overlays are context for that number, so they are a
 * line and nothing else. Their values are still in the hover, where a reader who wants
 * one asks for it.
 */

export function SmileChart({
  minute,
  grid,
  scale,
  underlying,
  expiry,
  overlays = [],
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
  /**
   * The comparison curves, already resolved to a stored minute each. An overlay the
   * store could not answer for never reaches this component — the screen says so above
   * the plot instead, because a series that renders as nothing and a series that renders
   * as zero look identical on an axis and only one of them is true.
   */
  overlays?: readonly ChartOverlay[];
}) {
  const rows = useMemo(
    () => toChartRows(minute, grid, overlays),
    // `overlays` is rebuilt by the screen on every live tick; its *contents* are what
    // matter, so the memo is keyed on the identity of each overlay's minute rather than
    // on the array. Without this the whole dataset is rebuilt once a second for nothing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [minute, grid, overlays.map((o) => `${o.id}:${o.minute.minute}`).join("|")],
  );
  const forward = minute.forward;

  const model = useMemo(
    () => smileChartModel(rows, forward, scale, overlays),
    // `overlays` is read by the model but is not a dependency, because it cannot change
    // without `rows` changing: the memo above is keyed on the overlay set, so a switched
    // overlay hands this one a new array. Listing it as well would recompute the whole
    // model on every live tick, since the screen rebuilds the array each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, forward, scale],
  );

  const offsetTick = offsetTickFor(forward);
  const ivTick = ivTickFor(model.ivDecimals);

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
            content={(props) => (
              <SmileTooltip {...props} overlays={overlays} byStrike={model.byStrike} />
            )}
            filterNull={false}
            // `--line-strong` measured 1.40:1 dark against the plot - a pointer cue nobody
            // can see is a pointer cue that was deleted. `--ink-faint` is 4.61:1 / 5.08:1.
            cursor={{ stroke: "var(--ink-faint)", strokeWidth: 1 }}
            isAnimationActive={false}
            wrapperStyle={{ outline: "none" }}
          />

          {/*
            The overlays, drawn **before** the primary so the primary paints over them:
            the curve for the minute on the scrubber is the subject, and the two
            historical ones are the ground it is read against. They obey the same two
            rules the primary does — `type="linear"` and no `connectNulls` — because a
            comparison curve that smoothed or bridged where the primary breaks would be
            comparing a drawing to a measurement.
          */}
          {overlays.map((overlay) => (
            <Line
              key={overlay.id}
              xAxisId="strike"
              yAxisId="iv"
              type="linear"
              dataKey={OVERLAY_COLUMN[overlay.id]}
              name={`${overlay.label} · ${overlay.minute.minute}`}
              stroke={OVERLAY_STROKE[overlay.id]}
              strokeWidth={OVERLAY_WIDTH[overlay.id]}
              strokeDasharray={OVERLAY_DASH[overlay.id]}
              isAnimationActive={false}
              dot={false}
              activeDot={false}
            />
          ))}

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
