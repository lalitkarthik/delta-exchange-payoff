"use client";

import { useEffect, useMemo, useState } from "react";

import type { ChainResponse, SmileMinute, Underlying } from "@/lib/contract";
import { subscribeChain, type LiveStatus } from "@/lib/live";
import { smileMinuteFromChain } from "@/lib/livesmile";

/**
 * How often the live curve is allowed to be redrawn.
 *
 * **A ladder and a smile are read differently and that is the whole argument.** The
 * chain screen takes every push, and it is right to: a ladder is read one cell at a
 * time, so a cell that changes under the eye is a cell that has news. A smile is read as
 * a *shape* — the tilt of the wings against the middle — and a shape has to hold still
 * long enough to be seen. At the stream's own rate the curve shimmers and the skew is
 * unreadable; at about a second it still reads as live and the shape settles between
 * frames.
 *
 * The sampler below is trailing, not leading: it publishes the newest push it holds when
 * the tick comes round and drops everything older, so the curve is never more than one
 * interval behind the stream and never renders a frame it is about to throw away. The
 * cost is that the first curve after a subscription waits up to one interval, which is
 * invisible beside the round trip that preceded it.
 */
export const LIVE_REDRAW_MS = 1000;

export interface LiveSmile {
  /** The newest published push as a minute of the smile, or `null`. */
  minute: SmileMinute | null;
  status: LiveStatus;
  detail: string | null;
}

/**
 * The right edge of the day, live: one chain subscription per series, sampled down to
 * `LIVE_REDRAW_MS` and projected into a smile minute.
 *
 * **The same socket the chain screen reads, and the same object off it.** The stream
 * carries a chain; the volatility screen draws a smile; `lib/livesmile.ts` is the
 * projection between them and carries the argument for doing it here rather than asking
 * the engine for a second payload or polling `/smile` on a timer. Nothing is recomputed —
 * the volatility this chart plots is the identical field of the identical push the ladder
 * prints.
 */
export function useLiveSmile(underlying: Underlying, expiry: string): LiveSmile {
  /** The newest push the sampler has published. See `LIVE_REDRAW_MS`. */
  const [liveChain, setLiveChain] = useState<ChainResponse | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("connecting");
  const [liveDetail, setLiveDetail] = useState<string | null>(null);

  /**
   * `latest` is a plain local rather than a ref because it belongs to this
   * subscription: it is created with the socket and dies with it, so a push that
   * arrived for the old expiry can never be published against the new one.
   */
  useEffect(() => {
    if (!expiry) return;
    setLiveChain(null);

    let latest: ChainResponse | null = null;
    const sampler = window.setInterval(() => {
      if (latest === null) return;
      setLiveChain(latest);
      latest = null;
    }, LIVE_REDRAW_MS);

    const stop = subscribeChain(underlying, expiry, {
      onChain: (chain) => {
        latest = chain;
      },
      onStatus: (next, detail) => {
        setLiveStatus(next);
        setLiveDetail(detail ?? null);
      },
    });

    return () => {
      window.clearInterval(sampler);
      stop();
    };
  }, [underlying, expiry]);

  /**
   * The series is checked rather than assumed. The subscription is torn down and rebuilt
   * on every change of underlying or expiry, so a mismatch should be impossible — and
   * the screen puts two sources of the same expiry's curve on one axis, which is
   * exactly the place where "should be impossible" is worth one comparison.
   */
  const minute = useMemo(() => {
    if (!liveChain) return null;
    if (liveChain.underlying !== underlying || liveChain.expiry !== expiry) return null;
    return smileMinuteFromChain(liveChain);
  }, [liveChain, underlying, expiry]);

  return { minute, status: liveStatus, detail: liveDetail };
}
