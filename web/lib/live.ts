/**
 * The live chain, over a websocket.
 *
 * The engine holds one connection to Delta and pushes the complete `ChainResponse` —
 * the identical object `/chain` returns — once a second. So nothing that renders a
 * chain changes: `ChainLadder` is handed the same shape it always was, and the transport
 * is the only thing that moved.
 *
 * The browser cannot talk to Delta directly. The pricing is Python, and a hundred and
 * thirty-six browsers would be a hundred and thirty-six connections against a budget of
 * a hundred and fifty per five minutes. So the engine is the one subscriber and every
 * browser is a consumer of it.
 *
 * Reconnection is the browser's job here, not the engine's. A dev server restart, a
 * laptop waking from sleep, a wifi blip — all of them close the socket with no error the
 * page can act on, so a closed socket is retried with backoff until it opens again.
 */
import type { ChainResponse, Underlying } from "./contract";
import { ENGINE_URL } from "./engine";

/** `http://localhost:8000` becomes `ws://localhost:8000`. */
export function engineSocketUrl(underlying: Underlying, expiry: string): string {
  const base = ENGINE_URL.replace(/^http/, "ws");
  return `${base}/ws/chain?underlying=${underlying}&expiry=${encodeURIComponent(expiry)}`;
}

/**
 * The venue connection's own state, #40. `docs/live-chain-contract.md` is the authority;
 * this mirrors it field for field, as `engineSocketUrl`'s envelope above already does
 * for `chain`/`waiting`/`error`.
 *
 * **A different fact from `LiveStatus` below, and the two must never be conflated on
 * screen.** `LiveStatus` is this browser's own socket to *the engine*; `FeedState` is
 * the engine's socket to *Delta*. They fail independently — the browser's connection to
 * a perfectly healthy engine can read `live` while `state` here reads `reconnecting` —
 * and that gap is the entire reason this type exists rather than folding its five values
 * into `LiveStatus`'s five.
 */
export type FeedState = "connecting" | "connected" | "degraded" | "reconnecting" | "stopped";

/** `docs/live-chain-contract.md`'s ten reasons. Kept as `string` rather than a union:
 * a reason nobody has named yet must still render on hover rather than fail to parse —
 * which is what let #41's `paused` reach the badge with no change on this side. */
export type FeedReason = string;

export interface FeedStatus {
  /** The venue name, e.g. `"DELTA"`. One adapter today — see the contract doc. */
  adapter: string;
  state: FeedState;
  /** ISO 8601 UTC, second precision, `Z`-suffixed. When the engine last told a browser
   * the feed entered `state` — not necessarily the instant it actually did; see the
   * contract doc's "Coalescing". */
  since: string;
  reason: FeedReason;
}

/** What the socket can say. Mirrors the envelope in `engine/src/deltapayoff/main.py`. */
export type LiveMessage =
  | { type: "chain"; data: ChainResponse }
  | { type: "waiting"; detail: string }
  | { type: "error"; detail: string }
  | { type: "feed"; data: FeedStatus };

/**
 * Where a subscription is, for the header chip.
 *
 * `waiting` is deliberately distinct from `live`. An empty ladder and a ladder that has
 * not arrived look identical on screen and are not the same thing: the first says Delta
 * lists nothing, the second says the socket has not spoken yet.
 */
export type LiveStatus = "connecting" | "live" | "waiting" | "closed" | "error";

/**
 * What the header chip says, in one place.
 *
 * Two screens read the same socket and the connection means the same thing on both, so
 * it is spelled once. Two copies of this map is two vocabularies for one state, and the
 * one that drifts is always the one nobody is looking at.
 */
export const LIVE_STATUS_LABEL: Record<LiveStatus, string> = {
  connecting: "connecting…",
  live: "live",
  waiting: "waiting for quotes…",
  closed: "reconnecting…",
  error: "error",
};

export interface LiveHandlers {
  onChain: (chain: ChainResponse) => void;
  onStatus: (status: LiveStatus, detail?: string) => void;
  /** The venue feed's own state, #40. Optional so every existing caller keeps
   * compiling unchanged; a caller that omits it simply never learns the feed's state,
   * exactly as before this ticket. */
  onFeed?: (feed: FeedStatus) => void;
}

/** The ladder header's badge, or what one non-`connected` `FeedStatus` becomes. */
export interface FeedBadge {
  state: Exclude<FeedState, "connected">;
  label: string;
  reason: FeedReason;
}

/**
 * What the badge says for each state that gets one. `connected` is deliberately absent
 * — see `feedBadge` below — so a state added here without ever being read is a type
 * error rather than a label nobody sees.
 */
const FEED_BADGE_LABEL: Record<Exclude<FeedState, "connected">, string> = {
  connecting: "feed connecting…",
  degraded: "feed degraded",
  reconnecting: "feed reconnecting…",
  stopped: "feed stopped",
};

/**
 * What the ladder header's feed badge should show, or `null` for nothing at all.
 *
 * **`null` for `connected`, and only for `connected`.** The badge exists to say "the
 * venue feed is not simply fine right now" — a `connected` feed does not need arguing
 * for on screen, and showing a badge for it anyway would be the fixed-chain-with-no-
 * indicator problem `docs/chain-contract.md` already solves for a fully quoted ladder,
 * repeated on the header. `null` also for `feed === null`, which is every screen before
 * the engine's first `feed` message has arrived, or against an engine old enough to
 * never send one at all — the badge's absence there is silence, not a claim that the
 * feed is fine.
 *
 * **Takes a `FeedStatus`, never a `LiveStatus`.** The whole reason two indicators exist
 * — see this module's own header comment on `FeedState` — is that they can disagree; a
 * caller that reached for the browser's own socket status here instead would be
 * building the exact bug #40 exists to prevent, and the type signature refuses it.
 */
export function feedBadge(feed: FeedStatus | null): FeedBadge | null {
  if (feed === null || feed.state === "connected") return null;
  return { state: feed.state, label: FEED_BADGE_LABEL[feed.state], reason: feed.reason };
}

/** Backoff between reconnect attempts, in milliseconds. Capped so it stays responsive. */
const FIRST_RETRY_MS = 500;
const MAX_RETRY_MS = 10_000;

/**
 * Subscribe until the returned function is called.
 *
 * Returns an unsubscribe. Calling it stops the retry loop as well as closing the socket —
 * without that, switching expiry twice quickly leaves an orphaned reconnect timer that
 * later opens a socket for an expiry nobody is looking at.
 */
export function subscribeChain(
  underlying: Underlying,
  expiry: string,
  handlers: LiveHandlers,
): () => void {
  let socket: WebSocket | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let delay = FIRST_RETRY_MS;
  let stopped = false;

  const open = () => {
    if (stopped) return;
    handlers.onStatus("connecting");

    socket = new WebSocket(engineSocketUrl(underlying, expiry));

    socket.onmessage = (event) => {
      let message: LiveMessage;
      try {
        message = JSON.parse(event.data as string) as LiveMessage;
      } catch {
        handlers.onStatus("error", "The engine sent something that was not JSON.");
        return;
      }

      if (message.type === "chain") {
        // #60 (I1): the same guard `lib/engine.ts`'s `loadChain` applies to `/chain`,
        // reapplied here because this is the primary path — `ChainScreen` follows the
        // socket, not the REST route, whenever it is live. A `ChainResponse` with no
        // `quote_currency` is a contract breach the app must not render as if it were
        // silently USD; the socket is not a promise a caller can catch a thrown error
        // from, so this reports it exactly as a malformed frame is reported below.
        if (
          typeof message.data.quote_currency !== "string" ||
          message.data.quote_currency.length === 0
        ) {
          handlers.onStatus(
            "error",
            "The engine sent a chain with no quote_currency, which docs/chain-contract.md " +
              "requires since #60 (I1).",
          );
          return;
        }
        // A message arriving proves the connection works, so the backoff resets here
        // rather than on open: a socket that opens and immediately closes would
        // otherwise reset the delay on every attempt and retry in a tight loop.
        delay = FIRST_RETRY_MS;
        handlers.onStatus("live");
        handlers.onChain(message.data);
      } else if (message.type === "waiting") {
        handlers.onStatus("waiting", message.detail);
      } else if (message.type === "feed") {
        // Never touches `delay`, `stopped` or `onStatus`: this is the *venue's*
        // connection, not this browser's socket to the engine, and must not be read as
        // proof of either — see this module's header comment on `FeedState`.
        handlers.onFeed?.(message.data);
      } else {
        // A rejected underlying or expiry can never succeed, so this is not retried.
        stopped = true;
        handlers.onStatus("error", message.detail);
        socket?.close();
      }
    };

    socket.onclose = () => {
      if (stopped) return;
      handlers.onStatus("closed");
      retry = setTimeout(open, delay);
      delay = Math.min(delay * 2, MAX_RETRY_MS);
    };

    // `onerror` carries nothing useful in a browser for security reasons, and `onclose`
    // always follows it, so the reconnect is left to that one path.
    socket.onerror = () => {};
  };

  open();

  return () => {
    stopped = true;
    if (retry !== null) clearTimeout(retry);
    socket?.close();
  };
}
