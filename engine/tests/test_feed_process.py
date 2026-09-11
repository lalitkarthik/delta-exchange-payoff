"""The dedicated Redis-backed feed process composition."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from deltapayoff import feed_main, feed_runtime, main
from deltapayoff.delta_client import DeltaUnavailable
from deltapayoff.events import ConnectionState, ControlCommand
from deltapayoff.models import HealthReport
from deltapayoff.redis_bus import BusUnavailable

TS = datetime(2026, 9, 12, tzinfo=timezone.utc)


def test_feed_process_entrypoint_and_shared_relisting_module_are_importable() -> None:
    assert importlib.util.find_spec("deltapayoff.feed_main") is not None
    assert importlib.util.find_spec("deltapayoff.feed_runtime") is not None


def test_monolith_and_feed_entrypoint_use_the_same_relisting_implementation() -> None:
    assert main.relist_instruments is feed_runtime.relist_instruments
    assert main.relist_forever is feed_runtime.relist_forever


class _Bus:
    instances: list[_Bus] = []

    def __init__(self, config) -> None:
        self.config = config
        self.events: list[str] = []
        self.subscriptions: list[_Subscription] = []
        self.instances.append(self)
        self.events.append("bus.construct")

    async def start(self) -> None:
        self.events.append("bus.start")

    async def aclose(self) -> None:
        self.events.append("bus.close")

    def subscribe(
        self, name: str, maxsize: int, lossless: bool = False, *, event_types=None
    ):
        subscription = _Subscription(name, maxsize, lossless, event_types)
        self.subscriptions.append(subscription)
        return subscription

    def publish(self, event) -> None:
        self.events.append("bus.publish")
        for subscription in self.subscriptions:
            subscription.queue.put_nowait(event)


class _Subscription:
    def __init__(self, name, capacity, lossless, event_types) -> None:
        self.name = name
        self.capacity = capacity
        self.lossless = lossless
        self.event_types = tuple(event_types) if event_types is not None else None
        self.queue: asyncio.Queue = asyncio.Queue()


class _Client:
    def __init__(self) -> None:
        self.events = _Bus.instances[-1].events
        self.events.append("client.construct")

    async def __aenter__(self):
        self.events.append("client.enter")
        return self

    async def aclose(self) -> None:
        self.events.append("client.close")


class _Adapter:
    def __init__(self, client, *, underlyings, feed_factory) -> None:
        del client, feed_factory
        self.events = _Bus.instances[-1].events
        self.events.append("adapter.construct")
        self.underlyings = tuple(underlyings)
        self.venue = "SCRIPT"
        self.fail_listing = False

    async def instruments(self, underlying):
        self.events.append(f"list.{underlying}")
        if self.fail_listing:
            raise DeltaUnavailable("listing unavailable")
        return [SimpleNamespace(venue_symbol=f"{underlying}-ONE")]

    def subscribe(self, instruments) -> None:
        symbols = [instrument.venue_symbol for instrument in instruments]
        self.events.append(f"subscribe.{','.join(symbols)}")

    def stop(self) -> None:
        self.events.append("adapter.stop")


class _Supervisor:
    instances: list[_Supervisor] = []

    def __init__(self, adapters, publish) -> None:
        del publish
        self.events = _Bus.instances[-1].events
        self.events.append("supervisor.construct")
        self.adapters = adapters
        self.started = False
        self.dispatched: list[object] = []
        self.instances.append(self)

    def start(self) -> None:
        self.started = True
        self.events.append("supervisor.start")

    async def aclose(self) -> None:
        self.events.append("supervisor.close")

    def dispatch_command(self, event) -> bool:
        self.dispatched.append(event)
        return True

    def report(self) -> HealthReport:
        state = ConnectionState.CONNECTED if self.started else ConnectionState.STOPPED
        return HealthReport(feed=state)


def _patch_feed_components(monkeypatch) -> None:
    _Bus.instances.clear()
    _Supervisor.instances.clear()
    monkeypatch.setattr(feed_main, "RedisBus", _Bus)
    monkeypatch.setattr(feed_main, "DeltaClient", _Client)
    monkeypatch.setattr(feed_main, "DeltaAdapter", _Adapter)
    monkeypatch.setattr(feed_main, "FeedSupervisor", _Supervisor)
    monkeypatch.setattr(feed_main, "DeltaFeed", object())
    monkeypatch.setattr(feed_main, "live_underlyings", lambda: ("BTC", "ETH"))


def test_feed_entrypoint_has_only_health_and_starts_its_redis_composition(
    monkeypatch,
) -> None:
    _patch_feed_components(monkeypatch)
    monkeypatch.setenv("DELTA_BUS", "fanout")

    assert [route.path for route in feed_main.app.routes] == ["/health"]

    with TestClient(feed_main.app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["feed"] == "connected"
        assert response.json()["watched"] == []
        assert _Bus.instances[-1].config.underlyings == ("BTC", "ETH")
        assert _Supervisor.instances[-1].started is True

    events = _Bus.instances[-1].events
    assert events[:11] == [
        "bus.construct",
        "bus.start",
        "client.construct",
        "client.enter",
        "adapter.construct",
        "supervisor.construct",
        "list.BTC",
        "subscribe.BTC-ONE",
        "list.ETH",
        "subscribe.ETH-ONE",
        "supervisor.start",
    ]
    assert "bus.close" in events
    assert "client.close" in events


def test_feed_entrypoint_propagates_redis_unavailable_before_constructing_delta(
    monkeypatch,
) -> None:
    _patch_feed_components(monkeypatch)

    async def fail_start(self) -> None:
        self.events.append("bus.start")
        raise BusUnavailable("feed Redis is unavailable")

    monkeypatch.setattr(_Bus, "start", fail_start)

    with pytest.raises(BusUnavailable, match="feed Redis is unavailable"):
        with TestClient(feed_main.app):
            pass

    assert "client.construct" not in _Bus.instances[-1].events


def test_feed_entrypoint_keeps_health_alive_when_initial_listing_is_unavailable(
    monkeypatch,
) -> None:
    _patch_feed_components(monkeypatch)

    async def unavailable(self, underlying):
        self.events.append(f"list.{underlying}")
        raise DeltaUnavailable("listing unavailable")

    monkeypatch.setattr(_Adapter, "instruments", unavailable)
    with TestClient(feed_main.app) as client:
        body = client.get("/health").json()
        assert body["feed"] == "stopped"
        assert _Supervisor.instances[-1].started is False

        command = ControlCommand(
            source="operator",
            ts_received=TS,
            adapter="SCRIPT",
            command="pause",
        )
        client.portal.call(_Bus.instances[-1].publish, command)
        client.portal.call(asyncio.sleep, 0)
        assert _Supervisor.instances[-1].dispatched == [command]


def test_feed_entrypoint_registers_one_command_consumer_and_dispatches_once(
    monkeypatch,
) -> None:
    _patch_feed_components(monkeypatch)

    with TestClient(feed_main.app) as client:
        bus = _Bus.instances[-1]
        assert [
            (sub.name, sub.capacity, sub.lossless, sub.event_types)
            for sub in bus.subscriptions
        ] == [
            ("feed-control", 100, False, ("control.command",))
        ]
        assert client.app.state.control_task.get_name() == "feed-control"

        command = ControlCommand(
            source="operator",
            ts_received=TS,
            adapter="SCRIPT",
            command="pause",
        )
        bus.publish(object())
        bus.publish(command)
        client.portal.call(asyncio.sleep, 0)
        assert _Supervisor.instances[-1].dispatched == [command]


def test_feed_entrypoint_shutdown_cancels_relisting_before_supervisor_redis_and_delta(
    monkeypatch,
) -> None:
    _patch_feed_components(monkeypatch)

    async def relist_forever(process) -> None:
        process.bus.events.append("relist.start")
        try:
            await asyncio.Event().wait()
        finally:
            process.bus.events.append("relist.cancel")

    monkeypatch.setattr(feed_main, "relist_forever", relist_forever)

    with TestClient(feed_main.app) as client:
        assert client.get("/health").status_code == 200

    events = _Bus.instances[-1].events
    assert "relist.start" in events
    assert "relist.cancel" in events
    assert events.index("relist.cancel") < events.index("supervisor.close")
    assert events.index("supervisor.close") < events.index("bus.close")
    assert events.index("bus.close") < events.index("client.close")
