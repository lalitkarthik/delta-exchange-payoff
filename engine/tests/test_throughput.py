"""The periodic throughput records, and the gate on the per-record ones.

`docs/design/lld/logging.md` section 3b is the design. No network and no real sleeping
beyond the tiny intervals here: `report_forever` takes its clock, and every subject is
duck-typed, so the whole module is exercised against plain objects.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from deltapayoff import log_events, throughput
from deltapayoff.fanout import FanOut


class _Feed:
    def __init__(self) -> None:
        self.name = "DELTA"
        self.messages = 0
        self.bytes_read = 0
        self.malformed = 0


def test_counters_report_the_difference_not_the_total() -> None:
    """"4,812 frames since the last report" is the sentence an operator wants; the
    engine's counters only climb, and a running total is one they have to subtract by
    hand and will get wrong at three in the morning."""
    counters = throughput.Counters()

    assert counters.since(frames=10) == {"frames": 10}
    assert counters.since(frames=13) == {"frames": 3}
    assert counters.since(frames=13) == {"frames": 0}, "a quiet window reports zero"


def test_a_feed_report_names_the_frames_read_since_the_last_one(caplog) -> None:
    """The record that distinguishes a quiet market from a dead socket. Nothing else in
    the log draws that line: `feed.transition` says `connected` either way."""
    feed = _Feed()
    emit = throughput.feed_report(feed)
    with caplog.at_level(logging.INFO, logger="deltapayoff.throughput"):
        emit(10.0)  # the first report, from a standing start
        feed.messages, feed.bytes_read = 4812, 962_400
        emit(10.0)

    records = [r for r in caplog.records if r.event == log_events.FEED_THROUGHPUT]
    assert len(records) == 2
    assert records[0].frames == 0, "nothing had arrived yet, and it says so"
    assert records[1].frames == 4812
    assert records[1].bytes == 962_400
    assert records[1].frames_per_second == 481.2
    assert records[1].venue == "DELTA"


def test_a_bus_report_names_each_consumer_and_who_is_behind(caplog) -> None:
    """`offered` is what the bus handed over and `consumed` what was taken off; the gap
    between them is a backlog forming, and `queued` is how big it is right now."""
    bus = FanOut()
    fast = bus.subscribe("fast", maxsize=10)
    bus.subscribe("slow", maxsize=10)  # deliberately never read, so it falls behind
    emit = throughput.bus_report(bus)

    async def drive() -> None:
        for _ in range(3):
            bus.publish(object())
        await fast.take()
        await fast.take()
        await fast.take()

    asyncio.run(drive())
    with caplog.at_level(logging.INFO, logger="deltapayoff.throughput"):
        emit(10.0)

    record = next(r for r in caplog.records if r.event == log_events.BUS_THROUGHPUT)
    assert record.published == 3
    assert record.consumers["fast"] == {
        "offered": 3,
        "consumed": 3,
        "dropped": 0,
        "queued": 0,
    }
    assert record.consumers["slow"]["consumed"] == 0
    assert record.consumers["slow"]["queued"] == 3, "nobody has read it"
    assert "behind: slow" in record.getMessage()


def test_a_failing_report_is_logged_and_the_reporting_continues(caplog) -> None:
    """A diagnostic that dies silently on one bad value leaves exactly the blind spot it
    was added to close."""
    calls: list[float] = []

    def emit(elapsed: float) -> None:
        calls.append(elapsed)
        if len(calls) == 1:
            raise ValueError("the first report is bad")

    async def drive() -> None:
        task = asyncio.create_task(throughput.report_forever(emit, interval=0.01))
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.throughput"):
        asyncio.run(drive())

    assert len(calls) >= 3, "one bad report did not stop the loop"
    assert any(r.event == log_events.ENGINE_ERROR for r in caplog.records)


def test_report_forever_stops_on_cancellation_rather_than_swallowing_it() -> None:
    """The catch-all above must not eat a cancellation, or shutdown waits on a sleep."""

    async def drive() -> float:
        task = asyncio.create_task(
            throughput.report_forever(lambda _: None, interval=60.0)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return 0.0

    asyncio.run(drive())


def test_start_adds_a_reporter_only_for_a_subject_that_exists() -> None:
    """The split api holds no socket, so it gets a bus reporter and no feed one — and a
    reporter for a subject that is not there would report zero forever and read as a
    feed that has gone quiet."""

    async def drive() -> tuple[set[str], set[str]]:
        both: list[asyncio.Task] = []
        throughput.start(both, adapter=_Feed(), bus=FanOut(), interval=60.0)
        bus_only: list[asyncio.Task] = []
        throughput.start(bus_only, bus=FanOut(), interval=60.0)
        names = ({t.get_name() for t in both}, {t.get_name() for t in bus_only})
        for task in both + bus_only:
            task.cancel()
        await asyncio.gather(*both, *bus_only, return_exceptions=True)
        return names

    both, bus_only = asyncio.run(drive())

    assert both == {"feed-throughput", "bus-throughput"}
    assert bus_only == {"bus-throughput"}


def test_a_bus_with_no_stats_gets_no_reporter() -> None:
    """Every test double of a bus in this suite would otherwise crash a reporter."""

    class Bare:
        pass

    async def drive() -> list[asyncio.Task]:
        tasks: list[asyncio.Task] = []
        throughput.start(tasks, bus=Bare(), interval=60.0)
        return tasks

    assert asyncio.run(drive()) == []


def test_the_monolith_starts_both_reporters_over_its_own_socket_and_bus(
    monkeypatch, caplog
) -> None:
    """The wiring, not the reporters: `start_feed_stack` must put both tasks on the list
    it later cancels. A reporter constructed but never started is the silence this whole
    thing was added to end, and it would look exactly like a healthy quiet market.
    """
    from deltapayoff import main

    class _Adapter:
        name = "DELTA"
        underlyings = ("BTC",)

        def __init__(self) -> None:
            self.feed = _Feed()

        def subscribe(self, symbols) -> None:
            return None

        async def instruments(self, underlying: str) -> list[object]:
            return []

    class _Runnable:
        async def run(self) -> None:
            await asyncio.Event().wait()

        def sample_chains(self, chains) -> None:  # pragma: no cover - never called
            return None

    class _Supervisor:
        started = False

        def start(self) -> None:
            type(self).started = True

    adapter = _Adapter()
    bus = FanOut()
    runnable = _Runnable()
    stack = main.FeedStack(
        events=bus,
        stream=runnable,
        writer=runnable,
        adapter=adapter,
        supervisor=_Supervisor(),
        feed_cache=None,
    )

    async def drive() -> set[str]:
        await main.start_feed_stack(stack)
        names = {task.get_name() for task in stack.tasks}
        for task in stack.tasks:
            task.cancel()
        await asyncio.gather(*stack.tasks, return_exceptions=True)
        return names

    monkeypatch.setattr(throughput, "REPORT_SECONDS", 60.0)
    names = asyncio.run(drive())

    assert "feed-throughput" in names, "nothing would ever say the feed was alive"
    assert "bus-throughput" in names


def test_a_report_is_filed_under_the_subsystem_it_is_about(caplog) -> None:
    """`component` is derived from the logger, and both reporters live in this one
    module — so without an explicit name every throughput record would file itself under
    the process, and `tools/logs.py feed` would omit the single record that says whether
    the feed is alive."""
    with caplog.at_level(logging.INFO, logger="deltapayoff.throughput"):
        throughput.feed_report(_Feed())(10.0)
        throughput.bus_report(FanOut())(10.0)

    by_event = {r.event: r.component for r in caplog.records}
    assert by_event[log_events.FEED_THROUGHPUT] == "feed"
    assert by_event[log_events.BUS_THROUGHPUT] == "bus"
