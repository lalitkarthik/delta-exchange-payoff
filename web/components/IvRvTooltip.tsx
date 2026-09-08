"use client";

import { Fragment } from "react";
import type { TooltipContentProps } from "recharts";

import { LINE_LABEL, SERIES_COLOUR, type ChartRow } from "@/components/IvRvChart";
import { ESTIMATORS, IV_KEY, type LineKey } from "@/lib/ivrv";

/**
 * The hover readout, and the reason the hover exists at all.
 *
 * **Coverage is the field that earns its place.** Every realised figure is a standard
 * deviation over a window, and a window with holes in it is computed from fewer returns
 * than its width implies — the estimate is quietly noisier and the line gives no sign.
 * The payload has carried `returns` and `coverage` per point per estimator since the
 * endpoint shipped and nothing drew them; this is where they land. A reader comparing
 * two points can now see that one rests on 14,000 returns and the other on 40.
 *
 * **The two absences are not interchangeable, and the readout says which.** A series can
 * be missing here because the window had too few observations to estimate from, or
 * because the constant-maturity index declined at this minute — no listed expiry
 * bracketed the tenor, so an index would have had to extrapolate. The first is thin
 * data; the second is a refusal. Collapsing them to one dash would hide a decision the
 * instrument made on the reader's behalf.
 */
export function IvRvTooltip({
  active,
  payload,
  visible,
}: TooltipContentProps & { visible: Set<LineKey> }) {
  if (!active) return null;
  const row = payload?.[0]?.payload as ChartRow | undefined;
  if (!row) return null;

  const at = new Date(row.at);
  const head = `${at.toISOString().slice(0, 10)} ${at.toISOString().slice(11, 16)}Z`;

  return (
    <div className="chart-tip">
      <div className="chart-tip-head">{head}</div>
      <dl className="chart-tip-list">
        {visible.has(IV_KEY) ? (
          <>
            <dt style={{ color: SERIES_COLOUR.iv }}>{LINE_LABEL.iv}</dt>
            <dd>
              {row.iv === null ? (
                <span className="chart-tip-diff">no expiry brackets this tenor</span>
              ) : (
                percent(row.iv)
              )}
            </dd>
          </>
        ) : null}

        {ESTIMATORS.filter((estimator) => visible.has(estimator)).map((estimator) => {
          const value = row[estimator];
          const returns = row.returns?.[estimator];
          const coverage = row.coverage?.[estimator];
          return (
            <Fragment key={estimator}>
              <dt style={{ color: SERIES_COLOUR[estimator] }}>
                {LINE_LABEL[estimator]}
              </dt>
              <dd>
                {typeof value !== "number" ? (
                  <span className="chart-tip-diff">too few observations</span>
                ) : (
                  <>
                    {percent(value)}
                    {typeof coverage === "number" ? (
                      <span className="chart-tip-diff">
                        {" "}
                        {(coverage * 100).toFixed(0)}% cover
                        {typeof returns === "number" ? ` · ${returns} obs` : ""}
                      </span>
                    ) : null}
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

function percent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}
