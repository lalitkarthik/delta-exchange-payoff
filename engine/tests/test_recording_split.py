"""The split recording routes read and command the standalone store."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.events import ControlCommand, StoreState
from deltapayoff.redis_bus import REDIS_BUS
from deltapayoff.supervisor import FeedSupervisor
from fakes.scripted_adapter import ScriptedAdapter

TS = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def state(recording: bool, *, second: int = 0) -> StoreState:
    return StoreState(
        source="store",
        ts_received=TS + timedelta(seconds=second),
        recording=recording,
        buffered_rows=3,
        rows_written=11,
        generation=2,
    )


def split_client(monkeypatch, cache, bus) -> TestClient:
    monkeypatch.setattr(main, "selected_bus", lambda: REDIS_BUS)
    main.app.dependency_overrides[main.get_bar_writer] = lambda: None
    main.app.dependency_overrides[main.get_store_cache] = lambda: cache
    main.app.dependency_overrides[main.get_event_bus] = lambda: bus
    return TestClient(main.app)


def test_split_get_recording_returns_503_before_the_first_store_state(
    monkeypatch,
) -> None:
    cache = main.StoreStateCache(monotonic_clock=Clock())
    bus = SimpleNamespace(config=SimpleNamespace(venue="DELTA"))
    client = split_client(monkeypatch, cache, bus)
    try:
        response = client.get("/recording")
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "has not reported" in response.json()["detail"]


def test_split_get_recording_returns_fresh_state_and_age(monkeypatch) -> None:
    clock = Clock()
    cache = main.StoreStateCache(monotonic_clock=clock)
    cache.apply(state(True))
    clock.value += 1.25
    bus = SimpleNamespace(config=SimpleNamespace(venue="DELTA"))
    client = split_client(monkeypatch, cache, bus)
    try:
        response = client.get("/recording")
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "recording": True,
        "buffered_rows": 3,
        "rows_written": 11,
        "state_age_seconds": 1.25,
    }


def test_split_get_recording_names_a_stale_age(monkeypatch) -> None:
    clock = Clock()
    cache = main.StoreStateCache(monotonic_clock=clock)
    cache.apply(state(True))
    clock.value += main.STORE_STATE_STALE_SECONDS + 1.0
    bus = SimpleNamespace(config=SimpleNamespace(venue="DELTA"))
    client = split_client(monkeypatch, cache, bus)
    try:
        response = client.get("/recording")
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert f"{main.STORE_STATE_STALE_SECONDS + 1.0:.3f}" in response.json()["detail"]


def test_split_post_publishes_one_store_command_and_returns_the_later_echo(
    monkeypatch,
) -> None:
    clock = Clock()
    cache = main.StoreStateCache(monotonic_clock=clock)
    published: list[ControlCommand] = []

    def publish(event) -> None:
        published.append(event)
        cache.apply(state(False, second=1))

    bus = SimpleNamespace(
        config=SimpleNamespace(venue="DELTA"),
        publish=publish,
    )
    client = split_client(monkeypatch, cache, bus)
    try:
        response = client.post("/recording", json={"recording": False})
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["recording"] is False
    assert len(published) == 1
    assert published[0].target == "store"
    assert published[0].command == "pause"


def test_split_post_times_out_without_a_matching_echo(monkeypatch) -> None:
    cache = main.StoreStateCache(monotonic_clock=Clock())
    published: list[ControlCommand] = []
    bus = SimpleNamespace(
        config=SimpleNamespace(venue="DELTA"),
        publish=published.append,
    )
    monkeypatch.setattr(main, "STORE_COMMAND_ACK_TIMEOUT_SECONDS", 0.01)
    client = split_client(monkeypatch, cache, bus)
    try:
        response = client.post("/recording", json={"recording": False})
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 504
    assert "recording=false" in response.json()["detail"]
    assert len(published) == 1


def test_split_post_in_requested_state_still_publishes_once_and_feed_drops_it(
    monkeypatch,
) -> None:
    cache = main.StoreStateCache(monotonic_clock=Clock())
    cache.apply(state(False))
    published: list[ControlCommand] = []
    bus = SimpleNamespace(
        config=SimpleNamespace(venue="DELTA"),
        publish=published.append,
    )
    client = split_client(monkeypatch, cache, bus)
    try:
        response = client.post("/recording", json={"recording": False})
    finally:
        client.close()
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(published) == 1
    command = published[0]
    feed_events = []
    adapter = ScriptedAdapter(venue="DELTA", underlyings=("BTC",))
    supervisor = FeedSupervisor([adapter], feed_events.append)
    before = supervisor.controllers[0].state
    try:
        assert supervisor.dispatch_command(command) is False
        assert supervisor.controllers[0].state is before
        assert feed_events == []
    finally:
        asyncio.run(supervisor.aclose())
