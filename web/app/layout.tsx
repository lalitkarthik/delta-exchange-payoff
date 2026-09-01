import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Delta option chain",
  description: "One option chain ladder from Delta Exchange — BTC and ETH, one expiry at a time.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
