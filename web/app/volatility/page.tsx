import type { Metadata } from "next";

import VolatilityScreen from "@/components/VolatilityScreen";
import { parseView } from "@/lib/view";

export const metadata: Metadata = {
  title: "Volatility",
  description: "Implied volatility across strikes for one expiry — the smile.",
};

/**
 * The volatility route.
 *
 * A server component holding the metadata and the URL, so the screen below it can be a
 * client component without the route losing its title — `export const metadata` and
 * `"use client"` cannot live in the same file.
 *
 * **The link is read here rather than with `useSearchParams` below.** Next's own docs
 * prefer the page's `searchParams` prop when there is already a server component to read
 * it in, and it buys two things here. The screen's first render already knows which
 * underlying, expiry and minute were asked for, so there is no default view flashing
 * past on the way to the linked one and no second fetch to correct it. And the screen
 * does not re-render every time the address bar is rewritten: the scrubber replaces the
 * URL as it moves, `useSearchParams` would make each of those a render, and this hands
 * the link down once as a plain value instead.
 *
 * A malformed parameter is dropped by `parseView` rather than raised — a mangled link
 * should open the screen, not an error page.
 */
export default async function VolatilityPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const initial = parseView(await searchParams);
  return <VolatilityScreen initial={initial} />;
}
