/**
 * The chain page's time slider, laid out on the same `Timeline` the volatility screen's
 * scrubber already understands.
 *
 * `docs/historical-chain-contract.md`'s minutes route returns plain stamps, not
 * `SmileMinute` objects — a ladder is roughly a hundred legs against a smile's one
 * number per strike, so this screen fetches one minute at a time over `/chain/at`
 * rather than the whole day in one response (`docs/design/lld/historical-read-path.md`
 * explains the cost trade this is built on). `lib/timeline.ts`'s gap computation, its
 * `MAX_POSITIONS` fallback and `TimeScrubber` itself do not care what a position
 * *carries*, only whether one is there — so wrapping each stamp in a placeholder
 * `SmileMinute` reuses all three unchanged rather than inventing a second grid.
 */
import type { SmileMinute, SmileResponse } from "./contract";
import { buildTimeline, withLiveMinute, type Timeline } from "./timeline";

function placeholder(minute: string): SmileMinute {
  return {
    minute,
    forward: null,
    discount: null,
    years_to_expiry: null,
    forward_method: null,
    model_version: null,
    points: [],
  };
}

/** Every stored minute of one day, as a `Timeline` — the stamps and the gaps, nothing
 * else. `underlying`/`expiry` on the fake response are never read by `buildTimeline`. */
export function chainTimeline(minutes: readonly string[]): Timeline {
  const fake: SmileResponse = {
    underlying: "BTC",
    expiry: "",
    model_versions: [],
    minutes: minutes.map(placeholder),
  };
  return buildTimeline(fake);
}

/**
 * The stored timeline with "live" standing one position past its right edge — exactly
 * `withLiveMinute`'s own behaviour, with the live chain's `fetched_at` standing in for
 * `smile`'s live minute. `null` while the socket has not delivered a chain yet, which
 * leaves the timeline's right edge as the last *stored* minute until it does — the same
 * rule the volatility screen already follows.
 */
export function withLiveEdge(timeline: Timeline, fetchedAt: string | null): Timeline {
  return withLiveMinute(timeline, fetchedAt ? placeholder(fetchedAt) : null);
}
