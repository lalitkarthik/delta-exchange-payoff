/**
 * Which screens exist, and in what order the rail lists them.
 *
 * One list, so a screen cannot be added to the app and forgotten by the rail. The
 * reference terminal's rail carries seven entries — BKT, PRT, LIB, CHN, STR, VOL, OI —
 * and this list is deliberately not seeded with the five that have no route: a rail
 * entry that leads nowhere is a promise the app cannot keep. Adding one later is a line
 * in this array and a directory under `app/`, which is what "built to take more" has to
 * mean.
 *
 * Three-letter codes because that is the terminal's alphabet, and the full name is
 * carried beside the code rather than hidden in a tooltip — CHN and VOL are guessable,
 * the other five are not, and a rail that has to be hovered to be read is a rail that
 * gets read once.
 *
 * Pure data and one pure predicate. Nothing here touches the DOM or the router, so the
 * rule about what counts as the current screen can be reasoned about — and one day
 * tested — without a browser.
 */

export interface Screen {
  /** The rail's label. Three letters, uppercase, the terminal's convention. */
  readonly code: string;
  /** What the code stands for. Shown under it, not only on hover. */
  readonly name: string;
  /** The route it navigates to. */
  readonly href: string;
}

export const SCREENS: readonly Screen[] = [
  { code: "CHN", name: "Chain", href: "/" },
  { code: "VOL", name: "Volatility", href: "/volatility" },
];

/**
 * Whether `pathname` is on this screen.
 *
 * A prefix match, so a screen that later grows sub-routes (`/volatility/smile`) keeps
 * its rail entry lit without this rule changing. The root is the exception and has to
 * be exact: every path starts with `/`, so a prefix match there would light CHN on
 * every screen in the app.
 */
export function isCurrentScreen(screen: Screen, pathname: string): boolean {
  if (screen.href === "/") return pathname === "/";
  return pathname === screen.href || pathname.startsWith(`${screen.href}/`);
}
