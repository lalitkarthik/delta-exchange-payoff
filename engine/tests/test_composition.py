"""How the engine is wired: one socket, one bus, two consumers with two queue policies.

**The tracer bullet through the contract half of the expand–contract.** #36 ran two buses
— canonical events on one, a rebuilt `feed.Quote` on the other — because the chain cache
and the bar writer still read the old record. #37 moved both onto the events and deleted
the record and the shim that made it, so what this file asserts is that a frame arriving
on the socket reaches the ladder **without any of that** in between.

A quiet regression here is the shape this project keeps refusing: nothing raises, the
ladder simply stops moving. So the path is driven end to end from a scripted connection
rather than assumed from the parts passing their own tests.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.adapters import DeltaAdapter, DeltaFeed
from deltapayoff.events import Event
from deltapayoff.fanout import FanOut
from deltapayoff.store import BarStore, BarWriter
from deltapayoff.stream import ChainStream

CALL = "C-BTC-77600-040926"
PUT = "P-BTC-77600-040926"
EXPIRY = "04-09-2026"


class _StubRedisBus:
    instances: list[_StubRedisBus] = []

    def __init__(self, config) -> None:
        self.config = config
        self._fanout = FanOut()
        self.subscription_types: dict[str, tuple[str, ...] | None] = {}
        self.started = False
        self.closed = False
        self.__class__.instances.append(self)

    def subscribe(
        self,
        name: str,
        maxsize: int,
        lossless: bool = False,
        *,
        event_types=None,
    ):
        self.subscription_types[name] = (
            None if event_types is None else tuple(event_types)
        )
        return self._fanout.subscribe(name, maxsize=maxsize, lossless=lossless)

    def publish(self, event) -> None:
        self._fanout.publish(event)

    def stats(self):
        return self._fanout.stats()

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True


def test_redis_bus_is_a_consumer_only_composition(monkeypatch, tmp_path) -> None:
    """Split mode has only the Redis consumers and never constructs venue components."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "RedisBus", _StubRedisBus)

    class Forbidden:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("split mode constructed a venue component")

    monkeypatch.setattr(main, "DeltaClient", Forbidden)
    monkeypatch.setattr(main, "DeltaAdapter", Forbidden)
    monkeypatch.setattr(main, "FeedSupervisor", Forbidden)
    # `main` never names the controller, so patching it there would guard nothing. Guard
    # the class itself: a construction from any module trips it.
    from deltapayoff.controller import ConnectionController

    def forbidden_init(self, *args, **kwargs) -> None:
        raise AssertionError("split mode constructed a ConnectionController")

    monkeypatch.setattr(ConnectionController, "__init__", forbidden_init)
    _StubRedisBus.instances.clear()

    with TestClient(main.app) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "feed": "stopped",
            "adapters": [
                {
                    "adapter": "DELTA",
                    "state": "stopped",
                    "reason": None,
                    "last_message_at": None,
                    "last_message_age_seconds": None,
                    "reconnects": None,
                    "budget_remaining": None,
                    "transitions": None,
                    "empty_opens": None,
                    "undecodable": None,
                    "state_learned_at": None,
                    "state_age_seconds": None,
                    "last_connection_at": None,
                    "last_connection_age_seconds": None,
                    "last_heartbeat_at": None,
                    "last_heartbeat_age_seconds": None,
                }
            ],
            "watched": [],
        }
        assert main.app.state.adapter is None
        assert main.app.state.feed is None
        assert main.app.state.supervisor is None
        assert {task.get_name() for task in main.app.state.tasks} == {
            "chain-stream",
            "chain-recompute",
            "chain-minute-pass",
            "feed-state",
            "store-state-cache",
            "computed-chain-publisher",
        }
        stats = main.app.state.events.stats()
        assert set(stats) == {"chain-stream", "feed-state", "store-state"}
        assert stats["feed-state"]["lossless"] is False
        assert stats["store-state"]["lossless"] is False
        assert _StubRedisBus.instances[-1].started is True

    assert _StubRedisBus.instances[-1].closed is True


def test_split_health_has_the_configured_remote_adapter_before_any_event(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("DELTA_BUS", "redis")
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))
    monkeypatch.setattr(main, "RedisBus", _StubRedisBus)

    with TestClient(main.app) as client:
        body = client.get("/health").json()

    assert body["feed"] == "stopped"
    assert [row["adapter"] for row in body["adapters"]] == ["DELTA"]
    assert body["adapters"][0]["reason"] is None
    assert body["adapters"][0]["state_learned_at"] is None
    assert body["adapters"][0]["last_heartbeat_at"] is None
    assert body["adapters"][0]["reconnects"] is None
    assert _StubRedisBus.instances[-1].subscription_types["feed-state"] == (
        "feed.connection",
        "heartbeat",
    )


def test_split_health_reads_fresh_state_from_the_cache_and_leaves_counters_null(
    monkeypatch,
) -> None:
    from datetime import datetime, timezone

    from deltapayoff.events import ConnectionState, Heartbeat

    cache = main.FeedConnectionCache(venue="DELTA")
    cache.apply(
        Heartbeat(
            source="controller",
            ts_received=datetime(2026, 9, 12, tzinfo=timezone.utc),
            adapter="DELTA",
            state=ConnectionState.CONNECTED,
        )
    )
    main.app.dependency_overrides[main.get_supervisor] = lambda: None
    main.app.dependency_overrides[main.get_feed_cache] = lambda: cache
    main.app.dependency_overrides[main.get_watched_stream] = lambda: None
    try:
        response = TestClient(main.app).get("/health")
    finally:
        main.app.dependency_overrides.clear()

    row = response.json()["adapters"][0]
    assert response.status_code == 200
    assert response.json()["feed"] == "connected"
    assert row["adapter"] == "DELTA"
    assert row["reconnects"] is None
    assert row["budget_remaining"] is None
    assert row["transitions"] is None


def ticker_frame(symbol: str, bid: float, ask: float) -> dict:
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": "77651.9",
        "ts": 1_788_430_765_832_299,
        "d": [
            {
                "s": symbol,
                "i": 1,
                "m": "580.6",
                "q": [str(ask), "100", str(bid), "200", None],
                "qiv": ["0.31", "0.29", "0.30"],
                "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
                "oi": ["100", "200"],
                "ohlc": ["1", "2", "3", "4"],
                "to": ["5", "5"],
            }
        ],
    }


class _Socket:
    """A scripted connection. Yields the frames, then idles until the test cancels."""

    def __init__(self, script) -> None:
        self.script = list(script)

    async def send(self, raw) -> None:
        json.loads(raw)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        return json.dumps(self.script.pop(0))

    async def ping(self):
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def _adapter(script) -> DeltaAdapter:
    """The real adapter over the real socket owner, over a scripted connection."""

    def factory(sink, **kwargs):
        return DeltaFeed(sink, connect=lambda url: _Socket(script), **kwargs)

    return DeltaAdapter(feed_factory=factory)


async def _drive(adapter, events: FanOut, seconds: float = 0.2) -> None:
    task = asyncio.create_task(adapter.stream(events.publish))
    await asyncio.sleep(seconds)
    adapter.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- socket to ladder -------------------------------------------------------------


def test_a_socket_frame_reaches_the_ladder_as_an_event() -> None:
    """**The tracer bullet, and the whole of #37 in one assertion.**

    A frame arrives on the socket, the adapter decodes it once into canonical events, and
    the chain cache builds a ladder from them. Nothing between the two knows the venue had
    channels, and no record carries a raw frame past the adapter.
    """
    bus = FanOut()
    stream = ChainStream()
    stream.attach(bus)
    adapter = _adapter([ticker_frame(CALL, 579, 584), ticker_frame(PUT, 120, 125)])

    async def scenario() -> None:
        await _drive(adapter, bus)
        while not stream._subscription.queue.empty():
            stream.apply(stream._subscription.queue.get_nowait())

    asyncio.run(scenario())

    chain = stream.chain("BTC", EXPIRY)
    assert chain is not None, "nothing the chain cache could use reached the bus"
    assert chain.spot == pytest.approx(77651.9)
    row = next(row for row in chain.rows if row.strike == 77600.0)
    assert row.call is not None and row.call.bid == 579.0
    assert row.put is not None and row.put.bid == 120.0
    # `product_id` and the venue symbol reach the browser off the reference event, which
    # is one of the four fields the shim used to carry a whole frame for.
    assert row.call.product_id == 1
    assert row.call.symbol == CALL


def test_a_socket_frame_reaches_the_bar_writer_as_an_event(tmp_path) -> None:
    """The second consumer, on the same bus and the same events.

    The reference event feeds table B outright and lends table A its fallback quote; the
    index quote feeds table D. All three counts move from one frame, which is what
    "the bar writer takes events" has to mean.
    """
    bus = FanOut()
    writer = BarWriter(BarStore(tmp_path))
    writer.attach(bus)
    adapter = _adapter([ticker_frame(CALL, 579, 584)])

    async def scenario() -> None:
        await _drive(adapter, bus)
        while not writer._subscription.queue.empty():
            writer.ingest(writer._subscription.queue.get_nowait())

    asyncio.run(scenario())

    stats = writer.stats()
    assert stats["skipped"] == 0, "the writer could make nothing of the events"
    assert stats["reference"]["ticks"] == 1
    assert stats["spot"]["ticks"] == 1
    assert stats["ticks"] == 1, "the reference event's fallback quote reached table A"


def test_only_canonical_events_leave_the_adapter() -> None:
    """One bus since #37, and everything on it is an `Event`.

    #36 needed two because an `Event` and the old quote record shared no attribute and
    either consumer would have raised on the other's records. With the record gone the
    separation is not needed, and a second bus would only be a second thing to forget to
    subscribe to.
    """
    bus = FanOut()
    subscription = bus.subscribe("test-events", maxsize=100)
    adapter = _adapter([ticker_frame(CALL, 579, 584)])

    asyncio.run(_drive(adapter, bus))
    published = _drain(subscription)

    assert all(isinstance(record, Event) for record in published)
    assert [record.type for record in published] == [
        "md.option_reference",
        "md.index_quote",
    ]


def _drain(subscription) -> list:
    drained = []
    while not subscription.queue.empty():
        drained.append(subscription.queue.get_nowait())
    return drained


# --- the venue stops at the adapter -----------------------------------------------


SRC = Path(__file__).resolve().parents[1] / "src" / "deltapayoff"

#: **The names that must not escape**, as a module would actually use them: the retired
#: record's two classes, the venue's two channel names as string literals, and the two
#: constants that held them. Prose is not searched — a docstring saying what a number was
#: measured against is history, and `bars.py` keeps one line of exactly that on purpose —
#: because the failure this guards is a module *routing* on a venue's vocabulary again.
FORBIDDEN = (
    "LegacyQuoteBridge",
    "TICKER_CHANNEL",
    "BOOK_CHANNEL",
    '"ob_l2"',
    "'ob_l2'",
    '"ticker"',
    "'ticker'",
    "feed.Quote",
)

#: The one package allowed a venue's vocabulary. **`wire.py` is deliberately not on this
#: list**: it is Delta's payload layout and it names both channels in prose, but it never
#: uses either as a literal or a constant, so the strict form holds there too.
ALLOWED = (SRC / "adapters",)


def test_the_venue_channel_names_and_the_old_record_live_only_in_the_adapter() -> None:
    """**The ticket's acceptance grep, run as a test rather than as a habit.**

    The boundary took a ticket to establish and would take one line to lose, and losing it
    produces no error at all: a module that reached for `"ob_l2"` again would simply be a
    module that knows a venue, and the second adapter would find out the hard way.

    What is searched is `src/`, because a test that decodes a captured frame must name
    which venue payload shape it is handing the decoder, and that is the decoder's
    parameter rather than a consumer's knowledge. And what is searched *for* is the
    load-bearing form — a literal, a constant, a class — not the word: `chain.py` loops
    over Delta's REST `tickers` rows and `bars.py` keeps one comment saying what these two
    constants used to be, and neither is a module routing on a venue's vocabulary.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if any(path == allowed or allowed in path.parents for allowed in ALLOWED):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in FORBIDDEN:
                if name in line:
                    offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert offenders == [], "the venue's vocabulary escaped the adapter:\n" + "\n".join(
        offenders
    )


def test_the_retired_quote_record_is_gone_from_the_package_entirely() -> None:
    """`feed.Quote` carried a raw venue frame past the adapter to two consumers. It is
    not moved, renamed or deprecated — it is deleted, with the module it lived in and the
    shim that rebuilt it, which is what makes the contract half of a parallel change a
    deletion rather than an archaeology."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("deltapayoff.feed")

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("deltapayoff.adapters.shim")

    from deltapayoff import adapters

    assert not hasattr(adapters, "LegacyQuoteBridge")


# --- the stack --------------------------------------------------------------------


def test_the_stack_gives_each_consumer_the_queue_policy_it_needs(monkeypatch) -> None:
    """Unchanged by the refactor, and the reason it must stay unchanged is in
    `fanout.py`: drop-oldest under load systematically shaves the highs and lows the bars
    exist to capture, which is a bias and not noise.

    **Both subscriptions are on the event bus now**, which is what #37 set out to do: the
    second bus and the shim that filled it are gone, and a stack with an unsubscribed
    event bus would mean the ladder and the store were being fed by nothing.
    """
    monkeypatch.setattr(main, "DeltaFeed", lambda sink, **kwargs: _NoFeed(sink))

    stack = main.build_feed_stack(client=None)
    stats = stack.events.stats()

    assert stats["bar-writer"]["lossless"] is True
    assert stats["chain-stream"]["lossless"] is False
    assert set(stats) == {"bar-writer", "chain-stream"}
    assert not hasattr(stack, "quotes"), "the second bus outlived the shim"
    assert not hasattr(stack, "shim"), "the shim outlived the record it existed for"
    assert stack.feed is stack.adapter.feed


class _NoFeed:
    def __init__(self, sink) -> None:
        self.sink = sink

    def on_open(self, listener) -> None:
        return None

    def on_close(self, listener) -> None:
        return None


# --- which underlyings are recorded ----------------------------------------------


def test_btc_and_eth_unless_the_environment_says_otherwise(monkeypatch) -> None:
    """#43 measured the feed's rate and bandwidth for sixty seconds with ETH enabled and
    recorded it in the HLD, so the default recorded set is now both."""
    monkeypatch.delenv(main.LIVE_UNDERLYINGS_ENV, raising=False)

    assert main.live_underlyings() == ("BTC", "ETH")


def test_the_recorded_set_is_configuration(monkeypatch) -> None:
    """Configuration read at start-up, not a constant in the application module — which
    is the whole of user story 7."""
    monkeypatch.setenv(main.LIVE_UNDERLYINGS_ENV, " btc , eth ")

    assert main.live_underlyings() == ("BTC", "ETH")


def test_an_underlying_delta_does_not_list_is_refused_loudly(
    monkeypatch, caplog
) -> None:
    """Delta answers a request for an asset it does not list with an empty ticker list,
    so a typo would otherwise produce a feed that connects, subscribes nothing and
    records nothing — with no error anywhere."""
    monkeypatch.setenv(main.LIVE_UNDERLYINGS_ENV, "BTC,DOGE")

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        assert main.live_underlyings() == ("BTC",)

    assert any("DOGE" in record.getMessage() for record in caplog.records)


def test_a_configuration_naming_nothing_valid_still_records_the_default(
    monkeypatch, caplog
) -> None:
    """Recording the default set is a better answer to a bad config line than recording
    nothing."""
    monkeypatch.setenv(main.LIVE_UNDERLYINGS_ENV, "DOGE")

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        assert main.live_underlyings() == ("BTC", "ETH")

    assert caplog.records, "it fell back silently"
