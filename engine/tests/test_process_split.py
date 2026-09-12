"""The feed and engine compositions exchange captured events through Redis."""

from __future__ import annotations

import asyncio
import copy
from collections import Counter
from dataclasses import replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import polars as pl
import pytest
from fastapi.testclient import TestClient

from deltapayoff import main, redis_bus, store_main
from deltapayoff.adapters import instrument_from_symbol
from deltapayoff.bar_buffer import BarBuffer
from deltapayoff.bars import ComputedAggregator
from deltapayoff.events import (
    BarTable,
    ChainLeg,
    ChainStrike,
    ComputedChain,
    ConnectionState,
    ControlCommand,
    IndexQuote,
    OptionBar,
    OptionQuote,
    OptionReference,
)
from deltapayoff.events.redis_wire import decode, stream_name, stream_names
from deltapayoff.redis_bus import BusConfig, RedisBus
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
    translate_bar_columns,
)
from deltapayoff.store import SCHEMA as QUOTE_SCHEMA
from deltapayoff.supervisor import FeedSupervisor
from fakes.decoder import delta_decoder
from fakes.scripted_adapter import Frames, ScriptedAdapter, Silence
from test_store import bar as quote_bar
from test_store import computed_bar, reference, spot

RECEIVED_FIRST = 1_788_430_800.5
RECEIVED_SECOND = 1_788_430_801.5
VENUE_TICKER_US = 1_789_290_000_000_000
VENUE_BOOK_US = 1_789_290_000_100_000
UNDERLYING = "BTC"
EXPIRY = "04-09-2026"
BAR_TIMESTAMP = datetime(2026, 9, 4, 9, 0, 0, 123456, tzinfo=timezone.utc)
COMPARISON_COMPUTED_GRACE_SECONDS = 0.0


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


def test_split_consumer_stack_has_a_lossless_bar_buffer_reader(monkeypatch) -> None:
    monkeypatch.setenv("DELTA_BUS", "redis")
    consumer = main.build_consumer_stack()

    assert consumer.writer is None
    assert "bar-writer" not in consumer.events._subscriptions
    assert consumer.bar_buffer is not None
    lossless = {
        name
        for name, subscription in consumer.events._subscriptions.items()
        if subscription.lossless
    }
    assert lossless == {"bar-buffer"}
    assert all(
        key.startswith("md.option_bar:DELTA:")
        for key in consumer.events._subscriptions["bar-buffer"].streams
    )


def test_split_bar_buffer_drains_option_bars_from_the_existing_redis_bus(
    monkeypatch,
) -> None:
    import fakeredis.aioredis

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_LIVE_UNDERLYINGS", UNDERLYING)
    server = fakeredis.aioredis.FakeServer()
    monkeypatch.setattr(redis_bus, "_default_client", _fake_client_factory(server))
    bar = spot(minute=BAR_TIMESTAMP)
    event = OptionBar(
        source="bar-writer",
        instrument=None,
        table=BarTable.SPOT,
        underlying=bar.underlying,
        minute=bar.minute,
        columns=translate_bar_columns(
            {name: getattr(bar, name) for name in SPOT_SCHEMA},
            SPOT_SCHEMA,
            to_wire=True,
        ),
        ts_received=BAR_TIMESTAMP,
    )

    async def scenario() -> None:
        start_calls = 0
        original_start_bus = main.start_bus

        async def counted_start(bus) -> None:
            nonlocal start_calls
            start_calls += 1
            await original_start_bus(bus)

        monkeypatch.setattr(main, "start_bus", counted_start)
        stack = main.build_consumer_stack()
        try:
            await main.start_consumer_stack(stack)
            assert start_calls == 1
            assert stack.events.client is not None
            stack.events.publish(event)
            await stack.events.flush()
            await _wait_until(lambda: stack.bar_buffer.stats()["bars"] == 1)
            bar_task = next(
                task for task in stack.tasks if task.get_name() == "bar-buffer"
            )
            await main.stop_consumer_stack(stack)
            assert bar_task.cancelled()
        finally:
            if stack.tasks:
                await main.stop_consumer_stack(stack)

    asyncio.run(scenario())


def test_store_process_publishes_sealed_bars_and_monolith_does_not(monkeypatch, tmp_path):
    import fakeredis.aioredis

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_LIVE_UNDERLYINGS", UNDERLYING)
    store_root = tmp_path / "store"
    server = fakeredis.aioredis.FakeServer()
    instrument = instrument_from_symbol("C-BTC-77600-040926")
    assert instrument is not None
    quote = OptionQuote(
        source="feed",
        ts_venue=BAR_TIMESTAMP,
        ts_received=BAR_TIMESTAMP,
        instrument=instrument,
        bid=100.0,
        ask=101.0,
    )
    reference = OptionReference(
        source="feed",
        ts_venue=BAR_TIMESTAMP,
        ts_received=BAR_TIMESTAMP,
        instrument=instrument,
        mark=100.5,
    )
    index = IndexQuote(
        source="feed",
        ts_venue=BAR_TIMESTAMP,
        ts_received=BAR_TIMESTAMP,
        underlying=UNDERLYING,
        spot=77600.0,
    )
    seal_at = (BAR_TIMESTAMP + timedelta(minutes=1, seconds=15)).timestamp()

    async def scenario() -> None:
        config = BusConfig(
            venue="DELTA",
            underlyings=(UNDERLYING,),
            batch_ms=60_000,
            read_block_ms=0,
            idle_sleep_seconds=0.001,
        )
        bus = RedisBus(config, client_factory=_fake_client_factory(server))
        process = await store_main._prepare_process(
            root=store_root,
            bus=bus,
            clock=lambda: seal_at,
        )
        try:
            for event in (quote, reference, index):
                process.writer.ingest(event)
            process.writer._seal(seal_at)
            process.writer._commit()
            assert process.writer.rows_written == 3
            await bus.flush()
            client = bus.client
            assert client is not None
            key = "md.option_bar:DELTA:BTC"
            decoded = [
                decode(fields, stream=key)
                for _entry_id, fields in await client.xrange(key)
            ]
            bars = [event for event in decoded if isinstance(event, OptionBar)]
            assert {event.table for event in bars} == {
                BarTable.QUOTE,
                BarTable.REFERENCE,
                BarTable.SPOT,
            }
            assert len(bars) == 3
            assert all(stream_name(event, venue="DELTA") == key for event in bars)
        finally:
            await store_main._close_process(process)

        monkeypatch.delenv("DELTA_BUS", raising=False)
        monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path / "monolith"))
        stack = main.build_feed_stack(object())
        probe = stack.events.subscribe("probe", maxsize=10)
        skipped = stack.writer.skipped
        for event in (quote, reference, index):
            stack.writer.ingest(event)
        stack.writer._seal(seal_at)
        assert stack.writer.skipped == skipped
        assert probe.queue.empty()

    asyncio.run(scenario())


def _configure_fake_split(monkeypatch, tmp_path):
    import fakeredis.aioredis

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_LIVE_UNDERLYINGS", UNDERLYING)
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    server = fakeredis.aioredis.FakeServer()
    monkeypatch.setattr(redis_bus, "_default_client", _fake_client_factory(server))


def test_split_api_routes_construct_no_bar_writer(monkeypatch, tmp_path) -> None:
    _configure_fake_split(monkeypatch, tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("split api constructed a BarWriter")

    monkeypatch.setattr(main, "BarWriter", forbidden)
    import deltapayoff.store as store_module

    monkeypatch.setattr(store_module, "BarWriter", forbidden)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        responses = [
            client.get(
                "/smile", params={"underlying": UNDERLYING, "expiry": EXPIRY}
            ),
            client.get(
                "/chain/minutes",
                params={
                    "underlying": UNDERLYING,
                    "expiry": EXPIRY,
                    "date": "2026-09-04",
                },
            ),
            client.get(
                "/chain/at",
                params={
                    "underlying": UNDERLYING,
                    "expiry": EXPIRY,
                    "minute": "2026-09-04T09:00:00Z",
                },
            ),
            client.get(
                "/bars",
                params={
                    "instrument": "DELTA-BTC-20260904-77600-C-USD",
                    "date": "2026-09-04",
                },
            ),
        ]

    assert [response.status_code for response in responses] == [200, 200, 200, 200]
    assert responses[0].json()["minutes"] == []
    assert responses[1].json()["minutes"] == []
    assert responses[2].json()["type"] == "waiting"
    assert responses[3].json()["bars"] == []


def test_split_api_routes_never_write_parquet(monkeypatch, tmp_path) -> None:
    _configure_fake_split(monkeypatch, tmp_path)
    seed = BarStore(tmp_path)
    seed.add([quote_bar(minute=BAR_TIMESTAMP)])
    seed.flush()
    before = sorted(str(path) for path in tmp_path.rglob("*.parquet"))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("split api wrote Parquet")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", forbidden)
    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        responses = [
            client.get(
                "/smile", params={"underlying": UNDERLYING, "expiry": EXPIRY}
            ),
            client.get(
                "/chain/minutes",
                params={
                    "underlying": UNDERLYING,
                    "expiry": EXPIRY,
                    "date": "2026-09-04",
                },
            ),
            client.get(
                "/chain/at",
                params={
                    "underlying": UNDERLYING,
                    "expiry": EXPIRY,
                    "minute": "2026-09-04T09:00:00Z",
                },
            ),
            client.get(
                "/bars",
                params={
                    "instrument": "DELTA-BTC-20260904-77600-C-USD",
                    "date": "2026-09-04",
                },
            ),
            client.get("/health"),
        ]

    assert all(response.status_code == 200 for response in responses)
    assert sorted(str(path) for path in tmp_path.rglob("*.parquet")) == before


def _comparison_computed(minute: datetime, *, iv: float) -> ComputedChain:
    call = ChainLeg(
        symbol="C-BTC-77600-040926",
        iv=iv,
        iv_leg="call",
        delta=0.5,
        gamma=0.001,
        vega=10.0,
        theta=-2.0,
        rho=1.0,
    )
    put = call.model_copy(update={"symbol": "P-BTC-77600-040926"})
    return ComputedChain(
        source="chain-cache",
        ts_received=minute,
        underlying=UNDERLYING,
        expiry=Date(2026, 9, 4),
        forward=77600.0,
        discount=0.9999,
        years_to_expiry=0.01,
        forward_method="fitted",
        fetched_at=minute,
        model_version="comparison-model",
        solver="S1-newton",
        strikes=(
            ChainStrike(
                strike=Decimal("77600"),
                call=call,
                put=put,
            ),
        ),
    )


def _comparison_events() -> tuple[list[Any], float]:
    events: list[Any] = []
    for index in range(3):
        minute = datetime(2026, 9, 4, 9, index, tzinfo=timezone.utc)
        stamp = minute.replace(second=5, microsecond=123456)
        instrument = instrument_from_symbol("C-BTC-77600-040926")
        assert instrument is not None
        events.extend(
            (
                OptionQuote(
                    source="feed",
                    ts_venue=stamp,
                    ts_received=stamp,
                    instrument=instrument,
                    bid=100.0 + index,
                    ask=101.0 + index,
                ),
                OptionReference(
                    source="feed",
                    ts_venue=stamp,
                    ts_received=stamp,
                    instrument=instrument,
                    mark=100.5 + index,
                ),
                IndexQuote(
                    source="feed",
                    ts_venue=stamp,
                    ts_received=stamp,
                    underlying=UNDERLYING,
                    spot=77600.0 + index,
                ),
                _comparison_computed(stamp, iv=0.4 + index / 100),
            )
        )
    seal_at = datetime(2026, 9, 4, 9, 3, 15, 123456, tzinfo=timezone.utc)
    return events, seal_at.timestamp()


def _comparison_responses(client) -> list[dict[str, Any]]:
    return [
        client.get(
            "/smile", params={"underlying": UNDERLYING, "expiry": EXPIRY}
        ).json(),
        client.get(
            "/chain/minutes",
            params={
                "underlying": UNDERLYING,
                "expiry": EXPIRY,
                "date": "2026-09-04",
            },
        ).json(),
        client.get(
            "/chain/at",
            params={
                "underlying": UNDERLYING,
                "expiry": EXPIRY,
                "minute": "2026-09-04T09:02:00Z",
            },
        ).json(),
        client.get(
            "/bars",
            params={
                "instrument": "DELTA-BTC-20260904-77600-C-USD",
                "date": "2026-09-04",
            },
        ).json(),
    ]


def _option_bar_event(bar: Any, table: BarTable, schema: dict[str, Any]) -> OptionBar:
    return OptionBar(
        source="bar-writer",
        instrument=None,
        table=table,
        underlying=bar.underlying,
        minute=bar.minute,
        columns=translate_bar_columns(
            {name: getattr(bar, name) for name in schema}, schema, to_wire=True
        ),
        ts_received=BAR_TIMESTAMP,
    )


def test_split_read_paths_match_the_monolith_at_the_newest_sealed_minute(
    monkeypatch, tmp_path
) -> None:
    import fakeredis.aioredis

    events, seal_at = _comparison_events()
    root_a = tmp_path / "monolith"
    root_b = tmp_path / "split"
    def clock() -> float:
        return seal_at
    monolith_writer = BarWriter(
        BarStore(root_a),
        computed=ComputedAggregator(grace_seconds=COMPARISON_COMPUTED_GRACE_SECONDS),
        clock=clock,
    )
    for event in events:
        monolith_writer.ingest(event)
    monolith_writer._seal(seal_at)
    main.app.state.writer = monolith_writer
    main.app.state.bar_buffer = None
    monolith = _comparison_responses(TestClient(main.app))

    server = fakeredis.aioredis.FakeServer()

    async def make_split_buffer() -> BarBuffer:
        config = BusConfig(
            venue="DELTA",
            underlyings=(UNDERLYING,),
            batch_ms=60_000,
            read_block_ms=0,
            idle_sleep_seconds=0.001,
        )
        bus = RedisBus(config, client_factory=_fake_client_factory(server))
        buffer = BarBuffer()
        buffer.attach(bus)
        await bus.start()
        buffer_task = asyncio.create_task(buffer.run(), name="bar-buffer")
        writer = BarWriter(
            BarStore(root_b),
            computed=ComputedAggregator(grace_seconds=COMPARISON_COMPUTED_GRACE_SECONDS),
            publish=bus.publish,
            clock=clock,
        )
        for event in events:
            writer.ingest(event)
        writer._seal(seal_at)
        await bus.flush()
        await _wait_until(lambda: buffer.stats()["bars"] == 15)
        buffer_task.cancel()
        await asyncio.gather(buffer_task, return_exceptions=True)
        await bus.aclose()
        return buffer

    split_buffer = asyncio.run(make_split_buffer())
    monkeypatch.setenv("DELTA_STORE_ROOT", str(root_b))
    main.app.state.writer = None
    main.app.state.bar_buffer = split_buffer
    try:
        split = _comparison_responses(TestClient(main.app))
    finally:
        main.app.state.writer = None
        main.app.state.bar_buffer = None

    assert split == monolith


def test_split_read_paths_deduplicate_a_minute_that_reached_disk_and_buffer(
    monkeypatch, tmp_path
) -> None:
    minute = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    buffered = {
        BarTable.QUOTE: quote_bar(minute=minute),
        BarTable.REFERENCE: reference(minute=minute),
        BarTable.SPOT: spot(minute=minute),
        BarTable.COMPUTED: computed_bar(minute=minute, iv=0.43212345),
    }
    schemas = {
        BarTable.QUOTE: QUOTE_SCHEMA,
        BarTable.REFERENCE: REFERENCE_SCHEMA,
        BarTable.SPOT: SPOT_SCHEMA,
        BarTable.COMPUTED: COMPUTED_SCHEMA,
    }
    buffer = BarBuffer()
    for table, bar in buffered.items():
        buffer.apply(_option_bar_event(bar, table, schemas[table]))

    disk_computed = replace(buffered[BarTable.COMPUTED], iv=0.98765432)
    stores = (
        BarStore(tmp_path),
        BarStore(tmp_path, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA),
        BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
        BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
    )
    for store, table in zip(stores, BarTable, strict=True):
        bar = disk_computed if table is BarTable.COMPUTED else buffered[table]
        store.add([bar])
        assert store.flush() == 1

    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    main.app.state.writer = None
    main.app.state.bar_buffer = buffer
    client = TestClient(main.app)
    try:
        smile = client.get(
            "/smile", params={"underlying": UNDERLYING, "expiry": EXPIRY}
        ).json()
        minutes = client.get(
            "/chain/minutes",
            params={
                "underlying": UNDERLYING,
                "expiry": EXPIRY,
                "date": "2026-09-04",
            },
        ).json()
        ladder = client.get(
            "/chain/at",
            params={
                "underlying": UNDERLYING,
                "expiry": EXPIRY,
                "minute": "2026-09-04T09:00:00Z",
            },
        ).json()
        bars = client.get(
            "/bars",
            params={
                "instrument": "DELTA-BTC-20260904-77600-C-USD",
                "date": "2026-09-04",
            },
        ).json()
    finally:
        main.app.state.writer = None
        main.app.state.bar_buffer = None

    assert len(smile["minutes"]) == 1
    assert len(smile["minutes"][0]["points"]) == 1
    assert smile["minutes"][0]["points"][0]["iv"] == disk_computed.iv
    assert minutes["minutes"] == ["2026-09-04T09:00:00Z"]
    assert ladder["type"] == "chain"
    assert len(ladder["data"]["rows"]) == 1
    assert ladder["data"]["rows"][0]["call"]["computed"]["iv"] == disk_computed.iv
    assert len(bars["bars"]) == 1
    assert buffer.stats()["bars"] == 4


def test_split_lifespan_exposes_no_local_writer(monkeypatch) -> None:
    monkeypatch.setenv("DELTA_BUS", "redis")
    stack = main.build_consumer_stack()
    monkeypatch.setattr(main, "build_consumer_stack", lambda: stack)

    async def no_start(_stack) -> None:
        return None

    async def no_stop(_stack) -> None:
        return None

    monkeypatch.setattr(main, "start_consumer_stack", no_start)
    monkeypatch.setattr(main, "stop_consumer_stack", no_stop)

    async def scenario() -> None:
        async with main.lifespan(main.app):
            assert main.app.state.writer is None

    asyncio.run(scenario())


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
            # The API process intentionally has no writer in split mode. Store/table
            # assertions belong to test_store_process.py, where the standalone store
            # owns the four roots; this test now proves only live-consumer delivery.
            assert consumer.writer is None
        finally:
            if second_feed is not None:
                await second_feed.supervisor.aclose()
                await second_feed.bus.aclose()
            if first_feed is not None and first_feed.bus.client is not None:
                await first_feed.bus.aclose()
            await main.stop_consumer_stack(consumer)
            await _cleanup_redis(kind, url, server, expected_streams)


def test_split_pause_resume_reconnect_round_trip_over_redis(monkeypatch) -> None:
    """API and feed processes command one adapter through Redis."""
    import fakeredis.aioredis
    monkeypatch.setenv("DELTA_BUS", "redis")

    async def scenario() -> None:
        server = fakeredis.aioredis.FakeServer()

        def client_factory(_config: BusConfig):
            return fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )

        config = BusConfig(
            venue="SCRIPT",
            underlyings=(UNDERLYING,),
            batch_ms=1,
            read_block_ms=0,
            idle_sleep_seconds=0.001,
        )
        api_bus = RedisBus(config, client_factory=client_factory)
        feed_bus = RedisBus(config, client_factory=client_factory)
        cache = main.FeedConnectionCache(venue="SCRIPT")
        cache.attach(api_bus)
        control = feed_bus.subscribe(
            "feed-control",
            maxsize=100,
            event_types=("control.command",),
        )
        script = ScriptedAdapter(
            script=[Silence(3600.0)],
            venue="SCRIPT",
            underlyings=(UNDERLYING,),
        )
        other = ScriptedAdapter(
            script=[Silence(3600.0)],
            venue="OTHER",
            underlyings=(UNDERLYING,),
        )
        supervisor = FeedSupervisor(
            [script, other],
            feed_bus.publish,
            poll_seconds=1_000.0,
            heartbeat_every=1_000.0,
            retry_delay=0.0,
        )
        received: list[ControlCommand] = []

        async def consume_commands() -> None:
            while True:
                event = await control.queue.get()
                if isinstance(event, ControlCommand):
                    received.append(event)
                    supervisor.dispatch_command(event)

        command_task = asyncio.create_task(consume_commands(), name="feed-control")
        cache_task: asyncio.Task | None = None
        monkeypatch_state = main.app.state
        main.app.dependency_overrides[main.get_supervisor] = lambda: None
        main.app.dependency_overrides[main.get_feed_cache] = lambda: cache
        monkeypatch_state.events = api_bus
        try:
            await feed_bus.start()
            await api_bus.start()
            cache_task = asyncio.create_task(cache.run(), name="feed-state")
            supervisor.start()
            await _wait_until(
                lambda: all(
                    controller.state is ConnectionState.CONNECTED
                    for controller in supervisor.controllers
                )
            )
            await _wait_until(
                lambda: cache.effective("SCRIPT") is not None
                and cache.effective("SCRIPT").state is ConnectionState.CONNECTED
            )
            other_before = supervisor.controllers[1].state

            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://engine.test"
            ) as client:
                paused = await client.post("/feed/script/pause")
                assert paused.status_code == 200
                assert paused.json()["state"] == "stopped"
                assert paused.json()["reason"] == "paused"
                assert supervisor.controllers[1].state is other_before
                assert cache.effective("SCRIPT").state is ConnectionState.STOPPED
                assert cache.effective("SCRIPT").reason == "paused"

                resume_generation = cache.generation("SCRIPT")
                resumed = await client.post("/feed/SCRIPT/resume")
                assert resumed.status_code == 200
                assert resumed.json()["state"] == "connecting"
                assert resumed.json()["reason"] == "resume"
                assert supervisor.controllers[1].state is other_before
                assert cache.generation("SCRIPT") > resume_generation

                await _wait_until(
                    lambda: cache.effective("SCRIPT") is not None
                    and cache.effective("SCRIPT").state
                    is ConnectionState.CONNECTED
                )
                reconnected = await client.post("/feed/script/reconnect")
                assert reconnected.status_code == 200
                assert reconnected.json()["state"] == "reconnecting"
                assert reconnected.json()["reason"] == "closed"
                assert supervisor.controllers[1].state is other_before
                assert cache.generation("SCRIPT") > resume_generation

            assert [event.adapter for event in received] == [
                "SCRIPT",
                "SCRIPT",
                "SCRIPT",
            ]
            assert [event.command for event in received] == [
                "pause",
                "resume",
                "reconnect",
            ]
        finally:
            main.app.dependency_overrides.clear()
            main.app.state.events = None
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)
            if cache_task is not None:
                cache_task.cancel()
                await asyncio.gather(cache_task, return_exceptions=True)
            await supervisor.aclose()
            await feed_bus.aclose()
            await api_bus.aclose()

    asyncio.run(scenario())
