"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { SCREENS, isCurrentScreen } from "@/lib/screens";

/**
 * The screen rail: the terminal's left edge, listing every screen and saying which one
 * you are on.
 *
 * It renders in the root layout rather than inside a page, so it survives navigation —
 * moving between the chain and the volatility screen swaps the pane beside it and does
 * not rebuild the rail, which is also what keeps the chain's websocket from being torn
 * down and rebuilt by a click on the rail's own entry.
 *
 * Each entry is the terminal's three parts: an icon, the three-letter code, and the
 * full name in the platform's uppercase. The icon is inline SVG built from the path in
 * `lib/screens.ts`, so it inherits `currentColor` from the entry and needs no colour
 * rule of its own in any of the three states.
 *
 * `aria-current="page"` is both the accessible answer to "which one am I on" and the
 * selector the stylesheet keys the amber off, so the two cannot drift apart: there is
 * no separate `.is-active` class that could be set on one and not the other.
 *
 * A client component only because it has to read the current path. Everything it knows
 * beyond that comes from `lib/screens.ts`.
 */
export default function ScreenRail() {
  const pathname = usePathname();

  return (
    <nav className="rail" aria-label="Screens">
      {SCREENS.map((screen) => (
        <Link
          key={screen.href}
          href={screen.href}
          className="rail-item"
          // `undefined` rather than "false": the attribute has to be absent when it does
          // not apply, because the stylesheet selects on its presence.
          aria-current={isCurrentScreen(screen, pathname) ? "page" : undefined}
        >
          {/*
            Hidden from the accessibility tree on purpose: the code and the name beside
            it already say what this entry is, and a third reading of the same thing is
            noise in a screen reader. `focusable="false"` is for legacy IE-era engines
            that would otherwise put the SVG in the tab order.
          */}
          <svg
            className="rail-icon"
            viewBox="0 0 16 16"
            aria-hidden="true"
            focusable="false"
          >
            <path d={screen.icon} />
          </svg>
          <span className="rail-code">{screen.code}</span>
          <span className="rail-name">{screen.name}</span>
        </Link>
      ))}
    </nav>
  );
}
