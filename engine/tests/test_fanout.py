"""The fan-out: one producer, many independent consumers.

The socket handler must never be inside a consumer's code. If it is, a slow disk flush
or a raised exception in one consumer stops the handler reading the socket, the operating
system's receive buffer fills, and **Delta closes the connection**. Nobody enforced a
limit; we simply failed to keep up. That is the failure this module exists to prevent,
and `test_a_stalled_and_a_crashed_consumer_do_not_disturb_the_rest` is the sabotage that
proves it.

Everything here runs on `asyncio.run` rather than through a plugin, so the suite keeps
its current dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from deltapayoff import fanout
from deltapayoff.events import ControlCommand
from deltapayoff.fanout import FanOut


def run(coro):
    return asyncio.run(coro)


def test_every_subscriber_receives_every_message() -> None:
    """The baseline. Two consumers, nobody slow, nothing lost."""

    async def scenario():
        bus = FanOut()
        first = bus.subscribe("first", maxsize=10)
        second = bus.subscribe("second", maxsize=10)

        for n in range(5):
            bus.publish(n)

        return (
            [first.queue.get_nowait() for _ in range(5)],
            [second.queue.get_nowait() for _ in range(5)],
        )

    a, b = run(scenario())
    assert a == [0, 1, 2, 3, 4]
    assert b == [0, 1, 2, 3, 4]


def test_a_full_queue_discards_the_oldest_and_keeps_the_newest() -> None:
    """The backpressure decision, asserted rather than described.

    A quote from four seconds ago is not slightly worse than the current one; it is
    worthless, because nobody trades on it. So the queue keeps the newest three of six
    and throws the first three away. Dropping the *newest* would be the intuitive
    implementation and exactly wrong.
    """

    async def scenario():
        bus = FanOut()
        slow = bus.subscribe("slow", maxsize=3)
        for n in range(6):
            bus.publish(n)
        return [slow.queue.get_nowait() for _ in range(3)]

    assert run(scenario()) == [3, 4, 5]


def test_drops_are_counted() -> None:
    """A silent drop is a lie.

    This project has twice been damaged by numbers that looked plausible and were not —
    candles padded with fabricated prices, and an inverse-settlement premise nobody
    measured. A discarded message with no counter is the same failure shape.
    """

    async def scenario():
        bus = FanOut()
        slow = bus.subscribe("slow", maxsize=3)
        for n in range(10):
            bus.publish(n)
        return slow.dropped, bus.stats()

    dropped, stats = run(scenario())
    assert dropped == 7
    assert stats["slow"]["dropped"] == 7
    # Ten reached the queue; seven of those were later evicted to make room. Three
    # remain readable. The bus counts what it offered, not what a consumer read — it
    # cannot know the latter, and claiming to would be the silent-drop lie again.
    assert stats["slow"]["offered"] == 10
    assert stats["slow"]["queued"] == 3


def test_publishing_never_awaits() -> None:
    """`publish` must be synchronous and non-blocking.

    If it could await, the handler would be suspended inside it and the receive buffer
    would fill while a consumer was slow — the exact failure this module prevents.
    Asserted by publishing far more than every queue can hold and requiring it to return.
    """

    async def scenario():
        bus = FanOut()
        bus.subscribe("tiny", maxsize=1)
        bus.subscribe("also_tiny", maxsize=1)
        for n in range(10_000):
            bus.publish(n)
        return True

    assert run(scenario()) is True


def test_a_stalled_and_a_crashed_consumer_do_not_disturb_the_rest() -> None:
    """The criterion, as a sabotage.

    Three consumers. One never reads at all. One is cancelled mid-stream. The third must
    still receive every message, and publishing must never have blocked.
    """

    async def scenario():
        bus = FanOut()
        healthy = bus.subscribe("healthy", maxsize=1000)
        stalled = bus.subscribe("stalled", maxsize=5)
        doomed = bus.subscribe("doomed", maxsize=1000)

        received: list[int] = []

        async def read_forever(subscription, sink):
            while True:
                sink.append(await subscription.queue.get())

        healthy_task = asyncio.create_task(read_forever(healthy, received))
        doomed_task = asyncio.create_task(read_forever(doomed, []))
        # `stalled` gets no reader at all — its queue simply fills and overflows.

        for n in range(50):
            bus.publish(n)
            await asyncio.sleep(0)

        doomed_task.cancel()  # the consumer that dies
        await asyncio.gather(doomed_task, return_exceptions=True)

        for n in range(50, 100):
            bus.publish(n)
            await asyncio.sleep(0)

        await asyncio.sleep(0.05)
        healthy_task.cancel()
        await asyncio.gather(healthy_task, return_exceptions=True)
        return received, stalled.dropped, bus.stats()

    received, stalled_drops, stats = run(scenario())

    assert received == list(range(100)), "the healthy consumer lost messages"
    assert stalled_drops == 95, "the stalled consumer should have overflowed"
    assert stats["healthy"]["dropped"] == 0


def test_unsubscribing_stops_delivery_without_touching_the_others() -> None:
    """Removing a consumer is an ordinary operation, not a failure. The UI disconnects
    every time someone closes a browser tab."""

    async def scenario():
        bus = FanOut()
        staying = bus.subscribe("staying", maxsize=10)
        leaving = bus.subscribe("leaving", maxsize=10)

        bus.publish("before")
        bus.unsubscribe(leaving)
        bus.publish("after")

        return staying.queue.qsize(), leaving.queue.qsize(), list(bus.stats())

    staying_size, leaving_size, names = run(scenario())
    assert staying_size == 2
    assert leaving_size == 1
    assert names == ["staying"]


def test_a_subscriber_needs_a_bounded_queue() -> None:
    """An unbounded queue is a memory leak with good manners. It does not fail when the
    consumer falls behind — it fails an hour later, somewhere else, out of memory."""
    bus = FanOut()

    with pytest.raises(ValueError):
        bus.subscribe("greedy", maxsize=0)


def test_a_lossless_consumer_that_stops_reading_during_a_burst_loses_nothing() -> None:
    """The inverse of the sabotage above, and #5's whole reason for existing.

    Drop-oldest is right for a screen and wrong for a store, and not because a drop
    leaves a hole — under one-minute bars a dropped tick perturbs a bar rather than
    removing a record. The problem is that drops happen *under load*, load is when price
    moves fastest, and so drop-oldest **systematically shaves the highs and lows**, which
    are the only columns the bars exist to capture. A bias, not noise, and invisible in
    the output.

    So: a consumer that stops reading for the whole of a burst far larger than its
    watermark must, on resuming, receive every message in order.
    """

    async def scenario():
        bus = FanOut()
        writer = bus.subscribe("writer", maxsize=5, lossless=True)
        received: list[int] = []

        reading = asyncio.Event()
        reading.set()

        async def read_while_allowed():
            while True:
                await reading.wait()
                received.append(await writer.queue.get())

        task = asyncio.create_task(read_while_allowed())
        for n in range(10):
            bus.publish(n)
            await asyncio.sleep(0)

        reading.clear()  # the consumer stalls for the whole burst
        for n in range(10, 500):
            bus.publish(n)
        await asyncio.sleep(0)

        reading.set()  # and resumes
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return received, writer.dropped, bus.stats()

    received, dropped, stats = run(scenario())
    assert received == list(range(500)), "a lossless consumer lost messages"
    assert dropped == 0
    assert stats["writer"]["dropped"] == 0


def test_a_lossless_backlog_is_counted_rather_than_silently_absorbed() -> None:
    """An unbounded queue is a memory leak with good manners *unless somebody counts*.

    So `maxsize` does not stop a lossless consumer, it watermarks one: every offer made
    while the queue is already at or above it is counted, and the deepest backlog is
    kept. That turns "the writer is falling behind" from an out-of-memory an hour later
    into a number available now.
    """

    async def scenario():
        bus = FanOut()
        writer = bus.subscribe("writer", maxsize=4, lossless=True)
        for n in range(10):
            bus.publish(n)
        return writer, bus.stats()

    writer, stats = run(scenario())
    assert writer.dropped == 0
    assert writer.queue.qsize() == 10, "nothing may be discarded"
    # Offers 0..3 fit under the watermark; offers 4..9 were made with the queue already
    # at or above it. Six, and it must not be ten — a watermark that trips on the first
    # message would make the counter useless.
    assert stats["writer"]["over_capacity"] == 6
    assert stats["writer"]["backlog_peak"] == 10
    assert stats["writer"]["lossless"] is True


def test_drop_oldest_remains_the_default() -> None:
    """The existing consumers must not silently change policy because #5 arrived."""

    async def scenario():
        bus = FanOut()
        screen = bus.subscribe("screen", maxsize=3)
        for n in range(6):
            bus.publish(n)
        drained = [screen.queue.get_nowait() for _ in range(3)]
        return screen.lossless, screen.dropped, drained

    lossless, dropped, drained = run(scenario())
    assert lossless is False
    assert dropped == 3
    assert drained == [3, 4, 5]


def test_a_lossless_drop_is_impossible_but_logged_if_it_ever_happens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#42's acceptance line: a drop on a lossless queue is error-level and named
    `queue.drop`.

    `subscribe()`'s own `maxsize=0` makes this unreachable through the public API — this
    test reaches around it, replacing the queue with a bounded one, precisely to prove
    the guard fires rather than merely existing.
    """
    caplog.set_level(logging.ERROR, logger="deltapayoff.fanout")
    bus = FanOut()
    writer = bus.subscribe("writer", maxsize=4, lossless=True)
    # Force the "impossible" branch: a bounded queue, already full, under the lossless
    # code path that believes it can never be.
    writer.queue = asyncio.Queue(maxsize=1)
    writer.queue.put_nowait("already here")

    bus.publish("dropped")

    assert writer.dropped == 1
    records = [r for r in caplog.records if r.event == "queue.drop"]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert records[0].levelno == logging.ERROR
    assert "writer" in records[0].getMessage()


def test_take_is_the_one_way_a_consumer_reads_and_it_counts(caplog) -> None:
    """The bus can count what it handed to a queue; it cannot see a `get()`.

    So `consumed` — and with it `bus.consume`, `bus.release` and the `offered`-versus-
    `consumed` gap in `bus.throughput` — exists only because every consumer in the engine
    goes through `take()`. A bare `queue.get()` is invisible here, which is why there are
    none left in `engine/src`.
    """

    async def drive() -> tuple[object, int, int]:
        bus = FanOut()
        subscription = bus.subscribe("reader", maxsize=10)
        record = object()
        bus.publish(record)
        got = await subscription.take()
        return got, subscription.consumed, subscription.queue.qsize()

    got, consumed, queued = asyncio.run(drive())

    assert consumed == 1
    assert queued == 0
    assert got is not None


def test_the_per_record_trace_is_silent_unless_it_is_turned_on(
    caplog, monkeypatch
) -> None:
    """`measured` 5,122 frames a second: a line per record is 1 MB/s of log for one
    underlying, multiplied by the consumer count, written on the path the socket owner
    documents as never allowed to block. It exists, and it is off."""
    monkeypatch.setattr(fanout, "TRACE", False)

    async def drive() -> None:
        bus = FanOut()
        subscription = bus.subscribe("reader", maxsize=10)
        bus.publish(object())
        await subscription.take()

    with caplog.at_level(logging.DEBUG, logger="deltapayoff.fanout"):
        asyncio.run(drive())

    assert [r for r in caplog.records if r.event.startswith("bus.")] == []


def test_the_trace_says_enter_read_and_release_when_it_is_turned_on(
    caplog, monkeypatch
) -> None:
    """The three moments of one record's life on the bus, in order.

    On this bus each subscriber holds its own copy, so the read *is* the release — they
    are one `take()` apart rather than separated by an acknowledgement, which is the
    difference from Redis Streams that `logging-catalogue.md` records.
    """
    monkeypatch.setattr(fanout, "TRACE", True)

    async def drive() -> None:
        bus = FanOut()
        subscription = bus.subscribe("reader", maxsize=10)
        bus.publish(
            ControlCommand(
                event_id="0f7c2a9e5b1d4c8fa3e6b0d2c4f18a97",
                ts_venue=datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
                ts_received=datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
                source="operator",
                adapter="delta",
                command="pause",
            )
        )
        await subscription.take()

    with caplog.at_level(logging.DEBUG, logger="deltapayoff.fanout"):
        asyncio.run(drive())

    events = [r.event for r in caplog.records if r.event.startswith("bus.")]
    assert events == ["bus.publish", "bus.consume", "bus.release"]
    published = next(r for r in caplog.records if r.event == "bus.publish")
    assert published.subscribers == 1
    assert published.event_type == "control.command"
    consumed = next(r for r in caplog.records if r.event == "bus.consume")
    assert consumed.subscriber == "reader"
    assert consumed.queued == 0


def test_a_drained_backlog_is_counted_as_read_not_as_a_stall() -> None:
    """`take_nowait` is the drain loop's counterpart, and it has to count too.

    `BarWriter.run` takes one record awaited and then drains whatever else is queued in a
    tight loop — which is exactly what a lossless consumer's backlog is made of. Counting
    the awaited read and not the drained ones would report a store that had fallen behind
    as one that had stopped consuming altogether, which is the opposite diagnosis.
    """

    async def drive() -> tuple[int, int]:
        bus = FanOut()
        subscription = bus.subscribe("bar-writer", maxsize=10, lossless=True)
        for _ in range(5):
            bus.publish(object())
        await subscription.take()
        drained = 0
        while True:
            try:
                subscription.take_nowait()
            except asyncio.QueueEmpty:
                break
            drained += 1
        return subscription.consumed, drained

    consumed, drained = asyncio.run(drive())

    assert drained == 4
    assert consumed == 5, "the drained four are reads, not a stall"
