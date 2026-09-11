"""The feed and engine compositions exchange captured events through Redis."""

from __future__ import annotations

import asyncio
import copy
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from deltapayoff import main, redis_bus
from deltapayoff.adapters import instrument_from_symbol
from deltapayoff.events import OptionQuote, OptionReference
from deltapayoff.events.redis_wire import decode, stream_name, stream_names
from deltapayoff.redis_bus import BusConfig, RedisBus
from deltapayoff.supervisor import FeedSupervisor
from fakes.decoder import delta_decoder
from fakes.scripted_adapter import Frames, ScriptedAdapter, Silence

RECEIVED_FIRST = 1_788_430_800.5
RECEIVED_SECOND = 1_788_430_801.5
VENUE_TICKER_US = 1_789_290_000_000_000
VENUE_BOOK_US = 1_789_290_000_100_000
UNDERLYING = "BTC"
EXPIRY = "04-09-2026"


@pytest.fixture(params=["redis-fake", "redis-docker"])
def split_redis(request: pytest.FixtureRequest) -> tuple[str, str | None]:
    if request.param == "redis-docker":
        return request.param, request.getfixturevalue("redis_server")
    return request.param, None


def _fake_client_factory(server: Any):
    import fakeredis.aioredis

    def factory(_config: BusConfig):
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)

    return factory


async def _cleanup_redis(
    kind: str,
    url: str | None,
    server: Any,
    keys: tuple[str, ...],
) -> None:
    if kind == "redis-fake":
        import fakeredis.aioredis

        client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
    else:
        import redis.asyncio

        client = redis.asyncio.Redis.from_url(url, decode_responses=False)
    try:
        await client.delete(*keys)
        if kind == "redis-fake":
            # fakeredis reports a null last-generated-id for an empty stream. Seed and
            # delete one entry so the consumer's starting cursor has the same concrete
            # Redis stream id as an empty stream on Redis 7.
            for key in keys:
                entry_id = await client.xadd(key, {"bootstrap": b"1"})
                await client.xdel(key, entry_id)
    finally:
        await client.aclose()


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


def _changed_frame(frame: dict[str, Any], *, book: bool) -> dict[str, Any]:
    changed = copy.deepcopy(frame)
    if book:
        changed["b"][0][0] = str(float(changed["b"][0][0]) + 17.25)
        changed["a"][0][0] = str(float(changed["a"][0][0]) + 17.25)
    else:
        changed["d"][0]["q"][0] = str(float(changed["d"][0]["q"][0]) + 17.25)
        changed["d"][0]["q"][2] = str(float(changed["d"][0]["q"][2]) + 17.25)
    return changed


def _fixed_frame(frame: dict[str, Any], *, book: bool) -> dict[str, Any]:
    fixed = copy.deepcopy(frame)
    if book:
        fixed["ts"] = VENUE_BOOK_US
        fixed["lts"] = VENUE_BOOK_US
    else:
        fixed["ts"] = VENUE_TICKER_US
    return fixed


async def _start_scripted_feed(
    ticker: dict[str, Any],
    book: dict[str, Any],
    instrument,
    received_at: float,
) -> SimpleNamespace:
    config = BusConfig.from_env((UNDERLYING,))
    bus = RedisBus(config)
    await bus.start()
    adapter = ScriptedAdapter(
        script=[
            Frames("ticker", [ticker], received_at=received_at),
            Frames("ob_l2", [book], received_at=received_at),
            Silence(3600.0),
        ],
        venue="DELTA",
        underlyings=(UNDERLYING,),
        listings={UNDERLYING: [instrument]},
    )
    relist = SimpleNamespace(adapter=adapter, listed={})
    await main.relist_instruments(relist)
    supervisor = FeedSupervisor([adapter], bus.publish)
    supervisor.start()
    await _wait_until(lambda: adapter.frames_replayed == 2)
    return SimpleNamespace(bus=bus, adapter=adapter, supervisor=supervisor)


async def _stop_feed_abruptly(feed: SimpleNamespace) -> None:
    await feed.supervisor.aclose()
    flusher = feed.bus._flusher
    if flusher is not None:
        flusher.cancel()
        await asyncio.gather(flusher, return_exceptions=True)
        feed.bus._flusher = None
    client = feed.bus.client
    assert client is not None
    await client.aclose()
    feed.bus._client = None


async def _redis_market_counts(client: Any, keys: tuple[str, ...]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for key in keys:
        for _entry_id, fields in await client.xrange(key):
            event = decode(fields, stream=key)
            if isinstance(event, (OptionQuote, OptionReference)) or event.type == (
                "md.index_quote"
            ):
                counts[event.type] += 1
    return counts


def test_feed_restart_delivers_captured_events_to_the_live_consumer(
    monkeypatch,
    tmp_path,
    split_redis,
    ws_ticker_frames,
    ws_book_frames,
) -> None:
    """A feed restart cannot make a successfully flushed frame disappear."""
    kind, url = split_redis
    server = None
    if kind == "redis-fake":
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        monkeypatch.setattr(redis_bus, "_default_client", _fake_client_factory(server))

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_BUS_BATCH_MS", "60000")
    monkeypatch.setenv("DELTA_LIVE_UNDERLYINGS", UNDERLYING)
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    if url is None:
        monkeypatch.delenv("DELTA_REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("DELTA_REDIS_URL", url)

    symbol = next(
        name
        for name in sorted(set(ws_ticker_frames) & set(ws_book_frames))
        if name.startswith("C-")
    )
    instrument = instrument_from_symbol(symbol)
    first_ticker = _fixed_frame(ws_ticker_frames[symbol], book=False)
    first_book = _fixed_frame(ws_book_frames[symbol], book=True)
    second_ticker = _changed_frame(first_ticker, book=False)
    second_book = _changed_frame(first_book, book=True)
    decoder = delta_decoder()
    first_events = decoder("ticker", first_ticker, RECEIVED_FIRST) + decoder(
        "ob_l2", first_book, RECEIVED_FIRST
    )
    second_events = decoder("ticker", second_ticker, RECEIVED_SECOND) + decoder(
        "ob_l2", second_book, RECEIVED_SECOND
    )
    expected_market_counts = Counter(
        event.type for event in [*first_events, *second_events]
    )
    expected_streams = stream_names(venues=("DELTA",), underlyings=(UNDERLYING,))

    async def scenario() -> None:
        await _cleanup_redis(kind, url, server, expected_streams)
        consumer = main.build_consumer_stack()
        first_feed = None
        second_feed = None
        try:
            await main.start_consumer_stack(consumer)
            first_feed = await _start_scripted_feed(
                first_ticker, first_book, instrument, RECEIVED_FIRST
            )
            await first_feed.bus.flush()
            first_stats = first_feed.bus.publisher()
            assert first_stats["batches"] >= 1

            client = consumer.events.client
            assert client is not None
            first_touched = {
                key for key in expected_streams if await client.xlen(key) > 0
            }
            assert first_touched
            assert first_stats["trims"] >= len(first_touched)

            await _wait_until(
                lambda: consumer.stream.applied >= len(first_events)
                and consumer.writer.stats()["reference"]["ticks"]
                >= expected_market_counts["md.option_reference"] // 2
            )
            boundary = {
                key: await client.xlen(key) for key in first_touched
            }

            await _stop_feed_abruptly(first_feed)
            assert first_feed.bus._outbox, "the cancelled publisher kept an outbox"
            assert {
                key: await client.xlen(key) for key in first_touched
            } == boundary

            second_feed = await _start_scripted_feed(
                second_ticker, second_book, instrument, RECEIVED_SECOND
            )
            await second_feed.bus.flush()
            second_stats = second_feed.bus.publisher()
            assert second_stats["batches"] >= 1
            second_touched = {
                key
                for key in expected_streams
                if await client.xlen(key) > boundary.get(key, 0)
            }
            assert second_touched
            assert second_stats["trims"] >= len(second_touched)

            new_bid = float(second_book["b"][0][0])

            def current_bid() -> float | None:
                chain = consumer.stream.chain(UNDERLYING, EXPIRY)
                if chain is None:
                    return None
                for row in chain.rows:
                    if row.call is not None and row.call.symbol == symbol:
                        return row.call.bid
                return None

            await _wait_until(
                lambda: current_bid() == pytest.approx(new_bid),
                timeout=main.PUSH_INTERVAL_SECONDS,
            )
            await _wait_until(
                lambda: consumer.stream.applied >= len(first_events) + len(second_events)
                and consumer.writer.stats()["reference"]["ticks"]
                == expected_market_counts["md.option_reference"]
                and consumer.writer.stats()["spot"]["ticks"]
                == expected_market_counts["md.index_quote"]
            )

            actual_keys = {
                key.decode() if isinstance(key, bytes) else key
                for key in await client.keys("*")
            }
            assert actual_keys <= set(expected_streams)
            assert {
                stream_name(event, venue="DELTA")
                for event in [*first_events, *second_events]
            } <= actual_keys
            assert (
                await _redis_market_counts(client, expected_streams)
                == expected_market_counts
            )
            stats = consumer.writer.stats()
            fallback_quotes = expected_market_counts["md.option_reference"]
            assert (
                stats["ticks"]
                == expected_market_counts["md.option_quote"] + fallback_quotes
            )
            assert (
                stats["reference"]["ticks"]
                == expected_market_counts["md.option_reference"]
            )
            assert stats["spot"]["ticks"] == expected_market_counts["md.index_quote"]
        finally:
            if second_feed is not None:
                await second_feed.supervisor.aclose()
                await second_feed.bus.aclose()
            if first_feed is not None and first_feed.bus.client is not None:
                await first_feed.bus.aclose()
            await main.stop_consumer_stack(consumer)
            await _cleanup_redis(kind, url, server, expected_streams)

    asyncio.run(scenario())
