"""A bus reader that meets a transient, and what must be true when it meets a permanent.

**#103, reproduced at the seam it happened at.** At 11:02:41.028Z a Redis socket read
timed out inside `xreadgroup`. `_read` logged the traceback and re-raised into an
unsupervised `asyncio` task; every bus reader in all three services died the same way
inside 34 seconds, the process stayed up, `/health` kept answering `ok`, and 97 minutes
of market data were trimmed away unread. The publisher's loop caught the same class of
exception, logged *"the bus flusher raised; it keeps running"*, and looped — which is the
only reason `feed` kept publishing through it.

Every test here raises `redis.exceptions.TimeoutError` out of `xreadgroup` itself, with
the message Redis actually produced. A test that does not raise that exception is not
testing this defect.

**No wall clock and no sleeps.** The retry backoff is configuration, set to a hundredth
of a second here, and every wait is `until(...)` on the thing the test cares about.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from deltapayoff.events import Alert, Instrument, OptionQuote, Right
from deltapayoff.events.redis_wire import stream_name
from deltapayoff.redis_bus import BusConfig, RedisBus

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


async def until(predicate, timeout: float = 10.0, what: str = "") -> None:
    """Wait for something a reader does on its own schedule, or fail saying what.

    The same helper `test_bus_contract.py` uses, and for the same reason: the bound is a
    fail-safe, never a duration the assertion depends on.
    """

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(poll(), timeout)
    except TimeoutError:  # pragma: no cover - only on a real failure
        raise AssertionError(f"never became true: {what}") from None


async def take(subscription, count: int, timeout: float = 10.0) -> list:
    got: list = []
    try:
        for _ in range(count):
            got.append(await asyncio.wait_for(subscription.queue.get(), timeout))
    except TimeoutError:  # pragma: no cover - only on a real failure
        raise AssertionError(
            f"{subscription.name!r} delivered {len(got)} of {count} records"
        ) from None
    return got


class Disrupted:
    """`fakeredis`'s client with a timeout the test decides the moment of.

    Everything except `xreadgroup` is the real fake. The one method this defect arrived
    through is the one method that is wrapped, so the loop under test is the loop that
    ran in the container rather than a stand-in for it.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        #: How many of the next reads raise. `-1` is "every one from now on", which is
        #: what a genuinely broken subscription looks like from inside the loop.
        self.failing = 0
        self.failures = 0
        self.reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any:
        import redis.exceptions

        self.reads += 1
        if self.failing:
            if self.failing > 0:
                self.failing -= 1
            self.failures += 1
            raise redis.exceptions.TimeoutError(TIMEOUT_MESSAGE)
        return await self._inner.xreadgroup(*args, **kwargs)


def build_bus(
    *, retries: int = 5, retry_seconds: float = 0.01
) -> tuple[RedisBus, Disrupted]:
    """A Redis bus on `fakeredis`, its one read method interceptable."""
    import fakeredis.aioredis

    server = fakeredis.aioredis.FakeServer()
    disrupted: list[Disrupted] = []

    def factory(_config: BusConfig) -> Any:
        client = Disrupted(
            fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
        )
        disrupted.append(client)
        return client

    config = BusConfig(
        venue="DELTA",
        underlyings=("BTC",),
        batch_ms=5,
        read_block_ms=0,
        read_count=10,
        idle_sleep_seconds=0.001,
        retention_seconds=1_000_000_000.0,
        read_retries=retries,
        read_retry_seconds=retry_seconds,
        read_retry_ceiling_seconds=retry_seconds,
    )
    bus = RedisBus(config, client_factory=factory)
    return bus, disrupted  # type: ignore[return-value]


async def open_store_reader(bus: RedisBus) -> Any:
    """The store's own subscription shape: lossless, one group, from the head."""
    await bus.start()
    subscription = bus.subscribe(
        "store",
        maxsize=1000,
        lossless=True,
        group_start="$",
        event_types=("md.option_quote",),
    )
    await bus.ready()
    return subscription


async def close(bus: RedisBus) -> None:
    with contextlib.suppress(Exception):
        await bus.client.delete(*bus.config.streams())
    await bus.aclose()


# ------------------------------------------------------- symptom 1: the reader dies


def test_a_reader_survives_a_transient_timeout_and_keeps_consuming() -> None:
    """**The original symptom.** One `redis.exceptions.TimeoutError` out of
    `xreadgroup`, and the reader must still be reading afterwards.

    `redis_bus.py:690-701`, the publisher, catches this exact class and loops.
    `redis_bus.py:732-741`, the reader, logged it and re-raised. One loop treats a
    transient as something to survive and the other treated a reader dying as something
    to record. That asymmetry is the whole of #103.
    """

    async def scenario() -> tuple[list[float], int, dict[str, Any]]:
        bus, clients = build_bus()
        subscription = await open_store_reader(bus)
        try:
            bus.publish(quote(1.0))
            await bus.flush()
            before = [event.bid for event in await take(subscription, 1)]

            client = clients[0]
            client.failing = 1
            await until(lambda: client.failures == 1, what="the timeout was raised")

            bus.publish(quote(2.0))
            await bus.flush()
            after = [event.bid for event in await take(subscription, 1)]
            return before + after, client.failures, bus.readers()["store"]
        finally:
            await close(bus)

    bids, failures, reader = asyncio.run(scenario())

    assert failures == 1, "the test did not actually raise the timeout it exists for"
    assert bids == [1.0, 2.0], "the reader did not keep consuming past a transient"
    assert reader["alive"] is True
    assert reader["retries_total"] == 1
    assert reader["gave_up"] is False


def test_a_reader_that_gives_up_says_so_on_the_bus_and_is_not_silent() -> None:
    """A reader that retries forever on a broken subscription is its own failure mode.

    So the retry is bounded — and **a reader that gives up must say so loudly**. It
    raises an `alert` with `code="bus.reader_stopped"`, which the Discord consumer and
    the logger both take, and its own bookkeeping records the failure that ended it.
    The dead readers in the incident left a connected, idle, perfectly healthy client on
    Redis's side and nothing anywhere else; that is the fingerprint this must not
    reproduce.
    """

    async def scenario() -> tuple[list[Alert], dict[str, Any], int]:
        bus, clients = build_bus(retries=2)
        subscription = await open_store_reader(bus)
        watcher = bus.subscribe("watcher", maxsize=100, event_types=("alert",))
        await bus.ready()
        try:
            client = clients[0]
            client.failing = -1
            await until(
                lambda: bus.readers()["store"]["gave_up"],
                what="the reader gave up",
            )
            await bus.flush()
            alerts = await take(watcher, 1)
            return alerts, bus.readers()["store"], client.failures
        finally:
            del subscription
            await close(bus)

    alerts, reader, failures = asyncio.run(scenario())

    # Three failures for two retries: the read that failed, then two retried reads.
    assert failures == 3
    assert reader["alive"] is False
    assert reader["gave_up"] is True
    assert TIMEOUT_MESSAGE in reader["failure"]
    assert [alert.code for alert in alerts] == ["bus.reader_stopped"]
    assert alerts[0].severity == "error"
    assert "store" in alerts[0].detail


# ------------------------------------- symptom 2: a dead reader reported itself caught up


def test_a_dead_reader_reads_as_maximally_behind_rather_than_caught_up() -> None:
    """**The symptom that destroyed data.** `behind` was a flag the loop wrote.

    It is initialised `False` and written only from inside the loop that died, so a dead
    reader froze it at its last value — `False`, caught up — and `store.py`'s seal clock,
    which is literally `min(wall, times of behind streams)`, advanced on wall clock over
    two hours of data nobody had read. Those minutes were sealed empty, and **a bar
    sealed empty is unrecoverable**: a seal closes a minute so nothing more can enter it.

    A reader that is not running cannot know it is caught up. Every stream it holds a
    position on is behind, and the position is where it stopped.
    """

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...], str]:
        bus, clients = build_bus(retries=0)
        subscription = await open_store_reader(bus)
        try:
            bus.publish(quote(1.0))
            await bus.flush()
            await take(subscription, 1)
            await until(
                lambda: subscription.behind_streams() == (),
                what="the reader reported itself caught up",
            )
            caught_up = subscription.behind_streams()

            clients[0].failing = -1
            await until(
                lambda: not bus.readers()["store"]["alive"],
                what="the reader stopped",
            )
            return caught_up, subscription.behind_streams(), subscription.positions[
                QUOTE_STREAM
            ].id
        finally:
            await close(bus)

    caught_up, dead, position = asyncio.run(scenario())

    assert caught_up == ()
    assert dead == (QUOTE_STREAM,), "a dead reader still reported itself caught up"
    assert position != "0-0"


def test_a_reader_that_stops_completing_passes_reads_as_behind_before_it_raises() -> None:
    """The loop can hang as well as die, and a hung loop is the same lie.

    `behind` is derived from three things: the reader is running, it has completed a
    pass recently, and its last read was a full batch. The middle one is what catches a
    reader blocked inside a call that never returns — the socket timeout makes that
    unlikely rather than impossible, and the cost of being wrong is asymmetric. A false
    "behind" seals a minute late, which the next pass corrects. A false "caught up" seals
    a minute empty, which nothing corrects.
    """

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        bus, _clients = build_bus()
        subscription = await open_store_reader(bus)
        try:
            bus.publish(quote(1.0))
            await bus.flush()
            await take(subscription, 1)
            await until(
                lambda: subscription.behind_streams() == (),
                what="the reader reported itself caught up",
            )
            fresh = subscription.behind_streams()
            # The loop stamps a monotonic reading at the top of every pass. Ageing the
            # stamp is how a test says "this loop has not come round" without waiting.
            subscription.last_pass -= subscription.stale_after + 1.0
            return fresh, subscription.behind_streams()
        finally:
            await close(bus)

    fresh, stalled = asyncio.run(scenario())

    assert fresh == ()
    assert stalled == (QUOTE_STREAM,)


def test_an_unstarted_reader_is_behind_rather_than_caught_up() -> None:
    """A subscription whose reader has not started has read nothing, and says so."""

    async def scenario() -> tuple[tuple[str, ...], bool]:
        bus, _clients = build_bus()
        # `_prepare_process`'s own order: subscribe, then start the bus without its
        # readers, then create the groups. The store needs that window to rebase a
        # trimmed replay cursor before any entry can be delivered.
        subscription = bus.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            event_types=("md.option_quote",),
        )
        await bus.start(start_readers=False)
        # One entry in the stream, so the group is created at a real head id rather than
        # at `0-0`. A position is what "behind" is measured from; the empty-stream case
        # is the test below.
        bus.publish(quote(1.0))
        await bus.flush()
        await bus.ensure_groups(subscription)
        try:
            return subscription.behind_streams(), bus.readers()["store"]["alive"]
        finally:
            await close(bus)

    behind, alive = asyncio.run(scenario())

    assert alive is False
    assert behind == (QUOTE_STREAM,)


def test_a_reader_with_no_position_at_all_does_not_pin_the_clock_to_the_epoch() -> None:
    """Maximally behind means "pinned at where it stopped", never "pinned at zero".

    A stream nobody has written to has a position of `0-0`, whose time is the Unix
    epoch. Reporting it behind would hand `store.py`'s seal clock a `min` of zero and
    nothing would ever seal again — a worse failure than the one being fixed, and the
    reason this is derived per stream rather than asserted for the whole subscription.
    """

    async def scenario() -> tuple[tuple[str, ...], str]:
        bus, _clients = build_bus()
        # `_prepare_process`'s own order: subscribe, then start the bus without its
        # readers, then create the groups. The store needs that window to rebase a
        # trimmed replay cursor before any entry can be delivered.
        subscription = bus.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            event_types=("md.option_quote",),
        )
        await bus.start(start_readers=False)
        await bus.ensure_groups(subscription)
        try:
            return (
                subscription.behind_streams(),
                subscription.positions[QUOTE_STREAM].id,
            )
        finally:
            await close(bus)

    behind, position = asyncio.run(scenario())

    assert position == "0-0"
    assert behind == (), "an empty stream pinned the seal clock at the epoch"


# ---------------------------------------------- symptom 3: nothing supervised the readers


def test_every_reader_task_is_observed_and_an_unexpected_exit_is_recorded() -> None:
    """`redis_bus.py:539/626` created bare tasks with no `add_done_callback`.

    `self._readers` holds a strong reference, so the Task was never garbage collected
    and Python never even printed *"Task exception was never retrieved"*. **A task that
    can die unobserved is the root of all five symptoms.**
    """

    async def scenario() -> tuple[dict[str, Any], dict[str, str]]:
        bus, clients = build_bus(retries=0)
        await open_store_reader(bus)
        try:
            clients[0].failing = -1
            await until(
                lambda: "store" in bus.reader_exits(),
                what="the reader's exit was recorded",
            )
            return bus.readers()["store"], dict(bus.reader_exits())
        finally:
            await close(bus)

    reader, exits = asyncio.run(scenario())

    assert reader["alive"] is False
    assert TIMEOUT_MESSAGE in exits["store"]


def test_an_ordinary_close_is_not_reported_as_a_reader_failure() -> None:
    """`aclose` and `unsubscribe` cancel their readers. A cancellation is not a death,
    and a supervisor that cried wolf at every shutdown would be turned off."""

    async def scenario() -> dict[str, str]:
        bus, _clients = build_bus()
        subscription = await open_store_reader(bus)
        bus.unsubscribe(subscription)
        await until(
            lambda: "store" not in bus.readers(), what="the subscription was removed"
        )
        exits = dict(bus.reader_exits())
        await close(bus)
        return exits

    assert asyncio.run(scenario()) == {}


@pytest.mark.parametrize("lossless", [True, False])
def test_both_reader_policies_are_supervised(lossless: bool) -> None:
    """The drop-oldest reader is the screen's and the lossless one is the store's, and
    both were bare `create_task` calls. `api`'s three readers died in the incident
    alongside `store`'s one."""

    async def scenario() -> dict[str, Any]:
        bus, _clients = build_bus()
        await bus.start()
        bus.subscribe(
            "reader",
            maxsize=100,
            lossless=lossless,
            event_types=("md.option_quote",),
            **({"group_start": "$"} if lossless else {}),
        )
        await bus.ready()
        try:
            return bus.readers()["reader"]
        finally:
            await close(bus)

    reader = asyncio.run(scenario())

    assert reader["alive"] is True
    assert reader["supervised"] is True


# ------------------------------------------------ the two XINFO answers, on real Redis


def build_real_bus(url: str) -> RedisBus:
    """A bus on a throwaway `redis:7-alpine`, started and removed by `redis_server`."""
    return RedisBus(
        BusConfig(
            url=url,
            venue="DELTA",
            underlyings=("BTC",),
            batch_ms=5,
            read_block_ms=20,
            read_count=10,
            idle_sleep_seconds=0.001,
            retention_seconds=1_000_000_000.0,
        )
    )


def test_consumer_lag_and_the_replay_gap_test_hold_against_a_real_redis(
    redis_server: str,
) -> None:
    """**`fakeredis` is our belief about Redis; this is Redis.**

    It matters more here than usual. `fakeredis` answers `XINFO GROUPS` with `lag: -1` --
    "unknown" -- for a group it cannot compute, so every fake-backed test of the lag
    threshold is really a test of a stub. The incident's own evidence was two `XINFO`
    calls against Redis 7 reporting `lag 1858892`, and this is that shape reproduced:
    a real `lag`, and a real `XTRIM` moving the oldest surviving entry past the group.
    """

    async def scenario() -> tuple[Any, Any, Any]:
        bus = build_real_bus(redis_server)
        subscription = await open_store_reader(bus)
        try:
            for n in range(4):
                bus.publish(quote(float(n)))
            await bus.flush()
            await take(subscription, 4)
            await until(
                lambda: subscription.behind_streams() == (),
                what="the reader caught up",
            )
            caught_up = (await bus.consumer_lag(subscription))[QUOTE_STREAM]

            # The reader stops; the publisher does not. Six entries nobody reads.
            # **Awaited, not merely cancelled**: `cancel()` takes effect at the task's
            # next await point, and the publishes below are await points, so a reader
            # left unawaited gets one more batch and the arithmetic moves under the test.
            reader = bus._readers["store"]
            bus.unsubscribe(subscription)
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            for n in range(4, 10):
                bus.publish(quote(float(n)))
            await bus.flush()
            behind = (await bus.consumer_lag(subscription))[QUOTE_STREAM]

            # Then retention passes the group's position.
            await bus.client.xtrim(QUOTE_STREAM, maxlen=2, approximate=False)
            trimmed = (await bus.consumer_lag(subscription))[QUOTE_STREAM]
            return caught_up, behind, trimmed
        finally:
            await close(bus)

    caught_up, behind, trimmed = asyncio.run(scenario())

    assert caught_up.lag == 0, "a caught-up group must report no lag"
    assert caught_up.trimmed_past is False
    assert caught_up.lost == 0

    assert behind.lag == 6, "real Redis reports the lag the incident's evidence quoted"
    assert behind.trimmed_past is False

    # Ten added, two retained, four read: four entries were trimmed before they were read.
    assert trimmed.trimmed_past is True, "the group's position is not older than the head"
    assert trimmed.lost == 4
    assert trimmed.entries_added == 10
    assert trimmed.length == 2
    assert trimmed.entries_read == 4
