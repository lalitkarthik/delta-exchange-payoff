"""The measurement window every tool in `tools/` sleeps out.

**Why a suite that tests the engine tests a script directory.** Every number this project
quotes was produced by `tools/measure_*.py`, and #39 broke all of them at once without
touching one of them: `DeltaFeed.run()` became *one* connection that returns when the
socket ends, and each tool started it with `asyncio.create_task(feed.run())` and then
slept out the window. After the first drop the task is finished, the tool sleeps out the
remainder against a dead feed, and the summary still reports the whole window.

That failure is invisible in the output — a truncated hour and a clean hour are the same
JSON — so it has to be pinned by a test rather than noticed by a reader.
`tools/_window.py` is the shared fix and this is its red-green.

The seam is a fake feed with the same one-connection contract as `DeltaFeed`: `run()`
dials, delivers, and returns. Real `asyncio`, real clock, windows of a few hundredths of
a second — the concurrency is the thing under test, so it is not faked away.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from _window import run_window  # noqa: E402


class OneConnectionFeed:
    """A feed that ends its connection the way `DeltaFeed` has since #39.

    `run()` opens, delivers `deliver` messages, and **returns**. It does not loop. That
    is the whole contract the tools got wrong.
    """

    def __init__(self, connection_seconds: float = 0.004, deliver: int = 1) -> None:
        self.connection_seconds = connection_seconds
        self.deliver = deliver
        self.messages = 0
        self.connections = 0
        self.last_error: str | None = None
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run(self) -> None:
        if self._stopping:
            return
        self.connections += 1
        await asyncio.sleep(self.connection_seconds)
        self.messages += self.deliver


class FeedThatStopsItself(OneConnectionFeed):
    """Ends the window early: after `after` attempts it refuses to dial again.

    Stands for the endings a tool cannot measure through — the feed stopped, the process
    told to wind up — as opposed to a drop, which it must redial.
    """

    def __init__(self, after: int = 2) -> None:
        super().__init__(connection_seconds=0.001, deliver=0)
        self.after = after

    async def run(self) -> None:
        if self._stopping:
            return
        if self.connections >= self.after:
            self._stopping = True
            return
        await super().run()


def test_a_window_redials_after_every_drop_instead_of_ending_at_the_first() -> None:
    """**The defect itself.** A feed that drops every 4 ms, across a 120 ms window.

    The broken tools produced exactly one connection here and reported the full window.
    A window that redials produces many, and `connections` on the feed is the count a
    summary can honestly print.
    """
    feed = OneConnectionFeed(connection_seconds=0.004)
    window = asyncio.run(run_window(feed, 0.12, retry_delay=0.001, max_retry_delay=0.01))

    assert window.attempts > 1, (
        "the window ended at the first drop — this is the #39 defect, and every number "
        f"taken through it is truncated (attempts={window.attempts})"
    )
    assert feed.connections == window.attempts
    assert window.redials == window.attempts - 1
    assert window.complete is True
    assert window.ended_early_because is None


def test_a_window_reports_the_time_it_observed_not_the_time_it_asked_for() -> None:
    """**The rule the summary rests on:** never report a window you did not observe.

    A feed that gives up after two attempts cannot fill a one-second window, so the
    window says so — `complete` is false, the reason is named, and `elapsed_seconds` is
    the time actually spent rather than the 1.0 that was requested.
    """
    feed = FeedThatStopsItself(after=2)
    window = asyncio.run(run_window(feed, 1.0, retry_delay=0.001, max_retry_delay=0.01))

    assert window.complete is False
    assert window.ended_early_because is not None
    assert window.elapsed_seconds < window.requested_seconds
    summary = window.summary()
    assert summary["complete"] is False
    assert summary["requested_seconds"] == 1.0
    assert summary["elapsed_seconds"] < 1.0


def test_the_summary_of_a_short_window_carries_every_attempt() -> None:
    """A drop is evidence, so each attempt is recorded with what it delivered.

    `connections: 1` on an hour that dropped twice was the old summary's answer. The
    per-attempt record is what makes a truncated run impossible to mistake for a clean
    one on inspection.
    """
    feed = OneConnectionFeed(connection_seconds=0.004, deliver=7)
    window = asyncio.run(run_window(feed, 0.08, retry_delay=0.001, max_retry_delay=0.01))

    summary = window.summary()
    records = summary["attempt_records"]
    assert len(records) == window.attempts
    # Every attempt that ran to its own end delivered its seven. The **last** record is
    # the attempt the window cut short, which delivered nothing and is recorded anyway —
    # dropping it would make the records disagree with the feed's own `connections`.
    assert all(record["messages"] == 7 for record in records[:-1])
    assert sum(r["messages"] for r in records) == feed.messages
    for record in records:
        assert record["ended_at_seconds"] >= record["started_at_seconds"]


def test_a_window_stops_the_feed_when_the_clock_runs_out() -> None:
    """A healthy socket never returns on its own, so the window is what ends it.

    Without this the tool would run until the venue dropped it rather than for the
    window it was asked for, which is the same defect pointing the other way.
    """
    feed = OneConnectionFeed(connection_seconds=10.0)
    window = asyncio.run(run_window(feed, 0.05, retry_delay=0.001))

    assert window.complete is True
    assert feed._stopping is True
    assert window.elapsed_seconds == pytest.approx(0.05, abs=0.06)


def test_a_window_can_dial_through_something_other_than_run() -> None:
    """`measure_store.capture()` drives `DeltaAdapter.stream(publish)`, not `feed.run()`.

    Since #39 `stream` is one connection for the same reason `run` is, so it has the same
    defect and needs the same window. The counters still come off the feed, so the dial
    and the thing being counted are allowed to be two different objects.
    """
    feed = OneConnectionFeed(connection_seconds=0.004)
    published: list[str] = []

    async def dial() -> None:
        published.append("dialled")
        await feed.run()

    stopped: list[str] = []
    window = asyncio.run(
        run_window(
            feed,
            0.08,
            dial=dial,
            stop=lambda: stopped.append("stopped") or feed.stop(),
            retry_delay=0.001,
            max_retry_delay=0.01,
        )
    )

    assert window.attempts > 1
    assert len(published) == window.attempts
    assert stopped == ["stopped"]
