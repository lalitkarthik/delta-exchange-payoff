"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { RecordingState } from "@/lib/contract";
import { ENGINE_URL, loadRecording, setRecording } from "@/lib/engine";

/**
 * How often the state is re-read from the engine.
 *
 * **It is polled rather than remembered because two tabs must not be able to disagree.**
 * A tab that only read the state on mount would go on saying "recording" for as long as
 * it was open after another tab — or a curl, or a restart — had stopped it, and the whole
 * point of the switch is that a reader is never wrong about whether a day is being
 * captured. Five seconds against a loopback engine returning three integers; the ladder
 * beside it is pushed once a second over a socket, so this is the quieter of the two by a
 * wide margin.
 */
const POLL_MS = 5_000;

function message(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  // The engine's own `detail` is a sentence fragment as often as a sentence, and it is
  // followed here by another sentence. Without this they run together on one line.
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/**
 * Whether the store is recording, and the switch that changes it.
 *
 * **The state is the engine's and is read from it.** Nothing here is kept in
 * `localStorage`, nothing is inferred, and nothing is painted optimistically: every
 * render shows the last thing the engine actually said, including the body the POST
 * itself answered with. A control that guessed would eventually show "off" for a request
 * the engine refused.
 *
 * **Recording is on when the engine starts, and a pause does not survive a restart.** The
 * copy below says so rather than letting the reader assume a pause is durable.
 *
 * **A paused stretch is indistinguishable from an outage in the stored data**, and the
 * control says that in words. The store records no pause boundary — doing so is a larger
 * change and is out of scope for this ticket — so a hole a reader creates here reads back
 * on the volatility screen exactly like a dead feed. Leaving that unsaid would be the
 * screen quietly lying about the data it is about to show.
 *
 * **A control that silently does nothing is worse than one that says it failed.** Three
 * things can go wrong and each is named on screen: the state cannot be read at all, the
 * state is known but the switch was refused, and a request is in flight.
 */
export default function RecordingToggle() {
  const [state, setState] = useState<RecordingState | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A poll landing on top of a switch would repaint the old state for a moment, which is
  // the one moment a reader is looking. The ref rather than `busy` itself: the interval
  // closes over its first render, and a stale `false` there would defeat the guard.
  const inFlight = useRef(false);

  const read = useCallback(async () => {
    if (inFlight.current) return;
    try {
      setState(await loadRecording());
      setFailure(null);
    } catch (err) {
      // The state stays as it was rather than being cleared: the last thing the engine
      // said is more use than nothing, and the notice below says it may be stale.
      setFailure(message(err));
    }
  }, []);

  useEffect(() => {
    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    return () => clearInterval(timer);
  }, [read]);

  async function toggle() {
    if (state === null) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    try {
      setState(await setRecording(!state.recording));
    } catch (err) {
      setFailure(message(err));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  const recording = state?.recording ?? null;
  const label = busy ? "…" : recording === null ? "UNKNOWN" : recording ? "ON" : "OFF";
  const known = state !== null;

  return (
    <>
      <div className="rec">
        <span className="stat-label">Recording</span>
        <span className="rec-value">
          <button
            type="button"
            className="rec-toggle"
            // Not a `class` per state: the ground, the dot and the label all key off this
            // one attribute, so a fourth state cannot be added to half of them.
            data-state={recording === null ? "unknown" : recording ? "on" : "off"}
            onClick={() => void toggle()}
            disabled={busy || !known}
            aria-pressed={recording ?? false}
            title={
              recording === null
                ? `Cannot reach the engine at ${ENGINE_URL}, so the recording state is unknown.`
                : recording
                  ? "The store is writing. Press to stop it — what is already buffered is written first, and the live screens keep running."
                  : "The store is not writing. Press to start it again. Nothing that arrived while it was off can be recovered."
            }
          >
            <span className="rec-dot" aria-hidden />
            {label}
          </button>
          {/* The count is what makes "recording" a fact rather than a label: a figure
              that climbs is the engine capturing. Buffered bars are in the tooltip
              because they are the flush's business, not the reader's. */}
          {state !== null ? (
            <span
              className="rec-rows"
              title={`${state.buffered_rows.toLocaleString()} sealed bars are buffered in memory, not yet on disk.`}
            >
              {state.rows_written.toLocaleString()} rows
            </span>
          ) : null}
        </span>
      </div>

      {/* Laid out last and full width — see `.rec-warn`, which carries `order: 1` so
          it wraps onto its own line beneath the figures however early it appears in the
          markup. Ordering rather than moving it in the DOM keeps the control and the
          sentence that explains it in one component. */}
      {recording === false ? (
        <p className="rec-warn">
          <strong>Recording is off.</strong> Nothing is being written to the store. The
          store keeps no record of the pause, so this stretch will read back on the
          volatility screen <strong>exactly like an outage</strong> — there is no way to
          tell a hole you made from one the venue caused. Recording comes back on by
          itself when the engine restarts.
        </p>
      ) : null}

      {failure !== null ? (
        <p className="rec-warn rec-warn-failed" role="alert">
          <strong>
            {known ? "The switch was not thrown." : "The recording state cannot be read."}
          </strong>{" "}
          {failure}{" "}
          {known
            ? `The engine last said recording was ${
                recording ? "on" : "off"
              }, and that reading may now be stale.`
            : `Nothing on this screen knows whether ${ENGINE_URL} is writing to the store.`}
        </p>
      ) : null}
    </>
  );
}
