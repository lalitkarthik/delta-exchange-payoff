"use client";

/**
 * The caption under the plot: what the chart claims, and the four things it refuses to
 * claim.
 *
 * Its own file because it is the screen's longest single statement and it is prose, not
 * layout — every sentence in it is an argument made elsewhere in the code, said once
 * more in the place a reader meets the picture.
 */
export default function SmileNote() {
  return (
    <p className="note">
      One point per strike, one strike per listed contract, at the minute the scrubber
      is standing on. <strong>Nothing here is fitted, smoothed or interpolated</strong>:
      the dots are the volatilities the engine solved and the segments between them are
      straight, because a spline would put a number between two strikes in exactly the
      place a reader would take one off. A dotted vertical rule is a strike that arrived
      with no solved volatility — the line breaks there and is never drawn through it,
      and it breaks the same way at a strike this minute stored no row for at all.
      Both x-axes are one linear scale in strike, read once as a strike and once as an
      offset from the forward, which is why their ticks are evenly spaced; the strike
      axis is the whole day&rsquo;s board, so it holds still while the scrubber moves.
      Hover any point for the strike, the volatility, its offset from the forward, the
      out-of-the-money leg it was solved on, the solver&rsquo;s reason when there is no
      number, and the same strike on any overlay that is switched on.{" "}
      <strong>
        The right edge is live and redraws about once a second
      </strong>{" "}
      — the same push the chain screen&rsquo;s ladder renders, sampled down because a
      shape has to hold still to be read while a ladder does not. Everything left of
      it is a sealed minute out of the store. The overlays are the same expiry an hour
      and a day earlier, anchored to the minute the scrubber is on rather than to the
      clock, drawn from minutes already in this response &mdash; switching one on asks
      the engine for nothing. An anchor the store holds no bar for draws no line and
      says so above.
    </p>
  );
}
