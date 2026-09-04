"use client";

import type { SmileMinute } from "@/lib/contract";
import { ENGINE_URL, type Source } from "@/lib/engine";
import type { LiveStatus } from "@/lib/live";
import { formatTimeToExpiry, isDying } from "@/lib/smile";

/**
 * Everything this screen admits before it draws anything: the two ways the stream can
 * fail, the fixture standing in for the store, a link naming a minute outside the day, a
 * response computed more than one way, and an expiry close enough to settlement that the
 * axis stops meaning what it usually means.
 *
 * They are one component because they are one job — the screen's voice, in the one place
 * a reader looks for it, at one size — and because their order on the page is itself a
 * decision: what went wrong first, then what is standing in for what, then what the data
 * itself will not support.
 */
export default function SmileNotices({
  error,
  liveStatus,
  liveDetail,
  source,
  fallbackReason,
  storedMinutes,
  unreachable,
  underlying,
  expiry,
  stamps,
  minute,
  hours,
}: {
  error: string | null;
  liveStatus: LiveStatus;
  liveDetail: string | null;
  source: Source;
  fallbackReason: string | null;
  /** How many minutes the **stored** day carried, before the live edge was folded on. */
  storedMinutes: number;
  /** A minute the link asked for that this expiry's store does not reach. */
  unreachable: string | null;
  underlying: string;
  expiry: string;
  /** `model_versions`, as the response listed them. */
  stamps: readonly string[];
  minute: SmileMinute | null;
  hours: number | null;
}) {
  return (
    <>
      {error ? <p className="notice error">{error}</p> : null}

      {liveStatus === "error" ? (
        <p className="notice error">
          {liveDetail ?? "The live stream reported an error."} The stored day below is
          unaffected; only the right edge has stopped moving.
        </p>
      ) : null}

      {liveStatus === "closed" ? (
        <p className="notice warn">
          Lost the connection to the engine at <code>{ENGINE_URL}</code>. Retrying — the
          curve is the last minute that arrived and it is not moving.
        </p>
      ) : null}

      {source === "fixture" && !error ? (
        <p className="notice warn">
          Showing the committed smile fixture, not the store — a real capture of{" "}
          <strong>
            {storedMinutes} minute
            {storedMinutes === 1 ? "" : "s"}
          </strong>
          , not a day.{" "}
          {fallbackReason ?? `Set NEXT_PUBLIC_USE_FIXTURE=0 and start the engine for stored data.`}
        </p>
      ) : null}

      {unreachable ? (
        <p className="notice warn">
          The link asked for <strong>{unreachable}</strong>, which is outside the minutes
          stored for {underlying} {expiry}. Showing the most recent minute instead — the
          nearest curve is not the one you asked for, so it is not offered as one.
        </p>
      ) : null}

      {stamps.length > 1 ? (
        <p className="notice warn">
          This response spans <strong>{stamps.length} model stamps</strong> — the curves
          in this day were not all computed the same way. {stamps.join(" · ")}. The
          forward convention alone is worth up to 3.9 volatility points, and this screen
          plots nothing but volatility points.
        </p>
      ) : null}

      {minute && isDying(minute) && hours !== null ? (
        <p className="notice warn">
          <strong>Under a day to settlement — {formatTimeToExpiry(hours)}.</strong> Vega
          collapses as time to expiry goes to zero, so a one-tick price change moves the
          implied volatility by tens of points. A spike here is a{" "}
          <strong>dying contract, not a computation error</strong>. Measured at 07:38Z on
          the front expiry: median 62.5%, maximum 400.5% at 4.4 hours out, against a
          median near 40% and a maximum under 72% everywhere else on the board. Switch
          the volatility axis to LOG to read the at-the-money region.
        </p>
      ) : null}
    </>
  );
}
