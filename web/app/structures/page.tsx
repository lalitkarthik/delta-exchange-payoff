import type { Metadata } from "next";

import StructureScreen from "@/components/StructureScreen";
import { parseView } from "@/lib/view";

export const metadata: Metadata = {
  title: "Straddle / strangle",
  description: "Every straddle and strangle on one expiry, by strike and wing width.",
};

/**
 * The structure chain route.
 *
 * A server component holding the metadata and reading the link, exactly as
 * `app/gex/page.tsx` does and for its reasons: `export const metadata` and `"use client"`
 * cannot share a file, and reading `searchParams` here rather than with `useSearchParams`
 * below means the first render already knows which board was asked for.
 *
 * A malformed parameter is dropped by `parseView` rather than raised — a mangled link
 * should open the screen, not an error page.
 */
export default async function StructuresPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  return <StructureScreen initial={parseView(await searchParams)} />;
}
