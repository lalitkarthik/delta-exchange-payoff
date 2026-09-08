"""The supervisor, and the health route it answers through.

**Two adapters, one healthy and one not, is the whole point of this file.** A supervisor
over one controller is indistinguishable from no supervisor at all: every aggregation
rule it could get wrong — worst rather than first, worst rather than best, an empty set
reading as green — is invisible until there are two. So the fake adapter appears in pairs
here, driven to different states by hand.

The route is tested at the highest seam this project has, the app under `TestClient` with
`DELTA_LIVE_FEED=0`, because `/health`'s shape is the contract #40, #41 and #44 read
through and a shape asserted anywhere else is a shape they do not get.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.events import Alert, ConnectionState
from deltapayoff.supervisor import SEVERITY, FeedSupervisor, worst
from fakes.scripted_adapter import Close, Frames, ScriptedAdapter, Silence

SYMBOL = "C-BTC-77600-040926"
BOOK_FRAME = {
    "type": "ob_l2",
    "sy": SYMBOL,
    "ts": 1_788_430_765_832_299,
    "lts": 1_788_430_765_000_000,
    "a": [["125", "12"]],
    "b": [["120", "10"]],
}


class FakeClock:
    """A monotonic clock a test moves by hand."""

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


# --- the order ---------------------------------------------------------------------


def test_the_order_runs_from_connected_down_to_stopped() -> None:
    """The ticket asks for an explicit order, so it is asserted explicitly rather than
    inferred from the enum's declaration — which is a different list that happens to
    look similar and would drift the first time a state is added."""
    assert [state.value for state in SEVERITY] == [
        "connected",
        "degraded",
        "connecting",
        "reconnecting",
        "stopped",
    ]


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([ConnectionState.CONNECTED, ConnectionState.CONNECTED], "connected"),
        ([ConnectionState.CONNECTED, ConnectionState.DEGRADED], "degraded"),
        ([ConnectionState.DEGRADED, ConnectionState.RECONNECTING], "reconnecting"),
        ([ConnectionState.STOPPED, ConnectionState.CONNECTED], "stopped"),
        ([ConnectionState.CONNECTED, None], "stopped"),
        ([], "stopped"),
    ],
)
def test_the_worst_state_wins(states, expected) -> None:
    """**Worst, not first and not best.** A feed is only as good as its sickest
    connection, and an empty supervisor is a process with no feed at all — which is the
    strongest possible "not ready" and the case an `any()` gets backwards."""
    assert worst(states).value == expected


# --- owning the controllers --------------------------------------------------------


def test_one_controller_per_adapter_in_the_configured_order() -> None:
    first, second = ScriptedAdapter(venue="ALPHA"), ScriptedAdapter(venue="BETA")

    supervisor = _supervisor(first, second)

    assert [c.adapter_name for c in supervisor.controllers] == ["ALPHA", "BETA"]
    assert supervisor.state is ConnectionState.STOPPED, "nothing has been started"


def test_closing_takes_every_controller_off_its_adapter() -> None:
    """**The leak #38 flagged this ticket for.** A controller registers itself on its
    adapter at construction, and one left registered goes on being told about a socket
    it no longer owns — answering, inside a reader it does not belong to, by raising.

    `run()` detaches on its own way out, but a supervisor closed before it ever started
    has controllers `run()` never spoke for, and this is that case.
    """
    first, second = ScriptedAdapter(venue="ALPHA"), ScriptedAdapter(venue="BETA")
    supervisor = _supervisor(first, second)
    assert len(first._listeners) == 1 and len(second._listeners) == 1

    asyncio.run(supervisor.aclose())

    assert first._listeners == []
    assert second._listeners == []


def test_closing_twice_is_not_an_error() -> None:
    """A lifespan that fails part way tidies up on both paths and must not be handed a
    second failure by the tidy-up.

    **This test asserts nothing, deliberately: the second `aclose()` not raising is the
    whole claim.** Said out loud because an assertionless test otherwise reads as one
    somebody forgot to finish. It fails by raising — `aclose` iterating a list it has
    already emptied, or `detach` being handed a controller already off its adapter.
    """
    supervisor = _supervisor(ScriptedAdapter())

    async def scenario() -> None:
        await supervisor.aclose()
        await supervisor.aclose()

    asyncio.run(scenario())


def test_starting_runs_every_controller_and_stopping_ends_them() -> None:
    """The lifespan's half: one task per adapter, and none left running after."""
    adapters = [
        ScriptedAdapter(venue="ALPHA", script=[Frames("ob_l2", [BOOK_FRAME])]),
        ScriptedAdapter(venue="BETA", script=[Frames("ob_l2", [BOOK_FRAME])]),
    ]
    supervisor = _supervisor(*adapters, poll_seconds=0.01)
    seen: dict[str, object] = {}

    async def scenario() -> None:
        supervisor.start()
        # Captured while it is running, because `aclose()` stops every controller
        # unconditionally — so a `STOPPED` asserted *after* it is guaranteed by
        # construction and passes with the whole supervisor deleted. What can fail is
        # that starting actually connected both adapters.
        #
        # Not the state, though, at either end. `start()` is synchronous and only
        # creates the tasks, so the supervisor reads `stopped` until the first of them
        # runs; and by the time the sleep is over these scripts have run out, so it
        # reads `stopped` again for a reason that has nothing to do with the lifespan.
        # The tasks are what this test is about, so the tasks are what it asserts.
        seen["tasks"] = list(supervisor._tasks)
        await asyncio.sleep(0.05)
        await supervisor.aclose()

    asyncio.run(scenario())

    assert [a.connections for a in adapters] == [1, 1]
    assert len(seen["tasks"]) == len(adapters), "one task per adapter"
    assert all(task.done() for task in seen["tasks"]), "none left running after"
    assert supervisor._tasks == []


# --- the report --------------------------------------------------------------------


def _healthy_and_degraded() -> FeedSupervisor:
    """One adapter connected and delivering, one that has gone quiet past the bound.

    Driven by hand rather than by a script, because the two have to be in **different**
    states at the same instant and a script walks one adapter at a time.
    """
    clock = FakeClock()
    healthy = ScriptedAdapter(venue="ALPHA")
    quiet = ScriptedAdapter(venue="BETA")
    supervisor = _supervisor(healthy, quiet, clock=clock)
    alpha, beta = supervisor.controllers

    for controller in (alpha, beta):
        controller.start()
        controller.connection_opened()
        controller.message_arrived()

    clock.now = 20.0
    alpha.message_arrived()
    beta.poll()
    return supervisor


def test_two_adapters_one_degraded_report_degraded_overall() -> None:
    supervisor = _healthy_and_degraded()

    report = supervisor.report()

    assert report.feed is ConnectionState.DEGRADED
    assert [(a.adapter, a.state.value) for a in report.adapters] == [
        ("ALPHA", "connected"),
        ("BETA", "degraded"),
    ]


def test_the_health_route_reports_the_supervisor(monkeypatch) -> None:
    """**The acceptance shape, over HTTP.** Every field the later tickets read, and the
    old `status` still beside them."""
    supervisor = _healthy_and_degraded()
    monkeypatch.setattr(main.app.state, "supervisor", supervisor, raising=False)

    body = TestClient(main.app).get("/health").json()

    assert body["status"] == "ok", "the shape this route used to be, preserved"
    assert body["feed"] == "degraded"
    assert [row["adapter"] for row in body["adapters"]] == ["ALPHA", "BETA"]
    alpha, beta = body["adapters"]
    assert alpha["state"] == "connected"
    assert alpha["last_message_age_seconds"] == 0.0
    assert alpha["last_message_at"] is not None
    assert alpha["reconnects"] == 0
    assert alpha["budget_remaining"] == 10
    assert alpha["transitions"] == 2
    assert beta["state"] == "degraded"
    assert beta["last_message_age_seconds"] == 20.0
    # A fake has no socket owner and no decoder counter to ask, and an absent count is
    # `null`, never `0` — each of these is a counter whose whole signal is being above
    # zero, so "still nought" and "nobody is counting" must not look the same.
    assert beta["empty_opens"] is None
    assert beta["undecodable"] is None


def test_a_process_with_no_feed_still_answers(monkeypatch) -> None:
    """**A health check that fails because there is no feed tells a monitor the engine
    is down when it is up.** So the report is still given, with `feed` reading `stopped`
    — which is exactly true of a process with no feed — rather than a 503."""
    monkeypatch.setattr(main.app.state, "supervisor", None, raising=False)

    body = TestClient(main.app).get("/health").json()

    assert body == {"status": "ok", "feed": "stopped", "adapters": []}


def test_the_report_carries_the_reconnect_count_and_what_is_left_of_the_budget() -> None:
    """A feed two drops from `stopped` and a feed that has never dropped are the same
    green badge without these two numbers."""
    adapter = ScriptedAdapter(venue="ALPHA")
    supervisor = _supervisor(adapter, reconnect_budget=5)
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.connection_closed("1006")
    controller.connection_opened()
    controller.connection_closed("1006")

    row = supervisor.report().adapters[0]

    assert (row.reconnects, row.budget_remaining) == (2, 3)


def test_an_adapter_that_has_never_delivered_reports_an_unknown_age() -> None:
    """`null` is not zero, and it is not the epoch. An age of `None` is an age nobody
    knows, which is the same rule the heartbeat's own field carries."""
    supervisor = _supervisor(ScriptedAdapter(venue="ALPHA"))
    supervisor.controllers[0].start()

    row = supervisor.report().adapters[0]

    assert row.last_message_age_seconds is None
    assert row.last_message_at is None


def test_a_silent_connection_shows_as_reconnecting_in_the_report() -> None:
    """The staleness path reaches the report as well as the badge, which is what makes
    `/health` answer the readiness question rather than the liveness one."""
    clock = FakeClock()
    supervisor = _supervisor(
        ScriptedAdapter(venue="ALPHA", script=[Silence(60.0)]), clock=clock
    )
    controller = supervisor.controllers[0]
    controller.start()
    controller.connection_opened()
    controller.message_arrived()

    clock.now = 60.0
    controller.poll()

    assert supervisor.report().feed is ConnectionState.RECONNECTING


def test_a_supervisors_controllers_publish_to_the_bus_it_was_given() -> None:
    """The wiring #38 built and nothing connected: a controller's `feed.connection`,
    `heartbeat` and `alert` reach the same bus the market data does, because the
    supervisor hands each controller the publish it was constructed with."""
    published: list = []
    adapter = ScriptedAdapter(
        venue="ALPHA", script=[Frames("ob_l2", [BOOK_FRAME]), Close()]
    )
    supervisor = FeedSupervisor(
        [adapter], published.append, heartbeat_every=1_000.0, poll_seconds=0.01
    )

    async def scenario() -> None:
        supervisor.start()
        await asyncio.sleep(0.05)
        await supervisor.aclose()

    asyncio.run(scenario())

    kinds = {type(event).__name__ for event in published}
    assert "FeedConnection" in kinds
    assert "OptionQuote" in kinds, "the market data goes to the same bus"


def test_the_supervisor_does_not_restart_a_controller_that_gave_up() -> None:
    """**It restarts nothing, on purpose.** A controller already owns its own reconnect,
    with a budget; a supervisor that restarted one whose budget was spent would undo the
    single decision the budget exists to make, and hammer the venue doing it."""
    adapter = ScriptedAdapter(venue="ALPHA", script=[Close(), Close()])
    published: list = []
    supervisor = FeedSupervisor(
        [adapter],
        published.append,
        reconnect_budget=1,
        heartbeat_every=1_000.0,
        poll_seconds=0.01,
    )

    async def scenario() -> None:
        supervisor.start()
        await asyncio.sleep(0.05)
        await supervisor.aclose()

    asyncio.run(scenario())

    controller = supervisor.controllers[0]
    # **The alert, not the state.** Three assertions were here that could not fail: an
    # `isinstance` guaranteed by the default factory, an identity check on a list
    # nothing in the supervisor mutates, and `state is STOPPED` after an `aclose()` that
    # stops every controller unconditionally. The last one is the trap — it also passes
    # when the budget is disabled outright, because this script runs out and the
    # controller stops for that reason instead. Verified by disabling the budget check
    # and watching all three still pass.
    #
    # What the budget actually produces, and nothing else here does, is one alert.
    spent = [
        event
        for event in published
        if isinstance(event, Alert) and event.code == "reconnect_budget_spent"
    ]
    assert len(spent) == 1, "the spent budget was not announced exactly once"
    assert controller.budget_remaining == 0
    # The fake reconnects inside its own `Close`, so three opens is the script running
    # out — not a fourth attempt. What matters is that the controller is `stopped` and
    # stayed that way: the supervisor built no replacement for it, which is what the
    # connection count says. (An `isinstance` on the controller and an identity check on
    # the list were here and are gone: the default factory guarantees the first and
    # nothing in the supervisor can mutate the second, so neither could ever fail.)
    assert adapter.connections == 3


def test_the_delta_adapters_silent_failure_counters_reach_the_report() -> None:
    """**The promise `delta.py` made and nothing kept.** Its comment says a systematic
    decode bug zeroes the event stream while the message counter climbs, and that
    `undecodable` is not on `/health` until #39. It is now, beside `empty_opens`, and
    both are real numbers rather than `null` when there is an adapter that counts them.
    """
    from deltapayoff.adapters import DeltaAdapter, DeltaFeed

    adapter = DeltaAdapter(feed_factory=lambda sink, **kw: DeltaFeed(sink, **kw))
    supervisor = _supervisor(adapter)
    adapter.feed.empty_opens = 2
    adapter.undecodable = 7

    row = supervisor.report().adapters[0]

    assert (row.empty_opens, row.undecodable) == (2, 7)
