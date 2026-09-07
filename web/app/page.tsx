import ChainScreen from "@/components/ChainScreen";
import { parseView } from "@/lib/view";

/**
 * The chain route.
 *
 * A server component holding the URL, so `ChainScreen` below it can be a client
 * component without losing the link — same split `volatility/page.tsx` uses, and for
 * the same reason: the screen's first render already knows which underlying, expiry and
 * minute were asked for, with no default view flashing past on the way to the linked
 * one, and no re-render every time the slider rewrites the address bar.
 *
 * `parseView` is `lib/view.ts`'s, shared with the volatility route. Its `tab` field is
 * ignored here — this page has no tabs — rather than duplicated into a chain-only
 * parser for two underlying/expiry/minute fields it would spell identically.
 */
export default async function ChainPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const initial = parseView(await searchParams);
  return <ChainScreen initial={initial} />;
}
