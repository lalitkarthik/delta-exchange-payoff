from __future__ import annotations

import ast
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
import redis.asyncio

from deltapayoff import discord_alerts
from deltapayoff.alert_consumer import GROUP_NAME, AlertConsumer
from deltapayoff.events import Alert, ConnectionState, FeedConnection
from deltapayoff.events.redis_wire import encode
from deltapayoff.redis_bus import BusConfig, RedisBus

TS = datetime(2026, 9, 12, tzinfo=timezone.utc)

#: Fixture-only stand-in for `AlertConsumer`'s real wall clock, fixed well before every
#: alert timestamp in this file so no dispatch test is accidentally starved by the
#: staleness drop `_run_once` applies (#86) -- that mechanism gets its own tests below
#: with wall clocks placed deliberately around the alerts under test. Never `now()` in a
#: test, per this repo's `AGENTS.md`.
EARLY_WALL_CLOCK = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _alert(code: str = "connection_silent", *, ts: datetime = TS) -> Alert:
    return Alert(
        source="feed",
        ts_received=ts,
        severity="error",
        code=code,
        detail="the feed stopped",
        adapter="delta",
    )


def _factory(server: Any):
    def client_factory(_config: BusConfig):
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)

    return client_factory


@pytest.fixture(params=["redis-fake", "redis-docker"])
def redis_kind(request: pytest.FixtureRequest) -> tuple[str, str | None]:
    """Run Redis group assertions against fakeredis and the test Redis service."""
    if request.param == "redis-docker":
        return request.param, request.getfixturevalue("redis_server")
    return request.param, None


def _redis_client(
    kind: str, url: str | None, server: Any | None
) -> Any:
    if kind == "redis-fake":
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    return redis.asyncio.Redis.from_url(
        url or "redis://127.0.0.1:6399", decode_responses=False
    )


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _poster() -> tuple[discord_alerts.DiscordPoster, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def post(_url: str, payload: dict[str, Any]) -> SimpleNamespace:
        calls.append(payload)
        return SimpleNamespace(status=200)

    return discord_alerts.DiscordPoster(post), calls


def _consumer_for_fanout(
    clock: _Clock,
) -> tuple[AlertConsumer, Any, list[dict[str, Any]]]:
    from deltapayoff.fanout import FanOut

    poster, calls = _poster()
    bus = FanOut()
    consumer = AlertConsumer(
        bus,
        poster=poster,
        gate=discord_alerts.AlertGate(),
        webhook_url="https://discord.test/webhook",
        clock=clock,
        wall_clock=lambda: EARLY_WALL_CLOCK,
    )
    consumer.subscribe()
    return consumer, bus, calls


def test_redis_alert_subscription_uses_its_own_tail_group_and_alert_stream() -> None:
    server = fakeredis.aioredis.FakeServer()

    async def scenario() -> None:
        bus = RedisBus(
            BusConfig(underlyings=(), read_block_ms=0), client_factory=_factory(server)
        )
        try:
            subscription = AlertConsumer(bus).subscribe()
            assert subscription.group == GROUP_NAME
            assert subscription.group_start == "$"
            assert subscription.streams == ("alert",)
        finally:
            await bus.aclose()

    asyncio.run(scenario())


def test_a_fresh_tail_group_does_not_deliver_alerts_published_before_subscription(
) -> None:
    server = fakeredis.aioredis.FakeServer()

    async def scenario() -> None:
        bus = RedisBus(
            BusConfig(
                underlyings=(), read_block_ms=0, idle_sleep_seconds=0.001
            ),
            client_factory=_factory(server),
        )
        try:
            await bus.start()
            bus.publish(_alert())
            assert await bus.flush() == 1

            subscription = AlertConsumer(bus).subscribe()
            await bus.ready()

            assert subscription.queue.empty()

            bus.publish(_alert("poll_failing"))
            assert await bus.flush() == 1
            received = await asyncio.wait_for(subscription.queue.get(), timeout=1.0)
            assert isinstance(received, Alert)
            assert received.code == "poll_failing"
        finally:
            await bus.aclose()

    asyncio.run(scenario())


def test_an_existing_alert_group_can_be_joined_again_without_a_busygroup_failure(
    redis_kind,
) -> None:
    kind, url = redis_kind
    server = fakeredis.aioredis.FakeServer() if kind == "redis-fake" else None
    config = BusConfig(
        url=url or "redis://127.0.0.1:6399",
        underlyings=(),
        read_block_ms=0,
        idle_sleep_seconds=0.001,
    )
    factory = _factory(server) if server is not None else None

    async def scenario() -> None:
        cleanup = _redis_client(kind, url, server)
        await cleanup.delete("alert")
        await cleanup.aclose()

        first = RedisBus(config, client_factory=factory)
        try:
            AlertConsumer(first).subscribe()
            await first.start()
        finally:
            await first.aclose()

        second = RedisBus(config, client_factory=factory)
        try:
            second_subscription = AlertConsumer(second).subscribe()
            await second.start()
            assert second_subscription.positioned.is_set()

            # `positioned` is set even when the reader died while positioning (the bus
            # sets it in a `finally`, so `ready()` never hangs on a dead reader) -- so
            # on its own it proves nothing about a BUSYGROUP failure being handled. The
            # reader task itself is the fact that would be false if `_ensure_group`'s
            # BUSYGROUP branch were removed: `_read` logs the error and re-raises, so a
            # `RedisBus` subclass that always raises BUSYGROUP leaves this task done,
            # with that `ResponseError` as its exception, well before anything is
            # published.
            reader = second._readers[second_subscription.name]
            assert not reader.done(), (
                "the rejoined group's reader task ended while positioning instead of "
                "settling into its read loop"
            )

            second.publish(_alert("rejoined"))
            assert await second.flush() == 1
            received = await asyncio.wait_for(
                second_subscription.queue.get(), timeout=1.0
            )
            assert isinstance(received, Alert)
            assert received.code == "rejoined"

            assert not reader.done(), (
                "the rejoined group's reader task died after delivering the message"
            )
        finally:
            await second.aclose()

        cleanup = _redis_client(kind, url, server)
        await cleanup.delete("alert")
        await cleanup.aclose()

    asyncio.run(scenario())


def test_pending_alerts_from_a_previous_consumer_are_not_replayed_on_restart(
    redis_kind,
) -> None:
    kind, url = redis_kind
    server = fakeredis.aioredis.FakeServer() if kind == "redis-fake" else None
    config = BusConfig(
        url=url or "redis://127.0.0.1:6399",
        underlyings=(),
        read_block_ms=0,
        idle_sleep_seconds=0.001,
    )
    factory = _factory(server) if server is not None else None

    async def scenario() -> None:
        previous = _redis_client(kind, url, server)
        bus = None
        try:
            await previous.delete("alert")
            await previous.xgroup_create("alert", GROUP_NAME, id="0", mkstream=True)
            await previous.xadd("alert", encode(_alert("connection_silent")))
            await previous.xadd("alert", encode(_alert("poll_failing")))
            pending = await previous.xreadgroup(
                GROUP_NAME, "previous-consumer", {"alert": ">"}, count=10
            )
            assert len(pending[0][1]) == 2

            bus = RedisBus(config, client_factory=factory)
            poster, calls = _poster()
            consumer = AlertConsumer(
                bus,
                poster=poster,
                webhook_url="https://discord.test/webhook",
                clock=_Clock(),
                wall_clock=lambda: EARLY_WALL_CLOCK,
            )
            consumer.subscribe()
            await bus.start()
            await bus.ready()
            assert consumer.subscription.queue.empty()
            assert calls == []

            bus.publish(_alert("fresh"))
            assert await bus.flush() == 1
            await consumer._run_once()
            assert consumer.subscription.queue.empty()
            assert len(calls) == 1
            assert "fresh" in calls[0]["content"]
        finally:
            await previous.aclose()
            if bus is not None:
                await bus.aclose()
            cleanup = _redis_client(kind, url, server)
            await cleanup.delete("alert")
            await cleanup.aclose()

    asyncio.run(scenario())


def test_alerts_published_while_no_consumer_ran_are_dropped_as_stale(
    redis_kind,
) -> None:
    """#86: a rejoin must not post what fired during this consumer's own downtime.

    Mirrors the audit's fakeredis repro (`scratchpad/repro_replay.py` scenario 3): a
    consumer receives one alert, is closed, three more are published with nothing
    reading, and a new consumer starts on the same group. `_read_group` still delivers
    all three -- Redis's `>` cursor has no notion of "while nothing read" -- so the
    guarantee has to live in `AlertConsumer._run_once`, which is what this asserts.
    """
    kind, url = redis_kind
    server = fakeredis.aioredis.FakeServer() if kind == "redis-fake" else None
    config = BusConfig(
        url=url or "redis://127.0.0.1:6399",
        underlyings=(),
        read_block_ms=0,
        idle_sleep_seconds=0.001,
    )
    factory = _factory(server) if server is not None else None

    stale_ts = TS
    started_at = datetime(2026, 9, 12, 0, 5, tzinfo=timezone.utc)
    fresh_ts = datetime(2026, 9, 12, 0, 10, tzinfo=timezone.utc)

    async def scenario() -> None:
        cleanup = _redis_client(kind, url, server)
        await cleanup.delete("alert")
        await cleanup.aclose()

        first = RedisBus(config, client_factory=factory)
        try:
            first_subscription = AlertConsumer(first).subscribe()
            await first.start()
            first.publish(_alert("while up", ts=stale_ts))
            assert await first.flush() == 1
            received = await asyncio.wait_for(
                first_subscription.queue.get(), timeout=1.0
            )
            assert received.code == "while up"
        finally:
            await first.aclose()

        # Nothing reads while these three publish directly onto the stream -- the
        # "published while down" case the existing pending-replay test does not cover.
        writer = _redis_client(kind, url, server)
        try:
            for i in range(3):
                await writer.xadd("alert", encode(_alert(f"stale {i}", ts=stale_ts)))
        finally:
            await writer.aclose()

        second = RedisBus(config, client_factory=factory)
        poster, calls = _poster()
        clock = _Clock(0.0)
        try:
            consumer = AlertConsumer(
                second,
                poster=poster,
                webhook_url="https://discord.test/webhook",
                clock=clock,
                wall_clock=lambda: started_at,
            )
            consumer.subscribe()
            await second.start()

            # A fresh alert reusing a stale alert's code: if the dropped backlog had
            # touched the gate, this would be collapsed instead of posted, so posting
            # it also pins the "rate limit and collapse rule still hold" criterion.
            second.publish(_alert("stale 0", ts=fresh_ts))
            assert await second.flush() == 1

            # Each dispatch clears both the collapse window and the global floor
            # (`ALERT_COLLAPSE_WINDOW_SECONDS` and `ALERT_MIN_POST_INTERVAL_SECONDS`)
            # ahead of the next one, so a static clock cannot rate-limit a later item
            # into looking dropped for the wrong reason: only the staleness check can
            # explain the count and content asserted below.
            for _ in range(4):
                clock.value += 1000.0
                await consumer._run_once()

            assert consumer.subscription.queue.empty()
            assert len(calls) == 1, [c["content"] for c in calls]
            assert "stale 0" in calls[0]["content"]
            assert "collapsed" not in calls[0]["content"]
        finally:
            await second.aclose()

        cleanup = _redis_client(kind, url, server)
        await cleanup.delete("alert")
        await cleanup.aclose()

    asyncio.run(scenario())


def test_fanout_alert_subscription_is_lossless_without_a_redis_event_filter() -> None:
    from deltapayoff.fanout import FanOut

    subscription = AlertConsumer(FanOut()).subscribe()

    assert subscription.lossless is True


def test_fanout_dispatch_collapses_repeats_and_passes_the_count_on_the_next_post(
) -> None:
    clock = _Clock()
    consumer, bus, calls = _consumer_for_fanout(clock)

    async def scenario() -> None:
        for _ in range(3):
            bus.publish(_alert())
            await consumer._run_once()
        clock.value += discord_alerts.ALERT_COLLAPSE_WINDOW_SECONDS
        bus.publish(_alert())
        await consumer._run_once()

    asyncio.run(scenario())

    assert len(calls) == 2
    assert calls[1]["content"].endswith(" (collapsed 2 times since last post)")


def test_fanout_dispatch_rate_limits_different_signatures() -> None:
    clock = _Clock()
    consumer, bus, calls = _consumer_for_fanout(clock)

    async def scenario() -> None:
        bus.publish(_alert("connection_silent"))
        await consumer._run_once()
        clock.value += discord_alerts.ALERT_MIN_POST_INTERVAL_SECONDS - 0.1
        bus.publish(_alert("poll_failing"))
        await consumer._run_once()

    asyncio.run(scenario())

    assert len(calls) == 1


def test_a_poster_refusal_does_not_lose_a_collapsed_count() -> None:
    clock = _Clock(0.0)
    calls: list[dict[str, Any]] = []
    responses = iter(
        (
            SimpleNamespace(status=200),
            SimpleNamespace(status=429, json_body={"retry_after": 10.0}),
            SimpleNamespace(status=200),
        )
    )

    async def post(_url: str, payload: dict[str, Any]) -> SimpleNamespace:
        calls.append(payload)
        return next(responses)

    from deltapayoff.fanout import FanOut

    bus = FanOut()
    consumer = AlertConsumer(
        bus,
        poster=discord_alerts.DiscordPoster(post),
        gate=discord_alerts.AlertGate(),
        webhook_url="https://discord.test/webhook",
        clock=clock,
        wall_clock=lambda: EARLY_WALL_CLOCK,
    )
    consumer.subscribe()

    async def scenario() -> None:
        bus.publish(_alert())
        await consumer._run_once()

        clock.value = 1.0
        bus.publish(_alert())
        await consumer._run_once()

        clock.value = 299.0
        bus.publish(_alert("poll_failing"))
        await consumer._run_once()

        # The gate is ready to reveal the folded alert, but Discord is still in the
        # backoff caused by the different signature's 429.
        clock.value = 301.0
        bus.publish(_alert())
        await consumer._run_once()

        clock.value = 309.0
        bus.publish(_alert())
        await consumer._run_once()

    asyncio.run(scenario())

    assert len(calls) == 3
    assert calls[-1]["content"].endswith(" (collapsed 1 times since last post)")


def test_a_pause_produces_no_discord_post(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pause is ignored *quietly*, which is the only observable that pins the guard.

    `docs/design/events.md` lines 152-153: "`degraded` does not alert, and nor does a
    `pause`" -- a pause is something a person just did. The consumer honours that by
    refusing every event that is not an `Alert`, in `_run_once`.

    Asserting only `calls == []` did not test that guard at all. `measured` here on
    2026-09-12: with `if not isinstance(event, Alert): return` deleted, the pause falls
    into the dispatch body, `gate.decide()` raises `AttributeError` reaching
    `event.code`, the handler swallows it -- and `calls` is still empty, so both pause
    tests went on passing. All 32 tests in this file, `test_discord_alerts.py` and
    `test_alert_main.py` passed with the guard gone. The error log is what separates
    "ignored by design" from "crashed on the way to the same place", so it is asserted.
    """
    clock = _Clock()
    consumer, bus, calls = _consumer_for_fanout(clock)

    paused = FeedConnection(
        source="feed",
        ts_received=TS,
        adapter="delta",
        to_state=ConnectionState.STOPPED,
        reason="paused",
    )

    async def scenario() -> None:
        bus.publish(paused)
        await consumer._run_once()

    with caplog.at_level(logging.DEBUG, logger="deltapayoff.alert_consumer"):
        asyncio.run(scenario())

    assert calls == []
    # Scoped to this module's own logger by name. `caplog` installs its handler on the
    # root, so it also captures the poster's delivery line from another logger, and an
    # unfiltered list assertion here passed or failed depending on test ordering.
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "deltapayoff.alert_consumer"
    ] == []


def test_a_paused_feed_followed_by_an_alert_posts_only_the_real_alert(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The pause must cost the real alert nothing -- not a slot, not a log line."""
    clock = _Clock()
    consumer, bus, calls = _consumer_for_fanout(clock)

    paused = FeedConnection(
        source="feed",
        ts_received=TS,
        adapter="delta",
        to_state=ConnectionState.STOPPED,
        reason="paused",
    )

    async def scenario() -> None:
        bus.publish(paused)
        await consumer._run_once()
        bus.publish(_alert())
        await consumer._run_once()

    with caplog.at_level(logging.DEBUG, logger="deltapayoff.alert_consumer"):
        asyncio.run(scenario())

    assert len(calls) == 1
    assert "connection_silent" in calls[0]["content"]
    # Scoped to this module's own logger by name. `caplog` installs its handler on the
    # root, so it also captures the poster's delivery line from another logger, and an
    # unfiltered list assertion here passed or failed depending on test ordering.
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "deltapayoff.alert_consumer"
    ] == []
    # The pause did not spend the global floor either: had it reached the gate, the
    # real alert 0 s later would have been rate-limited instead of posted.
    assert consumer.gate.rate_limited_count == 0


def test_a_bad_alert_does_not_kill_the_consumer_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    poster, calls = _poster()
    from deltapayoff.fanout import FanOut

    class FailingOnceGate:
        """A gate that raises once, and otherwise satisfies the whole gate interface.

        `commit_post` and `rollback_post` are here because the consumer calls them
        directly rather than through a `getattr` fallback. That is deliberate: behind a
        silent fallback an incomplete gate reverts to the defect the #66 review caught --
        an occurrence spent although Discord never saw the alert -- and this double was
        incomplete in exactly that way until the fallback was removed.
        """

        def __init__(self) -> None:
            self.calls = 0
            self.commits = 0
            self.rollbacks = 0

        def decide(self, **_kwargs: Any) -> discord_alerts.GateDecision:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bad alert")
            return discord_alerts.GateDecision(
                post=True, collapsed_count=0, rate_limited=False
            )

        def commit_post(self) -> None:
            self.commits += 1

        def rollback_post(self) -> None:
            self.rollbacks += 1

    bus = FanOut()
    consumer = AlertConsumer(
        bus,
        poster=poster,
        gate=FailingOnceGate(),
        webhook_url="https://discord.test/webhook",
        clock=clock,
        wall_clock=lambda: EARLY_WALL_CLOCK,
    )
    consumer.subscribe()

    async def scenario() -> None:
        bus.publish(_alert("first"))
        await consumer._run_once()
        bus.publish(_alert("second"))
        await consumer._run_once()

    with caplog.at_level(logging.ERROR, logger="deltapayoff.alert_consumer"):
        asyncio.run(scenario())

    assert len(calls) == 1
    assert "second" in calls[0]["content"]
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_run_auto_subscribes_and_dispatches_until_cancelled() -> None:
    clock = _Clock()
    poster, calls = _poster()
    from deltapayoff.fanout import FanOut

    bus = FanOut()
    consumer = AlertConsumer(
        bus,
        poster=poster,
        gate=discord_alerts.AlertGate(),
        webhook_url="https://discord.test/webhook",
        clock=clock,
        wall_clock=lambda: EARLY_WALL_CLOCK,
    )

    async def scenario() -> None:
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0)
        bus.publish(_alert())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert len(calls) == 1


def test_redis_wire_alert_reaches_the_poster() -> None:
    server = fakeredis.aioredis.FakeServer()
    clock = _Clock()
    poster, calls = _poster()

    async def scenario() -> None:
        bus = RedisBus(
            BusConfig(
                underlyings=(), read_block_ms=0, idle_sleep_seconds=0.001
            ),
            client_factory=_factory(server),
        )
        consumer = AlertConsumer(
            bus,
            poster=poster,
            gate=discord_alerts.AlertGate(),
            webhook_url="https://discord.test/webhook",
            clock=clock,
            wall_clock=lambda: EARLY_WALL_CLOCK,
        )
        consumer.subscribe()
        try:
            await bus.start()
            bus.publish(_alert())
            assert await bus.flush() == 1
            await consumer._run_once()
        finally:
            await bus.aclose()

    asyncio.run(scenario())

    assert len(calls) == 1
    for field in ("error", "connection_silent", "delta", "the feed stopped"):
        assert field in calls[0]["content"]


# --- The seven alerts of the 2026-09-12T15:52Z restart --------------------------------
#
# `measured` on the live `dxp` stack, 2026-09-12T16:30Z: `XLEN alert` was 7 and
# `XINFO GROUPS alert` reported `entries-read 7`, `pending 0`, `lag 0` for group
# `discord-alerts`. Seven published, seven read, none pending -- and yet
# `.stack-logs/discord-alerts/2026-09-12.log` held exactly one record, a
# `ConnectTimeout`. Three numbers that look contradictory until the gate is replayed.
#
# The seven, from `XRANGE alert - +`, with their `ts_received` as offsets from the
# first. Every one is severity `error`.
LIVE_ALERTS_2026_09_12: tuple[tuple[float, str, str | None], ...] = (
    (0.000000, "store.replay_gap", None),
    (0.004565, "store.replay_gap", None),
    (0.005813, "store.replay_gap", None),
    (0.006754, "store.replay_gap", None),
    (259.857097, "bus.reader_stopped", None),
    (259.948102, "bus.reader_stopped", None),
    (1267.118962, "reconnect_budget_spent", "DELTA"),
)


def _live_alert(code: str, adapter: str | None) -> Alert:
    return Alert(
        source="feed",
        ts_received=TS,
        severity="error",
        code=code,
        detail="replayed from the live alert stream",
        adapter=adapter,
    )


def test_the_seven_live_alerts_reach_the_poster_as_exactly_three_posts() -> None:
    """Seven acked alerts, three Discord attempts, four folded by the collapse rule.

    This is the arithmetic that explains the live run: the four `store.replay_gap`
    entries share one signature and arrive 7 ms apart, so three of them fold into the
    first; the two `bus.reader_stopped` entries do the same; `reconnect_budget_spent`
    is a signature of its own. The 2.0 s global floor never bites here -- the gaps are
    260 s and 1,007 s -- so collapse alone accounts for all four drops.
    """
    clock = _Clock()
    consumer, bus, calls = _consumer_for_fanout(clock)

    async def scenario() -> None:
        for offset, code, adapter in LIVE_ALERTS_2026_09_12:
            clock.value = 10.0 + offset
            bus.publish(_live_alert(code, adapter))
            await consumer._run_once()

    asyncio.run(scenario())

    posted = [call["content"] for call in calls]
    assert len(posted) == 3, posted
    assert "store.replay_gap" in posted[0]
    assert "bus.reader_stopped" in posted[1]
    assert "reconnect_budget_spent" in posted[2]
    # None of the three revealed a folded count: each was the first of its signature.
    assert not any("collapsed" in content for content in posted)
    # The mechanism, not only the total. Three posts survive a broken collapse window
    # as well, because the 2.0 s floor happens to absorb the same four repeats -- so
    # counting posts alone would pass while the rule under test was gone. What
    # separates the two readings is which counter moved: collapse leaves
    # `rate_limited_count` at zero.
    assert consumer.gate.rate_limited_count == 0


def test_the_four_folded_live_alerts_are_still_unreported_after_the_run() -> None:
    """The repeats are held, not counted out loud, until the signature recurs.

    Reveal-on-next-occurrence is #66's deliberate choice over buffering the window.
    Its cost is visible here: at the end of the live run the gate is still holding
    three folded `store.replay_gap` repeats and one `bus.reader_stopped` repeat, and
    if neither signature ever recurs, nothing ever says they happened.
    """
    clock = _Clock()
    consumer, bus, _calls = _consumer_for_fanout(clock)

    async def scenario() -> None:
        for offset, code, adapter in LIVE_ALERTS_2026_09_12:
            clock.value = 10.0 + offset
            bus.publish(_live_alert(code, adapter))
            await consumer._run_once()

    asyncio.run(scenario())

    suppressed = consumer.gate._suppressed_count_by_signature
    assert suppressed[("store.replay_gap", None, "error")] == 3
    assert suppressed[("bus.reader_stopped", None, "error")] == 1


def test_the_live_connect_timeout_loses_its_alert_and_says_which_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The live failure, reproduced: the seventh alert is acked and never delivered.

    `reconnect_budget_spent` was the alert that said the venue connection would not
    come back without a resume. Its post raised, the occurrence was committed anyway
    because the HTTP seam *was* attempted, and no replay exists to bring it back. The
    consumer keeps running, which is the point of the trade -- and the log now names
    what it cost, which before this test it did not.
    """
    clock = _Clock()
    from deltapayoff.fanout import FanOut

    calls: list[dict[str, Any]] = []

    async def post(_url: str, payload: dict[str, Any]) -> SimpleNamespace:
        calls.append(payload)
        if "reconnect_budget_spent" in payload["content"]:
            raise ConnectionError("offline")
        return SimpleNamespace(status=204)

    bus = FanOut()
    consumer = AlertConsumer(
        bus,
        poster=discord_alerts.DiscordPoster(post),
        gate=discord_alerts.AlertGate(),
        webhook_url="https://discord.test/webhook",
        clock=clock,
        wall_clock=lambda: EARLY_WALL_CLOCK,
    )
    consumer.subscribe()

    async def scenario() -> None:
        for offset, code, adapter in LIVE_ALERTS_2026_09_12:
            clock.value = 10.0 + offset
            bus.publish(_live_alert(code, adapter))
            await consumer._run_once()

    with caplog.at_level(logging.ERROR, logger="deltapayoff.discord_alerts"):
        asyncio.run(scenario())

    assert len(calls) == 3
    assert "reconnect_budget_spent" in caplog.text
    assert "dropped, not retried" in caplog.text
    assert "https://discord.test/webhook" not in caplog.text
    # Acked on receipt and committed as spent: nothing is pending and nothing retries.
    assert consumer.gate._pending_post is None


def test_every_test_in_this_module_has_an_assertion() -> None:
    """#91's own fix, kept honest: a test with no `assert` and no `pytest.raises`.

    completes whether or not the thing it names is true. The audit found exactly this
    shape in
    `test_an_existing_alert_group_can_be_joined_again_without_a_busygroup_failure`
    with an AST scan of the test tree; this is that scan, scoped to this file rather
    than the whole tree -- the audit's own scan named one other instance, a
    pre-existing supervisor test elsewhere, which is out of this ticket's allowed scope
    to touch. A future test added here with no assertion fails this one instead of
    shipping green and meaning nothing.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if node.name == "test_every_test_in_this_module_has_an_assertion":
            continue
        has_assertion = any(
            isinstance(inner, ast.Assert)
            or (
                isinstance(inner, ast.Call)
                and (
                    (isinstance(inner.func, ast.Name) and inner.func.id == "raises")
                    or (
                        isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "raises"
                    )
                )
            )
            for inner in ast.walk(node)
        )
        if not has_assertion:
            offenders.append(node.name)
    assert offenders == [], (
        f"test function(s) with no assert and no pytest.raises: {offenders}"
    )
