import type { Metadata } from "next";

import OiScreen from "@/components/OiScreen";
import { firstParam, parseExpiries, parseView } from "@/lib/view";

export const metadata: Metadata = {
  title: "Open interest",
  description: "Open interest by strike for one expiry, calls against puts.",
};

/** The open interest route. `app/gex/page.tsx`'s shape, for its reasons. */
export default async function OiPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const params = await searchParams;
  return (
    <OiScreen
      initial={parseView(params)}
      initialExpiries={parseExpiries(firstParam(params.expiry))}
    />
  );
}
