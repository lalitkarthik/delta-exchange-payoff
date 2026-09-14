"use client";

import ExposureScreen from "@/components/ExposureScreen";
import { oiByStrike } from "@/lib/exposure";
import { formatOi } from "@/lib/format";
import type { ViewRequest } from "@/lib/view";

/**
 * Open interest by strike, calls against puts.
 *
 * **This board can never be refused.** Open interest is a count the venue publishes
 * outright — no volatility to solve, no spot to square, no lot size to apply — so unlike
 * GEX beside it there is no state where the figures cannot be produced. The `refusal`
 * prop is still required by the shell and its sentence is written anyway, for the day a
 * chain arrives with no rows at all.
 *
 * **The running total is off.** On the GEX screen the cumulative line is the thing the
 * sign change is read off; here it would just be a number that grows, and a line that
 * only ever goes up says nothing a reader could use.
 */
export default function OiScreen({
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
      board="oi"
      title="Open interest"
      project={oiByStrike}
      refusal={() => "This chain lists no strikes."}
      axisTitle="contracts"
      format={formatOi}
      note={
        <>
          Open interest in <strong>contracts</strong>, which is what the venue publishes
          and what the word means; one contract is {"​"}
          <code>contract_value</code> of the underlying — 0.001 BTC, 0.01 ETH. Calls are
          drawn upwards and puts downwards, both at the size of what is there. A strike
          with open interest of exactly <code>0</code> is a listed strike nobody holds,
          which is a measured fact and is drawn as zero; a strike with no figure at all is
          drawn as nothing, and the two are not the same statement.{" "}
          <strong>The USD notional is deliberately absent.</strong> Delta&rsquo;s ticker
          channel does not carry one: the field this app has long called{" "}
          <code>oi_value_usd</code> was measured against the venue&rsquo;s own REST snapshot
          and found to be a six-hour change, which goes negative and so cannot be a
          notional. Nothing on this screen reads it, and a notional derived here from
          contracts and spot would be a calculation wearing an observation&rsquo;s clothes.
        </>
      }
    />
  );
}
