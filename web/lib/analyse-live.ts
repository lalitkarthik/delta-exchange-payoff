/**
 * The analyse tab's subscription: one request, then either a heartbeat or silence.
 *
 * **Live when the link names no minute, static when it names one.** A stored minute is a
 * fixed set of numbers; re-asking for it would return the same answer forever, and
 * quietly re-asking for the *newest* one instead would be the same class of lie as
 * forward-filling a bar — the reader would be shown a different minute from the one
 * their link names, with nothing on screen saying so. So the timer is not merely
 * pointless on a historical link, it is not started at all, and
 * `tests/analyse-live.test.ts` asserts that on the **request count**.
 *
 * **A quiet second is normal.** `docs/design/quiet-gap.md` (#59) measured the worst quiet
 * gap on a live socket at 6.792 s, so a poll that comes back identical to the last one is
 * the expected case and is not an error, not a reconnect, and not worth a badge.
 *
 * **Why a timer here rather than the websocket `lib/live.ts` already holds.** That socket
 * carries a `ChainResponse` — the ladder — and an analysis is not a ladder: it is a
 * strategy priced against one, and the pricing is Python. There is no `/ws/analyse`, and
 * inventing one is not this ticket. A one-second POST is the same cadence the engine
 * pushes the chain at, so the tab is no more stale than the ladder behind it.
 *
 * **The scheduler and the clock are injected** — never read here — so the tests can hold
 * the tick and fire it by hand. Nothing in this project reads the wall clock in a test.
 */
import { postAnalyse } from "./engine";
import type { AnalyseResponse, LegRequest } from "./payoff";

/** One second, from P6. */
export const POLL_MS = 1000;

/** The real timer. The only place in this module that touches one. */
function defaultSchedule(fire: () => void, ms: number): () => void {
  const id = setInterval(fire, ms);
  return () => clearInterval(id);
}

export interface AnalysisHandlers {
  /** An answer, and **when it arrived** — the as-of chip's "live · updated 09:21:07".
   *  Passed in rather than stamped by the caller so the instant belongs to the response
   *  it describes and cannot drift a render behind it. */
  onAnalysis: (analysis: AnalyseResponse, receivedAt: string) => void;
  /** Raw, unflattened. The wording of a refusal is the screen's business, not the
   *  timer's — `AnalyseScreen.problemOf` is where the three kinds are told apart. */
  onError: (err: unknown) => void;
}

/** The seams this module is tested through. Every one of them has a real default, so
 *  production passes nothing at all. */
export interface AnalysisDeps {
  ask?: (legs: LegRequest[], asOf: string | null) => Promise<AnalyseResponse>;
  schedule?: (fire: () => void, ms: number) => () => void;
  now?: () => string;
}

/**
 * Ask once, and keep asking only if this is a live link. Returns the unsubscribe.
 *
 * After the returned function is called nothing else is delivered — including an answer
 * already in flight, which belongs to a strategy the reader has since edited away.
 */
export function subscribeAnalysis(
  legs: LegRequest[],
  minute: string | null,
  handlers: AnalysisHandlers,
  deps: AnalysisDeps = {},
): () => void {
  const ask = deps.ask ?? postAnalyse;
  const schedule = deps.schedule ?? defaultSchedule;
  const now = deps.now ?? (() => new Date().toISOString());

  // An empty strategy is not asked about. `POST /analyse` refuses `legs: []` with a 422
  // by design (docs/payoff-contract.md §Refusals, the schema row), so polling one would
  // be asking the engine to say no once a second — and the screen already has something
  // truthful to show for it, which is that there are no legs.
  if (legs.length === 0) return () => {};

  let stopped = false;
  let inFlight = false;

  const run = () => {
    // /analyse is deliberately fat and a slow answer can outlast the second it was asked
    // in. Two in flight at once can land out of order, and the older one landing last
    // would put a stale spot on screen. The tick is skipped rather than queued: a poll
    // is a request for the *current* numbers, and a backlog of them is worth nothing.
    if (inFlight) return;
    inFlight = true;
    void ask(legs, minute)
      .then((analysis) => {
        if (!stopped) handlers.onAnalysis(analysis, now());
      })
      .catch((err) => {
        if (!stopped) handlers.onError(err);
      })
      .finally(() => {
        inFlight = false;
      });
  };

  run();

  if (minute !== null) {
    return () => {
      stopped = true;
    };
  }

  const cancel = schedule(run, POLL_MS);
  return () => {
    stopped = true;
    cancel();
  };
}
