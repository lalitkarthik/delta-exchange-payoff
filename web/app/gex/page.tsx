import type { Metadata } from "next";

import GexScreen from "@/components/GexScreen";
import { firstParam, parseExpiries, parseView } from "@/lib/view";

export const metadata: Metadata = {
  title: "Gamma exposure",
  description: "Dealer gamma by strike for one expiry, in USD per 1% move.",
};

/**
 * The gamma exposure route.
 *
 * A server component holding the metadata and reading the link, exactly as
 * `app/volatility/page.tsx` does and for its reasons: `export const metadata` and
 * `"use client"` cannot share a file, and reading `searchParams` here rather than with
 * `useSearchParams` below means the first render already knows which board was asked for.
 *
 * A malformed parameter is dropped by `parseView` rather than raised — a mangled link
 * should open the screen, not an error page.
 */
export default async function GexPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  return (
    <GexScreen
      initial={parseView(params)}
      initialExpiries={parseExpiries(firstParam(params.expiry))}
    />
  );
}
