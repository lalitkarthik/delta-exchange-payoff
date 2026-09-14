"use client";

import ExposureScreen from "@/components/ExposureScreen";
import { gammaCoverage, gexByStrike } from "@/lib/exposure";
import { formatUsdCompact } from "@/lib/format";
import type { ViewRequest } from "@/lib/view";

/**
 * Gamma exposure by strike: what the board's convexity is worth per one percent, and
 * where it changes sign.
 *
 * Everything that could be got wrong is in `lib/exposure.ts` and is tested there. What is
 * left here is the naming — which is not nothing, because every sentence on this screen is
 * a claim about a convention, and each of the three below is one somebody would otherwise
 * have to reverse-engineer off an axis.
 */
export default function GexScreen({
  initial,
  initialExpiries,
}: {
  initial: ViewRequest;
  initialExpiries: string[];
}) {
  return (
    <ExposureScreen
      initial={initial}
      initialExpiries={initialExpiries}
      board="gex"
      title="Gamma exposure"
      project={gexByStrike}
      refusal={(chain) =>
        chain.spot === null
          ? "This chain carries no spot price, and gamma exposure in dollars is a multiple of spot squared. Nothing is drawn rather than an axis in units nobody could name."
          : `No lot size is published for ${chain.underlying}, so a figure in contracts cannot be turned into one in dollars. Nothing is drawn rather than a guess at 0.001.`
      }
      axisTitle="USD per 1% move"
      format={formatUsdCompact}
      coverage={(chains) => {
        // Summed across every checked expiry: the board is their total, so its coverage
        // has to be too. One expiry solving cleanly does not qualify a board that another
        // contributed half of.
        let solved = 0;
        let total = 0;
        for (const chain of chains) {
          const count = gammaCoverage(chain);
          solved += count.solved;
          total += count.total;
        }
        if (total === 0) return null;
        if (solved === total) return `all ${total} strikes carry a computed gamma`;
        return `${solved} of ${total} strikes carry a computed gamma — the rest contribute nothing`;
      }}
      note={
        <>
          <strong>The gamma is ours, not the venue&rsquo;s.</strong> Every bar is built from
          the volatility this engine solved out of the order book, priced Black-76 against
          the fitted forward; Delta publishes its own gamma on the same payload and it is
          never read here. A strike whose volatility did not solve carries no gamma at all
          and contributes nothing — the count above says how many, because a total summed
          over half a board is not a total.{" "}
          <strong>The convention is dealer-short:</strong> dealers are taken to be long
          calls and short puts, so a call-heavy strike is positive and a put-heavy one is
          negative. Calls are drawn upwards and puts downwards; both bars are the size of
          what is there, and the sign lives in the net. The figure is{" "}
          <code>Γ × OI × contract_value × spot² × 1%</code> — gamma per one unit of the
          underlying, open interest in contracts, and the lot size the venue publishes.
          Our gamma is a derivative with respect to the <em>forward</em> and the dollars
          square <em>spot</em>; within a day the two agree to well under a percent, which
          is below the resolution of a bar, but the axis is built that way and says so
          rather than pretending otherwise. <strong>The FLIP line keeps a familiar name for
          a smaller claim.</strong> It is the strike at which the running total across the
          board changes sign — a fact about this board as it stands, not the price at which
          exposure would actually turn, which would mean re-pricing every gamma at every
          spot and is not something this payload can support. The ranked strikes beside the
          chart are ordered by the size of their net gamma, not by their distance from
          spot: a large positive strike below the price is still where the gamma is.
        </>
      }
    />
  );
}
