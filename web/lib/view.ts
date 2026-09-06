/**
 * The screen state that survives a paste: which tab, underlying, expiry, minute.
 *
 * One file because it is one rule, and the rule is a boundary rather than a convenience.
 * **Everything here is UTC and nothing here is local.** The scrubber's clock shows the
 * reader's wall time because that is the only clock it can usefully match, but a local
 * time in a link would mean two people opening the same URL see two different curves.
 * So the local time is a rendering, produced by `format.ts` at the moment of drawing,
 * and the identity of the view is the store's own UTC stamp, carried through unchanged.
 *
 * Both directions live together on purpose: a parser and a writer that disagree about a
 * parameter name is a link that silently opens on the wrong minute, and the only way to
 * be sure they cannot is to keep them in each other's sight.
 */
import { UNDERLYINGS, type Underlying } from "./contract";
import { isMinuteStamp } from "./timeline";

/** `DD-MM-YYYY`, exactly as `/chain`, `/smile` and Delta all spell it. */
const EXPIRY = /^\d{2}-\d{2}-\d{4}$/;

/**
 * The volatility section's two views.
 *
 * In the URL because a tab is part of what someone means when they send a link. A
 * colleague pasted the address of the smile at a minute; sending them the IV-vs-RV chart
 * instead, because the tab reset to its default, would be the same failure as opening on
 * the wrong minute.
 */
export const TABS = ["smile", "iv-rv"] as const;
export type Tab = (typeof TABS)[number];

/**
 * What a URL asked for. Every field is optional: a bare `/volatility` is a valid
 * request for the default view, and a partial one is a valid request for as much of it
 * as was named.
 */
export interface ViewRequest {
  tab: Tab | null;
  underlying: Underlying | null;
  expiry: string | null;
  /** ISO 8601 UTC, second precision. Never a local time. */
  minute: string | null;
}

export const NO_VIEW: ViewRequest = {
  tab: null,
  underlying: null,
  expiry: null,
  minute: null,
};

/**
 * A parameter that is present but malformed is treated as absent rather than as an
 * error. A pasted link with a mangled minute should open the screen on the latest curve
 * and say so; it should not render an error page, and it must not be trusted as far as
 * being looked up in the store.
 */
export function parseView(raw: Record<string, string | string[] | undefined>): ViewRequest {
  return {
    tab: parseTab(one(raw.tab)),
    underlying: parseUnderlying(one(raw.underlying)),
    expiry: parseExpiry(one(raw.expiry)),
    minute: parseMinute(one(raw.minute)),
  };
}

function one(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) return value[0] ?? null;
  return value ?? null;
}

function parseTab(value: string | null): Tab | null {
  return TABS.find((tab) => tab === value) ?? null;
}

function parseUnderlying(value: string | null): Underlying | null {
  if (value === null) return null;
  const upper = value.toUpperCase();
  return UNDERLYINGS.find((u) => u === upper) ?? null;
}

function parseExpiry(value: string | null): string | null {
  return value !== null && EXPIRY.test(value) ? value : null;
}

function parseMinute(value: string | null): string | null {
  return value !== null && isMinuteStamp(value) ? value : null;
}

/**
 * The query string for a view, including the leading `?`.
 *
 * The colons in the minute are put back after `URLSearchParams` escapes them. They are
 * legal in a query string, and the value is the store's own key — a link someone reads
 * before clicking should show the minute it opens on rather than `%3A`.
 */
export function viewQuery(
  underlying: Underlying,
  expiry: string,
  minute: string,
  tab: Tab = "smile",
): string {
  // The default tab is left out rather than spelled. `?underlying=BTC&expiry=…` is the
  // address the smile has always had, and adding `&tab=smile` to every link it writes
  // would change every existing one for no gain.
  const params = new URLSearchParams({ underlying, expiry, minute });
  if (tab !== "smile") params.set("tab", tab);
  return `?${params.toString().replace(/%3A/g, ":")}`;
}
