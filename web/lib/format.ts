/**
 * Display formatting. The only place in the app where a number is turned into text.
 *
 * Three rules live here, and each of them is a correctness rule wearing a formatting hat:
 *
 *   1. **A `null` field inside a quote renders as nothing at all** — an empty cell.
 *      Never `0`, never `0.00`, and no longer a dash: a dash sits in the column where
 *      prices sit and at a glance reads as one. This follows the sibling chain, which
 *      renders `""` for a null delta. A *whole missing side* is a different statement
 *      and gets a different treatment — `td.blank`, hatched, in `ChainLadder.tsx`.
 *   2. **A zero is a zero.** Open interest of exactly `0` is a measured fact on this
 *      venue and prints as `0`. Empty and zero mean opposite things and must not look
 *      alike.
 *   3. IV arrives as a decimal fraction and is shown as a percentage. `0.3730` is
 *      `37.30%`. The engine never multiplies by 100; this is the web app's job.
 *
 * Nothing here parses a string into a number. Every input is already `number | null`.
 */

/**
 * What an absent value looks like: nothing. Exported so the rule is nameable and so a
 * tooltip, which has no cell to leave empty, can say `formatIv(x) || DASH` on purpose.
 */
export const EMPTY = "";

/** U+2014. Only for prose and tooltips, never for a cell in the ladder. */
export const DASH = "—";

/** Prices, in USD. Grouped thousands, two decimals — a real zero shows as `0.00`. */
export function formatPrice(value: number | null): string {
  if (value === null) return EMPTY;
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

/** Spot, same shape as a strike but with cents when the venue gives them.
 *  `null` on the historical route when `spot-bars` has no row for the minute — #47 —
 *  and renders as `DASH`, matching `ChainScreen.tsx`'s own `chain ? ... : "—"` fallback
 *  for when there is no chain at all. */
export function formatSpot(value: number | null): string {
  if (value === null) return DASH;
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
  if (value === null) return EMPTY;
  const pct = value * 100;
  if (pct !== 0 && Math.abs(pct) < 0.01) return `${pct.toPrecision(1)}%`;
  return `${pct.toFixed(2)}%`;
}

/** Delta, signed, three places. Puts are negative and shown that way. */
export function formatDelta(value: number | null): string {
  if (value === null) return EMPTY;
  return value.toFixed(3);
}

/**
 * Gamma, which is several orders of magnitude smaller than every other Greek.
 *
 * On a BTC chain it runs around `0.000086`. Three decimals would print that as `0.000`
 * for the whole ladder — a column of zeros claiming there is no convexity anywhere,
 * which is the same lie `formatIv` refuses to tell about a floored volatility. So it is
 * scaled by 10,000 and the header says so: `0.86` on a header reading `Γ ×10⁴`.
 *
 * Scaling in the formatter rather than in the engine keeps the contract's number the
 * real one. This is presentation, exactly like `formatIv`'s `* 100`.
 */
export function formatGamma(value: number | null): string {
  if (value === null) return EMPTY;
  return (value * 10_000).toFixed(2);
}

/**
 * Vega, theta and rho, which share a scale and a convention.
 *
 * All three arrive already scaled by the engine — vega and rho per one percent, theta
 * as one calendar day — so this only has to choose a precision. Two decimals: they run
 * from single digits to a few hundred USD and a third would be noise.
 */
export function formatGreek(value: number | null): string {
  if (value === null) return EMPTY;
  return value.toFixed(2);
}

/**
 * Open interest, in contracts.
 *
 * This is the one column where a zero is routine and genuine — a listed strike that
 * nobody holds. It renders as `0`, not as an empty cell, because zero open interest is
 * a measured fact rather than missing data. The contrast is the point.
 */
export function formatOi(value: number | null): string {
  if (value === null) return EMPTY;
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

/** The clock part alone, for the header, where the date is already elsewhere. */
export function formatFetchedClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${PAD(d.getUTCHours())}:${PAD(d.getUTCMinutes())}:${PAD(d.getUTCSeconds())}`;
}

/*
 * Local time, and why it is allowed here when the two functions above insist on UTC.
 *
 * They stamp a **fact**: when the data was taken. A fact rendered in the reader's zone
 * makes two people reading one screenshot disagree about it, so it stays in UTC.
 *
 * The scrubber is a **control**. Its clock has one job, which is to match the wall clock
 * of the person dragging it — this market runs continuously, so there is no session open
 * or close to anchor to, and a reader asking "what did the skew look like an hour ago"
 * is asking in their own time. So the control below the plot reads local and is labelled
 * with the zone, exactly as the sibling terminal's own time control labels its `IST`;
 * the header keeps the UTC minute beside it, and the URL carries UTC and only UTC.
 *
 * **None of this ever reaches the store, a request or a URL.** It is produced at the
 * moment of drawing from the UTC stamp and thrown away. See `lib/view.ts`.
 */

/** `17:20` in the reader's zone. Twenty-four hour, because every other clock here is. */
export function formatLocalClock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(d);
}

/** `04 Sep, 17:20` — the clock with enough date to place it, for the ends of the track. */
export function formatLocalStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(d);
}

/**
 * The zone's short name **at that instant** — `IST`, `BST`, `GMT+5:30`.
 *
 * Taken at the displayed minute rather than at "now" on purpose: a reader in London
 * scrubbing back across the last Sunday in October would otherwise see every minute
 * labelled with the zone they happen to be in today, and an hour of the day would be
 * labelled wrongly. Falls back to the IANA name if the runtime offers no short form.
 */
export function localZoneLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return localZoneName();
  const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d);
  return parts.find((part) => part.type === "timeZoneName")?.value ?? localZoneName();
}

/** The IANA zone the browser is in — `Asia/Calcutta`. For the control's `title`. */
export function localZoneName(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/**
 *
 * **Separate from `formatGreek` rather than replacing it**, because the two see numbers
 * of different sizes. The ladder's Greeks are always per one unit of the underlying and
 * run from hundredths to hundreds, which two decimals renders exactly. The analyse
 * screen's may be per *contract*, and `contract_value` is 0.001 — a delta of 0.5231
 * becomes 0.0005231, which two decimals prints as `0.00` for the whole column. That is
 * the same lie `formatIv` refuses to tell about a floored volatility and `formatGamma`
 * refuses to tell about convexity, so a value that is nonzero but would round away keeps
 * enough precision to stay visibly nonzero.
 *
 * A real zero still prints `0.00`, and `null` is still nothing at all.
 */
export function formatScaled(value: number | null): string {
  if (value === null) return EMPTY;
  if (value !== 0 && Math.abs(value) < 0.005) return value.toPrecision(3);
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * A bound that may not exist: `null` is **unlimited**, and it is a word.
 *
 * Never an infinity, never a large sentinel and never a blank — all three read as a
 * value, and the last reads as missing data. `null` is also not `0`: a `max_loss` of
 * `0` is a strategy that cannot lose, and a `max_loss` of `null` is one that can lose
 * everything, so the two must not look alike.
 */
export function formatBound(value: number | null): string {
  if (value === null) return "unlimited";
  return formatScaled(value);
}

/**
 * Reward against risk, or `n/a` when one side of it is unbounded.
 *
 * Dimensionless — a P&L over a P&L — so the per-contract multiplier cancels and this is
 * the one figure on the screen the toggle does not move. A ratio against an unlimited
 * gain has no meaning, and a large number in its place would read as a good trade.
 */
export function formatRatio(value: number | null): string {
  if (value === null) return "n/a";
  return value.toFixed(2);
}

/**
 * The per-contract toggle, as one rule rather than as a multiplication scattered over
 * six components.
 *
 * **Every number `/analyse` returns is per one unit of the underlying**, and
 * `contract_value` — 0.001 for BTC, 0.01 for ETH — is a lot size the screen applies at
 * the very end. `docs/settlement.md` is explicit that the multiplier never enters a
 * pricing calculation, and the engine echoes it without ever applying it.
 *
 * The factor comes from the response and never from a constant here: two underlyings
 * with lot sizes a factor of ten apart are on this venue at the same time, and a
 * hard-coded 0.001 would be silently wrong on one of them.
 */
export function unitFactor(perContract: boolean, contractValue: number): number {
  return perContract ? contractValue : 1;
}

/** What that factor makes a column mean. Belongs in the column header: with the toggle
 * on, our Greeks read 1,000x smaller than Delta's own in the ladder beside them, and an
 * untagged column would give a reader no way to notice. */
export function unitLabel(perContract: boolean): string {
  return perContract ? "USD/contract" : "USD";
}

/**
 * A dollar figure at the scale gamma exposure lives on — thousands to hundreds of
 * millions — abbreviated so an axis tick and a bar label stay readable.
 *
 * **The sign is kept, and it is the whole point of the GEX screen**: a minus here is a
 * put-heavy strike, not a formatting accident, so it is printed rather than wrapped in
 * parentheses the way an accountant would. Three significant figures under a thousand
 * and one decimal above it: `$1.2M` is what a reader compares strikes with, and the
 * digits after that are noise at the resolution of a bar.
 *
 * `null` is nothing at all, as everywhere. A real `0` prints `$0`.
 */

export function formatUsdCompact(value: number | null): string {
  if (value === null) return EMPTY;
  const sign = value < 0 ? "-" : "";
  const size = Math.abs(value);
  if (size >= 1e9) return `${sign}$${(size / 1e9).toFixed(1)}B`;
  if (size >= 1e6) return `${sign}$${(size / 1e6).toFixed(1)}M`;
  if (size >= 1e3) return `${sign}$${(size / 1e3).toFixed(1)}K`;
  return `${sign}$${size.toFixed(0)}`;
}
