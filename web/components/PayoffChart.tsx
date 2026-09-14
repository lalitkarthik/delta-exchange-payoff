"use client";

import { useEffect, useRef, useState } from "react";

import { formatScaled, formatStrike } from "@/lib/format";
import type { Curve } from "@/lib/payoff";
import {
  PLOT_HEIGHT,
  PLOT_LEFT,
  PLOT_TOP,
  PLOT_WIDTH,
  VIEWBOX_HEIGHT,
  VIEWBOX_WIDTH,
  engineSpan,
  polyline,
  priceAtPointer,
  projectX,
  projectY,
  verticalSpan,
  visiblePoints,
  wheelFactor,
  zoomAbout,
  type Span,
} from "@/lib/payoffChart";
import { linearTicks } from "@/lib/scale";

/** One press of the zoom buttons. `1 / 1.6` in, `1.6` out — about three presses to
 * double, which is a readable step for a control that has no focus point to aim at. */
const BUTTON_FACTOR = 1.6;

/**
 * P&L at expiry, drawn from the corner points, with the forward and every breakeven
 * marked — and zoomable **in and out, without limit**.
 *
 * **The window is a suggestion, not a clamp.** `docs/payoff-contract.md` sends the curve
 * as its corners plus the two end slopes, which describes the line exactly at every
 * price, so there is no edge of the data to be stopped at. That is the whole reason the
 * curve travels as corners, and it is the one rule this chart does not inherit from
 * `convex-hedge-payoff/web/components/PayoffChart.tsx`, whose 400 samples across a fixed
 * ±6% leave nothing outside them to draw. Reset returns to the window the engine sent —
 * `anchor · e^(±3σ√t)`, the anchor being the fitted forward where there is one and spot
 * otherwise — never to whatever the data happens to span.
 *
 * **One line only: P&L at expiry.** It depends on the strikes and what was paid, neither
 * of which moves, so this curve sits still while everything else on the screen updates.
 * A value-today line is genuinely curved, would have to be sampled, and would forfeit
 * the corner points; `docs/payoff-contract.md` §curve declines to build it.
 *
 * All the arithmetic is `lib/payoffChart.ts`, tested without a browser. What is left
 * here is the markup and the two pieces of state a browser owns: the window, and the
 * pointer position a wheel event zooms about.
 */
export default function PayoffChart({
  curve,
  forward,
  breakevens,
  factor,
  unit,
}: {
  curve: Curve;
  /** The fitted forward, or `null` on a chain that could not be fitted — in which case
   * there is no marker rather than a marker standing on a guess. */
  forward: number | null;
  breakevens: number[];
  /** `1`, or `contract_value` when the per-contract toggle is on. Scales the P&L axis
   * and nothing else: the horizontal axis is a price of the underlying. */
  factor: number;
  unit: string;
}) {
  const [span, setSpan] = useState<Span>(() => engineSpan(curve.window));
  const frame = useRef<SVGSVGElement | null>(null);

  const points = visiblePoints(curve, span);
  const ySpan = verticalSpan(points);

  const zoomBy = (by: number) => setSpan((current) => zoomAbout(current, by, mid(current)));

  /*
   * Zoom about the price under the pointer, so the strike being read stays put.
   *
   * **Subscribed by hand rather than through `onWheel`, and that is not a style
   * choice.** React attaches wheel listeners at the root as **passive**, so a handler
   * given as a prop cannot call `preventDefault` — the page scrolls underneath while
   * the chart zooms, which makes the gesture unusable rather than merely imperfect.
   * `addEventListener` with `{ passive: false }` is the only way to take the event.
   *
   * The listener is re-registered on nothing: it reads the window through the state
   * updater's argument rather than closing over it, so one subscription for the life of
   * the chart is correct however many times the reader zooms.
   */
  useEffect(() => {
    const svg = frame.current;
    if (svg === null) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = svg.getBoundingClientRect();
      setSpan((current) =>
        zoomAbout(
          current,
          wheelFactor(event.deltaY),
          // Not the fraction across the element: the plot is inset from it by the axis
          // gutter, and reading one as the other displaces the focus by 7% of the window
          // and compounds over a gesture. `priceAtPointer` owns that mapping and is
          // hand-checked in `payoff-chart.test.ts`, which is the only place it can be.
          priceAtPointer(current, event.clientX, box.left, box.width),
        ),
      );
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  const zero = projectY(0, ySpan, PLOT_HEIGHT);
  /* Where zero sits as a fraction of the frame, clamped — unlike `zero` this is defined
     even when the whole curve is on one side of it, which is exactly when the fill has to
     be all green or all red rather than absent. */
  const zeroFraction = (ySpan.max - 0) / (ySpan.max - ySpan.min);
  const zeroAt = Number.isFinite(zeroFraction) ? Math.min(1, Math.max(0, zeroFraction)) : 0.5;
  const line = polyline(points, span, ySpan, PLOT_WIDTH, PLOT_HEIGHT);
  const area = closeToZero(line, Number((zeroAt * PLOT_HEIGHT).toFixed(2)));
  const forwardX = forward === null ? null : projectX(forward, span, PLOT_WIDTH);

  return (
    <figure className="payoff-chart">
      <figcaption className="payoff-chart-head">
        <span className="stat-label">P&amp;L at expiry — {unit}</span>
        <span className="payoff-zoom">
          <button type="button" onClick={() => zoomBy(1 / BUTTON_FACTOR)} title="Zoom in">
            +
          </button>
          <button type="button" onClick={() => zoomBy(BUTTON_FACTOR)} title="Zoom out">
            −
          </button>
          <button
            type="button"
            onClick={() => setSpan(engineSpan(curve.window))}
            title="Back to the ±3σ window the engine suggested"
          >
            Reset
          </button>
        </span>
      </figcaption>

      <svg
        className="payoff-svg"
        viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
        role="img"
        aria-label={`Profit and loss at expiry between ${formatStrike(span.min)} and ${formatStrike(span.max)}`}
        ref={frame}
      >
        {/* Profit green, loss red, both fading out away from zero. One gradient with a
            hard stop where zero falls, so the area closed back to the zero line is tinted
            by which side of it each part is on. */}
        <defs>
          <linearGradient id="payoff-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--up)" stopOpacity="0.35" />
            <stop offset={zeroAt} stopColor="var(--up)" stopOpacity="0.02" />
            <stop offset={zeroAt} stopColor="var(--down)" stopOpacity="0.02" />
            <stop offset="1" stopColor="var(--down)" stopOpacity="0.35" />
          </linearGradient>
        </defs>

        <g transform={`translate(${PLOT_LEFT},${PLOT_TOP})`}>
          {/* The P&L axis. Its labels carry the multiplier; the price axis never does. */}
          {linearTicks(ySpan.min, ySpan.max, 5).map((tick) => {
            const y = projectY(tick, ySpan, PLOT_HEIGHT);
            if (y === null) return null;
            return (
              <g key={`y${tick}`}>
                <line className="payoff-grid" x1={0} y1={y} x2={PLOT_WIDTH} y2={y} />
                <text className="payoff-tick" x={-8} y={y + 4} textAnchor="end">
                  {formatScaled(tick * factor)}
                </text>
              </g>
            );
          })}

          {linearTicks(span.min, span.max, 6).map((tick) => {
            const x = projectX(tick, span, PLOT_WIDTH);
            if (x === null) return null;
            return (
              <text
                key={`x${tick}`}
                className="payoff-tick"
                x={x}
                y={PLOT_HEIGHT + 18}
                textAnchor="middle"
              >
                {formatStrike(tick)}
              </text>
            );
          })}

          {/* Zero: the line every figure on this screen is quoted against. */}
          {zero === null ? null : (
            <line className="payoff-zero" x1={0} y1={zero} x2={PLOT_WIDTH} y2={zero} />
          )}

          {area === null ? null : <polygon fill="url(#payoff-fill)" points={area} />}

          <polyline
            className="payoff-line"
            points={line}
          />

          {breakevens.map((price) => {
            const x = projectX(price, span, PLOT_WIDTH);
            // Off-screen breakevens are not drawn at the frame edge: a marker clamped
            // there reads as a crossing that happens at the edge, which it does not.
            if (x === null) return null;
            return (
              <g key={`be${price}`}>
                <line className="payoff-breakeven" x1={x} y1={0} x2={x} y2={PLOT_HEIGHT} />
                <text className="payoff-breakeven-label" x={x} y={12} textAnchor="middle">
                  {formatStrike(price)}
                </text>
              </g>
            );
          })}

          {forwardX === null ? null : (
            <g>
              <line className="payoff-forward" x1={forwardX} y1={0} x2={forwardX} y2={PLOT_HEIGHT} />
              <text
                className="payoff-forward-label"
                x={forwardX}
                y={PLOT_HEIGHT - 4}
                textAnchor="middle"
              >
                F {formatStrike(forward!)}
              </text>
            </g>
          )}
        </g>
      </svg>
    </figure>
  );
}

/** A polyline's points closed down (or up) to the zero line, so it can be filled.
 *  `null` when there is no line to close. */
function closeToZero(line: string, zeroY: number): string | null {
  const pairs = line.split(" ").filter((pair) => pair !== "");
  if (pairs.length < 2) return null;
  const x = (pair: string) => pair.split(",")[0];
  return `${x(pairs[0]!)},${zeroY} ${line} ${x(pairs[pairs.length - 1]!)},${zeroY}`;
}

function mid(span: Span): number {
  return span.min + (span.max - span.min) / 2;
}
