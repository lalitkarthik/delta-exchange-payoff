"use client";

import type { XAxisTickContentProps, YAxisTickContentProps } from "recharts";

import { formatStrike } from "@/lib/format";
import { formatOffset, formatPercent } from "@/lib/smile";

/**
 * The three tick labels on the smile chart, drawn by hand.
 *
 * Recharts' own tick is grey Helvetica with a fill written straight into the markup, so
 * none of the three is a default: each is a `<text>` carrying `chart-tick` and the
 * palette follows from the stylesheet. The offsets (`dy`, `dx`, the anchor) are what
 * seats each label against its own axis and are the only difference between them.
 *
 * Two of the three need a fact from outside the tick — the forward to subtract, the
 * decimals the axis settled on — so they are factories rather than components. That
 * keeps the argument next to the label it changes instead of threading it through props
 * Recharts would have to carry.
 */

/** The bottom axis: the strike itself. */
export function strikeTick(props: XAxisTickContentProps) {
  return (
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
}

/** The top axis: the same position, labelled as a shift from the forward. */
export function offsetTickFor(forward: number | null) {
  return (props: XAxisTickContentProps) => (
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
}

/** The volatility axis, at whatever precision its own tick spacing needs. */
export function ivTickFor(decimals: number) {
  return (props: YAxisTickContentProps) => (
    <text
      className="chart-tick"
      x={Number(props.x)}
      y={Number(props.y)}
      dx={-8}
      dy={4}
      textAnchor="end"
    >
      {formatPercent(Number(props.payload.value), decimals)}
    </text>
  );
}
