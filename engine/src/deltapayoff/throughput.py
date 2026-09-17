"""Periodic "what moved since last time" records, and the gate on the per-record ones.

**Why this module exists at all.** The engine's counters were always there --
`DeltaFeed.messages`, `FanOut.stats()`, `RedisBus.publisher()` -- and nothing ever said
them out loud. So the log could tell you a connection was `connected` and could not tell
you whether one byte had crossed it since, which is the same "healthy badge, zero
messages" failure `adapters/delta_socket.py` is written against, one layer up.

**Why a summary and not a line per message.** `measured` 2026-09-02, over a 40-minute
capture of 585 BTC contracts (`tools/capture_ws.py`): 307,301 frames a minute,
**5,122 a second**, `ob_l1` alone 4,874. At roughly 200 bytes a JSON record that is 1 MB/s
of log for one underlying before the bus multiplies it by the number of consumers, and the
write would land on the socket read loop that the architecture documents as never allowed
to block -- a slow flush there fills the receive buffer and Delta closes the connection.

So the per-record events exist, and they are **off unless `DELTA_LOG_TRACE=1`**. Turn them
on to follow one contract for a minute; leave them off to run. What stays on is six
records a minute per reporter, which is the affordable answer to the only question anybody
was actually asking: is anything arriving, and is anyone falling behind.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import log_events
from .logging_setup import log_event

logger = logging.getLogger(__name__)

#: Seconds between reports. Ten is `assumed`, not measured: short enough that a dead feed
#: is obvious while you watch the page, long enough that the log stays six records a
#: minute per reporter against the feed's three hundred thousand.
REPORT_SECONDS = float(os.environ.get("DELTA_THROUGHPUT_SECONDS", "10"))

#: The truthy spellings of `DELTA_LOG_TRACE`. Read once at import: this is consulted on
#: the per-frame path, and an `os.environ` lookup five thousand times a second to answer a
#: question that cannot change is the kind of cost that gets a trace flag blamed for
#: something it did not do.
_OFF = ("", "0", "false", "no")
TRACE = os.environ.get("DELTA_LOG_TRACE", "").strip().lower() not in _OFF


class Counters:
    """Cumulative counters, reported as the difference since the last report.

    The engine's counters only ever climb, and "4,812 frames since the last report" is the
    sentence an operator wants; "51,204,118 frames" is one they have to subtract by hand
    and will get wrong at three in the morning.
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def since(self, **current: float) -> dict[str, float]:
        delta = {name: value - self._last.get(name, 0) for name, value in current.items()}
        self._last.update(current)
        return delta


async def report_forever(
    emit: Callable[[float], None],
    *,
    interval: float = REPORT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Call `emit(seconds_since_last_report)` every `interval` seconds, forever.

    **A failing report must never stop the reporting.** It is a diagnostic, and a
    diagnostic that dies silently on one bad value leaves exactly the blind spot it was
    added to close -- so the exception is logged as `engine.error` and the loop goes round
    again. Cancellation is the one thing that does end it, and it is allowed straight
    through so shutdown is not delayed by a sleep.
    """
    last = clock()
    while True:
        await asyncio.sleep(interval)
        now = clock()
        elapsed, last = now - last, now
        try:
            emit(elapsed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "a throughput report failed; reporting continues",
                exc_info=True,
            )


def feed_report(adapter: Any) -> Callable[[float], None]:
    """Report what the venue socket read. Works on anything carrying the counters.

    Duck-typed rather than importing `DeltaFeed`: the fake adapter the smoke stack runs
    on keeps the same counters, and a reporter that only worked against the real socket
    would be untestable without the network the test suite forbids.
    """
    counters = Counters()

    def emit(elapsed: float) -> None:
        feed = getattr(adapter, "feed", adapter)
        moved = counters.since(
            frames=getattr(feed, "messages", 0),
            bytes=getattr(feed, "bytes_read", 0),
            malformed=getattr(feed, "malformed", 0),
        )
        log_event(
            logger,
            logging.INFO,
            log_events.FEED_THROUGHPUT,
            "feed read %d frames in %.1fs",
            int(moved["frames"]),
            elapsed,
            # **Named explicitly, because this module is neutral.** A record belongs to
            # the subsystem it is *about*, not the one that emitted it, and
            # `COMPONENT_BY_MODULE` can only see the logger -- which is this file for
            # both reporters. Without these two lines `tools/logs.py feed` would omit the
            # one record that says whether the feed is alive.
            component="feed",
            venue=getattr(adapter, "name", None),
            frames=int(moved["frames"]),
            bytes=int(moved["bytes"]),
            malformed=int(moved["malformed"]),
            frames_per_second=round(moved["frames"] / elapsed, 1) if elapsed else None,
            since_seconds=round(elapsed, 1),
        )

    return emit


def bus_report(bus: Any) -> Callable[[float], None]:
    """Report what the bus moved. Reads `stats()`, which both buses already answer.

    One record for the whole bus rather than one per consumer: the question is almost
    always "is anyone behind", and four consumers on four lines is four times the log to
    answer it, with the comparison between them left to the reader.
    """
    counters = Counters()

    def emit(elapsed: float) -> None:
        stats: Mapping[str, Mapping[str, Any]] = bus.stats()
        moved = counters.since(
            published=getattr(bus, "published", 0),
            **{f"offered:{name}": row.get("offered", 0) for name, row in stats.items()},
            **{f"consumed:{name}": row.get("consumed", 0) for name, row in stats.items()},
            **{f"dropped:{name}": row.get("dropped", 0) for name, row in stats.items()},
        )
        # `offered` is what the bus handed to the queue and `consumed` what the consumer
        # actually took off it -- two different numbers, and the gap between them over a
        # window is the backlog forming. `consumed` is countable only because every reader
        # goes through `Subscription.take()`; a bare `queue.get()` is invisible here.
        consumers = {
            name: {
                "offered": int(moved[f"offered:{name}"]),
                "consumed": int(moved[f"consumed:{name}"]),
                "dropped": int(moved[f"dropped:{name}"]),
                "queued": int(row.get("queued", 0)),
            }
            for name, row in stats.items()
        }
        behind = [name for name, row in consumers.items() if row["queued"] > 0]
        log_event(
            logger,
            logging.INFO,
            log_events.BUS_THROUGHPUT,
            "bus moved %d events in %.1fs to %d consumers%s",
            int(moved["published"]),
            elapsed,
            len(consumers),
            f"; behind: {', '.join(behind)}" if behind else "",
            component="bus",
            published=int(moved["published"]),
            events_per_second=round(moved["published"] / elapsed, 1) if elapsed else None,
            consumers=consumers,
            since_seconds=round(elapsed, 1),
        )

    return emit


def start(
    tasks: list[asyncio.Task],
    *,
    adapter: Any = None,
    bus: Any = None,
    interval: float = REPORT_SECONDS,
) -> None:
    """Append a reporter task per subject that exists. Called from an entrypoint's start.

    Taking the task list rather than returning the tasks keeps the cancellation story
    where it already is: every caller has a list it cancels on shutdown, and a reporter
    that was not in it would outlive the thing it reports on.
    """
    if adapter is not None:
        tasks.append(
            asyncio.create_task(
                report_forever(feed_report(adapter), interval=interval),
                name="feed-throughput",
            )
        )
    if bus is not None and hasattr(bus, "stats"):
        tasks.append(
            asyncio.create_task(
                report_forever(bus_report(bus), interval=interval),
                name="bus-throughput",
            )
        )


def _self_check() -> None:
    counters = Counters()
    assert counters.since(a=10) == {"a": 10}
    assert counters.since(a=13) == {"a": 3}
    assert counters.since(a=13) == {"a": 0}
    assert counters.since(b=5) == {"b": 5}, "an unseen name starts from zero"
    print("ok")


if __name__ == "__main__":
    _self_check()
