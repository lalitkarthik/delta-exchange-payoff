/**
 * Committed fixture, used when the engine is unreachable or when
 * `NEXT_PUBLIC_USE_FIXTURE=1` forces it. See `web/README.md`.
 *
 * The chain in `fixture.chain.json` is shaped exactly to `docs/chain-contract.md`, and
 * shaped after PRODUCTION: across 582 live BTC and 316 live ETH tickers, every strike
 * carried a bid, an ask and all three IVs. So almost every leg here is fully quoted.
 * The nulls are a deliberate handful of contract edge cases, not the house style —
 * a ladder that were mostly dashes would give the next reader a false picture of the
 * venue, while a ladder with no dashes at all would leave the code path untested.
 *
 *   - 20 strikes, 70000 to 86000, spot 77543, ATM 77500. 39 legs, fully quoted but for:
 *   - `P-86000` is `null` — a whole side absent from the listing. The row still renders,
 *     with its strike, and five hatched cells. This is the case that breaks naive table code.
 *   - `C-86000` and `P-70000` have a `null` bid and so a `null` bid_iv: the extreme wings.
 *   - `P-84000` has a `null` mark_iv and therefore `null` Greeks. Production did not show
 *     this, but the contract permits it and the table has to survive it.
 *   - `C-86000` and `P-70000` have `oi` of exactly **0** — not null. Zero open interest is
 *     a measured fact (18 of 316 live ETH tickers), and renders as `0`, never as blank.
 *     This is the pair that proves empty and zero are not the same thing on screen.
 *   - `C-70000` and `C-71000` carry `bid_iv: 0.000005`, the floor the venue reports on
 *     deep in-the-money calls. It is a real value, so it renders — as `0.0005%` in the
 *     IV tooltip. Not a null, and not a bug.
 *
 * The `satisfies` below is the point of this file: if the JSON ever drifts from the
 * contract types, `tsc --noEmit` fails here rather than the page failing in a browser.
 */
import type { ChainResponse, ExpiriesResponse, Underlying } from "./contract";
import chain from "./fixture.chain.json";

/**
 * TypeScript widens a string literal in an imported JSON module to `string`, so
 * `underlying` cannot satisfy the `"BTC" | "ETH"` union on its own. It is the only
 * field with that problem, so it is narrowed by hand and everything else — every row,
 * every nullable leg field — is still checked against the contract by the `satisfies`.
 */
type RawChain = Omit<ChainResponse, "underlying"> & { underlying: string };

function narrowUnderlying(value: string): Underlying {
  if (value === "BTC" || value === "ETH") return value;
  throw new Error(`fixture.chain.json has underlying "${value}", which is not BTC or ETH.`);
}

const raw = chain satisfies RawChain;

export const FIXTURE_CHAIN: ChainResponse = {
  ...raw,
  underlying: narrowUnderlying(raw.underlying),
};

/**
 * Around eight live BTC expiries, mostly clustered inside the next month —
 * the shape the real venue shows. ETH lists fewer.
 */
const FIXTURE_EXPIRIES: Record<Underlying, string[]> = {
  BTC: [
    "02-09-2026",
    "03-09-2026",
    "04-09-2026",
    "05-09-2026",
    "11-09-2026",
    "18-09-2026",
    "25-09-2026",
    "30-10-2026",
  ],
  ETH: ["02-09-2026", "04-09-2026", "11-09-2026", "25-09-2026", "30-10-2026"],
};

export function fixtureExpiries(underlying: Underlying): ExpiriesResponse {
  return { underlying, expiries: FIXTURE_EXPIRIES[underlying] };
}

/**
 * Only BTC / 04-09-2026 was actually captured. Rather than scale those numbers into
 * an invented ETH chain, every other combination reports that no fixture exists.
 * A made-up chain that looks real is worse than an honest gap, and this also gives
 * the error path something to render in fixture mode.
 */
export function fixtureChain(underlying: Underlying, expiry: string): ChainResponse {
  if (underlying === FIXTURE_CHAIN.underlying && expiry === FIXTURE_CHAIN.expiry) {
    return FIXTURE_CHAIN;
  }
  throw new Error(
    `No fixture for ${underlying} ${expiry}. The committed fixture covers ` +
      `${FIXTURE_CHAIN.underlying} ${FIXTURE_CHAIN.expiry} only — start the engine for the rest.`,
  );
}
