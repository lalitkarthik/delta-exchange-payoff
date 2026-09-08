"""#38 step, extended in #39: how long does the live feed actually go quiet?

The connection controller marks a connection `degraded` when nothing has arrived for a
bounded interval, and `reconnecting` at a longer one. Those bounds have to sit above the
longest gap a **healthy** feed produces, or the badge fires on a quiet market and means
nothing; and below the point a person would call the feed dead. This measures the real
thing.

It subscribes every listed BTC option on both channels — exactly what the engine
subscribes — and records the wall-clock gap between consecutive frames off the socket,
which is precisely what the controller's staleness timer measures. One hour by default.

    python tools/measure_quiet_gap.py             # one hour
    python tools/measure_quiet_gap.py 300         # five minutes

**Every gap is tagged with the connection events inside it, which is #39's addition and
the reason a second hour was worth taking.** The first hour recorded three connections
and a 44.785 s maximum and could not say whether those were the same event: attributing
the gap to a reconnect was inference. A gap that contains no `open` and no `close` is the
venue going quiet on a socket that stayed up — the only kind of gap `degraded_after` and
`reconnect_after` are actually chosen against, since a gap that spans a drop is a
reconnect the controller is *supposed* to react to. Both distributions are reported.

Writes a JSON summary to `tools/out/`, named by the run's start time and length, so the
number can be quoted with its run named and **so a later run cannot overwrite an earlier
one**. `tools/out/` is gitignored: what a re-run destroys there is not recoverable, so the
results that matter are copied into `docs/design/quiet-gap.md`, which is where the runs
taken so far are recorded.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from _window import run_window  # noqa: E402
from deltapayoff.adapters.delta_socket import (  # noqa: E402
    BOOK_CHANNEL,
    TICKER_CHANNEL,
    DeltaFeed,
)

REST = "https://api.india.delta.exchange"
DEFAULT_SECONDS = 3600.0
#: **Not optional.** Delta's edge answers a request without one with HTTP 403 and an
#: HTML body, which `json.load` then fails to parse — a listing failure that reads as a
#: bug in this script. `probe_api.py` measured that behaviour and every sibling probe
#: sets one; this one did not.
USER_AGENT = "convex-hedge-probe/1.0 (+delta-exchange-payoff)"


def symbols() -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


def describe(gaps: list[float]) -> dict[str, float | int | None]:
    """The five numbers, over whatever subset of gaps was handed in."""
    if not gaps:
        return {
            "count": 0,
            "max": None,
            "p99": None,
            "p95": None,
            "median": None,
            "mean": None,
        }
    ordered = sorted(gaps)
    last = len(ordered) - 1
    return {
        "count": len(ordered),
        "max": round(ordered[-1], 3),
        "p99": round(ordered[min(int(len(ordered) * 0.99), last)], 3),
        "p95": round(ordered[min(int(len(ordered) * 0.95), last)], 3),
        "median": round(statistics.median(ordered), 3),
        "mean": round(statistics.fmean(ordered), 4),
    }


class GapRecorder:
    """A feed sink that keeps the gaps and not the frames.

    Holding 3.6 million `VenueMessage`s to measure the distance between them would be a
    different experiment. Only the gap, the channel, the instant and — since #39 — the
    connection events that fell inside it are kept.
    """

    def __init__(self) -> None:
        self.gaps: list[float] = []
        #: Gaps with **no** connection event inside them: the venue going quiet on a
        #: socket that stayed up. This is the distribution the two staleness bounds are
        #: chosen against; the other kind is a drop, which the controller must react to.
        self.quiet_gaps: list[float] = []
        #: Gaps that contained an open or a close. A reconnect, measured rather than
        #: inferred.
        self.spanning_gaps: list[float] = []
        self.longest: list[tuple[float, float, str, tuple[str, ...]]] = []
        self.channels: Counter[str] = Counter()
        #: `(monotonic, kind, detail)` for every socket open and close, so a gap can be
        #: attributed rather than guessed at.
        self.connection_events: list[tuple[float, str, str]] = []
        self.messages = 0
        self.last: float | None = None

    def mark(self, kind: str, detail: str) -> None:
        """A `DeltaFeed.on_open` / `on_close` listener. Records; never blocks."""
        self.connection_events.append((time.monotonic(), kind, detail))

    def _spanned(self, since: float, until: float) -> tuple[str, ...]:
        """Which connection events fell inside one gap, oldest first."""
        return tuple(
            kind for at, kind, _ in self.connection_events if since < at <= until
        )

    def publish(self, message) -> None:
        now = time.monotonic()
        self.messages += 1
        self.channels[message.channel] += 1
        if self.last is not None:
            gap = now - self.last
            spanned = self._spanned(self.last, now)
            self.gaps.append(gap)
            (self.spanning_gaps if spanned else self.quiet_gaps).append(gap)
            self.longest.append((gap, now, message.channel, spanned))
            self.longest.sort(key=lambda row: row[0], reverse=True)
            del self.longest[20:]
        self.last = now


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    names = symbols()
    recorder = GapRecorder()
    feed = DeltaFeed(recorder)
    # Registered before `run`, so an open in the first millisecond is not missed.
    # `on_open` fires after the subscribe replay, which is the moment the socket can
    # actually deliver, and so is the right edge of any gap it belongs to.
    feed.on_open(lambda detail: recorder.mark("open", detail))
    feed.on_close(lambda detail: recorder.mark("close", detail))
    feed.subscribe(TICKER_CHANNEL, names)
    feed.subscribe(BOOK_CHANNEL, names)

    started_at = datetime.now(UTC)
    started = time.monotonic()
    # **Redials for the whole window.** Before this the tool started `feed.run()` as a
    # task and slept out the window; since #39 that task ends at the first drop, and the
    # summary still reported the full hour. See `tools/_window.py`.
    window = await run_window(feed, seconds)
    elapsed = window.elapsed_seconds

    summary = {
        "run": "tools/measure_quiet_gap.py",
        "started_at": started_at.isoformat(),
        # Measured, not requested. `window.complete` is false when the run ended
        # early, and the gaps below then cover only what was observed.
        "elapsed_seconds": round(elapsed, 1),
        "window": window.summary(),
        "symbols_subscribed": len(names),
        "channels": ["ticker", "ob_l2"],
        "messages": recorder.messages,
        "connections": feed.connections,
        "malformed": feed.malformed,
        "empty_opens": feed.empty_opens,
        "last_error": feed.last_error,
        "gap_seconds": describe(recorder.gaps),
        # The number the bounds are chosen against: no drop inside it.
        "quiet_gap_seconds": describe(recorder.quiet_gaps),
        # Gaps that contained an open or a close, which is a reconnect and not a quiet
        # market. Reported separately rather than excluded silently.
        "spanning_gap_seconds": describe(recorder.spanning_gaps),
        "connection_events": [
            {"at_seconds": round(at - started, 1), "kind": kind, "detail": detail}
            for at, kind, detail in recorder.connection_events
        ],
        "longest_20": [
            {
                "gap": round(gap, 3),
                "at_seconds": round(at - started, 1),
                "channel": channel,
                "spanned": list(spanned),
            }
            for gap, at, channel, spanned in recorder.longest
        ],
        "per_channel": dict(recorder.channels),
    }
    # **Named by the run, not by the script.** A fixed filename means the second run
    # silently destroys the first, and `tools/out/` is gitignored, so what it destroys
    # is unrecoverable — a completed thirty-five-second measurement was one re-run away
    # from being lost while this was being written. `latest.json` is a convenience copy
    # and is the only thing here that is ever overwritten.
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    body = json.dumps(summary, indent=2)
    window.warn_if_truncated()
    (out / f"measure_quiet_gap-{stamp}-{int(elapsed)}s.json").write_text(
        body, encoding="utf-8"
    )
    (out / "measure_quiet_gap-latest.json").write_text(body, encoding="utf-8")
    print(body)


if __name__ == "__main__":
    asyncio.run(main())
