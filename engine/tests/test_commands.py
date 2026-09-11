"""#41: pause, resume and reconnect — the one inbound event, and the route that sends it.

Two seams, in the order `docs/design/hld.md`'s testing decisions rank them:

* **The app under `TestClient`** with `DELTA_LIVE_FEED=0`, for the route — the two names
  it validates, the status codes it refuses with, and the report it answers with.
* **The scripted fake** (`fakes/scripted_adapter.py`), for what a command *does*.

The controller tests here run a real `ConnectionController.run()` on a real event loop,
which the rest of `test_controller.py` deliberately does not: every other test there
drives the machine synchronously through `poll()` with a scripted clock, because a state
machine that needs a loop to be tested is a state machine nobody can reason about. These
three cannot be written that way. A pause is a *cancellation* of a stream in flight, and
a cancellation only exists on a loop — the whole question is what the dial loop does when
the stream it is awaiting ends without the venue having closed anything.

So the script's `Silence` step is given the **real** `asyncio.sleep`, which parks the fake
adapter inside `stream()` exactly as a live socket parks inside `recv()`, and the tests
wait on **conditions** with a deadline rather than on durations. The staleness watchdog is
neutralised by a poll interval no test reaches, for the same reason `run_script` does it:
this file is about commands, and `test_controller.py` owns staleness.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.adapters.delta import instrument_from_symbol
from deltapayoff.controller import ConnectionController
from deltapayoff.events import ConnectionState, ControlCommand, Event, FeedConnection
from deltapayoff.main import app, get_supervisor
from deltapayoff.supervisor import FeedSupervisor
from fakes.scripted_adapter import Frames, ScriptedAdapter, Silence

SYMBOL = "C-BTC-77600-040926"
#: A minimal, valid `ob_l2` frame — the same literal `test_controller.py` and
#: `test_ws_feed_badge.py` keep locally, following this suite's own precedent.
BOOK_FRAME = {
    "type": "ob_l2",
    "sy": SYMBOL,
    "ts": 1_788_430_765_832_299,
    "lts": 1_788_430_765_000_000,
    "a": [["125", "12"]],
    "b": [["120", "10"]],
}
#: What a subscribed `ScriptedAdapter` replays on every open. A reconnect that replayed a
#: subset would show up as a smaller set, which is the silent failure being asserted for.
FULL_REPLAY = {"ticker": {SYMBOL}, "ob_l2": {SYMBOL}}

#: Long enough that no test in this file reaches a poll. The watchdog is
#: `test_controller.py`'s subject, not this one's.
NEVER = 3_600.0


def subscribed_adapter(*, script) -> ScriptedAdapter:
    """A scripted adapter with one contract on both channels, parking on real time.

    `sleep=asyncio.sleep` is the point: a `Silence` step then holds the fake inside
    `stream()` the way `recv()` holds a real socket, so a `pause` has something to cut.
    """
    adapter = ScriptedAdapter(script=script, sleep=asyncio.sleep)
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    adapter.subscribe([instrument])
    return adapter


def held_open() -> list[Any]:
    """A script that delivers one frame and then holds the connection open forever."""
    return [Frames("ob_l2", [BOOK_FRAME]), Silence(NEVER)]


async def until(condition: Callable[[], bool], what: str, seconds: float = 5.0) -> None:
    """Wait for a **condition**, not a duration, and say what was being waited for.

    A `sleep` long enough to be reliable is a suite that gets slower every ticket, and
    one short enough to be quick is a suite that fails on a loaded machine. `eccf6a8`
    made the same change to the measurement tools' siblings for the same reason.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds}s waiting for {what}")
        await asyncio.sleep(0.005)


def controller_over(adapter: ScriptedAdapter, published: list[Event], **kwargs):
    return ConnectionController(
        adapter,
        published.append,
        poll_seconds=NEVER,
        heartbeat_every=NEVER,
        retry_delay=0.0,
        **kwargs,
    )


async def shut_down(controller: ConnectionController, task: asyncio.Task) -> None:
    """Take a running controller down the way `FeedSupervisor.aclose` does."""
    controller.stop(detail="the test is finished")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    controller.detach()


def moves(published: list[Event]) -> list[tuple[str | None, str, str]]:
    return [
        (
            None if e.from_state is None else e.from_state.value,
            e.to_state.value,
            e.reason,
        )
        for e in published
        if isinstance(e, FeedConnection)
    ]


# --- what a command does (seam 2: the scripted fake) --------------------------------


def test_pause_stops_without_spending_budget_and_resume_dials_again() -> None:
    """The ticket's first acceptance line, at the controller.

    Three separate facts, and the middle one is the ticket's own sentence: a deliberate
    stop is **not** a failure. The budget is untouched, no drop is counted, and the
    subscriptions the resumed socket needs are still there to replay — which is what
    makes a resume a redial rather than a restart.
    """

    async def scenario() -> None:
        published: list[Event] = []
        adapter = subscribed_adapter(script=held_open())
        controller = controller_over(adapter, published, reconnect_budget=10)
        task = asyncio.create_task(controller.run())
        try:
            await until(
                lambda: controller.state is ConnectionState.CONNECTED, "the first open"
            )

            controller.pause()

            assert controller.state is ConnectionState.STOPPED
            assert controller.reason == "paused"
            assert controller.budget_remaining == 10, "a pause is not a failure"
            assert controller.reconnects == 0, "a pause is not a drop"
            # The socket really went: the fake's stream was cut and has not been
            # re-entered, so no second connection has been opened.
            await until(lambda: controller.paused, "the pause to take")
            assert adapter.connections == 1

            controller.resume()

            assert controller.state is ConnectionState.CONNECTING
            assert controller.reason == "resume"
            await until(
                lambda: controller.state is ConnectionState.CONNECTED,
                "the resumed connection",
            )
            assert adapter.connections == 2, "resume dialled again"
            assert adapter.replays[-1] == FULL_REPLAY, (
                "a resume that replayed nothing is the failure with no error"
            )
            assert controller.reconnects == 0
            assert moves(published)[-3:] == [
                ("connected", "stopped", "paused"),
                ("stopped", "connecting", "resume"),
                ("connecting", "connected", "open"),
            ]
        finally:
            await shut_down(controller, task)

    asyncio.run(scenario())


def test_a_pause_survives_a_resume_that_lands_inside_the_cancellation() -> None:
    """The window between cancelling a stream and the cancellation being delivered.

    `pause` cancels the task running `adapter.stream` and returns; the loop learns about
    it on its next turn. A `resume` in that window clears the paused flag first, so a
    dial loop that read `self._paused` to decide what the ending meant would find it
    already `False`, treat a cut stream as an adapter that had finished, and **return out
    of `run()`** — leaving `/health` reporting `connecting` with nothing dialling. So the
    loop reads which command did the cutting instead. Without that, this test hangs at
    the wait below rather than failing on an assertion.
    """

    async def scenario() -> None:
        published: list[Event] = []
        adapter = subscribed_adapter(script=held_open())
        controller = controller_over(adapter, published)
        task = asyncio.create_task(controller.run())
        try:
            await until(
                lambda: controller.state is ConnectionState.CONNECTED, "the first open"
            )
            # No `await` between them: the loop has had no turn in which to notice.
            controller.pause()
            controller.resume()

            await until(
                lambda: controller.state is ConnectionState.CONNECTED,
                "the connection to come back rather than the loop returning",
            )
            assert not task.done(), "run() returned instead of dialling again"
            assert adapter.connections == 2
        finally:
            await shut_down(controller, task)

    asyncio.run(scenario())


def test_reconnect_drops_the_socket_and_comes_back_through_the_ordinary_route() -> None:
    """The ticket's second acceptance line: `reconnecting -> connecting -> connected`.

    And the fake records a replayed subscription, which is the half a controller cannot
    see for itself. The drop spends one of the budget because it **is** a drop — the
    ticket asks for the ordinary close handling and this is it — and the first frame off
    the new socket restores it in full, which is why the budget reads full at the end.
    """

    async def scenario() -> None:
        published: list[Event] = []
        adapter = subscribed_adapter(script=held_open())
        controller = controller_over(adapter, published, reconnect_budget=10)
        task = asyncio.create_task(controller.run())
        try:
            await until(
                lambda: controller.state is ConnectionState.CONNECTED, "the first open"
            )
            published.clear()

            controller.reconnect()

            assert controller.state is ConnectionState.RECONNECTING
            await until(
                lambda: controller.state is ConnectionState.CONNECTED,
                "the redialled connection",
            )
            assert moves(published) == [
                ("connected", "reconnecting", "closed"),
                ("reconnecting", "connecting", "backoff"),
                ("connecting", "connected", "open"),
            ]
            assert adapter.connections == 2
            assert adapter.replays[-1] == FULL_REPLAY
            assert controller.reconnects == 1, "a commanded reconnect is still a drop"
            assert controller.budget_remaining == 10, (
                "the frame off the new socket restored it"
            )
        finally:
            await shut_down(controller, task)

    asyncio.run(scenario())


def test_resume_restores_a_budget_spent_before_the_pause() -> None:
    """What a resume does to the budget, which the ticket asks to be decided in writing.

    An operator's resume is the same assertion a delivered frame makes — this endpoint is
    worth trying — and it is the only assertion available while nothing is connected.
    Resuming with two of ten left is a feed that gives up in the night over drops that
    predate the person who asked for it.
    """

    async def scenario() -> None:
        published: list[Event] = []
        adapter = subscribed_adapter(script=held_open())
        controller = controller_over(adapter, published, reconnect_budget=10)
        task = asyncio.create_task(controller.run())
        try:
            await until(
                lambda: controller.state is ConnectionState.CONNECTED, "the first open"
            )
            # Two drops with no frame between them, spent the way a real one spends it.
            controller.connection_closed("a drop")
            controller.connection_closed("another drop")
            assert controller.budget_remaining == 8

            controller.pause()
            assert controller.budget_remaining == 8, "a pause changes nothing"
            controller.resume()

            assert controller.budget_remaining == 10
            await until(
                lambda: controller.state is ConnectionState.CONNECTED, "the resume"
            )
        finally:
            await shut_down(controller, task)

    asyncio.run(scenario())


def test_a_resume_is_refused_on_a_connection_that_was_not_paused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other way to reach `stopped` is a spent budget, and that one has left `run()`.

    Moving it to `connecting` would put a state on `/health` and on the badge that
    nothing was working to make true. It is refused and said out loud instead — see
    `docs/design/lld/commands.md` section 5 for what reviving it would take.
    """
    adapter = ScriptedAdapter()
    controller = ConnectionController(adapter, lambda _event: None)
    controller.start()
    controller.stop(detail="out of budget")
    assert controller.state is ConnectionState.STOPPED

    with caplog.at_level(logging.INFO, logger="deltapayoff.controller"):
        controller.resume()

    assert controller.state is ConnectionState.STOPPED
    assert controller.reason == "stopped", "the reason was not overwritten"
    assert any("resume ignored" in r.getMessage() for r in caplog.records)


def test_a_command_for_another_adapter_is_not_taken() -> None:
    """The addressing check, at the controller, which is where the ticket puts it."""
    controller = ConnectionController(ScriptedAdapter(), lambda _event: None)
    controller.start()

    taken = controller.command(_command("DELTA", "pause"))

    assert taken is False
    assert controller.state is ConnectionState.CONNECTING, "it acted on someone else's"


def test_pause_and_reconnect_are_idempotent_and_quiet_when_there_is_nothing_to_do() -> (
    None
):
    """A verb an operator repeats must not be an error, and must not move anything.

    What this pins is the **state guard** in each verb, not a flag: a second pause with
    no refusal to leave `stopped` raises `IllegalTransition` on a `stopped -> stopped`
    move, and a reconnect on a stopped connection would report a drop against a socket
    that does not exist. Red-green verified by removing that guard from `pause`.
    """
    published: list[Event] = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    controller.pause()
    published.clear()

    controller.pause()
    controller.reconnect()

    assert controller.state is ConnectionState.STOPPED
    assert published == [], "a repeated pause described a change that did not happen"


def test_a_pause_and_a_resume_before_run_do_not_start_the_machine_twice() -> None:
    """Both verbs are reachable on a supervisor that is built and not yet started.

    `FeedSupervisor` constructs its controllers in `build_feed_stack` and only starts them
    in the lifespan, so a `pause` followed by a `resume` in that window leaves the machine
    already in `connecting`. `run()` used to call `start()` unconditionally, which is a
    `connecting -> connecting` move: `IllegalTransition`, raised out of `run()`, killing
    the controller's task at start-up with the connection never dialled. Found by probing
    the edge rather than by a report, and it fails with that exception without the guard.
    """

    async def scenario() -> None:
        published: list[Event] = []
        adapter = subscribed_adapter(script=held_open())
        controller = controller_over(adapter, published)
        controller.pause()
        controller.resume()
        assert controller.state is ConnectionState.CONNECTING

        task = asyncio.create_task(controller.run())
        try:
            await until(
                lambda: controller.state is ConnectionState.CONNECTED,
                "the connection to open rather than run() raising",
            )
            assert adapter.connections == 1, "it dialled once, not twice"
        finally:
            await shut_down(controller, task)

    asyncio.run(scenario())


# --- the route (seam 1: the app under TestClient) ------------------------------------


def _command(adapter: str, verb: str) -> ControlCommand:
    return ControlCommand(
        source="operator",
        ts_received=datetime.now(UTC),
        adapter=adapter,
        command=verb,  # type: ignore[arg-type]
    )


class StubDeltaClient:
    """Stands in for `DeltaClient` inside the lifespan. Opens nothing.

    `TestClient(app)` must be entered as a context manager for `client.portal` to exist,
    and entering it runs the real lifespan — which builds an HTTP client. `conftest.py`
    refuses a real one, correctly. `test_ws_feed_badge.py` and `test_recording.py` each
    keep their own copy of this stub; this is a third, for the same reason they did not
    share theirs: it is four lines and a shared fixture would be the larger dependency.
    """

    async def __aenter__(self) -> StubDeltaClient:
        return self

    async def aclose(self) -> None:
        return None


class Wiring:
    """A supervisor over one scripted adapter, plus the bus it publishes to.

    The real `FeedSupervisor` and the real `ConnectionController`, not a double: the
    route's whole job is to reach them, and a fake supervisor here would leave the one
    seam this ticket adds untested.
    """

    def __init__(self, script) -> None:
        self.published: list[Event] = []
        self.adapter = subscribed_adapter(script=script)
        self.supervisor = FeedSupervisor(
            [self.adapter],
            self.published.append,
            poll_seconds=NEVER,
            heartbeat_every=NEVER,
            retry_delay=0.0,
        )

    @property
    def controller(self) -> ConnectionController:
        return self.supervisor.controllers[0]


async def _start(supervisor: FeedSupervisor) -> None:
    """`FeedSupervisor.start` is synchronous but needs a running loop to create tasks in.

    Started **through the supervisor** rather than as a loose task on the portal, so that
    `aclose` below is the thing that takes it down: these scripts hold their connection
    open for an hour, and a task the portal spawned but nobody owns would still be parked
    inside it when `TestClient`'s `with` block tried to close — which hangs the suite
    rather than failing it.
    """
    supervisor.start()


def poll_health(client: TestClient, wanted: str, seconds: float = 5.0) -> dict:
    """Read `/health` until the one adapter reaches `wanted`. Condition, not duration.

    Each request round-trips through `TestClient`'s portal, which is the same event loop
    the controller's own task runs on — so this both observes the state and gives the
    loop the turns it needs to reach it.
    """
    deadline = time.monotonic() + seconds
    while True:
        line = client.get("/health").json()["adapters"][0]
        if line["state"] == wanted:
            return line
        if time.monotonic() > deadline:
            raise AssertionError(f"/health never reached {wanted}: {line}")


def test_the_route_pauses_and_resumes_a_running_adapter(monkeypatch) -> None:
    """How you will know, line 1, over HTTP.

    `/health` shows `stopped` with reason `paused` and the budget unchanged; `resume`
    answers `connecting` and the fake gets there on its own.
    """
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient)
    wiring = Wiring(held_open())
    app.dependency_overrides[get_supervisor] = lambda: wiring.supervisor
    try:
        with TestClient(app) as client:
            client.portal.call(_start, wiring.supervisor)
            poll_health(client, "connected")

            paused = client.post("/feed/SCRIPT/pause")

            assert paused.status_code == 200
            assert paused.json()["state"] == "stopped"
            assert paused.json()["reason"] == "paused"
            assert paused.json()["budget_remaining"] == 10
            assert paused.json()["reconnects"] == 0
            assert client.get("/health").json()["feed"] == "stopped"

            resumed = client.post("/feed/SCRIPT/resume")

            assert resumed.json()["state"] == "connecting"
            assert resumed.json()["reason"] == "resume"
            assert poll_health(client, "connected")["reason"] == "open"
            assert wiring.adapter.connections == 2
            client.portal.call(wiring.supervisor.aclose)
    finally:
        app.dependency_overrides.clear()


def test_the_route_reconnects_and_the_fake_records_a_replay(monkeypatch) -> None:
    """How you will know, line 2, over HTTP: the sequence, and the replayed registry."""
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient)
    wiring = Wiring(held_open())
    app.dependency_overrides[get_supervisor] = lambda: wiring.supervisor
    try:
        with TestClient(app) as client:
            client.portal.call(_start, wiring.supervisor)
            poll_health(client, "connected")
            wiring.published.clear()

            answer = client.post("/feed/SCRIPT/reconnect")

            assert answer.json()["state"] == "reconnecting"
            poll_health(client, "connected")
            client.portal.call(wiring.supervisor.aclose)
    finally:
        app.dependency_overrides.clear()

    # The three the ticket names, then the shutdown's own `stopped` from `aclose`.
    assert moves(wiring.published)[:3] == [
        ("connected", "reconnecting", "closed"),
        ("reconnecting", "connecting", "backoff"),
        ("connecting", "connected", "open"),
    ]
    assert wiring.adapter.replays[-1] == FULL_REPLAY


def test_the_command_reaches_the_bus_before_the_transitions_it_causes(
    monkeypatch,
) -> None:
    """A command is an event like any other, and a log read in order shows cause first.

    The route only publishes; the controller does the work when the event arrives. Both
    happen inside the one call, and `docs/design/lld/commands.md` section 3 says why.
    """
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient)
    wiring = Wiring(held_open())
    app.dependency_overrides[get_supervisor] = lambda: wiring.supervisor
    try:
        with TestClient(app) as client:
            client.portal.call(_start, wiring.supervisor)
            poll_health(client, "connected")
            wiring.published.clear()
            client.post("/feed/script/pause")
            client.portal.call(wiring.supervisor.aclose)
    finally:
        app.dependency_overrides.clear()

    kinds = [e.type for e in wiring.published]
    assert kinds[0] == "control.command", kinds
    assert kinds[1] == "feed.connection", kinds
    command = wiring.published[0]
    assert isinstance(command, ControlCommand)
    assert (command.adapter, command.command, command.source) == (
        "SCRIPT",
        "pause",
        "operator",
    ), "the route matched a lower-case name to the adapter's own spelling"


def test_an_unknown_adapter_is_a_404_that_names_it_and_publishes_nothing(
    monkeypatch,
) -> None:
    """A command nobody can carry out must not reach the bus, where a later reader would
    find it and assume it happened."""
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient)
    wiring = Wiring([])
    app.dependency_overrides[get_supervisor] = lambda: wiring.supervisor
    try:
        with TestClient(app) as client:
            answer = client.post("/feed/NYSE/pause")
    finally:
        app.dependency_overrides.clear()

    assert answer.status_code == 404
    assert "NYSE" in answer.json()["detail"]
    assert "SCRIPT" in answer.json()["detail"], "it says which ones do exist"
    assert wiring.published == []


def test_an_unknown_verb_is_a_422_that_names_it_and_publishes_nothing(
    monkeypatch,
) -> None:
    """The address is fine and the instruction is not, which is what 422 means."""
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient)
    wiring = Wiring([])
    app.dependency_overrides[get_supervisor] = lambda: wiring.supervisor
    try:
        with TestClient(app) as client:
            answer = client.post("/feed/SCRIPT/restart")
    finally:
        app.dependency_overrides.clear()

    assert answer.status_code == 422
    assert "restart" in answer.json()["detail"]
    assert "pause, resume or reconnect" in answer.json()["detail"]
    assert wiring.published == []


def test_a_process_with_no_feed_answers_404_rather_than_raising(monkeypatch) -> None:
    """`get_supervisor` is `None` when no lifespan has run, and this route must say so
    the way `/health` does — an engine with no adapters has no `DELTA` to command."""
    monkeypatch.setattr(main, "DeltaClient", StubDeltaClient)
    app.dependency_overrides[get_supervisor] = lambda: None
    try:
        with TestClient(app) as client:
            answer = client.post("/feed/DELTA/pause")
    finally:
        app.dependency_overrides.clear()

    assert answer.status_code == 404
    assert "DELTA" in answer.json()["detail"]
    assert "none" in answer.json()["detail"]


def test_the_supervisor_offers_a_command_to_the_controller_that_owns_the_adapter() -> (
    None
):
    """The dispatch itself, without a route: published once, taken by exactly one."""
    published: list[Event] = []
    first = ScriptedAdapter(venue="ONE")
    second = ScriptedAdapter(venue="TWO")
    supervisor = FeedSupervisor([first, second], published.append)
    for controller in supervisor.controllers:
        controller.start()
    published.clear()

    taken = supervisor.command(_command("TWO", "pause"))

    assert taken is True
    assert [c.state for c in supervisor.controllers] == [
        ConnectionState.CONNECTING,
        ConnectionState.STOPPED,
    ], "the command reached the adapter it named and only that one"
    assert published[0].type == "control.command"
    assert supervisor.names() == ["ONE", "TWO"]


def test_dispatch_command_routes_a_published_event_without_republishing_it() -> None:
    published: list[Event] = []
    first = ScriptedAdapter(venue="ONE")
    second = ScriptedAdapter(venue="TWO")
    supervisor = FeedSupervisor([first, second], published.append)
    for controller in supervisor.controllers:
        controller.start()
    published.clear()

    taken = supervisor.dispatch_command(_command("TWO", "pause"))

    assert taken is True
    assert [controller.state for controller in supervisor.controllers] == [
        ConnectionState.CONNECTING,
        ConnectionState.STOPPED,
    ]
    assert [event.type for event in published] == ["feed.connection"]
    assert not any(isinstance(event, ControlCommand) for event in published)


def test_dispatch_command_offers_to_every_controller_after_one_accepts() -> None:
    offered: list[str] = []

    class Controller:
        def __init__(self, adapter, _publish) -> None:
            self.adapter_name = adapter.venue

        def command(self, _event: ControlCommand) -> bool:
            offered.append(self.adapter_name)
            return self.adapter_name == "ONE"

    supervisor = FeedSupervisor(
        [ScriptedAdapter(venue="ONE"), ScriptedAdapter(venue="TWO")],
        lambda _event: None,
        controller_factory=Controller,
    )

    assert supervisor.dispatch_command(_command("ONE", "pause")) is True
    assert offered == ["ONE", "TWO"]


def test_command_publishes_before_dispatching_the_event() -> None:
    published: list[Event] = []
    supervisor = FeedSupervisor([ScriptedAdapter(venue="TWO")], published.append)
    supervisor.controllers[0].start()
    published.clear()

    assert supervisor.command(_command("TWO", "pause")) is True

    assert [event.type for event in published] == [
        "control.command",
        "feed.connection",
    ]
