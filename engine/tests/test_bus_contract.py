"""One contract, three implementations behind it.

`events/bus.py` names two methods and defines no behaviour. `fanout.py` defines the
behaviour and `tests/test_fanout.py` pins it **for the fan-out**. This file is the same
behaviour asked of whatever is behind the seam, so that "the broker is undecided" stays
true in the tests as well as in the prose.

Every test below runs three times:

* `fanout` — the in-process bus, unchanged and still the default.
* `redis-fake` — `fakeredis`, in this process, no container and no port. What keeps the
  Redis path covered on a machine with no Docker.
* `redis-docker` — a real `redis:7-alpine` on the test-only port 6399, started and
  removed by the `redis_server` session fixture, **skipped loudly** without Docker. It is
  the only one of the three that proves anything about Redis's own trimming, blocking
  reads or consumer groups, because the fake is our belief about them.

The Redis-only behaviours — replay from an id, the group's isolation, trim by age, the
skip count — are further down, marked as such, because the fan-out has no answer to them
and a parametrised test that skipped two thirds of the time would say less than a named
one.

**No test here touches the network or the wall clock.** The clock the bus trims against is
injected, so "thirty minutes later" is an assignment. Waiting for a reader is done by
awaiting its queue with a timeout, which fails the test rather than sleeping through it.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from deltapayoff.events import (
    ConnectionState,
    ControlCommand,
    FeedConnection,
    Heartbeat,
    Instrument,
    OptionQuote,
    Right,
)
from deltapayoff.events.redis_wire import stream_name
from deltapayoff.fanout import FanOut
from deltapayoff.redis_bus import BusConfig, BusUnavailable, Position, RedisBus, Span

TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 6, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270626",
)
ETH_INSTRUMENT = INSTRUMENT.model_copy(
    update={"underlying": "ETH", "venue_symbol": "C-ETH-60000-270626"}
)

#: A fixed instant, so the trim floor a test asserts against is arithmetic rather than
#: whatever the machine's clock said. Redis stamps entries with **its own** clock, which
#: is why the trim tests move this one forward rather than backwards.
NOW = 1_788_000_000.0


def quote(bid: float) -> OptionQuote:
    """One `md.option_quote`, distinguishable by its bid. The hot event type, so the
    contract is proven on the stream that actually carries 1,537 messages a second."""
    return OptionQuote(source="delta", ts_received=TS, instrument=INSTRUMENT, bid=bid)


def quote_on(underlying: str, bid: float) -> OptionQuote:
    instrument = INSTRUMENT if underlying == "BTC" else ETH_INSTRUMENT
    return OptionQuote(source="delta", ts_received=TS, instrument=instrument, bid=bid)


def run(coro):
    """`asyncio.run`, as `test_fanout.py` does it — no plugin, no new dependency."""
    return asyncio.run(coro)


async def live(bus) -> None:
    """Wait until every subscription taken so far is actually reading. A no-op on the
    fan-out, where subscribing *is* being live.

    **This is a real property of the seam and not test scaffolding.** A drop-oldest
    subscription starts at `$` — from now — so anything published before its reader
    reached the head is something it legitimately never had. In the engine that cannot
    bite, because `build_feed_stack` subscribes and `start()` waits here before the feed
    is allowed to publish a thing; in a test that subscribes on a running bus, it can, and
    waiting is the honest fix rather than a sleep.
    """
    ready = getattr(bus, "ready", None)
    if ready is not None:
        await ready()


async def settle(bus) -> None:
    """Make everything published so far readable. A no-op on the fan-out.

    The Redis publisher batches, so a test that read straight after `publish` would be
    asserting against a batch that had not left the outbox yet. That is the seam's one
    honest difference and the tests say so out loud rather than sleeping for it.
    """
    flush = getattr(bus, "flush", None)
    if flush is not None:
        await flush()


async def take(subscription, count: int, timeout: float = 10.0) -> list:
    """The next `count` records off a subscription, or a failure naming the shortfall.

    `wait_for` is a fail-safe and not a clock dependency: on the fan-out every record is
    already there, and on Redis the reader task delivers as fast as the loop turns.
    """
    got: list = []
    try:
        for _ in range(count):
            got.append(await asyncio.wait_for(subscription.queue.get(), timeout))
    except TimeoutError:  # pragma: no cover - only on a real failure
        seen = {
            key: at
            for key, at in getattr(subscription, "last_ids", {}).items()
            if at != "0-0"
        }
        raise AssertionError(
            f"{subscription.name!r} delivered {len(got)} of {count} records; "
            f"offered {subscription.offered}, dropped {subscription.dropped}, "
            f"queued {subscription.queue.qsize()}, "
            f"undecodable {getattr(subscription, 'undecodable', 0)}, "
            f"skipped {getattr(subscription, 'skipped', 0)}, read through {seen}"
        ) from None
    return got


async def drain(subscription, timeout: float = 1.0) -> list:
    """Everything still queued, read until nothing more arrives. **The surplus.**

    `take` asks for a number and is satisfied by it, so a seam that delivered one record
    too many stayed green behind it for as long as #84 was open. This is the other half:
    a test asserts what it took *and* that nothing was left.
    """
    got: list = []
    while True:
        try:
            got.append(await asyncio.wait_for(subscription.queue.get(), timeout))
        except TimeoutError:
            return got


def _entries_added(info) -> int:
    """`XINFO STREAM`'s lifetime counter, whichever way the client decoded its keys."""
    return int(info.get("entries-added", info.get(b"entries-added")))


async def until(predicate, timeout: float = 10.0, what: str = "") -> None:
    """Wait for something a reader does on its own schedule, or fail saying what.

    Not a sleep: it yields until the condition holds and fails at the bound. The bound is
    a fail-safe, which is the only sense in which any test here touches a clock.
    """

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(poll(), timeout)
    except TimeoutError:  # pragma: no cover - only on a real failure
        raise AssertionError(f"never became true: {what}") from None


@asynccontextmanager
async def open_bus(
    kind: str,
    url: str | None,
    clock=lambda: NOW,
    *,
    read_count: int = 500,
):
    """A started bus of the kind under test, and its keys removed afterwards.

    Every Redis run deletes its configured streams before closing, so two tests on one
    container cannot read each other's entries.
    """
    if kind == "fanout":
        yield FanOut()
        return

    config = BusConfig(
        url=url or "redis://127.0.0.1:6399",
        underlyings=("BTC", "ETH"),
        # Small, so a test that lets the flusher run does not wait on a 50 ms tick. Every
        # assertion still goes through an explicit `flush()`.
        batch_ms=5,
        read_block_ms=20,
        read_count=read_count,
    )
    factory = _fake_client if kind == "redis-fake" else None
    bus = RedisBus(config, client_factory=factory, clock=clock)
    await bus.start()
    try:
        yield bus
    finally:
        # Delete this run's keys before closing. The container outlives the session — it
        # is reused when one is already serving the port — and a suite that left its
        # streams behind would grow a Redis nobody is trimming.
        with contextlib.suppress(Exception):
            await bus.client.delete(*config.streams())
        await bus.aclose()


def _fake_client(config: BusConfig):
    """`fakeredis`'s async client, built with the same arguments the real one gets."""
    import fakeredis.aioredis

    return fakeredis.aioredis.FakeRedis(decode_responses=False)


@pytest.fixture(params=["fanout", "redis-fake", "redis-docker"])
def bus_kind(request: pytest.FixtureRequest) -> tuple[str, str | None]:
    """The three implementations, and the URL each needs. Docker's is fetched lazily so
    the other two do not pay for a container they do not use."""
    if request.param == "redis-docker":
        return request.param, request.getfixturevalue("redis_server")
    return request.param, None


# --------------------------------------------------------------- the shared contract


def test_every_subscriber_receives_every_message_in_order(bus_kind) -> None:
    """The baseline, and the one thing a bus may never reorder.

    Order holds **per stream**. Redis gives no order across keys and neither does the
    fan-out's own delivery to two different consumers; a producer that needed a total
    order across event types would need one stream, which #58 rejected for other reasons.
    """
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("first", maxsize=100)
            second = bus.subscribe("second", maxsize=100)
            await live(bus)
            for n in range(5):
                bus.publish(quote(float(n)))
            await settle(bus)
            return (
                [event.bid for event in await take(first, 5)],
                [event.bid for event in await take(second, 5)],
            )

    a, b = run(scenario())
    assert a == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert b == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_a_lossless_subscriber_that_stalls_through_a_burst_loses_nothing(bus_kind):
    """#5's whole reason for existing, asked of the broker instead of the queue.

    Drop-oldest is wrong for the store not because a drop leaves a hole but because drops
    happen under load, load is when price moves fastest, and dropping then **shaves the
    highs and the lows** — a bias, not noise, and invisible in the output. So a lossless
    consumer that reads nothing for the whole of a burst must still receive all of it.
    """
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            writer = bus.subscribe("writer", maxsize=5, lossless=True)
            await live(bus)
            for n in range(200):
                bus.publish(quote(float(n)))
            await settle(bus)
            received = await take(writer, 200)
            return [event.bid for event in received], writer.dropped

    bids, dropped = run(scenario())
    assert bids == [float(n) for n in range(200)], "a lossless consumer lost messages"
    assert dropped == 0


def test_a_drop_oldest_subscriber_keeps_the_newest_and_counts_the_rest(bus_kind):
    """A quote from four seconds ago is worthless, so the queue keeps the newest.

    And every discard is counted, because a silent drop is a lie — this project has twice
    been damaged by numbers that looked plausible and were not.
    """
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            screen = bus.subscribe("screen", maxsize=4)
            await live(bus)
            for n in range(20):
                bus.publish(quote(float(n)))
            await settle(bus)
            # **Wait for all twenty to have been offered before taking any.** A batch is
            # not atomic on the wire — Redis applies a pipeline's `XADD`s one at a time,
            # so a blocking read can return the first entry while the rest are still
            # landing. Which four are left is a fact about the queue, not about the read
            # boundaries, and this is what makes the assertion about the queue.
            await until(lambda: screen.offered == 20, what="all twenty offered")
            newest = await take(screen, 4)
            return [event.bid for event in newest], screen.dropped, bus.stats()

    bids, dropped, stats = run(scenario())
    assert bids == [16.0, 17.0, 18.0, 19.0], "the newest four must survive"
    assert dropped == 16
    assert stats["screen"]["dropped"] == 16
    assert stats["screen"]["lossless"] is False


def test_a_lossless_backlog_is_counted_rather_than_silently_absorbed(bus_kind):
    """An unbounded queue is a memory leak with good manners *unless somebody counts*."""
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            writer = bus.subscribe("writer", maxsize=4, lossless=True)
            await live(bus)
            for n in range(10):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(writer, 10)
            return bus.stats()

    stats = run(scenario())
    assert stats["writer"]["dropped"] == 0
    assert stats["writer"]["over_capacity"] > 0, "a backlog nobody counted"
    assert stats["writer"]["backlog_peak"] >= 4


def test_a_stalled_consumer_does_not_disturb_the_one_beside_it(bus_kind):
    """The sabotage, and on Redis it is what one consumer group per consumer buys.

    Two readers of the same events: one never reads at all, the other must still receive
    every message. On the fan-out they are two queues; on Redis they are a group and a
    `$` reader, which is exactly why `store` and the screen never compete for a message.
    """
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            stalled = bus.subscribe("stalled", maxsize=2)
            healthy = bus.subscribe("healthy", maxsize=500, lossless=True)
            await live(bus)
            for n in range(50):
                bus.publish(quote(float(n)))
            await settle(bus)
            received = await take(healthy, 50)
            # The two readers run at their own pace, so wait for the stalled one to have
            # been offered the burst before asking what it did with it. Reading its
            # counter the instant the *other* consumer finishes is a race, not a fact.
            await until(lambda: stalled.offered >= 50, what="the stalled reader read")
            return [event.bid for event in received], bus.stats()

    bids, stats = run(scenario())
    assert bids == [float(n) for n in range(50)]
    assert stats["healthy"]["dropped"] == 0
    assert stats["stalled"]["dropped"] > 0, "the stalled reader should have overflowed"


def test_publishing_never_awaits(bus_kind) -> None:
    """`publish` is synchronous on both sides of the seam or the socket handler stalls.

    The Redis publisher is why this is a test and not an observation: it would have been
    natural to `await` the `XADD`, and that would put a network round trip between two
    reads of the venue's socket. It appends to an outbox instead and a flusher task
    pipelines it.
    """
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            bus.subscribe("tiny", maxsize=1)
            for n in range(5_000):
                bus.publish(quote(float(n)))
            return True

    assert run(scenario()) is True


def test_a_subscriber_needs_a_bounded_queue(bus_kind) -> None:
    """`maxsize` is required under either policy, because an unbounded queue with no
    number attached is the memory leak the fan-out refuses."""
    kind, url = bus_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            with pytest.raises(ValueError):
                bus.subscribe("greedy", maxsize=0)
            return True

    assert run(scenario()) is True


def test_redis_subscriptions_can_receive_only_selected_event_types() -> None:
    """A state reader must not drain market data or inbound commands."""

    async def scenario():
        async with open_bus("redis-fake", None) as bus:
            with pytest.raises(ValueError):
                bus.subscribe("empty", maxsize=10, event_types=[])
            with pytest.raises(ValueError):
                bus.subscribe("unknown", maxsize=10, event_types=["not-an-event"])
            assert "empty" not in bus._subscriptions
            assert "unknown" not in bus._subscriptions

            feed_state = bus.subscribe(
                "feed-state",
                maxsize=10,
                event_types=["feed.connection", "heartbeat"],
            )
            commands = bus.subscribe(
                "feed-control", maxsize=10, event_types=["control.command"]
            )
            await live(bus)
            bus.publish(
                FeedConnection(
                    source="controller",
                    ts_received=TS,
                    adapter="DELTA",
                    to_state=ConnectionState.CONNECTED,
                    reason="open",
                )
            )
            bus.publish(
                Heartbeat(
                    source="controller",
                    ts_received=TS,
                    adapter="DELTA",
                    state=ConnectionState.CONNECTED,
                )
            )
            bus.publish(_command_event())
            bus.publish(quote(1.0))
            await settle(bus)
            return await take(feed_state, 2), await take(commands, 1)

    feed_events, command_events = run(scenario())
    assert [event.type for event in feed_events] == ["feed.connection", "heartbeat"]
    assert [event.type for event in command_events] == ["control.command"]


def _command_event() -> ControlCommand:
    return ControlCommand(
        source="operator",
        ts_received=TS,
        adapter="DELTA",
        command="pause",
    )


# ------------------------------------------------------------------- Redis only


@pytest.fixture(params=["redis-fake", "redis-docker"])
def redis_kind(request: pytest.FixtureRequest) -> tuple[str, str | None]:
    if request.param == "redis-docker":
        return request.param, request.getfixturevalue("redis_server")
    return request.param, None


def test_two_consumers_in_two_groups_each_receive_every_entry(redis_kind) -> None:
    """One group per consumer, never one per instance.

    Two groups on one stream each receive every entry; two consumers *in* one group
    would split them. That difference is the whole reason `store` and `api` are
    independent readers rather than competitors for one message, and it is a
    configuration mistake that would look like intermittent data loss.
    """
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            store = bus.subscribe("store", maxsize=500, lossless=True)
            api = bus.subscribe("api", maxsize=500, lossless=True)
            await live(bus)
            for n in range(20):
                bus.publish(quote(float(n)))
            await settle(bus)
            return (
                [event.bid for event in await take(store, 20)],
                [event.bid for event in await take(api, 20)],
            )

    store_bids, api_bids = run(scenario())
    assert store_bids == [float(n) for n in range(20)]
    assert api_bids == [float(n) for n in range(20)]


def test_subscribe_rejects_removed_scalar_start_id(redis_kind) -> None:
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            with pytest.raises(TypeError):
                bus.subscribe("store", maxsize=10, lossless=True, start_id="1-0")

    run(scenario())


def test_lossless_replay_uses_a_saved_position_per_stream(redis_kind) -> None:
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("store", maxsize=500, lossless=True)
            await live(bus)
            for n in range(3):
                bus.publish(quote_on("BTC", float(n)))
            for n in range(2):
                bus.publish(quote_on("ETH", float(10 + n)))
            await settle(bus)
            await take(first, 5)
            keys = {
                "BTC": stream_name(quote_on("BTC", 0)),
                "ETH": stream_name(quote_on("ETH", 0)),
            }
            saved = {underlying: first.positions[key] for underlying, key in keys.items()}

            for n in range(3, 5):
                bus.publish(quote_on("BTC", float(n)))
            for n in range(12, 15):
                bus.publish(quote_on("ETH", float(n)))
            await settle(bus)
            await take(first, 5)
            bus.unsubscribe(first)

            restarted = bus.subscribe(
                "store",
                maxsize=500,
                lossless=True,
                start_ids={key: saved[underlying] for underlying, key in keys.items()},
            )
            await live(bus)
            replayed = await take(restarted, 5)
            return keys, saved, restarted, replayed

    keys, saved, restarted, replayed = run(scenario())
    assert sorted(event.bid for event in replayed) == [3.0, 4.0, 12.0, 13.0, 14.0]
    assert restarted.positions[keys["BTC"]].index == saved["BTC"].index + 2
    assert restarted.positions[keys["ETH"]].index == saved["ETH"].index + 3


def test_a_restarted_reader_replays_from_the_id_it_last_flushed(redis_kind) -> None:
    """The five-minute loss window #57 names, closed.

    The store acks on receipt and records the id of the last message it **flushed**, so a
    restart reads forward from there rather than from the pending list. The pending list
    would carry only what was never acked, and everything was acked on receipt — it says
    nothing about what reached a Parquet file.
    """
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            writer = bus.subscribe("store", maxsize=500, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))

            for n in range(5):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(writer, 5)
            # **The position is recorded with the queue drained**, which is what makes
            # it the position of the last message this consumer actually had. It is taken
            # as one value, id and index together, because that is what a flush writes
            # into the checkpoint; reading the two at different moments would be a saved
            # position that never existed.
            flushed = writer.positions[key]

            # Five more, read and acked on receipt — and then the process dies before the
            # next flush. These are exactly what the pending list would not recover.
            for n in range(5, 10):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(writer, 5)
            bus.unsubscribe(writer)

            # And five that arrive while nothing at all is reading.
            for n in range(10, 15):
                bus.publish(quote(float(n)))
            await settle(bus)

            restarted = bus.subscribe(
                "store",
                maxsize=500,
                lossless=True,
                start_ids={key: flushed},
            )
            await live(bus)
            replayed = await take(restarted, 10)
            # **Drained to empty, not counted out.** Taking exactly the ten expected was
            # green for as long as the seam delivered fifteen (#84): the surplus sat in
            # the queue behind them and nothing ever looked.
            extra = await drain(restarted)
            info = await bus.client.xinfo_stream(key)
            return (
                [event.bid for event in replayed],
                [event.bid for event in extra],
                restarted.positions[key].index,
                _entries_added(info),
            )

    bids, extra, index, entries_added = run(scenario())
    # Five replayed — 5..9, the ones acked but not flushed — then the five that arrived
    # while nothing was reading. Nothing lost and nothing before the flush repeated.
    assert bids == [5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]
    assert extra == [], "the replay and the group's `>` must not both deliver the tail"
    # The index is the ordinal the next checkpoint saves and `replay_gaps` subtracts a
    # trim from. One past the end of the stream would under-report the next loss.
    assert (index, entries_added) == (15, 15)


def test_a_replay_from_a_trimmed_id_delivers_the_whole_retained_suffix(
    redis_kind,
) -> None:
    """A replay cursor is exclusive, so it belongs *before* the first retained entry.

    When Redis has trimmed past a saved position the store replays what is left. The
    saved id is the cursor for that even though Redis no longer holds it — a read from a
    trimmed id returns the whole retained suffix. `first_retained_id` is the wrong cursor,
    and #85 is exactly that mistake: both halves are pinned here, because the store's
    rebase is only safe while Redis keeps behaving this way.
    """
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            reader = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))
            # Two, flushed. **Published and drained in two batches**, because the reader
            # records what it put on the queue and not what the test took off: one batch
            # of ten would leave the saved position at the head whatever `take` asked for.
            for n in range(2):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(reader, 2)
            saved = reader.positions[key]

            # Eight more, read and acked but never flushed, so the group ends at the head
            # while the saved position stays two entries in.
            for n in range(2, 10):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(reader, 8)
            bus.unsubscribe(reader)

            # Redis keeps the newest five — bids 5..9. The saved position is three
            # entries behind the first one that survived.
            await bus.client.xtrim(key, maxlen=5, approximate=False)

            restarted = bus.subscribe(
                "store", maxsize=100, lossless=True, start_ids={key: saved}
            )
            await live(bus)
            gap = (await bus.replay_gaps(restarted, {key: saved}))[key]
            replayed = await take(restarted, 5)
            extra = await drain(restarted)

            # The same suffix asked for from `first_retained_id`, in a group of its own.
            # This is what the store did until #85, and it is one entry short.
            from_boundary = bus.subscribe(
                "boundary",
                maxsize=100,
                lossless=True,
                start_ids={key: Position(gap.first_retained_id, gap.trimmed)},
            )
            await live(bus)
            short = await drain(from_boundary)
            return (
                [event.bid for event in replayed],
                [event.bid for event in extra],
                [event.bid for event in short],
                gap.lost,
                gap.trimmed,
                saved.index,
            )

    bids, extra, short, lost, trimmed, saved_index = run(scenario())
    assert bids == [5.0, 6.0, 7.0, 8.0, 9.0], "the first retained entry is not optional"
    assert extra == []
    # bids 2, 3 and 4 — the three between the saved position and the trim boundary.
    assert (trimmed, saved_index) == (5, 2)
    # **The reported loss is the real one**: everything published after the saved
    # position, less everything the replay handed over.
    assert lost == (10 - saved_index) - len(bids) == 3
    assert short == [6.0, 7.0, 8.0, 9.0], "a cursor on the boundary skips the boundary"


def test_a_configured_stream_missing_from_saved_positions_starts_at_the_group_start(
    redis_kind,
) -> None:
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            key = stream_name(quote_on("ETH", 0.0))
            for n in range(3):
                bus.publish(quote_on("ETH", float(n)))
            await settle(bus)
            restarted = bus.subscribe(
                "new-store",
                maxsize=100,
                lossless=True,
                start_ids={
                    stream_name(quote_on("BTC", 0.0)): Position("0-0", 0)
                },
                group_start="0",
            )
            await live(bus)
            return key, [event.bid for event in await take(restarted, 3)]

    key, bids = run(scenario())
    assert key.endswith(":ETH")
    assert bids == [0.0, 1.0, 2.0]


def test_replay_positions_keep_the_saved_index_and_count_every_stream_entry(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote_on("BTC", 0.0))
            for n in range(5):
                bus.publish(quote_on("BTC", float(n)))
            await settle(bus)
            await take(first, 5)
            saved = first.positions[key]
            bus.unsubscribe(first)
            for n in range(5, 8):
                bus.publish(quote_on("BTC", float(n)))
            await settle(bus)
            restarted = bus.subscribe(
                "store",
                maxsize=100,
                lossless=True,
                start_ids={key: saved},
            )
            await live(bus)
            await take(restarted, 3)
            return saved.index, restarted.positions[key].index

    saved_index, current_index = run(scenario())
    assert current_index == saved_index + 3


def test_a_pause_span_withholds_its_replay_range_but_not_the_entries_outside_it(
    redis_kind,
):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))
            bus.publish(quote(0.0))
            await settle(bus)
            await take(first, 1)
            saved = first.positions[key]
            for n in range(1, 5):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(first, 4)
            end = first.last_ids[key]
            bus.unsubscribe(first)
            restarted = bus.subscribe(
                "store",
                maxsize=100,
                lossless=True,
                start_ids={key: saved},
                skip=(Span({key: saved.id}, {key: end}),),
            )
            await live(bus)
            await until(lambda: not restarted.behind[key], what="replay completed")
            bus.publish(quote(5.0))
            await settle(bus)
            replayed = await take(restarted, 1)
            return replayed[0].bid, restarted.span_dropped, restarted.positions[key]

    bid, dropped, position = run(scenario())
    assert bid == 5.0
    assert dropped == 4
    assert position.index == 6


def test_a_pause_span_on_one_stream_does_not_withhold_the_other(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            btc_key = stream_name(quote_on("BTC", 0.0))
            eth_key = stream_name(quote_on("ETH", 0.0))
            bus.publish(quote_on("BTC", 0.0))
            bus.publish(quote_on("ETH", 0.0))
            await settle(bus)
            await take(first, 2)
            saved = {btc_key: first.positions[btc_key], eth_key: first.positions[eth_key]}
            bus.publish(quote_on("BTC", 1.0))
            bus.publish(quote_on("ETH", 1.0))
            await settle(bus)
            await take(first, 2)
            bus.unsubscribe(first)
            restarted = bus.subscribe(
                "store",
                maxsize=100,
                lossless=True,
                start_ids=saved,
                skip=(Span({btc_key: saved[btc_key].id}, None),),
            )
            await live(bus)
            await until(
                lambda: not restarted.behind[btc_key], what="BTC replay completed"
            )
            return (
                [event.bid for event in await take(restarted, 1)],
                restarted.span_dropped,
            )

    bids, dropped = run(scenario())
    assert bids == [1.0]
    assert dropped == 1


def test_replay_gaps_reports_exact_trimmed_loss(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))
            for n in range(10):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(first, 10)
            saved = first.positions[key]
            await bus.client.xtrim(key, maxlen=5, approximate=False)
            bus.unsubscribe(first)
            restarted = bus.subscribe(
                "store", maxsize=100, lossless=True, start_ids={key: saved}
            )
            await live(bus)
            gaps = await bus.replay_gaps(restarted, {key: saved})
            return gaps[key]

    gap = run(scenario())
    assert gap.lost == 0
    assert gap.trimmed == 5


def test_replay_gaps_reports_loss_past_a_saved_index(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            first = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))
            for n in range(10):
                bus.publish(quote(float(n)))
            await settle(bus)
            entries = await bus.client.xrange(key)
            saved = Position(entries[0][0].decode(), 1)
            await take(first, 10)
            await bus.client.xtrim(key, maxlen=5, approximate=False)
            bus.unsubscribe(first)
            restarted = bus.subscribe(
                "store", maxsize=100, lossless=True, start_ids={key: saved}
            )
            await live(bus)
            gaps = await bus.replay_gaps(restarted, {key: saved})
            return gaps[key]

    gap = run(scenario())
    assert gap.lost == 4
    assert gap.trimmed == 5
    assert gap.first_retained_id is not None


def test_replay_gaps_distinguishes_a_missing_group_and_an_empty_stream(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            key = stream_name(quote(0.0))
            bus.publish(quote(1.0))
            await settle(bus)
            saved = Position("0-0", 0)
            missing = bus.subscribe(
                "missing-group", maxsize=100, lossless=True, start_ids={key: saved}
            )
            await live(bus)
            missing_gap = (await bus.replay_gaps(missing, {key: saved}))[key]

            empty_key = stream_name(quote_on("ETH", 0.0))
            existing_empty = bus.subscribe("empty-group", maxsize=100, lossless=True)
            await live(bus)
            bus.unsubscribe(existing_empty)
            empty = bus.subscribe(
                "empty-group",
                maxsize=100,
                lossless=True,
                start_ids={empty_key: Position("0-0", 0)},
            )
            await live(bus)
            empty_gap = (
                await bus.replay_gaps(empty, {empty_key: Position("0-0", 0)})
            )[empty_key]
            missing_empty = bus.subscribe(
                "missing-empty",
                maxsize=100,
                lossless=True,
                start_ids={empty_key: Position("0-0", 0)},
            )
            await live(bus)
            missing_empty_gap = (
                await bus.replay_gaps(
                    missing_empty, {empty_key: Position("0-0", 0)}
                )
            )[empty_key]
            return missing_gap, empty_gap, missing_empty_gap

    missing, empty, missing_empty = run(scenario())
    assert missing.lost is None
    assert empty.first_retained_id is None
    assert empty.lost == 0
    assert missing_empty.first_retained_id is None
    assert missing_empty.lost is None


def test_lossless_behind_clears_after_a_short_read(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url, read_count=2) as bus:
            subscription = bus.subscribe("behind", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))
            gate = asyncio.Event()
            client = bus.client
            assert client is not None
            original = client.xreadgroup
            calls = 0

            async def blocked_second_read(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    await gate.wait()
                return await original(*args, **kwargs)

            client.xreadgroup = blocked_second_read
            for n in range(3):
                bus.publish(quote(float(n)))
            await settle(bus)
            await until(lambda: subscription.behind[key], what="full read marks behind")
            assert subscription.behind_streams() == (key,)
            await take(subscription, 2)
            gate.set()
            await take(subscription, 1)
            await until(
                lambda: not subscription.behind[key], what="short read clears behind"
            )
            return subscription.behind_streams()

    assert run(scenario()) == ()


def test_lossless_replay_is_behind_until_its_saved_range_is_drained(redis_kind):
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url, read_count=2) as bus:
            first = bus.subscribe("store", maxsize=100, lossless=True)
            await live(bus)
            key = stream_name(quote(0.0))
            for n in range(4):
                bus.publish(quote(float(n)))
            await settle(bus)
            await take(first, 1)
            saved = first.positions[key]
            await take(first, 3)
            bus.unsubscribe(first)
            for n in range(4, 7):
                bus.publish(quote(float(n)))
            await settle(bus)
            gate = asyncio.Event()
            client = bus.client
            assert client is not None
            original = client.xread

            async def blocked_xread(*args, **kwargs):
                result = await original(*args, **kwargs)
                await gate.wait()
                return result

            client.xread = blocked_xread
            restarted = bus.subscribe(
                "store", maxsize=100, lossless=True, start_ids={key: saved}
            )
            await until(lambda: restarted.behind[key], what="replay marks behind")
            gate.set()
            await live(bus)
            await take(restarted, 3)
            await until(lambda: not restarted.behind[key], what="replay clears behind")

    run(scenario())


def test_every_batch_write_trims_by_age_and_nothing_younger_goes(redis_kind) -> None:
    """§4: `XTRIM MINID ~`, thirty minutes, in the same pipeline as that batch's `XADD`s.

    A count is a guess about rate; thirty minutes is the promise made to a restarting
    store. So the floor is a clock — this one injected, which is what makes "thirty-one
    minutes later" an assignment rather than a wait.

    Five hundred entries because `~` removes whole macro nodes rather than counting: at
    the default hundred entries a node, a stream of five would be trimmed by nothing at
    all and the assertion would be about listpack internals rather than about retention.
    """
    kind, url = redis_kind
    clock = {"now": NOW}

    async def scenario():
        async with open_bus(kind, url, clock=lambda: clock["now"]) as bus:
            key = stream_name(quote(1.0))
            for n in range(500):
                bus.publish(quote(float(n)))
            await settle(bus)
            before = await bus.client.xlen(key)

            # Nothing has aged out yet, so the next batch's trim must remove nothing.
            bus.publish(quote(500.0))
            await settle(bus)
            untrimmed = await bus.client.xlen(key)

            # **Redis stamps entries with its own clock**, so "thirty-one minutes later"
            # is measured from the newest entry's own id rather than from this machine's
            # wall clock. Still an assignment, and still nothing waits.
            newest = await bus.client.xrevrange(key, count=1)
            newest_ms = int(newest[0][0].split(b"-")[0])
            clock["now"] = newest_ms / 1000 + 31 * 60
            bus.publish(quote(501.0))
            await settle(bus)
            after = await bus.client.xlen(key)
            return before, untrimmed, after

    before, untrimmed, after = run(scenario())
    assert before == 500
    assert untrimmed == 501, "a trim floor half an hour in the past must remove nothing"
    assert after < 501, "thirty-one minutes on, the oldest entries must be gone"


def test_a_drop_oldest_reader_counts_what_it_skipped_rather_than_falling_behind(
    redis_kind,
) -> None:
    """The thing to notice, made a test.

    A reader that reads `>` in a group never drops — it accumulates a pending list, and a
    screen fed from one would fall further and further behind while reporting nothing.
    Ours reads outside any group, and when more than a queue's worth is waiting it jumps
    to the head and **counts what it jumped over**. Redis trims silently; ours never has.
    """
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            screen = bus.subscribe("screen", maxsize=8)
            await live(bus)
            # One burst far past both the queue and what a single read takes, so the
            # reader's first read comes back full and the backlog is still there behind
            # it. That is the shape a stalled screen makes under load.
            for n in range(1_200):
                bus.publish(quote(float(n)))
            await settle(bus)
            await until(lambda: screen.resyncs > 0, what="the screen jumped")
            latest = await take(screen, 1)
            return latest[0].bid, screen.skipped, screen.resyncs, bus.stats()

    bid, skipped, resyncs, stats = run(scenario())
    assert resyncs > 0, "a reader that far behind must jump rather than replay"
    assert skipped > 0, "and it must say how much it jumped over"
    assert stats["screen"]["skipped"] == skipped
    assert bid >= 1_000.0, "after a jump the queue holds recent records, not a replay"


def test_the_publisher_reports_what_it_wrote_and_what_it_trimmed(redis_kind) -> None:
    """The batch metrics the interval measurement is read off, and `/health` could."""
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            for n in range(30):
                bus.publish(quote(float(n)))
            await settle(bus)
            return bus.publisher()

    stats = run(scenario())
    assert stats["published"] == 30
    assert stats["written"] == 30
    assert stats["batches"] >= 1
    assert stats["trims"] >= 1, "every batch write trims"
    assert stats["failures"] == 0
    assert stats["outbox"] == 0


# ----------------------------------------------------------- the loud failures


def test_a_publisher_that_cannot_reach_redis_fails_at_startup(monkeypatch) -> None:
    """#57's user story 22: a feed that starts with no Redis must fail loudly rather than
    buffer forever, so a misconfiguration is visible at once.

    Driven with a client that refuses rather than with a closed port, because no test here
    opens a socket at all. What bounds the wait in production is the connect timeout this
    also asserts is passed down.
    """
    import redis.exceptions

    built: dict[str, object] = {}

    class Refusing:
        def __init__(self, **kwargs) -> None:
            built.update(kwargs)

        async def ping(self):
            raise redis.exceptions.ConnectionError("connection refused")

        async def aclose(self) -> None:
            return None

    config = BusConfig(url="redis://127.0.0.1:6399", connect_timeout_seconds=2.0)
    bus = RedisBus(config, client_factory=lambda cfg: Refusing(**cfg.client_kwargs()))

    with pytest.raises(BusUnavailable) as raised:
        run(bus.start())

    assert "redis://127.0.0.1:6399" in str(raised.value)
    assert "connection refused" in str(raised.value)
    assert built["socket_connect_timeout"] == 2.0, "an unbounded dial is not a failure"
    assert built["socket_timeout"] == 2.0


def test_an_event_that_cannot_be_keyed_is_counted_and_never_raised(redis_kind) -> None:
    """`publish` is called between two reads of the venue's socket.

    So something that is not an event, or an event that cannot be keyed, must not raise
    out of it: an exception there stops the handler reading,
    the receive buffer fills, and Delta closes the connection — the failure `fanout.py`
    exists to prevent. It is logged and counted instead.
    """
    kind, url = redis_kind

    async def scenario():
        async with open_bus(kind, url) as bus:
            bus.publish("not an event at all")
            bus.publish(quote(2.0))
            await settle(bus)
            return bus.publisher()

    stats = run(scenario())
    assert stats["unroutable"] == 1
    assert stats["written"] == 1, "the good event still went"


# ------------------------------------------------------- selected by configuration


def test_the_default_bus_is_the_in_process_fan_out(monkeypatch) -> None:
    """The acceptance line: **nothing changes for anyone who does not ask.**

    And a typo is not asking. `DELTA_BUS=redis-streams` is a mistake, and a mistake that
    silently picked a broker would be a process that fails to start for a reason nobody
    typed.
    """
    from deltapayoff import main

    monkeypatch.delenv("DELTA_BUS", raising=False)
    assert isinstance(main.build_bus(), FanOut)

    monkeypatch.setenv("DELTA_BUS", "redis-streams")
    assert isinstance(main.build_bus(), FanOut)


def test_asking_for_redis_builds_a_redis_bus_from_the_environment(monkeypatch) -> None:
    """One variable selects it and four more configure it. Read at start-up, never at
    import, so which bus a process runs is a deployment decision."""
    from deltapayoff import main

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_REDIS_URL", "redis://redis:6379")
    monkeypatch.setenv("DELTA_BUS_BATCH_MS", "100")
    monkeypatch.setenv("DELTA_BUS_RETENTION_SECONDS", "900")
    monkeypatch.setenv("DELTA_LIVE_UNDERLYINGS", "BTC,ETH")

    bus = main.build_bus()

    assert isinstance(bus, RedisBus)
    assert bus.config.url == "redis://redis:6379"
    assert bus.config.batch_ms == 100
    assert bus.config.retention_seconds == 900
    assert "md.option_quote:DELTA:ETH" in bus.config.streams()
    # Nothing connected. Building is separated from starting here for the same reason
    # `build_feed_stack` separates them: a process with no live feed still has the whole
    # structure present and introspectable.
    assert bus.client is None


def test_bus_config_from_env_ignores_the_legacy_environment_slot(monkeypatch) -> None:
    monkeypatch.setenv("DELTA_BUS_ENV", "prod")

    config = BusConfig.from_env(underlyings=("BTC", "ETH"))

    assert not hasattr(config, "env")
    assert all(not key.startswith(("dev:", "prod:")) for key in config.streams())


def test_the_app_refuses_to_start_when_the_redis_it_was_told_to_use_is_absent(
    monkeypatch,
) -> None:
    """#57's user story 22, at the level a person meets it: the process exits.

    Driven with a client that refuses rather than a closed port, because no test here
    opens a socket. What is proven is the wiring — that `BusUnavailable` reaches out of
    the lifespan instead of being swallowed the way `DeltaUnavailable` deliberately is,
    and that the venue's HTTP client is never opened at all: with `DELTA_BUS=redis` the
    engine is a consumer only (#62), so the Redis branch fails before `DeltaClient`
    exists.
    """
    import redis.exceptions
    from fastapi.testclient import TestClient

    from deltapayoff import main, redis_bus

    closed: list[str] = []

    class Refusing:
        def __init__(self, **kwargs) -> None:
            pass

        async def ping(self):
            raise redis.exceptions.ConnectionError("connection refused")

        async def aclose(self) -> None:
            closed.append("redis")

    class StubDeltaClient:
        """Stands in for `DeltaClient` inside the lifespan; opens nothing. The fourth
        copy of a four-line stub in this suite, for the reason the third gives."""

        async def __aenter__(self) -> StubDeltaClient:
            return self

        async def aclose(self) -> None:
            closed.append("delta")

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_REDIS_URL", "redis://127.0.0.1:6399")
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient, raising=True)
    monkeypatch.setattr(
        redis_bus, "_default_client", lambda config: Refusing(), raising=True
    )

    with pytest.raises(BusUnavailable) as raised:
        with TestClient(main.app):
            pass  # pragma: no cover - start-up never completes

    assert "redis://127.0.0.1:6399" in str(raised.value)
    assert "DELTA_BUS" in str(raised.value), "the message must say how to turn it off"
    assert "delta" not in closed, "Redis must fail before the Delta client is entered"
