"use client";

import { useMemo } from "react";

import { SmileChart } from "@/components/SmileChart";
import type { SmileMinute } from "@/lib/contract";
import { formatFetchedClock, formatLocalClock, localZoneLabel } from "@/lib/format";
import type { ChartOverlay } from "@/lib/overlay";
import { solvedPercents, toRows } from "@/lib/smile";
import type { VolScale } from "@/lib/smilemodel";
import type { Timeline } from "@/lib/timeline";

/**
 * The plot, and every state the store can legitimately be in where there is nothing to
 * put in it. None of them is an error page.
 *
 * `docs/smile-contract.md` makes absence a 200 with an empty `minutes`: an underlying
 * nobody has collected yet and a day nobody has lived through are both "nothing yet".
 * So the empty cases render as an explanation in the plot's own box, at the size the
 * chart would occupy, and the screen does not change shape when data arrives.
 *
 * **A position with no stored minute is one of those states, not an accident.** It
 * renders empty. Drawing the neighbouring minute's curve there would be the same error
 * as joining the line across a gap — a shape in a place where there is no shape — and
 * it is the one thing a scrubber makes easy to do by mistake.
 */
export default function SmilePlot({
  error,
  busy,
  hasResponse,
  timeline,
  underlying,
  expiry,
  stamp,
  minute,
  grid,
  scale,
  overlays,
}: {
  error: string | null;
  busy: boolean;
  /** Whether a `/smile` response has landed at all, however empty. */
  hasResponse: boolean;
  timeline: Timeline;
  underlying: string;
  expiry: string;
  stamp: string | null;
  minute: SmileMinute | null;
  grid: readonly number[];
  scale: VolScale;
  overlays: readonly ChartOverlay[];
}) {
  const rows = useMemo(() => (minute ? toRows(minute, grid) : []), [minute, grid]);
  const solved = solvedPercents(rows);

  if (error) return null;

  if (timeline.stamps.length === 0) {
    return (
      <section className="plot-empty" aria-label="Smile plot">
        <p>
          {busy || !hasResponse
            ? "Reading the store…"
            : `No stored minutes for ${underlying} ${expiry}, and nothing on the stream yet.`}
          {hasResponse && timeline.stamps.length === 0 ? (
            <>
              <br />
              Nothing has gone wrong — the store is answering &ldquo;nothing yet&rdquo;.
            </>
          ) : null}
        </p>
      </section>
    );
  }

  if (!minute) {
    return (
      <section className="plot-empty" aria-label="Smile plot">
        <p>
          No minute stored at{" "}
          <strong>
            {stamp ? formatLocalClock(stamp) : "—"} {stamp ? localZoneLabel(stamp) : ""}
          </strong>{" "}
          for {underlying} {expiry}.
          <br />
          {stamp} — the store wrote no bar here, so nothing is drawn. The curve either
          side of it belongs to another minute.
        </p>
      </section>
    );
  }

  if (rows.length === 0) {
    return (
      <section className="plot-empty" aria-label="Smile plot">
        <p>
          {underlying} {expiry} has a stored minute at{" "}
          {formatFetchedClock(minute.minute)} UTC with no strikes on it.
        </p>
      </section>
    );
  }

  if (solved.length === 0) {
    return (
      <section className="plot-empty" aria-label="Smile plot">
        <p>
          {rows.length} strikes at {formatFetchedClock(minute.minute)} UTC, and not one of
          them solved.
          <br />
          Every point carries a reason; there is no curve to draw.
        </p>
      </section>
    );
  }

  return (
    <>
      {minute.forward === null ? (
        <p className="notice warn">
          This minute has no fitted forward, so there is no offset axis and no reference
          line. The strikes and their volatilities are unchanged.
        </p>
      ) : null}
      <SmileChart
        // Re-keyed per series and per scale, and deliberately **not** per minute. A
        // different expiry is a different curve and a different axis is a different
        // chart, so both remount; a different minute is new data on the same chart, and
        // remounting the whole SVG on every drag step is the difference between a
        // scrubber that feels like dragging and one that stutters. Nothing animates
        // out of the last minute because nothing on this chart animates at all.
        key={`${underlying}:${expiry}:${scale}`}
        minute={minute}
        grid={grid}
        scale={scale}
        underlying={underlying}
        expiry={expiry}
        overlays={overlays}
      />
    </>
  );
}
