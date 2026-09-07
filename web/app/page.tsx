import ChainScreen from "@/components/ChainScreen";
import { looksCanonical } from "@/lib/instrument";
import { parseView } from "@/lib/view";

function firstParam(value: string | string[] | undefined): string | null {
  return Array.isArray(value) ? (value[0] ?? null) : (value ?? null);
}

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
 *
 * **`instrument` is read separately, not folded into `parseView`.** `lib/view.ts`'s own
 * docstring fixes its scope as "which tab, underlying, expiry, minute" — the vocabulary
 * the volatility screen and this one share. A canonical instrument string is #46's alone;
 * the volatility screen has no strike to open a chart panel for, so adding a fifth field
 * neither screen but this one would read is a scope creep `view.ts` does not need. A
 * malformed or truncated value is treated as absent — `looksCanonical` rather than the
 * engine's own strict parse — so a mistyped link opens the chain page cleanly rather than
 * with a panel stuck trying to load nothing.
 */
export default async function ChainPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  const initial = parseView(params);
  const instrument = firstParam(params.instrument);
  const initialInstrument = instrument && looksCanonical(instrument) ? instrument : null;
  return <ChainScreen initial={initial} initialInstrument={initialInstrument} />;
}
