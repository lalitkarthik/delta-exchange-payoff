"use client";

import { useEffect, useState } from "react";

import AnalyseView from "@/components/AnalyseView";
import { subscribeAnalysis } from "@/lib/analyse-live";
import {
  ContractViolationError,
  ENGINE_URL,
  EngineResponseError,
  EngineUnreachableError,
} from "@/lib/engine";
import { LegsUrlError, decodeLegs, syncedAnalyseHref } from "@/lib/legs-url";
import type { AnalyseResponse, LegRequest } from "@/lib/payoff";

/**
 * The strategy the link named, decoded exactly once — `ChainScreen.decodeInitialLegs`'s
 * rule and its reason. A malformed `legs=` is never treated as "no strategy":
 * `lib/legs-url.ts` throws naming the part that was wrong, and that message is carried
 * to the screen rather than the reader being started on an empty position their link
 * did not ask for.
 */
function decodeInitial(param: string | null): { legs: LegRequest[]; error: string | null } {
  try {
    return { legs: decodeLegs(param), error: null };
  } catch (err) {
    return {
      legs: [],
      error: err instanceof LegsUrlError ? err.message : String(err),
    };
  }
}

/**
 * One sentence for whatever went wrong, with the three kinds kept apart.
 *
 * A **refusal** is the engine answering — the request was understood and declined — and
 * its own sentence is the useful part, with the code beside it so the reason is
 * findable in `docs/payoff-contract.md`'s table. An **unreachable** engine is a local
 * condition and says where it tried. A **contract violation** is our own side's bug and
 * says so in full rather than being softened into "something went wrong".
 */
function problemOf(err: unknown): string {
  if (err instanceof EngineResponseError) {
    return `The engine declined this strategy (${err.status}): ${err.message}`;
  }
  if (err instanceof EngineUnreachableError) {
    return `Could not reach the engine at ${ENGINE_URL} — ${err.message}. Nothing is analysed.`;
  }
  if (err instanceof ContractViolationError) return err.message;
  return err instanceof Error ? err.message : String(err);
}

/** The same settle `ChainScreen` and `VolatilityScreen` use, and for the same reason. */
const URL_SETTLE_MS = 200;

/**
 * The analyse tab: the strategy, kept up to date in both directions.
 *
 * **Live when the link names no minute, static when it names one.** `subscribeAnalysis`
 * holds that rule and the reasoning for it; this screen holds the state it produces. The
 * timer is not here, which is what keeps this component clear of the wall clock and the
 * rule testable on a request count rather than on a duration.
 *
 * **The legs are state, and the address bar follows them.** They start as whatever the
 * link decoded to and are edited from `LegEditor` after that. `window.history.replaceState`
 * rather than a router push, for `ChainScreen`'s and `VolatilityScreen`'s reason: a
 * strategy being edited must not fill the back button, and the link has to stay copyable
 * without the page reloading under the reader. Same 200 ms settle as those two — Firefox
 * and Safari throttle `replaceState`, and a quantity being nudged is a drag.
 *
 * **A new strategy clears the old analysis.** The curve on screen belongs to the legs it
 * was asked for; leaving it up while a different strategy is in flight would draw one
 * position and label it another. A poll of the *same* strategy does not go through here
 * — only `legs` and `minute` restart the subscription — so the live case does not flash.
 *
 * The fetching is all that lives here; `AnalyseView` renders the result and is testable
 * without a browser because of that split.
 */
export default function AnalyseScreen({
  legsParam,
  minute,
}: {
  /** The raw `legs=` value, undecoded — the server component hands it down exactly as it
   * arrived, because a malformed one has to be reported rather than dropped, and a
   * server component has no notice to report it into. */
  legsParam: string | null;
  /** A stored minute, or `null` for live. Validated by `parseView` upstream. */
  minute: string | null;
}) {
  const [strategy] = useState(() => decodeInitial(legsParam));
  const [legs, setLegs] = useState<LegRequest[]>(strategy.legs);
  const [analysis, setAnalysis] = useState<AnalyseResponse | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [receivedAt, setReceivedAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setAnalysis(null);
    setProblem(null);
    setReceivedAt(null);
    if (legs.length === 0) {
      setBusy(false);
      return;
    }
    setBusy(true);
    return subscribeAnalysis(legs, minute, {
      onAnalysis: (next, at) => {
        setAnalysis(next);
        setProblem(null);
        setReceivedAt(at);
        setBusy(false);
      },
      // A refusal replaces the analysis rather than sitting beside it — `AnalyseView`'s
      // own rule. The engine has declined to answer for *these* legs, so there is no
      // curve that belongs to them, and a poll that starts failing is not a licence to
      // keep showing the last one as though it were current.
      onError: (err) => {
        setAnalysis(null);
        setProblem(problemOf(err));
        setBusy(false);
      },
    });
  }, [legs, minute]);

  // The address bar follows the strategy; it never drives it after the first render.
  //
  // **`syncedAnalyseHref`, never `analyseHref` — a rewrite must never outlive an
  // unreported parse failure.** When the strategy was decoded out of this very address
  // bar and the decode threw, the text naming what was wrong is *in* the address bar, and
  // rewriting it 200 ms later leaves the notice on screen describing a string nobody can
  // read any more. The rule and its history are written down beside the codec, so the next
  // screen that adopts this pattern inherits the rule rather than the bug.
  useEffect(() => {
    const href = syncedAnalyseHref(legs, minute, strategy.error);
    if (href === null) return;
    if (`${window.location.pathname}${window.location.search}` === href) return;
    const timer = window.setTimeout(() => {
      window.history.replaceState(null, "", href);
    }, URL_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [legs, minute, strategy.error]);

  return (
    <AnalyseView
      legs={legs}
      analysis={analysis}
      problem={problem}
      legsError={strategy.error}
      busy={busy}
      minute={minute}
      receivedAt={receivedAt}
      onLegsChange={setLegs}
    />
  );
}
