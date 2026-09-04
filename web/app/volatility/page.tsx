import type { Metadata } from "next";

import VolatilityScreen from "@/components/VolatilityScreen";

export const metadata: Metadata = {
  title: "Volatility",
  description: "Implied volatility across strikes for one expiry — the smile.",
};

/**
 * The volatility route.
 *
 * A server component holding nothing but the metadata, so the screen below it can be a
 * client component without the route losing its title — `export const metadata` and
 * `"use client"` cannot live in the same file. Everything the screen does, including the
 * tab strip that was here while this was a shell, moved into `VolatilityScreen`.
 */
export default function VolatilityPage() {
  return <VolatilityScreen />;
}
