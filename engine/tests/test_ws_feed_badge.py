"""#40: `/ws/chain` gains a fourth message, `feed` — `docs/live-chain-contract.md`.

Two tests, matching the ticket's two acceptance lines exactly:

* The first needs a controller already sitting in a state when a browser connects, and
  nothing else — driven synchronously, no clock, no task.
* The second needs a controller actually running *while* the websocket is open, so the
  scripted degraded-at-15s transition is observed as it happens rather than read back
  afterwards. `test_controller.py::run_script` cannot be reused as-is: its `ScriptClock`
  advances a `Silence` step with no `await` inside the loop at all, which starves the
  websocket task of any chance to run between poll ticks — fine for a test that only
  inspects the published list once the script has finished, wrong here, where a second
  task has to observe an intermediate state while it is current. `_YieldingClock` below
  is the same trick with one real yield added per tick, and `ConnectionController.run()`
  is scheduled onto `TestClient`'s own portal — the one event loop the websocket handler
  runs on — via `client.portal.start_task_soon`, which is available once the app has been
  entered as a context manager (`with TestClient(app) as client:`) even though nothing
  here needs the app's own lifespan to build a live feed.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.controller import ConnectionController
from deltapayoff.events import ConnectionState, FeedConnection, Heartbeat
from deltapayoff.main import (
    FeedConnectionCache,
    app,
    get_chain_stream,
    get_feed_cache,
    get_supervisor,
)
from deltapayoff.stream import ChainStream
from fakes.scripted_adapter import Frames, ScriptedAdapter, Silence

EXPIRY = "04-09-2026"
SYMBOL = "C-BTC-77600-040926"
#: A minimal, valid `ob_l2` frame — copied from `test_controller.py`'s `BOOK_FRAME`
#: rather than imported, following that file's own precedent of small local literals.
BOOK_FRAME = {
    "type": "ob_l2",
    "sy": SYMBOL,
    "ts": 1_788_430_765_832_299,
    "lts": 1_788_430_765_000_000,
    "a": [["125", "12"]],
    "b": [["120", "10"]],
}


@dataclass
class _FakeSupervisor:
    """Just the one attribute `live_chain` reads off a `FeedSupervisor`: `.controllers`.

    A `FeedSupervisor` proper builds its own `ConnectionController` and would need the
    real adapter/publish wiring `build_feed_stack` does; these tests want a controller
    they already hold a reference to, so they hand the route this instead of the real
    class. Duck-typed rather than a `FeedSupervisor` subclass: the route's own dependency
    type hint is `FeedSupervisor | None`, which FastAPI does not enforce at runtime on an
    overridden dependency.
    """

    controllers: list[ConnectionController] = field(default_factory=list)


def test_the_first_message_is_feed_with_the_current_state_before_any_chain() -> None:
    """How you will know, line 1: `feed` first, carrying the fake's current state."""
    cache = FeedConnectionCache()
    adapter = ScriptedAdapter()
    controller = ConnectionController(adapter, cache.apply)
    controller.start()  # -> connecting, synchronously; no clock needed for this much.

    app.dependency_overrides[get_supervisor] = lambda: _FakeSupervisor([controller])
    app.dependency_overrides[get_feed_cache] = lambda: cache
    app.dependency_overrides[get_chain_stream] = lambda: ChainStream()
    try:
        client = TestClient(app)
        with client.websocket_connect(
            f"/ws/chain?underlying=BTC&expiry={EXPIRY}"
        ) as socket:
            first = json.loads(socket.receive_text())
    finally:
        app.dependency_overrides.clear()

    assert first["type"] == "feed"
    assert first["data"]["adapter"] == "SCRIPT"  # ScriptedAdapter's own default venue
    assert first["data"]["state"] == "connecting"
    assert first["data"]["reason"] == "start"
    assert first["data"]["since"]  # a non-empty stamp; the exact format is main.py's own


def test_feed_connection_cache_drains_only_feed_state_events_and_propagates_cancel(
) -> None:
    bus = main.FanOut()
    cache = FeedConnectionCache()
    subscription = cache.attach(bus)

    event = FeedConnection(
        source="DELTA",
        ts_received=datetime(2026, 9, 12, tzinfo=timezone.utc),
        adapter="DELTA",
        to_state=ConnectionState.CONNECTED,
        reason="open",
    )
    heartbeat = Heartbeat(
        source="DELTA",
        ts_received=datetime(2026, 9, 12, tzinfo=timezone.utc),
        adapter="DELTA",
        state=ConnectionState.CONNECTED,
    )

    async def scenario() -> None:
        task = asyncio.create_task(cache.run())
        bus.publish(heartbeat)
        await asyncio.sleep(0)
        assert cache.latest == {}
        bus.publish(event)
        for _ in range(10):
            if cache.get("DELTA") is event:
                break
            await asyncio.sleep(0)
        assert cache.get("DELTA") is event
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert subscription.name == "feed-state"
    assert subscription.capacity == 100
    assert subscription.lossless is False
    asyncio.run(scenario())


def test_split_websocket_uses_a_received_feed_connection_without_a_supervisor() -> None:
    cache = FeedConnectionCache()
    event = FeedConnection(
        source="DELTA",
        ts_received=datetime(2026, 9, 12, tzinfo=timezone.utc),
        adapter="DELTA",
        to_state=ConnectionState.CONNECTED,
        reason="open",
    )
    cache.apply(event)
    app.dependency_overrides[get_supervisor] = lambda: None
    app.dependency_overrides[get_feed_cache] = lambda: cache
    app.dependency_overrides[get_chain_stream] = lambda: ChainStream()
    try:
        client = TestClient(app)
        with client.websocket_connect(
            f"/ws/chain?underlying=BTC&expiry={EXPIRY}"
        ) as socket:
            first = json.loads(socket.receive_text())
    finally:
        app.dependency_overrides.clear()

    assert first["type"] == "feed"
    assert first["data"]["adapter"] == "DELTA"
    assert first["data"]["state"] == "connected"
    assert first["data"]["reason"] == "open"


class _YieldingClock:
    """`test_controller.py::ScriptClock`, with one real `await` added per tick.

    That file's version drives a `Silence` step with no yield point at all inside the
    loop, which is exactly right for a test that inspects the published list once the
    whole script has run — and exactly wrong here, where a second coroutine (the
    websocket's own push loop) has to get scheduled *between* ticks to observe
    `degraded` while it is still the current state rather than after `stopped` has
    already replaced it.
    """

    def __init__(self, step: float = 1.0, yield_seconds: float = 0.05) -> None:
        self.now = 0.0
        self.step = step
        self.yield_seconds = yield_seconds
        self.controller: ConnectionController | None = None

    def read(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = min(end, self.now + self.step)
            if self.controller is not None:
                self.controller.poll(self.now)
            await asyncio.sleep(self.yield_seconds)


async def _never(_seconds: float) -> None:
    """The controller's own watchdog timer, neutralised — `_YieldingClock.sleep` is the
    only thing calling `poll()` in this test, exactly as `run_script` does it."""
    await asyncio.Event().wait()


class _StubDeltaClient:
    """Stands in for `DeltaClient` inside the lifespan. Opens nothing — `test_recording
    .py`'s own stub, copied rather than imported to keep this file's fixtures local."""

    async def __aenter__(self) -> _StubDeltaClient:
        return self

    async def aclose(self) -> None:
        return None


def test_a_silent_feed_shows_degraded_at_the_degraded_interval_then_clears(
    monkeypatch,
) -> None:
    """How you will know, line 2: frames, then silence, watched live over the socket.

    `test_controller.py::test_frames_then_silence_degrades_at_the_interval_and_nothing_else`
    already proves this exact script reaches `degraded` at 15s and `stopped` at 20s; this
    proves the websocket forwards that transition while a browser is connected to watch
    it, and that the badge would clear (there being no state after `stopped` in this
    script to send) rather than staying stuck on an old one.
    """
    monkeypatch.setenv("DELTA_LIVE_FEED", "0")
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)

    clock = _YieldingClock()
    adapter = ScriptedAdapter(
        script=[Frames("ob_l2", [BOOK_FRAME]), Silence(20.0)], sleep=clock.sleep
    )
    cache = FeedConnectionCache()
    controller = ConnectionController(
        adapter,
        cache.apply,
        clock=clock.read,
        sleep=_never,
        degraded_after=15.0,
        reconnect_after=45.0,
    )
    clock.controller = controller

    app.dependency_overrides[get_supervisor] = lambda: _FakeSupervisor([controller])
    app.dependency_overrides[get_feed_cache] = lambda: cache
    states: list[str] = []
    try:
        with TestClient(main.app) as client:
            # Runs on the same event loop the websocket below is served from — the
            # portal `TestClient` opens for the whole `with` block, not a private one
            # per request. The real lifespan built a real (unstarted, `DELTA_LIVE_FEED`
            # disabled) supervisor of its own; this task is a second, independent
            # controller the two dependency overrides above point the route at instead.
            client.portal.start_task_soon(controller.run)

            with client.websocket_connect(
                f"/ws/chain?underlying=BTC&expiry={EXPIRY}&interval=0.02"
            ) as socket:
                deadline = time.monotonic() + 5.0
                messages_read = 0
                while time.monotonic() < deadline and messages_read < 2000:
                    message = json.loads(socket.receive_text())
                    messages_read += 1
                    if message["type"] != "feed":
                        continue
                    state = message["data"]["state"]
                    if not states or states[-1] != state:
                        states.append(state)
                    if state == "stopped":
                        break
    finally:
        app.dependency_overrides.clear()

    # `connecting` is not asserted as the very first observed state: `Frames` publishes
    # with no `await` inside it, so the adapter can race from `connecting` to `connected`
    # before the websocket has finished connecting, and that ordering is not the thing
    # this test is pinning. What it must show, in order, is the silence taking effect and
    # then ending — the interval the ticket names.
    assert "degraded" in states, states
    assert states[-1] == "stopped"
    assert states.index("degraded") < states.index("stopped")
