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
from deltapayoff.redis_bus import BusConfig, Position, RedisBus

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
    """`fakeredis`'s client with a timeout the test decides the moment and the *side* of.

    Everything except `xreadgroup` is the real fake. The one method this defect arrived
    through is the one method that is wrapped, so the loop under test is the loop that
    ran in the container rather than a stand-in for it.

    **Which side of the wire the timeout lands on is the whole of #111, and until #111
    this class only had one of them.** `after_read = False` raises *instead of* calling
    the inner client: Redis never executes the read, nothing moves into the pending
    list, and there is nothing to lose. That is a real failure — a dial or a write that
    never reached the server — and #103's tests are right to use it.

    It is not the failure that was observed. On the container Redis **did** serve the
    `XREADGROUP`, moved five hundred entries into the group's pending list, and the
    client's socket read timed out afterwards; `XPENDING` is how that was found.
    `after_read = True` is that: let the inner call run to completion, discard the
    reply, then raise. It leaves a genuine pending-list entry behind, which is the only
    way a test can reach #111 at all. 1,523 tests passed against a stack losing 26% of a
    lossless stream because no test could set this flag.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        #: How many of the next reads raise. `-1` is "every one from now on", which is
        #: what a genuinely broken subscription looks like from inside the loop.
        self.failing = 0
        #: Whether a disrupted read runs before it raises. See the class docstring.
        self.after_read = False
        self.failures = 0
        self.reads = 0
        #: Ids the discarded replies carried, in order. A test asserts against what
        #: Redis actually handed over rather than against what it assumes it did.
        self.discarded: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any:
        import redis.exceptions

        self.reads += 1
        if self.failing:
            # **Armed before the await, counted after it.** `failing` is spent first so
            # that a second read cannot arm itself on the same budget while this one is
            # suspended. `failures` is what every test waits on, and it must not become
            # true until the discard it stands for has actually happened -- against a
            # real Redis the inner read takes about a millisecond and `until` polls
            # every five, so incrementing it first let a test read `discarded` empty.
            if self.failing > 0:
                self.failing -= 1
            if self.after_read:
                got = await self._inner.xreadgroup(*args, **kwargs)
                for _key, entries in got or ():
                    for entry_id, _fields in entries:
                        self.discarded.append(
                            entry_id.decode()
                            if isinstance(entry_id, bytes)
                            else str(entry_id)
                        )
            self.failures += 1
            raise redis.exceptions.TimeoutError(TIMEOUT_MESSAGE)
        return await self._inner.xreadgroup(*args, **kwargs)


def build_bus(
    *, retries: int = 5, retry_seconds: float = 0.01, server: Any = None
) -> tuple[RedisBus, Disrupted]:
    """A Redis bus on `fakeredis`, its one read method interceptable.

    `server` is how a test runs a **second** bus against the first one's data, which is
    what a restart is: the streams, the consumer groups and their pending lists all
    survive the process that made them, and #111's inherited pending list is only
    reachable that way.
    """
    import fakeredis.aioredis

    server = server if server is not None else fakeredis.aioredis.FakeServer()
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


# ---------------------------------- #111: the batch Redis had already moved into the PEL


async def drain(subscription, client, *, offered: int) -> list:
    """**Drain to empty, then prove it stays empty.** Never take N and count to N.

    Taking N cannot see a duplicate — it takes the first N and stops, which is how #84
    stayed green while a restart folded its replay tail twice. So this waits on the
    subscription's own `offered` reaching the number the test published, then lets the
    reader come round three more times, and only then empties the queue. A duplicate
    raises `offered` above the bound and a late one arrives inside those three passes.

    The passes are counted on the client, not waited out: `reads` is incremented by the
    reader itself, so this is a condition and not a duration in disguise.
    """
    await until(
        lambda: subscription.offered >= offered,
        what=f"{subscription.name!r} was offered {offered} entries",
    )
    mark = client.reads
    await until(
        lambda: client.reads >= mark + 3,
        what="the reader completed three more passes with nothing more to deliver",
    )
    got: list = []
    while not subscription.queue.empty():
        got.append(subscription.queue.get_nowait())
    return got


async def strand(
    bus: RedisBus, clients: list, *, count: int, retries: int = 0, **subscribe: Any
) -> Any:
    """Leave `count` entries in this consumer's own pending list, delivered to nobody.

    **Deterministic, with no window for the reader to win.** The group is created before
    anything is published and the reader is started after, so the very first pass is the
    one that reads the whole batch — and that pass is the armed one. Nothing here races
    the loop for the right moment.
    """
    subscription = bus.subscribe(
        "store",
        maxsize=1000,
        lossless=True,
        event_types=("md.option_quote",),
        **subscribe,
    )
    await bus.start(start_readers=False)
    await bus.ensure_groups(subscription)
    for n in range(count):
        bus.publish(quote(float(n)))
    await bus.flush()
    client = clients[0]
    client.after_read = True
    client.failing = 1
    await bus.start_readers()
    await until(
        lambda: client.failures == 1,
        what="a pass failed after xreadgroup had already returned",
    )
    if retries == 0:
        await until(
            lambda: not bus.readers()["store"]["alive"],
            what="the reader stopped, leaving the pending list where it was",
        )
    return subscription


def test_the_fake_can_now_strand_entries_in_a_real_pending_list() -> None:
    """**The test that had to exist before any of the others could mean anything.**

    The audit's finding: `Disrupted.xreadgroup` raised *instead of* calling the inner
    client, so Redis never executed the read, nothing moved into the pending list and
    there was nothing to lose. Every #103 resilience test runs on that fake, which is
    why 1,523 of them passed against a stack losing 26% of a lossless stream.

    This asserts the condition itself rather than any repair: six entries handed over by
    `XREADGROUP`, a reply the client threw away, and six entries sitting in the
    consumer's pending list afterwards with `XPENDING` to show for it. A regression test
    that cannot reproduce the condition is the tenth test that proves nothing.
    """

    async def scenario() -> tuple[list[str], Any, int]:
        bus, clients = build_bus(retries=0)
        subscription = await strand(bus, clients, count=6, group_start="$")
        try:
            pending = await bus.client.xpending(QUOTE_STREAM, "store")
            return clients[0].discarded, pending, subscription.offered
        finally:
            await close(bus)

    discarded, pending, offered = asyncio.run(scenario())

    assert len(discarded) == 6, "Redis did not hand the batch over before the timeout"
    assert pending["pending"] == 6, "nothing reached the pending list; the fake is blind"
    assert _text_id(pending["min"]) == discarded[0]
    assert _text_id(pending["max"]) == discarded[-1]
    assert offered == 0, "the discarded batch was somehow delivered as well"


def _text_id(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def test_a_pass_that_fails_after_xreadgroup_returned_loses_nothing() -> None:
    """**#111's headline, and the assertion the acceptance criterion asks for.**

    The reader's `>` cursor is fixed once, at `_read_group`, so every retry asks for new
    entries only. A batch Redis has already moved into the pending list is therefore
    unreachable for the life of the process: on the running stack the api's `bar-buffer`
    group held 16,502 pending of 63,632 read, `measured` 2026-09-12T16:44Z, while
    `/health` reported `skipped: 0` and Redis reported `lag 0`.

    The repair reads the consumer's own pending list before it reads `>`. Six entries
    in, six entries out, once each, and the pending list empty afterwards.
    """

    async def scenario() -> tuple[list[float], dict[str, Any], Any]:
        bus, clients = build_bus()
        subscription = await strand(bus, clients, count=6, retries=5, group_start="$")
        try:
            bids = [
                event.bid for event in await drain(subscription, clients[0], offered=6)
            ]
            pending = await bus.client.xpending(QUOTE_STREAM, "store")
            return bids, bus.stats()["store"], pending
        finally:
            await close(bus)

    bids, stats, pending = asyncio.run(scenario())

    assert bids == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0], (
        "the batch the failed pass had already been handed was never delivered"
    )
    assert stats["offered"] == 6, "an entry was delivered more than once"
    assert stats["recovered"] == 6
    assert stats["stranded"] == 0
    assert pending["pending"] == 0, "the pending list did not drain"


def test_a_recovered_batch_is_counted_where_skipped_never_could_be() -> None:
    """`skipped` is documented *"always zero on a lossless subscription"* and it stayed
    zero through all of this, correctly: nothing was skipped. The loss arrived as a
    batch handed over and not delivered, which had no field at all. `recovered` is that
    number, and it is on the same block."""

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        bus, clients = build_bus()
        subscription = await strand(bus, clients, count=4, retries=5, group_start="$")
        try:
            await drain(subscription, clients[0], offered=4)
            return bus.stats()["store"], bus.readers()["store"]
        finally:
            await close(bus)

    stats, reader = asyncio.run(scenario())

    assert stats["skipped"] == 0, "a lossless subscription skipped something"
    assert stats["recovered"] == 4, "the repair is invisible on /health"
    assert stats["deferred"] == 0
    assert reader["recovered"] == 4, "the readers block carries it too"
    assert reader["retries_total"] == 1
    assert reader["gave_up"] is False


def test_entries_trimmed_out_of_the_pending_list_are_counted_as_loss() -> None:
    """Retention does not wait for a reader to come back. An entry the pending list
    still names and the stream no longer holds comes back from `XREADGROUP` as an id
    with no fields, and it is gone: `recovered` cannot cover it and `stranded` is what
    says so. This is the residual the repair does not repair, and it must not be
    silent — which is the whole complaint against `lag 0`."""

    async def scenario() -> tuple[dict[str, Any], list[float]]:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        first, clients = build_bus(retries=0, server=server)
        await strand(first, clients, count=6, group_start="$")
        # Retention passes four of the six while the reader is not reading. `MAXLEN 2`
        # rather than an age, so the test states what survives instead of timing it.
        await first.client.xtrim(QUOTE_STREAM, maxlen=2, approximate=False)
        await first.aclose()

        second, second_clients = build_bus(server=server)
        await second.start(start_readers=False)
        subscription = second.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            event_types=("md.option_quote",),
        )
        await second.start_readers()
        try:
            await until(
                lambda: second.stats()["store"]["recovered"] >= 2,
                what="the two surviving entries were recovered",
            )
            got = [
                event.bid
                for event in await drain(subscription, second_clients[0], offered=2)
            ]
            return second.stats()["store"], got
        finally:
            await close(second)

    stats, bids = asyncio.run(scenario())

    assert stats["stranded"] == 4, "four entries were trimmed away and nothing said so"
    assert stats["recovered"] == 2
    assert bids == [4.0, 5.0], "the entries that did survive were not recovered"


def test_a_replaying_subscription_does_not_fold_its_pending_list_twice() -> None:
    """**The trap #103 mapped, reached from the other side, and the reason this is a
    per-stream decision rather than a bus-wide one.**

    #103 rejected automatic reader restart because re-entering `_read` re-runs `_replay`
    from `start_ids` — the *checkpoint* position, not where the reader has since reached
    — so everything between is re-folded. That is #84 at the scale of the whole run.
    A pending list is not the same thing: under this file's ack order it holds entries
    handed over and **not delivered**, exactly.

    But the two repairs overlap at exactly one moment, a restart. A store that hands
    back `start_ids` replays `(start_id, last-delivered]` from the raw stream, and every
    pending entry has an id at or below `last-delivered` — so `_replay` already covers
    the whole inherited pending list. Delivering it here as well would fold four entries
    twice. It is acked and counted `deferred`, and `_replay` is what delivers it.

    Four entries stranded, four entries delivered. Eight would be #84.
    """

    async def scenario() -> tuple[list[float], dict[str, Any], Any]:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        first, clients = build_bus(retries=0, server=server)
        # Two entries delivered and flushed, four stranded behind them.
        subscription = first.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            event_types=("md.option_quote",),
        )
        await first.start(start_readers=False)
        await first.ensure_groups(subscription)
        for n in range(2):
            first.publish(quote(float(n)))
        await first.flush()
        await first.start_readers()
        await take(subscription, 2)
        checkpoint = Position(subscription.last_ids[QUOTE_STREAM], 2)
        clients[0].after_read = True
        clients[0].failing = 1
        for n in range(2, 6):
            first.publish(quote(float(n)))
        await first.flush()
        await until(
            lambda: clients[0].failures == 1, what="the pass failed after the read"
        )
        await until(
            lambda: not first.readers()["store"]["alive"], what="the reader stopped"
        )
        stranded_ids = list(clients[0].discarded)
        await first.aclose()

        second, second_clients = build_bus(server=server)
        await second.start(start_readers=False)
        restarted = second.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            start_ids={QUOTE_STREAM: checkpoint},
            event_types=("md.option_quote",),
        )
        await second.start_readers()
        try:
            bids = [
                event.bid
                for event in await drain(restarted, second_clients[0], offered=4)
            ]
            return bids, second.stats()["store"], len(stranded_ids)
        finally:
            await close(second)

    bids, stats, stranded_count = asyncio.run(scenario())

    assert stranded_count == 4, "the first process did not strand what the test needs"
    assert bids == [2.0, 3.0, 4.0, 5.0], "the replay did not cover the pending list"
    assert stats["offered"] == 4, "the inherited pending list was folded twice (#84)"
    assert stats["deferred"] == 4, "the choice not to deliver here is not recorded"
    assert stats["recovered"] == 0, (
        "a replaying stream recovered its own pending list as well as replaying it"
    )


def test_a_subscription_with_no_checkpoint_adopts_its_inherited_pending_list() -> None:
    """The other half of the same decision, and the api's `bar-buffer` shape.

    `bar-buffer` passes no `start_ids` and has no checkpoint, so nothing replays for it
    and this is the only path that can ever deliver what a dead process left pending.
    The ticket's own words: *"for it there is no recovery on any path"*. There is now.
    """

    async def scenario() -> tuple[list[float], dict[str, Any]]:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        first, clients = build_bus(retries=0, server=server)
        await strand(first, clients, count=5, group_start="$")
        await first.aclose()

        second, second_clients = build_bus(server=server)
        await second.start(start_readers=False)
        rejoined = second.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            event_types=("md.option_quote",),
        )
        await second.start_readers()
        try:
            bids = [
                event.bid for event in await drain(rejoined, second_clients[0], offered=5)
            ]
            return bids, second.stats()["store"]
        finally:
            await close(second)

    bids, stats = asyncio.run(scenario())

    assert bids == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert stats["recovered"] == 5
    assert stats["deferred"] == 0, (
        "a subscription with no checkpoint deferred to a replay that will not run"
    )
    assert stats["offered"] == 5


def test_a_checkpointed_reader_recovers_a_batch_stranded_after_its_replay() -> None:
    """**The deferral rule is true at reader start and false for the rest of the run**,
    and a rule applied one moment too widely is the loss it was written to repair.

    `_replay` runs once, before the first pass. A `store`-shaped subscription that
    strands a batch *after* that has nothing else coming for it: there will be no second
    replay, and the next checkpoint it writes is derived from what it delivered. So mid
    run every stream delivers its own pending list, including the ones that have a
    `start_id`, and `deferred` stays where `_adopt_pending` left it.

    This is the case the first draft of the repair got wrong -- it read `start_ids`
    without asking *when*, and quietly acked the store's own stranded batch away.
    """

    async def scenario() -> tuple[list[float], dict[str, Any]]:
        bus, clients = build_bus()
        subscription = bus.subscribe(
            "store",
            maxsize=1000,
            lossless=True,
            group_start="$",
            event_types=("md.option_quote",),
        )
        await bus.start(start_readers=False)
        await bus.ensure_groups(subscription)
        bus.publish(quote(0.0))
        await bus.flush()
        await bus.start_readers()
        await take(subscription, 1)

        # A checkpoint the reader is now carrying, exactly as `store` hands one back.
        subscription.start_ids[QUOTE_STREAM] = Position(
            subscription.last_ids[QUOTE_STREAM], 1
        )
        client = clients[0]
        client.after_read = True
        client.failing = 1
        for n in range(1, 5):
            bus.publish(quote(float(n)))
        await bus.flush()
        await until(
            lambda: client.failures == 1, what="the pass failed after the read"
        )
        try:
            bids = [event.bid for event in await drain(subscription, client, offered=5)]
            return bids, bus.stats()["store"]
        finally:
            await close(bus)

    bids, stats = asyncio.run(scenario())

    assert bids == [1.0, 2.0, 3.0, 4.0], (
        "a checkpointed stream's mid-run pending list was acked away instead of recovered"
    )
    assert stats["recovered"] == 4
    assert stats["deferred"] == 0, "the start-of-reader rule leaked into the run"
    assert stats["offered"] == 5


def test_a_healthy_reader_never_asks_for_its_pending_list() -> None:
    """The repair must stay off the healthy path. At `derived` 1,849.8 events a second
    an unconditional pending read per pass would double the reader's round trips to buy
    a case that has not happened, so it is armed by a failed pass and by nothing else.

    Counted on the client rather than reasoned about: a run with no failure makes
    exactly as many `xreadgroup` calls as it makes passes.
    """

    async def scenario() -> tuple[int, int, dict[str, Any]]:
        bus, clients = build_bus()
        subscription = await open_store_reader(bus)
        try:
            for n in range(3):
                bus.publish(quote(float(n)))
            await bus.flush()
            await take(subscription, 3)
            client = clients[0]
            mark = client.reads
            await until(
                lambda: client.reads >= mark + 5, what="five more healthy passes"
            )
            return client.reads, client.failures, bus.stats()["store"]
        finally:
            await close(bus)

    reads, failures, stats = asyncio.run(scenario())

    assert failures == 0
    assert reads >= 5
    assert stats["recovered"] == 0
    assert stats["deferred"] == 0
    assert stats["stranded"] == 0


def test_the_pending_list_repair_holds_against_a_real_redis(redis_server: str) -> None:
    """**`fakeredis` is our belief about Redis; this is Redis** — and here the belief and
    the article differ in a way that matters.

    A pending entry whose stream has been trimmed comes back from `fakeredis` as an
    empty field mapping and from redis-py as `None`. Both are falsy and the repair reads
    them the same way, but that is a fact about the two clients and not something a
    fake-backed test establishes. The pending list is also the one Redis structure this
    repository now depends on for correctness rather than for reporting.
    """
    import redis.asyncio

    clients: list[Disrupted] = []

    def factory(config: BusConfig) -> Any:
        client = Disrupted(
            redis.asyncio.Redis.from_url(config.url, **config.client_kwargs())
        )
        clients.append(client)
        return client

    def bus_on(retries: int) -> RedisBus:
        return RedisBus(
            BusConfig(
                url=redis_server,
                venue="DELTA",
                underlyings=("BTC",),
                batch_ms=5,
                read_block_ms=0,
                read_count=10,
                idle_sleep_seconds=0.001,
                retention_seconds=1_000_000_000.0,
                read_retries=retries,
                read_retry_seconds=0.01,
                read_retry_ceiling_seconds=0.01,
            ),
            client_factory=factory,
        )

    async def scenario() -> tuple[Any, list[float], dict[str, Any]]:
        bus = bus_on(5)
        subscription = await strand(bus, clients, count=6, retries=5, group_start="$")
        try:
            pending_at_failure = len(clients[0].discarded)
            bids = [
                event.bid for event in await drain(subscription, clients[0], offered=6)
            ]
            after = await bus.client.xpending(QUOTE_STREAM, "store")
            return pending_at_failure, bids, (bus.stats()["store"], after)
        finally:
            await close(bus)

    handed_over, bids, (stats, after) = asyncio.run(scenario())

    assert handed_over == 6, "real Redis did not serve the read before the timeout"
    assert bids == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert stats["recovered"] == 6
    assert stats["offered"] == 6, "an entry was delivered twice against real Redis"
    assert after["pending"] == 0
