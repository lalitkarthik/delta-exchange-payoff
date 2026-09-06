import type { Metadata } from "next";

import ScreenRail from "@/components/ScreenRail";
import "./globals.css";
import { THEME_KEY } from "@/lib/theme";

export const metadata: Metadata = {
  title: "Delta option chain",
  description: "One option chain ladder from Delta Exchange — BTC and ETH, one expiry at a time.",
};

/**
 * Runs before the first paint, and before the bundle exists.
 *
 * Without it the page paints with the system palette and then flips to the stored one
 * a frame later, which is worse than having no toggle at all - a chain of figures
 * inverting under the eye reads as the numbers changing.
 *
 * It cannot import `readTheme`, because it has to run before any module does, so it
 * repeats that rule in one line. The duplication is deliberate and small: an unknown
 * value sets nothing, which leaves the media query in charge, which is what `auto`
 * means. `try` because Safari throws on `localStorage` in private browsing, and a
 * theme is not worth a blank page.
 */
const NO_FLASH = `try{var t=localStorage.getItem(${JSON.stringify(THEME_KEY)});if(t==="dark"||t==="light")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // The script writes `data-theme` before React hydrates, so the server's markup and
    // the client's first read of `<html>` legitimately differ.
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: NO_FLASH }} />
      </head>
      <body>
        {/*
          The rail lives here rather than in a page, so it is rendered once and outlives
          every navigation. A page owning its own copy would mean the chain's rail and
          the volatility screen's rail were two components that had to be kept looking
          alike, and it would tear the chain's live subscription down and rebuild it on
          a click that landed back on the chain.
        */}
        <div className="app">
          <ScreenRail />
          {children}
        </div>
      </body>
    </html>
  );
}
