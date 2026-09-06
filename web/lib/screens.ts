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
 * **The names are the platform's own labels, spelled the platform's way.** The terminal
 * calls the first screen OPTION CHAIN, so this list does too: the same screen named two
 * different things in two places is a thing a reader has to reconcile every time.
 *
 * Pure data and one pure predicate. Nothing here touches the DOM or the router, so the
 * rule about what counts as the current screen can be reasoned about — and one day
 * tested — without a browser. The icon is part of that: it is stored as SVG **path
 * geometry**, a string, not as a component, which keeps this file free of JSX and keeps
 * everything the rail needs about a screen reachable from this one array.
 */

export interface Screen {
  /** The rail's label. Three letters, uppercase, the terminal's convention. */
  readonly code: string;
  /** What the code stands for, in the platform's own wording. Shown under the code. */
  readonly name: string;
  /** The route it navigates to. */
  readonly href: string;
  /**
   * The `d` of a single `<path>`, drawn in a 16x16 box, stroked rather than filled.
   *
   * Hand-drawn rather than pulled from an icon set. Two screens need two icons, and the
   * app has one runtime dependency that earns its place; a second one to draw two
   * shapes would not. The real argument is `currentColor`: an inline stroke follows
   * whatever colour the entry is wearing — soft grey, hover ink, or the amber of the
   * current screen — with no second styling path to keep in step with the first.
   */
  readonly icon: string;
}

export const SCREENS: readonly Screen[] = [
  {
    code: "CHN",
    name: "OPTION CHAIN",
    href: "/",
    // A stack of plates seen edge-on: the chain is one ladder per expiry, stacked.
    icon: "M8 1.75 L14.25 5 L8 8.25 L1.75 5 Z M1.75 8 L8 11.25 L14.25 8 M1.75 11 L8 14.25 L14.25 11",
  },
  {
    code: "VOL",
    name: "VOLATILITY",
    href: "/volatility",
    // A pulse: a flat line that moves and settles, which is what the screen shows.
    icon: "M1.5 9 L4.25 9 L6.25 3.75 L9.5 12.25 L11.5 9 L14.5 9",
  },
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
