"""The two endpoints and the error table, with the venue stubbed out entirely.

**The routes reach the venue through the adapter since #37**, so the stub goes in behind a
real `DeltaAdapter` rather than in place of a client the route holds itself. That is one
more real layer under test than before — the adapter's `expiries` and `chain_snapshot` are
the code that turns a ticker list into the two response shapes — and it is what makes the
error table below assert about the boundary the browser actually talks to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.adapters import DeltaAdapter
from deltapayoff.adapters.delta import instrument_from_symbol
from deltapayoff.bar_buffer import BUFFER_HORIZON_SECONDS, BarBuffer
from deltapayoff.delta_client import DeltaUnavailable, parse_envelope
from deltapayoff.events import (
    BarTable,
    ConnectionState,
    ControlCommand,
    FeedConnection,
    OptionBar,
    OptionReference,
)
from deltapayoff.main import (
    FeedConnectionCache,
    app,
    get_adapter,
    get_feed_cache,
    get_supervisor,
    get_watched_stream,
)
from deltapayoff.smile import MINUTE_FORMAT
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    translate_bar_columns,
)
from deltapayoff.store import SCHEMA as QUOTE_SCHEMA
from deltapayoff.stream import ChainStream
from test_store import bar as quote_bar
from test_store import computed_bar, reference, spot


class StubDelta:
    """Stands in for DeltaClient. Records what was asked for; never opens a socket."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.raises = raises
        self.calls: list[tuple[str, str | None]] = []

    async def tickers(
        self, underlying: str, expiry: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((underlying, expiry))
        if self.raises is not None:
            raise self.raises
        return self.rows


class StubSocket:
    """Stands in for the socket owner, so building an adapter opens nothing."""

    def __init__(self, sink, **_kwargs) -> None:
        self.sink = sink
        self.registry: dict[str, list[str]] = {}

    def subscribe(self, channel: str, symbols) -> None:
        self.registry.setdefault(channel, []).extend(symbols)

    async def run(self) -> None:  # pragma: no cover - never started here
        raise AssertionError("the REST routes must not start a socket")

    def stop(self) -> None:  # pragma: no cover - never started here
        pass


@pytest.fixture
def make_client() -> Iterator[Callable[[StubDelta], TestClient]]:
    """A TestClient whose adapter is a real one wrapped around the given stub client.

    TestClient is not entered as a context manager, so the app lifespan never runs and
    no real httpx client is ever constructed; `StubSocket` makes sure building the
    adapter does not dial out either.
    """

    def factory(stub: StubDelta) -> TestClient:
        adapter = DeltaAdapter(client=stub, feed_factory=StubSocket)
        app.dependency_overrides[get_adapter] = lambda: adapter
        return TestClient(app)

    yield factory
    app.dependency_overrides.clear()


def _bar_event(bar, table: BarTable, schema) -> OptionBar:
    return OptionBar(
        source="bar-writer",
        instrument=None,
        table=table,
        underlying=bar.underlying,
        minute=bar.minute,
        columns=translate_bar_columns(
            {name: getattr(bar, name) for name in schema}, schema, to_wire=True
        ),
        ts_received=bar.minute,
    )


def test_store_providers_keep_the_writer_stores_when_a_writer_exists(tmp_path) -> None:
    writer_stores = SimpleNamespace(
        store=BarStore(tmp_path),
        reference_store=BarStore(
            tmp_path, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA
        ),
        computed_store=BarStore(
            tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA
        ),
        spot_store=BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
    )
    app.state.writer = writer_stores
    app.state.bar_buffer = BarBuffer()
    try:
        assert main.get_computed_store() is writer_stores.computed_store
        source = main.get_historical_source()
        assert source.quote is writer_stores.store
        assert source.reference is writer_stores.reference_store
        assert source.computed is writer_stores.computed_store
        assert source.spot is writer_stores.spot_store
    finally:
        app.state.writer = None
        app.state.bar_buffer = None


def test_store_providers_use_only_the_matching_table_from_the_bar_buffer(
    tmp_path,
) -> None:
    minute = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    buffer = BarBuffer()
    buffer.apply(
        _bar_event(computed_bar(minute=minute), BarTable.COMPUTED, COMPUTED_SCHEMA)
    )
    buffer.apply(_bar_event(quote_bar(minute=minute), BarTable.QUOTE, QUOTE_SCHEMA))
    app.state.writer = None
    app.state.bar_buffer = buffer
    try:
        computed = main.get_computed_store()
        source = main.get_historical_source()
        assert computed.pending().collect()["symbol"].to_list() == [
            "C-BTC-77600-040926"
        ]
        assert source.quote.pending().collect()["symbol"].to_list() == [
            "C-BTC-77600-040926"
        ]
        assert source.reference.pending().collect().height == 0
        assert source.spot.pending().collect().height == 0
    finally:
        app.state.writer = None
        app.state.bar_buffer = None


def test_store_providers_are_disk_only_without_a_writer_or_bar_buffer(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    app.state.writer = None
    app.state.bar_buffer = None

    computed = main.get_computed_store()
    source = main.get_historical_source()

    assert computed.pending().collect().height == 0
    assert source.quote.pending().collect().height == 0
    assert source.reference.pending().collect().height == 0
    assert source.computed.pending().collect().height == 0
    assert source.spot.pending().collect().height == 0


def test_read_routes_answer_from_the_split_bar_buffer(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    minute = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    buffer = BarBuffer()
    buffer.apply(
        _bar_event(computed_bar(minute=minute), BarTable.COMPUTED, COMPUTED_SCHEMA)
    )
    buffer.apply(_bar_event(quote_bar(minute=minute), BarTable.QUOTE, QUOTE_SCHEMA))
    buffer.apply(
        _bar_event(reference(minute=minute), BarTable.REFERENCE, REFERENCE_SCHEMA)
    )
    buffer.apply(_bar_event(spot(minute=minute), BarTable.SPOT, SPOT_SCHEMA))
    app.state.writer = None
    app.state.bar_buffer = buffer
    try:
        client = TestClient(app)
        smile = client.get(
            "/smile", params={"underlying": "BTC", "expiry": "04-09-2026"}
        )
        minutes = client.get(
            "/chain/minutes",
            params={
                "underlying": "BTC",
                "expiry": "04-09-2026",
                "date": "2026-09-04",
            },
        )
        ladder = client.get(
            "/chain/at",
            params={
                "underlying": "BTC",
                "expiry": "04-09-2026",
                "minute": "2026-09-04T09:00:00Z",
            },
        )
        bars = client.get(
            "/bars",
            params={
                "instrument": "DELTA-BTC-20260904-77600-C-USD",
                "date": "2026-09-04",
            },
        )
    finally:
        app.state.writer = None
        app.state.bar_buffer = None

    assert smile.status_code == 200
    assert smile.json()["minutes"][0]["minute"] == "2026-09-04T09:00:00Z"
    assert minutes.status_code == 200
    assert minutes.json()["minutes"] == ["2026-09-04T09:00:00Z"]
    assert ladder.status_code == 200
    assert ladder.json()["data"]["minute"] == "2026-09-04T09:00:00Z"
    assert bars.status_code == 200
    assert bars.json()["bars"][0]["minute"] == "2026-09-04T09:00:00Z"


def _health_without_lifespan() -> TestClient:
    app.dependency_overrides[main.get_supervisor] = lambda: None
    app.dependency_overrides[main.get_feed_cache] = lambda: None
    app.dependency_overrides[main.get_watched_stream] = lambda: None
    return TestClient(app)


def test_health_reports_a_null_bar_buffer_without_one() -> None:
    app.state.bar_buffer = None
    try:
        response = _health_without_lifespan().get("/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["bar_buffer"] is None
    assert response.json()["status"] == "ok"
    assert response.json()["feed"] == "stopped"


def test_health_reports_bar_buffer_stats_and_shared_minute_stamps() -> None:
    buffer = BarBuffer()
    first = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    second = datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc)
    buffer.apply(_bar_event(quote_bar(minute=first), BarTable.QUOTE, QUOTE_SCHEMA))
    buffer.apply(_bar_event(spot(minute=second), BarTable.SPOT, SPOT_SCHEMA))
    app.state.bar_buffer = buffer
    try:
        response = _health_without_lifespan().get("/health")
    finally:
        app.dependency_overrides.clear()
        app.state.bar_buffer = None

    report = response.json()["bar_buffer"]
    assert report["bars"] == buffer.stats()["bars"]
    assert report["per_table"] == buffer.stats()["per_table"]
    assert report["minutes"] == buffer.stats()["minutes"]
    assert report["oldest_minute"] == first.strftime(MINUTE_FORMAT)
    assert report["newest_minute"] == second.strftime(MINUTE_FORMAT)
    assert report["horizon_seconds"] == BUFFER_HORIZON_SECONDS
    assert datetime.strptime(report["oldest_minute"], MINUTE_FORMAT)
    assert datetime.strptime(report["newest_minute"], MINUTE_FORMAT)


# --- happy paths ----------------------------------------------------------------


def test_expiries_endpoint(make_client, all_expiry_tickers) -> None:
    stub = StubDelta(all_expiry_tickers)
    response = make_client(stub).get("/expiries", params={"underlying": "BTC"})
    assert response.status_code == 200
    assert response.json() == {
        "underlying": "BTC",
        "expiries": [
            "02-09-2026",
            "03-09-2026",
            "04-09-2026",
            "11-09-2026",
            "18-09-2026",
            "25-09-2026",
            "30-10-2026",
            "27-11-2026",
        ],
    }
    assert stub.calls == [("BTC", None)], "no expiry_date filter when listing expiries"


def test_chain_endpoint(make_client, chain_tickers) -> None:
    stub = StubDelta(chain_tickers)
    response = make_client(stub).get(
        "/chain", params={"underlying": "BTC", "expiry": "04-09-2026"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "BTC"
    assert body["expiry"] == "04-09-2026"
    assert body["spot"] == 77568.2
    assert body["atm_strike"] == 77600.0
    assert body["fetched_at"].endswith("Z")
    assert len(body["rows"]) == 65
    assert stub.calls == [("BTC", "04-09-2026")]

    row = body["rows"][0]
    assert set(row) == {"strike", "call", "put"}
    assert set(row["put"]) == {
        "symbol", "product_id", "bid", "ask", "mark", "bid_iv", "ask_iv", "mark_iv",
        "delta", "gamma", "theta", "vega", "rho", "oi", "oi_value_usd",
        "oi_change_usd_6h", "tick_size",
        # Ours, added beside Delta's rather than replacing any of them. Every name
        # above is still the venue's own figure.
        "computed",
    }
    assert set(row["put"]["computed"]) == {
        "iv", "iv_leg", "iv_reason", "delta", "gamma", "vega", "theta", "rho",
    }


def test_underlying_is_case_insensitive(make_client, all_expiry_tickers) -> None:
    stub = StubDelta(all_expiry_tickers)
    response = make_client(stub).get("/expiries", params={"underlying": "btc"})
    assert response.status_code == 200
    assert response.json()["underlying"] == "BTC"
    assert stub.calls == [("BTC", None)], "the normalised symbol is what is queried"


def test_cors_allows_the_next_dev_server(make_client, all_expiry_tickers) -> None:
    response = make_client(StubDelta(all_expiry_tickers)).get(
        "/expiries",
        params={"underlying": "BTC"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


# --- the error table ------------------------------------------------------------


@pytest.mark.parametrize("underlying", ["SOL", "BTCUSD", "xyz"])
def test_bad_underlying_is_400(make_client, underlying: str) -> None:
    stub = StubDelta([])
    response = make_client(stub).get("/expiries", params={"underlying": underlying})
    assert response.status_code == 400
    assert "detail" in response.json()
    assert stub.calls == [], "a bad parameter never reaches the venue"


@pytest.mark.parametrize("expiry", ["2026-09-04", "4-9-2026", "32-09-2026", "nonsense"])
def test_malformed_expiry_is_400(make_client, expiry: str) -> None:
    stub = StubDelta([])
    response = make_client(stub).get(
        "/chain", params={"underlying": "BTC", "expiry": expiry}
    )
    assert response.status_code == 400
    assert stub.calls == []


def test_missing_parameter_is_422_from_fastapi(make_client) -> None:
    """A parameter that is absent altogether is FastAPI's own validation, not ours."""
    response = make_client(StubDelta([])).get("/chain", params={"underlying": "BTC"})
    assert response.status_code == 422


def test_no_contracts_for_that_pair_is_404(make_client) -> None:
    response = make_client(StubDelta([])).get(
        "/chain", params={"underlying": "BTC", "expiry": "01-01-2030"}
    )
    assert response.status_code == 404
    assert "01-01-2030" in response.json()["detail"]


def test_no_contracts_for_that_underlying_is_404(make_client) -> None:
    response = make_client(StubDelta([])).get("/expiries", params={"underlying": "ETH"})
    assert response.status_code == 404


def test_split_feed_command_round_trips_over_the_bus(
    monkeypatch,
) -> None:
    fixed = datetime(2026, 9, 12, tzinfo=timezone.utc)
    cache = FeedConnectionCache(
        venue="SCRIPT",
        wall_clock=lambda: fixed,
        monotonic_clock=lambda: 0.0,
    )
    cache.apply(
        FeedConnection(
            source="controller",
            ts_received=fixed,
            adapter="SCRIPT",
            to_state=ConnectionState.CONNECTED,
            reason="open",
        )
    )

    class Bus:
        def __init__(self) -> None:
            self.published: list[ControlCommand] = []

        def publish(self, event) -> None:
            assert isinstance(event, ControlCommand)
            self.published.append(event)
            asyncio.get_running_loop().call_soon(
                cache.apply,
                FeedConnection(
                    source="controller",
                    ts_received=fixed.replace(second=1),
                    adapter="SCRIPT",
                    to_state=ConnectionState.STOPPED,
                    reason="paused",
                ),
            )

    events = Bus()
    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setattr(app.state, "events", events, raising=False)
    app.dependency_overrides[get_supervisor] = lambda: None
    app.dependency_overrides[get_feed_cache] = lambda: cache
    try:
        response = TestClient(app).post("/feed/script/pause")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["adapter"] == "SCRIPT"
    assert response.json()["state"] == "stopped"
    assert response.json()["reason"] == "paused"
    assert len(events.published) == 1
    assert events.published[0].adapter == "SCRIPT"


def test_split_feed_command_rejects_unknown_adapter_without_publishing(
    monkeypatch,
) -> None:
    cache = FeedConnectionCache(venue="SCRIPT")
    events = type("Bus", (), {"published": []})()

    def publish(event) -> None:
        events.published.append(event)

    events.publish = publish
    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setattr(app.state, "events", events, raising=False)
    app.dependency_overrides[get_supervisor] = lambda: None
    app.dependency_overrides[get_feed_cache] = lambda: cache
    try:
        response = TestClient(app).post("/feed/other/pause")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert events.published == []


def test_split_feed_command_rejects_unknown_command_without_publishing(
    monkeypatch,
) -> None:
    cache = FeedConnectionCache(venue="SCRIPT")
    events = type("Bus", (), {"published": []})()

    def publish(event) -> None:
        events.published.append(event)

    events.publish = publish
    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setattr(app.state, "events", events, raising=False)
    app.dependency_overrides[get_supervisor] = lambda: None
    app.dependency_overrides[get_feed_cache] = lambda: cache
    try:
        response = TestClient(app).post("/feed/script/unknown")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert events.published == []


def test_split_feed_command_times_out_without_a_qualifying_later_event(
    monkeypatch,
) -> None:
    fixed = datetime(2026, 9, 12, tzinfo=timezone.utc)
    cache = FeedConnectionCache(
        venue="SCRIPT",
        wall_clock=lambda: fixed,
        monotonic_clock=lambda: 0.0,
    )
    cache.apply(
        FeedConnection(
            source="controller",
            ts_received=fixed,
            adapter="SCRIPT",
            to_state=ConnectionState.CONNECTED,
            reason="open",
        )
    )

    class Bus:
        def __init__(self) -> None:
            self.published: list[ControlCommand] = []

        def publish(self, event) -> None:
            self.published.append(event)

    events = Bus()
    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setattr(main, "COMMAND_ACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app.state, "events", events, raising=False)
    app.dependency_overrides[get_supervisor] = lambda: None
    app.dependency_overrides[get_feed_cache] = lambda: cache
    try:
        response = TestClient(app).post("/feed/script/pause")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 504
    assert response.json()["detail"] == (
        "feed command was published but SCRIPT did not acknowledge it"
    )
    assert len(events.published) == 1


def test_split_feed_command_publishes_idempotent_pause_and_returns_cached_line(
    monkeypatch,
) -> None:
    fixed = datetime(2026, 9, 12, tzinfo=timezone.utc)
    cache = FeedConnectionCache(
        venue="SCRIPT",
        wall_clock=lambda: fixed,
        monotonic_clock=lambda: 0.0,
    )
    cache.apply(
        FeedConnection(
            source="controller",
            ts_received=fixed,
            adapter="SCRIPT",
            to_state=ConnectionState.STOPPED,
            reason="paused",
        )
    )

    class Bus:
        def __init__(self) -> None:
            self.published: list[ControlCommand] = []

        def publish(self, event) -> None:
            self.published.append(event)

    events = Bus()
    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setattr(main, "COMMAND_ACK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app.state, "events", events, raising=False)
    app.dependency_overrides[get_supervisor] = lambda: None
    app.dependency_overrides[get_feed_cache] = lambda: cache
    try:
        response = TestClient(app).post("/feed/script/pause")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    assert response.json()["reason"] == "paused"
    assert len(events.published) == 1


def _reference(symbol: str) -> OptionReference:
    instrument = instrument_from_symbol(symbol)
    assert instrument is not None
    return OptionReference(
        source="DELTA",
        ts_received=datetime(2026, 9, 12, tzinfo=timezone.utc),
        instrument=instrument,
        bid=100.0,
        ask=110.0,
        mark=105.0,
    )


def test_chain_stream_expiry_query_is_distinct_case_insensitive_and_date_sorted() -> None:
    stream = ChainStream()
    for symbol in (
        "C-BTC-60000-301026",
        "P-BTC-61000-041126",
        "C-BTC-62000-301026",
        "C-ETH-60000-041126",
    ):
        stream.apply(_reference(symbol))

    assert stream.expiries("btc") == ["30-10-2026", "04-11-2026"]


def test_redis_consumer_routes_read_chain_stream_state_when_adapter_is_absent() -> None:
    stream = ChainStream()
    stream.apply(_reference("C-BTC-60000-301026"))
    app.dependency_overrides[get_adapter] = lambda: None
    app.dependency_overrides[get_watched_stream] = lambda: stream
    try:
        client = TestClient(app)
        expiries_response = client.get("/expiries", params={"underlying": "BTC"})
        chain_response = client.get(
            "/chain", params={"underlying": "BTC", "expiry": "30-10-2026"}
        )
    finally:
        app.dependency_overrides.clear()

    assert expiries_response.status_code == 200
    assert expiries_response.json() == {
        "underlying": "BTC",
        "expiries": ["30-10-2026"],
    }
    assert chain_response.status_code == 200
    assert chain_response.json()["rows"]


@pytest.mark.parametrize(
    "failure",
    [
        DeltaUnavailable("Delta timed out after 10s"),
        DeltaUnavailable("Delta was unreachable: connection refused"),
        DeltaUnavailable("Delta answered HTTP 400 without success"),
    ],
)
def test_upstream_failure_is_502(make_client, failure: Exception) -> None:
    for path, params in (
        ("/expiries", {"underlying": "BTC"}),
        ("/chain", {"underlying": "BTC", "expiry": "04-09-2026"}),
    ):
        response = make_client(StubDelta(raises=failure)).get(path, params=params)
        assert response.status_code == 502
        assert response.json()["detail"] == str(failure)


# --- the Delta envelope ---------------------------------------------------------


def _response(status: int, body: Any) -> httpx.Response:
    request = httpx.Request("GET", "https://api.india.delta.exchange/v2/tickers")
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, text=body, request=request)


def test_parse_envelope_unwraps_result() -> None:
    rows = parse_envelope(_response(200, {"success": True, "result": [{"symbol": "x"}]}))
    assert rows == [{"symbol": "x"}]


def test_parse_envelope_rejects_success_false() -> None:
    """Delta's failure envelope carries `error` as a bare string here, not an object."""
    body = {"success": False, "error": "Invalid date format. Expected format: DD-MM-YYYY"}
    with pytest.raises(DeltaUnavailable, match="Invalid date format"):
        parse_envelope(_response(400, body))


def test_parse_envelope_rejects_success_false_with_an_error_object() -> None:
    body = {"success": False, "error": {"code": "unavailable"}}
    with pytest.raises(DeltaUnavailable, match="unavailable"):
        parse_envelope(_response(500, body))


def test_parse_envelope_rejects_a_non_json_body() -> None:
    """A request with no User-Agent gets an HTML 403 from Delta's edge, not JSON."""
    with pytest.raises(DeltaUnavailable, match="non-JSON"):
        parse_envelope(_response(403, "<HTML><HEAD><TITLE>ERROR</TITLE>"))


def test_parse_envelope_rejects_a_missing_result_list() -> None:
    with pytest.raises(DeltaUnavailable, match="no result list"):
        parse_envelope(_response(200, {"success": True}))


def test_parse_envelope_drops_non_object_rows() -> None:
    rows = parse_envelope(_response(200, {"success": True, "result": [{"a": 1}, "junk"]}))
    assert rows == [{"a": 1}]
