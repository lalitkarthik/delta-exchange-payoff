from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
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


def _alert(code: str = "connection_silent") -> Alert:
    return Alert(
        source="feed",
        ts_received=TS,
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
            AlertConsumer(second).subscribe()
            await second.start()
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


def test_a_pause_produces_no_discord_post() -> None:
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

    asyncio.run(scenario())

    assert calls == []


def test_a_paused_feed_followed_by_an_alert_posts_only_the_real_alert() -> None:
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

    asyncio.run(scenario())

    assert len(calls) == 1
    assert "connection_silent" in calls[0]["content"]


def test_a_bad_alert_does_not_kill_the_consumer_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    poster, calls = _poster()
    from deltapayoff.fanout import FanOut

    class FailingOnceGate:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, **_kwargs: Any) -> discord_alerts.GateDecision:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("bad alert")
            return discord_alerts.GateDecision(
                post=True, collapsed_count=0, rate_limited=False
            )

    bus = FanOut()
    consumer = AlertConsumer(
        bus,
        poster=poster,
        gate=FailingOnceGate(),
        webhook_url="https://discord.test/webhook",
        clock=clock,
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
