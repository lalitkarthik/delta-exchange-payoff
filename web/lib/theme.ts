/**
 * Which palette the interface wears, and how that choice survives a reload.
 *
 * Ported unchanged from `payoff-project/web/lib/theme.ts`, storage key included, so a
 * trader who set the theme on one of the two screens finds the other already wearing it.
 *
 * Three settings, not two. `auto` is the default and means "follow the operating
 * system" — it is a real choice a trader can return to, not the absence of one, which
 * is why it is stored like the others rather than represented by an empty slot.
 *
 * Everything here is pure. The browser objects this ultimately drives — `localStorage`
 * and `document.documentElement` — are touched only by the toggle component, so the
 * rules about what a stored value means can be tested without a DOM.
 */

export type Theme = "auto" | "light" | "dark";

/** Where the choice is kept. Named, because the anti-flash script repeats it. */
export const THEME_KEY = "convex-hedge-theme";

const THEMES: readonly Theme[] = ["auto", "light", "dark"];

/**
 * What `localStorage` handed back, read as a Theme.
 *
 * Anything unrecognised becomes `auto`. Storage is editable from the console and
 * outlives any deploy that changes what these strings mean, so a value this app did
 * not write is not evidence of what the trader chose — following the operating system
 * is the honest reading of "we do not know".
 */
export function readTheme(stored: string | null): Theme {
  return THEMES.includes(stored as Theme) ? (stored as Theme) : "auto";
}

/**
 * The setting one press of the toggle moves to: `auto` -> `light` -> `dark` -> `auto`.
 *
 * A cycle rather than a switch, because there are three settings and one control. It
 * closes over all three so a trader who has left `auto` can get back to it.
 */
export function nextTheme(current: Theme): Theme {
  return THEMES[(THEMES.indexOf(current) + 1) % THEMES.length]!;
}

/**
 * What belongs on `<html data-theme>`, or `null` to remove the attribute.
 *
 * `auto` removes it. That is the mechanism, not a shortcut: the dark palette is
 * declared under `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])`,
 * so an attribute left in place - whatever its value - would go on overriding the
 * system instead of deferring to it.
 */
export function themeAttribute(theme: Theme): string | null {
  return theme === "auto" ? null : theme;
}
