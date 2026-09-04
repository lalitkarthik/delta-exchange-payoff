"use client";

import { Fragment } from "react";
import type { TooltipContentProps } from "recharts";

import { formatStrike } from "@/lib/format";
import { OVERLAY_STROKE, type ChartOverlay } from "@/lib/overlay";
import {
  formatOffset,
  formatPercent,
  formatSignedPoints,
  OVERLAY_COLUMN,
  type SmileRow,
} from "@/lib/smile";

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
export function SmileTooltip({
  active,
  payload,
  label,
  overlays,
  byStrike,
}: TooltipContentProps & {
  overlays: readonly ChartOverlay[];
  byStrike: ReadonlyMap<number, SmileRow>;
}) {
  if (!active) return null;
  const fromPayload = payload?.[0]?.payload as SmileRow | undefined;
  const row = fromPayload ?? byStrike.get(Number(label));
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

        {/* One line per overlay on screen, in the overlay's own colour so the row and
            the curve are the same thing. The signed figure beside it is this minute
            minus that one — the subtraction of two numbers already on the readout,
            which is the comparison the overlay exists to make. */}
        {overlays.map((overlay) => {
          const value = row[OVERLAY_COLUMN[overlay.id]];
          return (
            <Fragment key={overlay.id}>
              <dt style={{ color: OVERLAY_STROKE[overlay.id] }}>{overlay.label}</dt>
              <dd>
                {value === null ? (
                  "—"
                ) : (
                  <>
                    {formatPercent(value, 2)}
                    {row.ivPct === null ? null : (
                      <span className="chart-tip-diff">
                        {" "}
                        {formatSignedPoints(row.ivPct - value)}
                      </span>
                    )}
                  </>
                )}
              </dd>
            </Fragment>
          );
        })}
      </dl>
    </div>
  );
}
