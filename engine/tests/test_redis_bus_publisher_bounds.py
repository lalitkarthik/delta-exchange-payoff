"""The publisher's two promises about a batch it cannot get rid of, both under test.

**Neither branch had ever executed.** Line coverage over 645 tests in the bus, store,
connector, composition and alert modules, plus `grep -rn "max_outbox\\|outbox_dropped"
engine/tests/ tools/` returning nothing, found `publish`'s eviction and `flush`'s failed
batch unreached by anything (#111, the audit comment). The module docstring makes a
promise about each of them:

* *"An outbox is bounded or it is a memory leak with good manners ... past `max_outbox`
  the oldest entries go and are counted"* -- `DEFAULT_MAX_OUTBOX = 200_000`, configured by
  no test, driven by no test, asserted by no test.
* *"A failed batch is **put back**, not discarded: Redis being briefly unreachable is a
  retry, and losing a batch to it silently would be the drop this bus refuses."* The only
  assertion anywhere near it was `test_bus_contract.py`'s `stats["failures"] == 0`, which
  is the branch not being taken.

They are the publisher's half of #111's question. #111 is a **reader** handed a batch it
never delivered; these are the **publisher** holding a batch it cannot write. The reader's
answer is that the batch is still in Redis and can be read again. The publisher has no
such answer -- its batch exists nowhere else -- which is why one drops loudly at a bound
and the other retries forever, and why #103 was right that the two loops are not
symmetric.

No wall clock and no sleeps: the flusher task is never started here. `flush()` is awaited
directly, which is what makes "the batch is still in the outbox afterwards" an assertion
about the method rather than a race against a loop.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from deltapayoff.events import Instrument, OptionQuote, Right
from deltapayoff.events.redis_wire import stream_name
from deltapayoff.redis_bus import DEFAULT_MAX_OUTBOX, BusConfig, RedisBus

TS = datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc)
INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 9, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270926",
)
QUOTE_STREAM = stream_name(
    OptionQuote(source="delta", ts_received=TS, instrument=INSTRUMENT, bid=1.0)
)

#: The message the live Redis produced, quoted from `dxp-store`'s preserved log.
TIMEOUT_MESSAGE = "Timeout reading from redis:6379"


def quote(bid: float) -> OptionQuote:
    return OptionQuote(source="delta", ts_received=TS, instrument=INSTRUMENT, bid=bid)


class Unwritable:
    """`fakeredis`, with the batch write refused on command.

    The wrap is on `pipeline` rather than on `execute`, because that is the object
    `flush` builds and hands the batch to. Everything else is the real fake, so the
    entries that do get written are really in the stream and the test can read them back.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        #: How many of the next batch writes raise. `-1` is every one from now on.
        self.failing = 0
        self.failures = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        pipe = self._inner.pipeline(*args, **kwargs)
        if not self.failing:
            return pipe
        if self.failing > 0:
            self.failing -= 1
        self.failures += 1
        return _RefusedPipeline(pipe)


class _RefusedPipeline:
    """A pipeline that takes the commands and then cannot deliver them.

    The `XADD`s are queued on the real pipeline and `execute` raises instead of running
    it, which is the shape of the failure: the batch was assembled, the round trip did
    not complete, and nothing reached the stream.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        import redis.exceptions

        raise redis.exceptions.TimeoutError(TIMEOUT_MESSAGE)


def build_bus(*, max_outbox: int = DEFAULT_MAX_OUTBOX) -> tuple[RedisBus, list]:
    """A publisher on `fakeredis`, with no reader and no flusher loop running."""
    import fakeredis.aioredis

    server = fakeredis.aioredis.FakeServer()
    clients: list[Unwritable] = []

    def factory(_config: BusConfig) -> Any:
        client = Unwritable(
            fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
        )
        clients.append(client)
        return client

    config = BusConfig(
        venue="DELTA",
        underlyings=("BTC",),
        batch_ms=5,
        read_block_ms=0,
        read_count=10,
        idle_sleep_seconds=0.001,
        retention_seconds=1_000_000_000.0,
        max_outbox=max_outbox,
    )
    return RedisBus(config, client_factory=factory), clients


def test_the_default_outbox_ceiling_is_the_one_the_docstring_quotes() -> None:
    """`DEFAULT_MAX_OUTBOX = 200_000` is named in the module docstring, in the cost
    tables and nowhere in a test. A constant nothing reads is a constant that can change
    under a document that still quotes it."""
    assert DEFAULT_MAX_OUTBOX == 200_000
    assert BusConfig().max_outbox == DEFAULT_MAX_OUTBOX


def test_a_full_outbox_drops_its_oldest_entries_and_counts_them() -> None:
    """**The eviction branch, executed for the first time.**

    Bounded, oldest first, and counted -- the fan-out's rule one layer up. The three
    parts are separable and all three are asserted: the outbox stops at the ceiling, the
    entries that survive are the **newest** ones, and `outbox_dropped` holds the number.

    The last part is what makes this different from the reader's side of #111. A
    publisher that drops says so on the same breath; a lossless reader handed a batch it
    never delivered said nothing at all, and that was the defect.
    """

    async def scenario() -> tuple[dict[str, float], list[float]]:
        bus, _clients = build_bus(max_outbox=4)
        await bus.start(start_readers=False)
        try:
            for n in range(10):
                bus.publish(quote(float(n)))
            before = bus.publisher()
            await bus.flush()
            entries = await bus.client.xrange(QUOTE_STREAM)
            import json

            survived = [
                json.loads(fields[b"payload"])["bid"] for _entry_id, fields in entries
            ]
            return before, survived
        finally:
            await bus.aclose()

    before, survived = asyncio.run(scenario())

    assert before["outbox"] == 4, "the outbox grew past its ceiling"
    assert before["published"] == 10
    assert before["outbox_dropped"] == 6, "the eviction was not counted"
    assert survived == [6.0, 7.0, 8.0, 9.0], (
        "the oldest entries were kept and the newest dropped"
    )


def test_an_outbox_exactly_at_its_ceiling_drops_nothing() -> None:
    """The boundary, because `>` and `>=` are one character apart and the branch had
    never run. A ceiling of four holds four."""

    async def scenario() -> dict[str, float]:
        bus, _clients = build_bus(max_outbox=4)
        await bus.start(start_readers=False)
        try:
            for n in range(4):
                bus.publish(quote(float(n)))
            return bus.publisher()
        finally:
            await bus.aclose()

    stats = asyncio.run(scenario())

    assert stats["outbox"] == 4
    assert stats["outbox_dropped"] == 0


def test_a_batch_that_cannot_be_written_stays_in_the_outbox() -> None:
    """**The failed-batch branch, executed for the first time.**

    *"A failed batch is put back, not discarded."* The only assertion anywhere near this
    was `stats["failures"] == 0`, which asserts the branch is **not** taken -- this
    repository's own check 5 from `tests-that-prove-nothing.md`, "check that something
    can raise before you trust 'nothing raised'."

    Three things must be true and each is a separate way to get this wrong: the failure
    is counted, `flush` reports zero written rather than the length of the batch it lost,
    and the entries are in the outbox afterwards rather than gone.
    """

    async def scenario() -> tuple[int, dict[str, float], int]:
        bus, clients = build_bus()
        await bus.start(start_readers=False)
        try:
            for n in range(3):
                bus.publish(quote(float(n)))
            clients[0].failing = 1
            written = await bus.flush()
            after = bus.publisher()
            entries = await bus.client.xrange(QUOTE_STREAM)
            return written, after, len(entries)
        finally:
            await bus.aclose()

    written, after, in_stream = asyncio.run(scenario())

    assert written == 0, "a failed batch reported entries written"
    assert after["failures"] == 1
    assert after["written"] == 0
    assert after["outbox"] == 3, "the batch was discarded rather than put back"
    assert in_stream == 0, "the refused pipeline wrote to the stream anyway"


def test_a_batch_put_back_is_written_in_order_by_the_next_flush() -> None:
    """Put back is only half a promise. **Put back at the front**, so that the retry
    writes the batch before anything published while Redis was unreachable -- `flush`
    splices with `self._outbox[:0] = batch` for exactly this. A bus that recovered by
    reordering its own stream would be a worse failure than one that dropped, because a
    stream's order is what every consumer's position means."""

    async def scenario() -> tuple[list[float], dict[str, float]]:
        bus, clients = build_bus()
        await bus.start(start_readers=False)
        try:
            for n in range(3):
                bus.publish(quote(float(n)))
            clients[0].failing = 1
            await bus.flush()
            # Published while the first batch was still stuck in the outbox.
            for n in range(3, 6):
                bus.publish(quote(float(n)))
            written = await bus.flush()
            assert written == 6
            entries = await bus.client.xrange(QUOTE_STREAM)
            import json

            return [
                json.loads(fields[b"payload"])["bid"] for _entry_id, fields in entries
            ], bus.publisher()
        finally:
            await bus.aclose()

    bids, stats = asyncio.run(scenario())

    assert bids == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0], "the retry reordered the stream"
    assert stats["failures"] == 1
    assert stats["written"] == 6
    assert stats["outbox"] == 0


@pytest.mark.parametrize("failures", [1, 3])
def test_a_batch_put_back_repeatedly_is_still_bounded_and_still_counted(
    failures: int,
) -> None:
    """The two branches meet here, and this is the failure mode the bound exists for.

    Redis stops answering, the flusher retries, the batch goes back each time and the
    outbox fills behind it. Past the ceiling the oldest go, counted -- which is the
    module docstring's whole sentence, and the reason `outbox_dropped` is a field rather
    than a log line. A publisher that could not write and could not drop would be the
    memory leak with good manners.
    """

    async def scenario() -> dict[str, float]:
        bus, clients = build_bus(max_outbox=4)
        await bus.start(start_readers=False)
        try:
            clients[0].failing = failures
            for _attempt in range(failures):
                for n in range(3):
                    bus.publish(quote(float(n)))
                await bus.flush()
            return bus.publisher()
        finally:
            await bus.aclose()

    stats = asyncio.run(scenario())

    assert stats["failures"] == failures
    assert stats["written"] == 0
    assert stats["outbox"] <= 4, "the outbox grew past its ceiling under retry"
    assert stats["published"] == 3 * failures
    if failures > 1:
        assert stats["outbox_dropped"] > 0, (
            "a publisher that could not write also could not drop"
        )
