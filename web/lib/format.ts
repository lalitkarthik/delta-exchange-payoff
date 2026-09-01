/**
 * Display formatting. The only place in the app where a number is turned into text.
 *
 * Two rules from `docs/chain-contract.md` live here:
 *
 *   1. `null` renders as an em dash, never as `0` or `0.00`. A null bid means nobody
 *      is bidding; `0.00` would claim somebody bid zero. Around 40% of listed strikes
 *      are illiquid, so this is on screen constantly and has to be unmistakable.
 *   2. IV arrives as a decimal fraction and is shown as a percentage. `0.3730` is
 *      `37.30%`. The engine never multiplies by 100; this is the web app's job.
 *
 * Nothing here parses a string into a number. Every input is already `number | null`.
 */

/** U+2014. The single marker for "no data", used for every absent value. */
export const DASH = "—";

/** Prices, in USD. Grouped thousands, two decimals — a real zero shows as `0.00`. */
export function formatPrice(value: number | null): string {
  if (value === null) return DASH;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** Strikes are whole numbers in practice; grouped, no decimals unless there are some. */
export function formatStrike(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  });
}

/** Spot, same shape as a strike but with cents when the venue gives them. */
export function formatSpot(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * Implied vol: decimal fraction in, percentage out. `0.3730` -> `37.30%`.
 * The `* 100` here is presentation, not computation — it is the one place it happens.
 *
 * Two decimals, except for a value that is nonzero but would round to `0.00%`. The
 * venue reports a floored `bid_iv` of `0.000005` on deep in-the-money calls; at two
 * decimals that prints as `0.00%`, which claims an implied vol of exactly zero. That
 * is the same lie as printing a null as `0`, so such a value keeps enough precision
 * to stay visibly nonzero — `0.000005` renders as `0.0005%`.
 */
export function formatIv(value: number | null): string {
  if (value === null) return DASH;
  const pct = value * 100;
  if (pct !== 0 && Math.abs(pct) < 0.01) return `${pct.toPrecision(1)}%`;
  return `${pct.toFixed(2)}%`;
}

/** Delta, signed, three places. Puts are negative and shown that way. */
export function formatDelta(value: number | null): string {
  if (value === null) return DASH;
  return value.toFixed(3);
}

/**
 * Open interest, in contracts.
 *
 * This is the one column where a zero is routine and genuine — a listed strike that
 * nobody holds. It renders as `0`, not as a dash, because zero open interest is a
 * measured fact rather than missing data. The contrast with the dash is the point.
 */
export function formatOi(value: number | null): string {
  if (value === null) return DASH;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  });
}

const PAD = (n: number) => String(n).padStart(2, "0");

/**
 * `fetched_at` is ISO 8601 UTC. Rendered in UTC on purpose: the viewer's local
 * timezone would make two people reading the same screenshot disagree about when
 * the data was taken.
 */
export function formatFetchedAt(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return (
    `${d.getUTCFullYear()}-${PAD(d.getUTCMonth() + 1)}-${PAD(d.getUTCDate())} ` +
    `${PAD(d.getUTCHours())}:${PAD(d.getUTCMinutes())}:${PAD(d.getUTCSeconds())} UTC`
  );
}
