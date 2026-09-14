import type { Metadata } from "next";

import AnalyseScreen from "@/components/AnalyseScreen";
import { firstParam, parseView } from "@/lib/view";

export const metadata: Metadata = {
  title: "Payoff",
  description: "Profit and loss at expiry for one strategy on one expiry.",
};

/**
 * The analyse route, opened in a **new tab** from the chain so the ladder keeps
 * streaming behind it. There is no rail entry: this screen is reached from a strategy,
 * and a strategy is built on the chain.
 *
 * A server component holding the metadata and the URL, so the screen below it can be a
 * client component without the route losing its title — the same split
 * `volatility/page.tsx` and `page.tsx` use, for the reason they give.
 *
 * **`legs` is passed through raw, undecoded**, exactly as `app/page.tsx` does and for
 * the same reason: a malformed strategy link must not be treated as absent, which is
 * `lib/legs-url.ts`'s whole reason to throw naming the part that was wrong. A server
 * component has no notice to show that failure into, so the decode happens in
 * `AnalyseScreen`, which does.
 *
 * **`minute` is read through `parseView`**, so this route and the chain agree on the
 * spelling of a stored minute down to the character. A malformed one is dropped rather
 * than raised — it becomes "live", which is the same disposition every other screen
 * takes to a mangled parameter.
 */
export default async function AnalysePage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  return (
    <AnalyseScreen legsParam={firstParam(params.legs)} minute={parseView(params).minute} />
  );
}
