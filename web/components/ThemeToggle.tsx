"use client";

import { useEffect, useState } from "react";

import { THEME_KEY, type Theme, nextTheme, readTheme, themeAttribute } from "@/lib/theme";

const LABEL: Record<Theme, string> = { auto: "Auto", light: "Light", dark: "Dark" };
const GLYPH: Record<Theme, string> = { auto: "◐", light: "☀", dark: "☾" };

/**
 * The one control that changes how everything else looks.
 *
 * Three states rather than two, and `auto` is the default: a trader whose machine is
 * already dark should not have to ask twice, and one whose machine is light should
 * still be able to get here. All three are reachable by pressing repeatedly.
 *
 * The rules live in `lib/theme.ts` and are tested there. What is left here is the part
 * that only a browser can do - reading storage, and writing the attribute the
 * stylesheet keys off.
 */
export default function ThemeToggle() {
  // `auto` until the effect below reads storage. Rendering the stored value directly
  // would mean rendering something the server could not know, which is the hydration
  // mismatch the inline script in `layout.tsx` exists to avoid for the *page*; the
  // button's own label is allowed to arrive a frame late.
  const [theme, setTheme] = useState<Theme>("auto");

  useEffect(() => {
    try {
      setTheme(readTheme(localStorage.getItem(THEME_KEY)));
    } catch {
      // Private browsing. `auto` is already the right answer.
    }
  }, []);

  function choose(next: Theme) {
    setTheme(next);

    const attribute = themeAttribute(next);
    if (attribute === null) delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = attribute;

    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // The choice still applies to this tab; it just will not outlive it.
    }
  }

  return (
    <button
      type="button"
      className="theme"
      onClick={() => choose(nextTheme(theme))}
      // The glyph alone does not say what pressing does, and `auto` has no glyph that
      // could. Both names are here so the control is legible without hovering.
      aria-label={`Theme: ${LABEL[theme]}. Switch to ${LABEL[nextTheme(theme)]}`}
      title={`Theme: ${LABEL[theme]}`}
    >
      <span aria-hidden>{GLYPH[theme]}</span> {LABEL[theme]}
    </button>
  );
}
