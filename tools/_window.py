"""Keep a one-connection feed dialling for a whole measurement window.

**The defect this exists to refuse.** #39 made `DeltaFeed.run()` *one* connection — dial,
replay, pump, return when the socket ends — because backoff and the reconnect budget moved
up into `controller.ConnectionController`. Every tool in this directory predates that and
started the feed the old way:

    task = asyncio.create_task(feed.run())
    await asyncio.sleep(seconds)

After #39 the first drop finishes that task. The tool then sleeps out the rest of the
window against a feed that is not connected to anything, and the summary still reports
`elapsed_seconds` for the whole window while the data behind it stops at the drop:
`connections` reads 1, the gap distribution covers a fraction of the hour it claims, and
nothing in the output says so. A truncated hour and a clean hour are the same JSON.

That matters more here than a bug in the engine would. `docs/design/quiet-gap.md` records
an hour with **three connections, i.e. two drops** — taken before #39 and still valid. Any
hour taken after it, through the unrepaired tools, would have been silently cut at the
first of those drops and quoted as an hour. Every bound in `controller.md` rests on that
number. A tool that under-reports without saying so is how a project that measures
everything ends up confidently wrong.

So this module does two things and refuses a third:

1. **Redials for the whole window**, with the same backoff rule the controller uses — a
   delay that doubles per attempt and is restored in full by an attempt that delivered.
2. **Records every attempt**, so a drop is evidence in the summary rather than an absence.
3. **Never claims a window it did not observe.** `elapsed_seconds` is measured, not
   requested; `complete` is false when the window ended early and `ended_early_because`
   names the ending.

`engine/tests/test_measure_window.py` is the red-green.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

#: The controller's own values, so a measurement redials the way the engine does rather
#: than on a schedule invented here. `controller.RETRY_DELAY_SECONDS` and
#: `MAX_RETRY_DELAY_SECONDS`; duplicated rather than imported because `tools/` runs
#: against a checkout without the engine installed.
RETRY_DELAY_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 60.0


class OneConnectionFeed(Protocol):
    """What `run_window` needs of a feed. `DeltaFeed` satisfies it."""

    messages: int
    connections: int

    def stop(self) -> None: ...

    async def run(self) -> None: ...


@dataclass
class Attempt:
    """One `run()` — one dial, whether or not it opened."""

    started_at_seconds: float
    ended_at_seconds: float
    messages: int
    error: str | None


@dataclass
class Window:
    """What was actually observed, as against what was asked for."""

    requested_seconds: float
    elapsed_seconds: float = 0.0
    complete: bool = False
    ended_early_because: str | None = None
    attempt_records: list[Attempt] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        return len(self.attempt_records)

    @property
    def redials(self) -> int:
        """Dials after the first. The number the old summary reported as zero."""
        return max(0, self.attempts - 1)

    @property
    def connected_seconds(self) -> float:
        """Time inside an attempt. The window minus this is time spent in backoff."""
        return sum(a.ended_at_seconds - a.started_at_seconds for a in self.attempt_records)

    def summary(self) -> dict[str, Any]:
        """The block every tool embeds in its JSON, under `window`."""
        return {
            "requested_seconds": round(self.requested_seconds, 1),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "connected_seconds": round(self.connected_seconds, 1),
            "complete": self.complete,
            "ended_early_because": self.ended_early_because,
            "attempts": self.attempts,
            "redials": self.redials,
            "attempt_records": [
                {
                    "started_at_seconds": round(a.started_at_seconds, 3),
                    "ended_at_seconds": round(a.ended_at_seconds, 3),
                    "messages": a.messages,
                    "error": a.error,
                }
                for a in self.attempt_records
            ],
        }

    def warn_if_truncated(self) -> None:
        """Say it on stderr as well as in the file.

        A reader who greps the printed summary for the maximum gap will not notice a
        `complete: false` four screens up, and the whole point of the flag is that it
        cannot be missed.
        """
        if self.complete:
            return
        import sys

        print(
            f"\n*** WINDOW ENDED EARLY after {self.elapsed_seconds:.1f}s of "
            f"{self.requested_seconds:.1f}s: {self.ended_early_because}\n"
            "*** These numbers cover the window observed, NOT the window requested. "
            "Do not quote them as the latter.\n",
            file=sys.stderr,
        )


async def run_window(
    feed: OneConnectionFeed,
    seconds: float,
    *,
    dial: Any = None,
    stop: Any = None,
    retry_delay: float = RETRY_DELAY_SECONDS,
    max_retry_delay: float = MAX_RETRY_DELAY_SECONDS,
    clock: Any = time.monotonic,
) -> Window:
    """Dial `feed` for `seconds`, redialling every drop, and report what was observed.

    Returns when the window is up or when the feed stops accepting dials, whichever comes
    first. The feed is stopped on the way out either way, so a caller does no tidying.

    **The window ends the connection, not the venue.** A healthy socket never returns from
    `run()` on its own, so a timer stops the feed at the deadline; without it the tool
    would measure until the venue dropped it rather than for the window it was given,
    which is the same defect pointing the other way.

    `dial` and `stop` override `feed.run` and `feed.stop` for the one caller that drives
    a connection some other way: `measure_store.capture()` runs the engine's whole
    pipeline and dials through `DeltaAdapter.stream(publish)`, which since #39 is one
    connection for the same reason `run()` is. The counters are still read off the feed,
    so the thing dialled and the thing counted may be two objects.
    """
    window = Window(requested_seconds=seconds)
    started = clock()
    open_connection = feed.run if dial is None else dial
    end_connection = feed.stop if stop is None else stop

    async def dial_forever() -> None:
        delay = retry_delay
        while True:
            attempt_started = clock() - started
            before = feed.messages
            # **Recorded in a `finally`, because the last attempt of any window is the
            # one the window itself cuts short.** Appending after `run()` returns loses
            # exactly that attempt to the cancellation below, so the records undercount
            # by one and disagree with the feed's own `connections` — a summary that
            # contradicts itself about how many times it connected is no better than the
            # one that said 1.
            try:
                await open_connection()
            finally:
                window.attempt_records.append(
                    Attempt(
                        started_at_seconds=attempt_started,
                        ended_at_seconds=clock() - started,
                        messages=feed.messages - before,
                        error=getattr(feed, "last_error", None),
                    )
                )
            delivered = feed.messages - before
            # A stop is not a drop. `DeltaFeed.run()` returns immediately once stopped,
            # so redialling would spin; and the window's own timer is what sets that
            # flag at the deadline, which is the ordinary ending rather than an early
            # one. The caller tells the two apart by the clock, below.
            if getattr(feed, "_stopping", False):
                window.ended_early_because = "the feed stopped accepting connections"
                return
            # Restored in full by an attempt that delivered, spent by one that did not —
            # the controller's rule, for the controller's reason: connecting proves
            # nothing, delivering proves the endpoint works.
            delay = retry_delay if delivered else min(delay * 2, max_retry_delay)
            remaining = seconds - (clock() - started)
            if remaining <= 0:
                return
            await asyncio.sleep(min(delay, remaining))

    dialling = asyncio.create_task(dial_forever())
    try:
        await asyncio.wait({dialling}, timeout=seconds)
    finally:
        end_connection()
        dialling.cancel()
        await asyncio.gather(dialling, return_exceptions=True)
        window.elapsed_seconds = clock() - started

    # The window is complete if the clock ran out, whatever the dial loop was doing when
    # it did. A loop that returned early *because the deadline passed* is complete; one
    # that returned because the feed refused to dial is not.
    if window.elapsed_seconds >= seconds:
        window.complete = True
        window.ended_early_because = None
    else:
        window.complete = False
        if window.ended_early_because is None:
            window.ended_early_because = "the dial loop ended before the window did"
    return window
