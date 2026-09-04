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
import type {
  ChainResponse,
  ExpiriesResponse,
  SmileResponse,
  Underlying,
} from "./contract";
import chain from "./fixture.chain.json";
import smile from "./fixture.smile.json";

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

/**
 * The smile fixture, `fixture.smile.json`, shaped to `docs/smile-contract.md`.
 *
 * **Built from the chain fixture beside it, not invented.** Every volatility here is the
 * one the engine actually solved for BTC 04-09-2026 at 2026-09-03T10:28:30Z — the same
 * capture `fixture.chain.json` holds — read off its `computed` blocks and de-duplicated
 * to one point per strike. The curve is therefore a real one: 20 strikes from 76000 to
 * 79200 around a fitted forward of 77646.88, and `iv_leg` genuinely flips from `put` to
 * `call` as the strikes cross it. That flip is the thing a made-up fixture would get
 * wrong and the thing the hover exists to explain.
 *
 * Two deliberate departures from the capture, both of them the chain fixture's own
 * house rule — construct the edge case the capture did not contain, and say so:
 *
 *   - **Strike 79400 carries a null `iv`** and the reason `"no two-sided quote"`. The
 *     capture solved every listed strike, but 1.2% of the real store does not, and the
 *     screen has to break the line there rather than join 79200 to nothing. Without it
 *     the null path would ship untested — and there is no test runner here to catch it.
 *   - **`iv_reason` is `null` where the chain fixture spells it `""`.** That is the
 *     store's spelling and the contract's, not a slip.
 *
 * **One minute, because one chain was captured** — and it stays one minute, which was
 * reconsidered when the scrubber arrived in #20 and deliberately left alone.
 *
 * Forward-filling it into a day of identical curves would be fabrication of exactly the
 * kind `docs/storage-start-here.md` refuses. The live alternative — replacing this with a
 * genuine window read out of the running store — was measured and declined on three
 * counts. This file is **statically imported into the client bundle**, so every page load
 * pays for it forever; the day the scrubber was built against is 2.0 MB on the wire and
 * even a twenty-minute window of that expiry's 85 strikes is twenty-five times the 3.4 KB
 * this fixture costs today. The capture would come from a different day than
 * `fixture.chain.json`, whose forward is 77,646.88 against the store's 81,190 — and the
 * one thing that makes this fixture worth trusting is that it is *the same capture* as the
 * chain beside it, down to the leg flip. And a window is not a day either: twenty minutes
 * of a thirteen-hour store demonstrates the control while teaching a false shape unless a
 * label does the work, at which point the label is doing the work and the honest
 * one-position degradation can do it instead.
 *
 * So in fixture mode the scrubber has one position and **says so**: the track is disabled,
 * both step buttons are disabled, and the readout reads `1 / 1 · one minute — nothing to
 * scrub`, with the fixture banner naming how many minutes the capture holds. `measured` by
 * serving the live endpoint's newest minute alone into the running screen — the same shape
 * this file has. The rest of the day needs a real capture from a running engine, and that
 * is what the engine is for.
 */
const rawSmile = smile satisfies Omit<SmileResponse, "underlying"> & { underlying: string };

export const FIXTURE_SMILE: SmileResponse = {
  ...rawSmile,
  underlying: narrowUnderlying(rawSmile.underlying),
};

/**
 * Only BTC / 04-09-2026 was captured, as with the chain. Every other pair reports an
 * honest gap rather than a plausible invention — and gives the screen's error path
 * something real to render without an engine.
 */
export function fixtureSmile(underlying: Underlying, expiry: string): SmileResponse {
  if (underlying === FIXTURE_SMILE.underlying && expiry === FIXTURE_SMILE.expiry) {
    return FIXTURE_SMILE;
  }
  throw new Error(
    `No smile fixture for ${underlying} ${expiry}. The committed fixture covers ` +
      `${FIXTURE_SMILE.underlying} ${FIXTURE_SMILE.expiry} only — start the engine for the rest.`,
  );
}
