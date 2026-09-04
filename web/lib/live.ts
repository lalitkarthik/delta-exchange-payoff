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

/** What the socket can say. Mirrors the envelope in `engine/src/deltapayoff/main.py`. */
export type LiveMessage =
  | { type: "chain"; data: ChainResponse }
  | { type: "waiting"; detail: string }
  | { type: "error"; detail: string };

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
        // A message arriving proves the connection works, so the backoff resets here
        // rather than on open: a socket that opens and immediately closes would
        // otherwise reset the delay on every attempt and retry in a tight loop.
        delay = FIRST_RETRY_MS;
        handlers.onStatus("live");
        handlers.onChain(message.data);
      } else if (message.type === "waiting") {
        handlers.onStatus("waiting", message.detail);
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
