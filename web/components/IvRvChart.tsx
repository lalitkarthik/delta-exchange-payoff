"use client";

import { useMemo } from "react";

import {
  ESTIMATOR_LABEL,
  IV_KEY,
  type Estimator,
  type LineKey,
  type VolatilitySeries,
  type VolPoint,
} from "@/lib/ivrv";

/**
 * Implied against realised volatility, drawn by hand in SVG.
 *
 * **The one behaviour it must get right is breaking across a gap.** A missing minute
 * produces no row — never-forward-fill is the project's moral as well as its rule — so a
 * line drawn straight through a hole asserts a value nobody measured, in the one place
 * the reader has no way to tell.
 *
 * **This was hand-rolled when the app had no charting library, and it no longer has to
 * be.** `recharts` arrived with the smile screen, and its `<Line>` defaults
 * `connectNulls` to **false** — checked, `recharts/lib/cartesian/Line.js` — so it breaks
 * on a gap out of the box and the original argument for hand-rolling does not survive
 * contact with it. What is left is smaller: a zero-based axis, six independently toggled
 * series, and sixty lines that are already written and already verified against a
 * rendered page. **Porting this to `recharts` for consistency with `SmileChart` is worth
 * doing and is not done here** — two charting approaches in one app is a real cost, and
 * the reason this one stayed is that it was finished before the other landed.
 *
 * **Nothing here is domain arithmetic.** The engine sends both series already scaled to
 * the window and already aligned; this file turns numbers into coordinates, which is
 * layout. The rule is that the web app does not compute what the chart *means*, not that
 * it may not work out where to put a pixel.
 */

const WIDTH = 1000;
const HEIGHT = 420;
const PAD = { top: 18, right: 18, bottom: 34, left: 56 };

const PLOT_W = WIDTH - PAD.left - PAD.right;
const PLOT_H = HEIGHT - PAD.top - PAD.bottom;

/**
 * One colour per line, from the palette in `globals.css`.
 *
 * Implied wears the accent because it is the line the other five are being compared
 * *against* — the eye should find it first. The two return estimators are deliberately
 * neighbouring hues: they will overlap almost exactly on minute bars, and two nearly
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

interface Props {
  series: VolatilitySeries;
  /** Which of the six to draw. Everything else stays out of the extent as well. */
  visible: Set<LineKey>;
}

interface Segment {
  key: LineKey;
  path: string;
}

/** A value the chart can plot: finite, and actually present. */
function plottable(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function IvRvChart({ series, visible }: Props) {
  const drawn = useMemo(() => {
    const points = series.points;
    if (points.length === 0) return null;

    const values: number[] = [];
    for (const point of points) {
      if (visible.has(IV_KEY) && plottable(point.iv)) values.push(point.iv);
      for (const key of series.estimators) {
        if (!visible.has(key)) continue;
        const value = point.rv[key];
        if (plottable(value)) values.push(value);
      }
    }
    if (values.length === 0) return null;

    const times = points.map((point) => Date.parse(point.at));
    const tMin = Math.min(...times);
    const tMax = Math.max(...times);
    const span = tMax - tMin || 1;

    // The y axis starts at zero. A volatility axis cropped to its own range makes a
    // two-point move look like a collapse, and the whole subject here is the *size* of
    // the gap between two lines — which only reads correctly against a true origin.
    const top = Math.max(...values) * 1.08 || 1;

    const x = (at: number) => PAD.left + ((at - tMin) / span) * PLOT_W;
    const y = (value: number) => PAD.top + PLOT_H - (value / top) * PLOT_H;

    const segments: Segment[] = [];
    const build = (key: LineKey, read: (point: VolPoint) => number | null | undefined) => {
      let path = "";
      let pen = false;
      points.forEach((point, index) => {
        const value = read(point);
        const at = times[index];
        if (!plottable(value) || at === undefined) {
          // The gap. Lifting the pen is the whole point: the next point starts a new
          // sub-path with `M`, so nothing is drawn across the hole.
          pen = false;
          return;
        }
        const command = pen ? "L" : "M";
        path += `${command}${x(at).toFixed(2)},${y(value).toFixed(2)}`;
        pen = true;
      });
      if (path) segments.push({ key, path });
    };

    if (visible.has(IV_KEY)) build(IV_KEY, (point) => point.iv);
    for (const key of series.estimators) {
      if (visible.has(key)) build(key, (point) => point.rv[key]);
    }

    const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => ({
      value: top * fraction,
      y: PAD.top + PLOT_H - fraction * PLOT_H,
    }));

    const timeTicks = [0, 0.5, 1].map((fraction) => ({
      at: tMin + span * fraction,
      x: PAD.left + fraction * PLOT_W,
    }));

    return { segments, ticks, timeTicks, top };
  }, [series, visible]);

  if (!drawn) {
    return (
      <p className="notice">
        Nothing to draw at this lookback. Every selected line is empty over the whole
        range — which is an honest answer rather than a failure, and usually means the
        window has not finished happening yet.
      </p>
    );
  }

  return (
    <svg
      className="ivr-chart"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      role="img"
      aria-label={`Implied and realised volatility over ${series.lookback_days} days`}
      preserveAspectRatio="none"
    >
      {drawn.ticks.map((tick) => (
        <g key={tick.y}>
          <line
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={tick.y}
            y2={tick.y}
            className="ivr-grid"
          />
          <text x={PAD.left - 8} y={tick.y + 4} className="ivr-axis ivr-axis-y">
            {(tick.value * 100).toFixed(1)}%
          </text>
        </g>
      ))}

      {drawn.timeTicks.map((tick) => (
        <text key={tick.x} x={tick.x} y={HEIGHT - 10} className="ivr-axis ivr-axis-x">
          {new Date(tick.at).toISOString().slice(0, 16).replace("T", " ")}
        </text>
      ))}

      {drawn.segments.map((segment) => (
        <path
          key={segment.key}
          d={segment.path}
          fill="none"
          stroke={COLOUR[segment.key]}
          strokeWidth={segment.key === IV_KEY ? 2.4 : 1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        >
          <title>{LINE_LABEL[segment.key]}</title>
        </path>
      ))}
    </svg>
  );
}

export { COLOUR as SERIES_COLOUR };
export type { Estimator };
