"""The feed's `/health`, and the 2026-09-12 reading it was unable to refuse.

**One payload is the whole ticket.** At 16:17Z `dxp-feed` answered:

    {"status":"ok","feed":"stopped",
     "adapters":[{"state":"stopped","reason":"stopped",
                  "last_message_age_seconds":583.654,
                  "reconnects":11,"budget_remaining":0}]}

with HTTP 200, and `docker ps` read `Up (healthy)` beside it. Every fact needed to know
the feed was dead was already in that body. Compose's check is `urlopen`, which raises on
a status and never reads a body, so the facts reached nobody. **Each test below asserts
the status code as well as the body**, because the body was never the part that failed.

The route is driven at the highest seam this process has -- `feed_main.app` under
`TestClient` -- for the reason `test_supervisor.py` gives about its own: a shape asserted
anywhere else is not the shape a caller gets. The controllers underneath are real and are
moved by hand on a clock a test owns, so nothing here waits on a duration.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi.testclient import TestClient

from deltapayoff import feed_main
from deltapayoff.controller import RECONNECT_AFTER_SECONDS, ConnectionController
from deltapayoff.events import ConnectionState
from deltapayoff.redis_bus import BusConfig, RedisBus
from deltapayoff.supervisor import FeedSupervisor
from fakes.scripted_adapter import Close, ScriptedAdapter
from wait_helpers import wait_until

#: The incident's own numbers, so a reader of this file and a reader of #108 are looking
#: at one event. `measured` 2026-09-12 from `dxp-feed`'s `/health`.
INCIDENT_AGE_SECONDS = 583.654
INCIDENT_DROPS = 11

#: **Written out rather than imported**, so a test of the bound fails on the bound and
#: not on an `ImportError`. #103 refused the weak reds this suite kept producing; a red
#: that says a name is missing proves the name is missing and nothing else.
BOUND_SECONDS = 135.0


class FakeClock:
    """A monotonic clock a test moves by hand. The same one `test_supervisor.py` uses."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def read(self) -> float:
        return self.now


def _supervisor(*adapters, clock=None, **kwargs) -> FeedSupervisor:
    published: list = []
    return FeedSupervisor(
        list(adapters),
        published.append,
        clock=(clock.read if clock else None) or (lambda: 0.0),
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1_000.0,
        **kwargs,
    )


def _mount(monkeypatch, supervisor, *, bus=None, control_task=None) -> TestClient:
    """Put one composition behind the route without running the lifespan.

    The lifespan builds a Redis bus and a Delta client, neither of which a health test
    has any business needing; `test_supervisor.py` reaches `main.app` the same way.
    """
    monkeypatch.setattr(feed_main.app.state, "supervisor", supervisor, raising=False)
    monkeypatch.setattr(feed_main.app.state, "bus", bus, raising=False)
    monkeypatch.setattr(
        feed_main.app.state, "control_task", control_task, raising=False
    )
    return TestClient(feed_main.app)


def _spend_the_budget(controller, drops: int = INCIDENT_DROPS) -> None:
    """Eleven closes against one continuous DNS failure, as the log records them.

    They arrive with the connection already `reconnecting` after the first, which is the
    case `connection_closed` exists to count: no transition happens, and the budget is
    spent on the drop regardless.
    """
    for _ in range(drops):
        controller.connection_closed("gaierror: [Errno -3] Temporary failure in name")


def test_the_staleness_bound_is_three_reconnect_intervals() -> None:
    """**The bound is `derived`, and this is the arithmetic it is derived from.**

    The venue pushes a ticker refresh every `measured` 5001 ms whether or not anything
    trades. `degraded_after` is three of those and `reconnect_after` three of *those*;
    this is the same rule once more. Written as the product so that moving
    `reconnect_after` moves the bound with it rather than leaving two numbers that used
    to agree, and pinned to 135.0 so moving it is a decision somebody makes on purpose.
    """
    assert feed_main.FEED_STALE_SECONDS == RECONNECT_AFTER_SECONDS * 3
    assert feed_main.FEED_STALE_SECONDS == BOUND_SECONDS


# --- the incident ------------------------------------------------------------------


def test_a_feed_that_spent_its_budget_fails_its_health_check(monkeypatch) -> None:
    """**The 2026-09-12 reading, reproduced, and now a 503.**

    All three facts were in the old 200 body and are asserted here as sentences: the
    connection is stopped and will not dial again, there is no budget left, and no
    market-data event has arrived for 584 seconds.
    """
    clock = FakeClock()
    adapter = ScriptedAdapter(venue="DELTA")
    supervisor = _supervisor(adapter, clock=clock)
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    _spend_the_budget(controller)
    clock.now = INCIDENT_AGE_SECONDS

    response = _mount(monkeypatch, supervisor).get("/health")
    body = response.json()

    assert response.status_code == 503, (
        "the feed answered 200 while stopped with a spent budget, and Compose's "
        "urlopen check never reads a body"
    )
    assert body["status"] == "error"
    assert body["feed"] == "stopped"
    row = body["adapters"][0]
    assert (row["reconnects"], row["budget_remaining"]) == (INCIDENT_DROPS, 0)
    assert body["problems"] == [
        "the 'DELTA' connection is stopped (reason 'stopped') and will not dial again "
        "on its own; only restarting this process revives it",
        "the 'DELTA' connection has no reconnect budget left after 11 drops; the next "
        "drop stops it for good",
        "the 'DELTA' connection has delivered no market-data event for 584s, past the "
        "bound of 135s; heartbeats and control traffic do not reset this clock",
    ]


def test_a_silent_feed_fails_before_anything_has_moved_its_state(monkeypatch) -> None:
    """**The staleness bound on its own, with the state still reading `connected`.**

    This is the failure the other clauses cannot see: a connection nothing has closed,
    whose staleness timer is not running -- `run()` alerts when that timer dies and the
    state then never moves again. The age is the only fact left that is still true, and
    it is measured over market-data events alone.
    """
    clock = FakeClock()
    supervisor = _supervisor(ScriptedAdapter(venue="DELTA"), clock=clock)
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = BOUND_SECONDS + 1.0

    response = _mount(monkeypatch, supervisor).get("/health")
    body = response.json()

    assert body["feed"] == "connected", "the state machine has not been told anything"
    assert response.status_code == 503
    assert body["problems"] == [
        "the 'DELTA' connection has delivered no market-data event for 136s, past the "
        "bound of 135s; heartbeats and control traffic do not reset this clock"
    ]


def test_a_quiet_market_inside_the_bound_is_not_a_dead_feed(monkeypatch) -> None:
    """**The other half of the bound, and the reason it is 135 s and not 45 s.**

    A connection the controller is in the middle of recovering must not fail the check:
    a cut at `reconnect_after` plus a dial costs about 47 s and two consecutive ones
    about 93 s, and all of that is the controller working, not the feed being dead.

    Red against today's code on the `problems` key, which does not exist; red against a
    bound of 45 s or 90 s on the status code.
    """
    clock = FakeClock()
    supervisor = _supervisor(ScriptedAdapter(venue="DELTA"), clock=clock)
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = BOUND_SECONDS - 1.0

    response = _mount(monkeypatch, supervisor).get("/health")
    body = response.json()

    assert body["problems"] == []
    assert response.status_code == 200
    assert body["status"] == "ok"


# --- the residual literal ----------------------------------------------------------


def test_a_process_with_no_supervisor_does_not_answer_ok(monkeypatch) -> None:
    """**The literal `{"status": "ok"}` this route had left, and `store` still has.**

    `supervisor.worst()` answers `stopped` for a supervisor holding no controllers on
    purpose: a process with no feed at all is the strongest possible "not ready". A route
    that then wrote `ok` over it would be the same constant in a new place.

    Unlike the `api`, this process does nothing else. `test_supervisor.py` argues that
    `main.app` must still answer 200 with no feed, because a monitor reading 503 there
    would be told an engine that is serving chains is down. Nothing is served here but
    the feed, so the two routes reach opposite answers from the same rule.
    """
    response = _mount(monkeypatch, None).get("/health")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "error"
    assert body["problems"] == [
        "this feed process has no adapter running; nothing is connected to the venue"
    ]


def test_a_paused_feed_fails_and_says_who_stopped_it(monkeypatch) -> None:
    """A pause is deliberate and it is still a feed delivering nothing.

    503 means not ready, and a paused feed is exactly that. The sentence carries the
    reason so nobody reading the check goes looking for a fault: `reason` is `paused` and
    not `stopped` precisely so an operator's own action can be told from a spent budget.
    """
    supervisor = _supervisor(ScriptedAdapter(venue="DELTA"))
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    controller.pause()

    response = _mount(monkeypatch, supervisor).get("/health")
    body = response.json()

    assert response.status_code == 503
    assert body["adapters"][0]["reason"] == "paused"
    assert body["problems"] == [
        "the 'DELTA' connection is stopped because an operator paused it; it delivers "
        "nothing until a resume"
    ]


# --- the publisher nothing was watching ---------------------------------------------


def test_a_dead_bus_flusher_reaches_the_feeds_health(monkeypatch) -> None:
    """**The feed had no view of its own bus, so a dead publisher was invisible.**

    Everything the adapter decodes goes into the outbox and the flusher is what puts it
    on Redis. `redis_bus._flusher_exited` has always logged and recorded this; no route
    has ever asked. A feed reading the venue perfectly and publishing nothing is the same
    outage as a feed with no socket, and it reads identically from `store`'s side.
    """
    clock = FakeClock()
    supervisor = _supervisor(ScriptedAdapter(venue="DELTA"), clock=clock)
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    bus = RedisBus(BusConfig())

    async def scenario() -> None:
        async def flusher() -> None:
            raise RuntimeError("the flusher loop died")

        task = asyncio.ensure_future(flusher())
        await asyncio.gather(task, return_exceptions=True)
        bus._flusher_exited(task)

    asyncio.run(scenario())

    response = _mount(monkeypatch, supervisor, bus=bus).get("/health")
    body = response.json()

    assert response.status_code == 503
    assert body["feed"] == "connected", "the socket is fine; the publisher is not"
    assert body["problems"] == [
        "the bus flusher exited and nothing this feed decodes is reaching Redis: "
        "RuntimeError: the flusher loop died"
    ]


def test_a_dead_control_consumer_reaches_the_feeds_health(monkeypatch) -> None:
    """A feed that can no longer be paused, resumed or reconnected says so.

    `_consume_control` is a bare `while True` with no supervision of its own: if it ever
    ends, every `control.command` addressed to this feed is silently ignored, and the one
    verb an operator has left for a sick connection stops working with no symptom.
    """
    supervisor = _supervisor(ScriptedAdapter(venue="DELTA"))
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()

    async def scenario() -> asyncio.Task:
        async def consumer() -> None:
            raise RuntimeError("the control subscription is gone")

        task = asyncio.ensure_future(consumer())
        await asyncio.gather(task, return_exceptions=True)
        return task

    task = asyncio.run(scenario())

    response = _mount(monkeypatch, supervisor, control_task=task).get("/health")
    body = response.json()

    assert response.status_code == 503
    assert body["problems"] == [
        "the feed's control consumer exited and pause, resume and reconnect commands "
        "are no longer applied: the control subscription is gone"
    ]


# --- the log record that called a designed ending a surprise -------------------------


def test_a_spent_budget_is_not_logged_as_an_unexpected_ending(caplog) -> None:
    """**An error-level record for a designed outcome trains a reader to ignore them.**

    At 16:13:40Z on 2026-09-12 the controller's own ERROR named the budget, the count and
    the alert. Four seconds later the supervisor raised a second ERROR calling the same
    ending `unexpected: returned without raising`. #103 lasted two hours behind the habit
    that teaches. The ending is still reported -- a feed that has given up must never be
    silent -- and it is reported as what it is.
    """
    supervisor = _supervisor(
        ScriptedAdapter(venue="DELTA", script=[Close(), Close()]), reconnect_budget=1
    )
    controller = supervisor.controllers[0]

    async def scenario() -> None:
        supervisor.start()
        await wait_until(
            lambda: any(r.name == "deltapayoff.supervisor" for r in caplog.records),
            message="the supervisor never reported the controller's ending",
        )

    with caplog.at_level(logging.WARNING, logger="deltapayoff.supervisor"):
        asyncio.run(scenario())

    assert controller.budget_remaining == 0, "the scenario did not spend the budget"
    assert controller.state is ConnectionState.STOPPED
    records = [r for r in caplog.records if r.name == "deltapayoff.supervisor"]
    assert [r.levelno for r in records] == [logging.WARNING], (
        "the supervisor called a spent budget an unexpected ending, at ERROR, beside "
        "the controller's own ERROR that had already said it properly"
    )
    assert "reconnect budget is spent" in records[0].getMessage()
    assert "only restarting this process revives it" in records[0].getMessage()


def test_a_controller_that_raises_is_still_an_unexpected_ending(caplog) -> None:
    """The other half, so the change above cannot silence a real surprise.

    **This one passes against the code before the change as well as after**, and that is
    what it is for: it pins the half that must not move. A controller whose task ends in
    an exception is unexpected by any reading, its budget is untouched, and the error
    level it has always had is the right one. A red run for it would mean the
    discriminator had swallowed a genuine surprise.
    """

    class _RaisingController(ConnectionController):
        async def run(self) -> None:
            raise RuntimeError("the dial loop raised")

    supervisor = _supervisor(
        ScriptedAdapter(venue="DELTA"), controller_factory=_RaisingController
    )

    async def scenario() -> None:
        supervisor.start()
        await wait_until(
            lambda: any(r.name == "deltapayoff.supervisor" for r in caplog.records),
            message="the supervisor never reported the controller's ending",
        )

    with caplog.at_level(logging.WARNING, logger="deltapayoff.supervisor"):
        asyncio.run(scenario())

    records = [r for r in caplog.records if r.name == "deltapayoff.supervisor"]
    assert [r.levelno for r in records] == [logging.ERROR]
    assert "the dial loop raised" in records[0].getMessage()
