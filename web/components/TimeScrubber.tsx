"use client";

import { useMemo } from "react";

import { formatLocalClock, formatLocalStamp, localZoneLabel, localZoneName } from "@/lib/format";
import { lastIndex, type Timeline } from "@/lib/timeline";

/**
 * The day, as something you can drag through.
 *
 * **Nothing here fetches.** `docs/smile-contract.md` sends the whole stored day for an
 * expiry in one response — `measured`, 6.8 ms for 540 minutes against 4.5 ms for one —
 * precisely so that moving through time is an array index rather than a round trip. A
 * scrubber that fetched per minute would put a network wait inside every drag, and the
 * endpoint was shaped the way it is to make that impossible to need.
 *
 * **The clock is local and says so.** BTC options trade continuously, so there is no
 * session open or close to anchor to and the only job this clock has is to match the
 * wall clock of the person reading it. The zone's short name sits beside the figure the
 * way the sibling terminal's own time control writes `IST` beside its. The UTC minute is
 * still on the header above and in the URL; see `lib/view.ts` for why those two are the
 * ones that travel.
 *
 * **Missing minutes are marked before you reach them.** The track carries a mark at
 * every run of positions the store holds nothing for, so a hole is something you see
 * coming rather than something you discover by landing in it. `measured` on the live
 * store at 2026-09-04T12:19Z, BTC 25-09-2026: 581 stored minutes across an 822-minute
 * span, 179 gap runs — 178 of them a single minute long and one of them 62. Those two
 * populations are drawn differently and `OUTAGE_MINUTES` below says why.
 *
 * The control is a native `<input type="range">` under a drawn track. That is what buys
 * the arrow keys, Home and End, touch dragging and the click-to-jump for nothing — and
 * `aria-valuetext` is what stops a screen reader reading position 431 of 822 as the
 * only thing it knows.
 */

/**
 * The length at which a hole stops being a dropped sample and becomes an outage, and
 * gets the louder mark.
 *
 * **Five minutes, because that is the store's flush interval** ([#16](https://github.com/lalitkarthik/delta-exchange-payoff/issues/16)).
 * A hole at least that long means a whole flush window produced nothing, which is a
 * different fact from a minute the sampler missed — and `measured` on the live store, the
 * two kinds are not close: BTC 25-09-2026 at 2026-09-04T12:19Z held 179 runs of which 178
 * were one minute long and one was 62.
 *
 * The split is what keeps the track honest at any width. A one-minute hole is sub-pixel
 * on a day-wide track — 0.39px at a 620px window — so it has to be drawn wider than it
 * is to be seen at all, and a comb of those drawn full height covered **119.5% of the
 * track** where the true figure was 29.4%: a day that is mostly there, rendered as a day
 * that is mostly gone. So a short hole is a tick along the floor of the groove, which
 * saturates into "perforated throughout" rather than into "nothing here", and only a run
 * long enough to be measurable is drawn at its own width, full height.
 */
const OUTAGE_MINUTES = 5;

export default function TimeScrubber({
  timeline,
  index,
  onChange,
}: {
  timeline: Timeline;
  index: number;
  onChange: (next: number) => void;
}) {
  const last = lastIndex(timeline);
  const stamp = timeline.stamps[index] ?? "";
  const stored = timeline.minutes[index] != null;

  // Only one position: the control is real but there is nowhere to go, and it says so
  // rather than pretending to be draggable. This is the committed-fixture case.
  const frozen = last <= 0;

  /**
   * Memoised on the timeline alone, so a drag re-renders the row around the marks
   * without React walking a hundred and seventy unchanged elements. The element
   * reference is identical between steps and React bails out of the subtree.
   */
  const marks = useMemo(() => {
    if (last <= 0) return null;
    return timeline.gaps.map((gap) => {
      // The thumb's centre for position `i` sits at `i / last` of the travel, so a run
      // covering positions `start .. start+length-1` spans half a position either side.
      const from = Math.max(0, (gap.start - 0.5) / last);
      const to = Math.min(1, (gap.start + gap.length - 0.5) / last);
      return (
        <span
          key={gap.start}
          className={gap.length >= OUTAGE_MINUTES ? "scrub-gap scrub-gap-long" : "scrub-gap"}
          style={{ left: `${from * 100}%`, width: `${(to - from) * 100}%` }}
        />
      );
    });
  }, [timeline.gaps, last]);

  const zone = stamp ? localZoneLabel(stamp) : "";

  return (
    <section className="scrub" aria-label="Minute">
      <div className="scrub-row">
        <button
          type="button"
          className="scrub-step"
          onClick={() => onChange(index - 1)}
          disabled={index <= 0}
          aria-label="Previous minute"
        >
          ‹
        </button>

        <div className="scrub-track">
          {/* Decoration for the pointer, and only for it: every fact these marks carry
              is also in the readout below and in `aria-valuetext`. */}
          <div className="scrub-gaps" aria-hidden="true">
            {marks}
          </div>
          <input
            type="range"
            className="scrub-range"
            min={0}
            max={Math.max(last, 0)}
            step={1}
            value={Math.max(index, 0)}
            onChange={(event) => onChange(Number(event.target.value))}
            disabled={frozen}
            aria-label="Minute"
            aria-valuetext={
              stamp
                ? `${formatLocalClock(stamp)} ${zone} — ${stored ? "stored" : "no stored minute"} — ${stamp}`
                : undefined
            }
          />
        </div>

        <button
          type="button"
          className="scrub-step"
          onClick={() => onChange(index + 1)}
          disabled={index >= last}
          aria-label="Next minute"
        >
          ›
        </button>

        {/* The clock. Local, labelled, and the `title` carries the IANA zone so the
            short name is never the only thing on offer. */}
        <span className="scrub-clock" title={`${stamp} · ${localZoneName()}`}>
          <span className="scrub-time">{stamp ? formatLocalClock(stamp) : "—"}</span>
          <span className="scrub-zone">{zone}</span>
        </span>
      </div>

      <div className="scrub-ends">
        <span>{timeline.stamps[0] ? formatLocalStamp(timeline.stamps[0]) : ""}</span>
        <span className="scrub-count">
          {index + 1} / {timeline.stamps.length}
          {/* The committed fixture is one real capture, so in fixture mode there is one
              position. The control says that rather than looking broken — and it says
              only that: a "1 stored, 0 missing" tally of a single minute is noise. */}
          {frozen ? (
            <> · one minute — nothing to scrub</>
          ) : timeline.gridded ? (
            <>
              {" · "}
              {timeline.storedCount} stored
              {timeline.emptyCount > 0 ? (
                <>
                  {" · "}
                  <span className="scrub-count-missing">{timeline.emptyCount} missing</span>
                </>
              ) : null}
            </>
          ) : (
            // Past `MAX_POSITIONS` the positions are the stored minutes alone, so there
            // are no holes to stand in and none to mark. Said out loud rather than left
            // to look like a store with no gaps in it.
            <> · stored minutes only — span too wide to mark the gaps</>
          )}
        </span>
        <span>
          {timeline.stamps[last] ? formatLocalStamp(timeline.stamps[last]) : ""}
        </span>
      </div>
    </section>
  );
}
