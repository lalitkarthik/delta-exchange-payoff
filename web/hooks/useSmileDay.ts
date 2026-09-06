"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { SmileResponse, Underlying } from "@/lib/contract";
import { loadExpiries, loadSmile, type Source } from "@/lib/engine";

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export interface SmileDay {
  /** Every expiry the engine lists for this underlying. Empty until the list lands. */
  expiries: string[];
  /** The one being read. `""` until the list has chosen one. */
  expiry: string;
  setExpiry: (next: string) => void;
  /** The whole stored day for that expiry, or `null` while it is in flight. */
  smile: SmileResponse | null;
  /** Whether the day came from the engine or from the committed fixture. */
  source: Source;
  /** Why the fixture was used, when it was. */
  fallbackReason: string | null;
  busy: boolean;
  error: string | null;
}

/**
 * The stored side of the volatility screen: the expiry list, the day behind whichever
 * expiry is chosen, and the fixture the client falls back to when the engine cannot be
 * reached.
 *
 * **Which expiry is being read lives here rather than in the screen**, because the list
 * request is what chooses it: a URL naming an expiry the engine does not list has to be
 * resolved against the list before anything can be fetched, and splitting the choice
 * from the list that constrains it is how the dropdown ends up showing one expiry while
 * the chart draws another.
 *
 * `initialExpiry` is the URL's request. It is honoured only if the list contains it;
 * otherwise the engine's own preferred expiry wins, and failing that the first listed.
 */
export function useSmileDay(underlying: Underlying, initialExpiry: string): SmileDay {
  const [expiries, setExpiries] = useState<string[]>([]);
  const [expiry, setExpiry] = useState<string>(initialExpiry);

  const [smile, setSmile] = useState<SmileResponse | null>(null);
  const [source, setSource] = useState<Source>("engine");
  const [fallbackReason, setFallbackReason] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Guards against a slow earlier request landing after a newer one.
  const listRequest = useRef(0);
  const smileRequest = useRef(0);

  const loadExpiryList = useCallback(async (next: Underlying, wantedExpiry: string | null) => {
    const id = ++listRequest.current;
    setBusy(true);
    setError(null);
    try {
      const list = await loadExpiries(next);
      if (id !== listRequest.current) return;

      const available = list.data.expiries;
      setExpiries(available);

      const fallback =
        list.preferredExpiry && available.includes(list.preferredExpiry)
          ? list.preferredExpiry
          : (available[0] ?? "");
      const chosen =
        wantedExpiry && available.includes(wantedExpiry) ? wantedExpiry : fallback;
      setExpiry(chosen);
      if (!chosen) setError(`No expiries listed for ${next}.`);
    } catch (err) {
      if (id !== listRequest.current) return;
      setError(message(err));
    } finally {
      if (id === listRequest.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadExpiryList(underlying, expiry || null);
    // Keyed on the underlying alone: re-running this when the expiry changes would
    // refetch a list that cannot have changed and reset the dropdown under the reader.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [underlying, loadExpiryList]);

  useEffect(() => {
    if (!expiry) return;
    const id = ++smileRequest.current;
    setSmile(null);
    setError(null);
    setBusy(true);

    void (async () => {
      try {
        const loaded = await loadSmile(underlying, expiry);
        if (id !== smileRequest.current) return;
        setSmile(loaded.data);
        setSource(loaded.source);
        setFallbackReason(loaded.fallbackReason ?? null);
      } catch (err) {
        if (id !== smileRequest.current) return;
        setError(message(err));
      } finally {
        if (id === smileRequest.current) setBusy(false);
      }
    })();
  }, [underlying, expiry]);

  return { expiries, expiry, setExpiry, smile, source, fallbackReason, busy, error };
}
