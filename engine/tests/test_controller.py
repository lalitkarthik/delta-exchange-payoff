"""The connection controller's state machine, driven by the scripted fake.

Every test here is written in #36's four script verbs, and the clock is injected, so a
suite that exercises a twenty-second silence and a forty-five-second one costs no wall
clock at all.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from deltapayoff import log_events
from deltapayoff.adapters import DeltaAdapter, DeltaFeed, instrument_from_symbol
from deltapayoff.adapters.base import ConnectionSignal
from deltapayoff.controller import ConnectionController, IllegalTransition
from deltapayoff.events import Alert, ConnectionState, FeedConnection, Heartbeat
from fakes.scripted_adapter import Close, Frames, ScriptedAdapter, Silence
from test_feed import FakeSocket, connector, ticker_frame


class FakeClock:
    """A monotonic clock a test moves by hand. Silence costs no wall clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def read(self) -> float:
        return self.now


def test_start_enters_connecting_from_nothing() -> None:
    """The `—` row of `hld.md` §3: an adapter start has no from-state."""
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)

    controller.start()

    assert controller.state is ConnectionState.CONNECTING
    (event,) = published
    assert isinstance(event, FeedConnection)
    assert event.from_state is None
    assert event.to_state is ConnectionState.CONNECTING
    assert event.reason == "start"


def test_open_after_start_reaches_connected() -> None:
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    published.clear()

    controller.connection_opened()

    assert controller.state is ConnectionState.CONNECTED
    (event,) = published
    assert (event.from_state, event.to_state) == (
        ConnectionState.CONNECTING,
        ConnectionState.CONNECTED,
    )
    assert event.reason == "open"


def test_open_while_reconnecting_passes_through_connecting() -> None:
    """The table forbids `reconnecting -> connected`, so the machine takes both steps.

    Backoff still lives in the adapter, so the controller learns an attempt was made
    only when it succeeds; it emits the `reconnecting -> connecting` move at that
    instant rather than skipping a state it is not allowed to skip.
    """
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    controller.connection_closed("socket gone")
    published.clear()

    controller.connection_opened()

    assert controller.state is ConnectionState.CONNECTED
    assert [(e.from_state, e.to_state, e.reason) for e in published] == [
        (ConnectionState.RECONNECTING, ConnectionState.CONNECTING, "backoff"),
        (ConnectionState.CONNECTING, ConnectionState.CONNECTED, "open"),
    ]


def test_close_reaches_reconnecting_with_reason_closed() -> None:
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    controller.connection_opened()
    published.clear()

    controller.connection_closed("1006 abnormal closure")

    assert controller.state is ConnectionState.RECONNECTING
    (event,) = published
    assert event.from_state is ConnectionState.CONNECTED
    assert event.to_state is ConnectionState.RECONNECTING
    assert event.reason == "closed"


def test_message_brings_a_degraded_connection_back() -> None:
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(), published.append, clock=clock.read, degraded_after=15.0
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = 20.0
    controller.poll()
    assert controller.state is ConnectionState.DEGRADED
    published.clear()

    controller.message_arrived()

    assert controller.state is ConnectionState.CONNECTED
    (event,) = published
    assert (event.from_state, event.to_state, event.reason) == (
        ConnectionState.DEGRADED,
        ConnectionState.CONNECTED,
        "message",
    )


def test_a_message_while_connecting_is_proof_the_socket_is_up() -> None:
    """Not a row the table lacks — the same `connecting -> connected` move, reached by a
    second trigger. A frame is stronger evidence than an open, and an adapter that
    reported its opens late would otherwise sit in `connecting` while data flowed."""
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    published.clear()

    controller.message_arrived()

    assert controller.state is ConnectionState.CONNECTED
    (event,) = published
    assert event.reason == "message"


def test_a_message_while_connected_changes_nothing_but_the_age() -> None:
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(), published.append, clock=clock.read
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    published.clear()

    clock.now = 4.0
    controller.message_arrived()

    assert published == []
    assert controller.last_message_age() == 0.0


def test_stop_reaches_stopped_from_anywhere_running() -> None:
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    controller.connection_opened()
    published.clear()

    controller.stop()

    assert controller.state is ConnectionState.STOPPED
    (event,) = published
    assert (event.from_state, event.to_state, event.reason) == (
        ConnectionState.CONNECTED,
        ConnectionState.STOPPED,
        "stopped",
    )


def test_start_out_of_stopped_is_a_resume() -> None:
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    controller.stop()
    published.clear()

    controller.start()

    assert controller.state is ConnectionState.CONNECTING
    (event,) = published
    assert (event.from_state, event.to_state, event.reason) == (
        ConnectionState.STOPPED,
        ConnectionState.CONNECTING,
        "resume",
    )


# --- the transition table ---------------------------------------------------------

#: **Transcribed from `hld.md` section 3 by hand**, not imported from the module under
#: test. A table the test read out of `controller.ALLOWED` could never disagree with it,
#: and disagreeing with it is the only thing this test is for.
#:
#: The two "any" rows are expanded here the way the design reads them: "any" is any state
#: the connection could be in and is not already, which excludes `stopped` — a stopped
#: connection resumes to `connecting` by the table's own row, so it cannot also drop to
#: `reconnecting` — and excludes a state moving to itself.
SPEC_TABLE = {
    (None, ConnectionState.CONNECTING),
    (ConnectionState.STOPPED, ConnectionState.CONNECTING),
    (ConnectionState.CONNECTING, ConnectionState.CONNECTED),
    (ConnectionState.CONNECTED, ConnectionState.DEGRADED),
    (ConnectionState.DEGRADED, ConnectionState.CONNECTED),
    (ConnectionState.DEGRADED, ConnectionState.RECONNECTING),
    (ConnectionState.CONNECTING, ConnectionState.RECONNECTING),
    (ConnectionState.CONNECTED, ConnectionState.RECONNECTING),
    (ConnectionState.RECONNECTING, ConnectionState.CONNECTING),
    (ConnectionState.RECONNECTING, ConnectionState.STOPPED),
    (ConnectionState.CONNECTING, ConnectionState.STOPPED),
    (ConnectionState.CONNECTED, ConnectionState.STOPPED),
    (ConnectionState.DEGRADED, ConnectionState.STOPPED),
}

FROM_STATES = [None, *ConnectionState]
TO_STATES = list(ConnectionState)


def drive_to(
    state: ConnectionState | None, published: list, clock: FakeClock
) -> ConnectionController:
    """Put a fresh controller in `state` using nothing but the cause methods."""
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
    )
    if state is None:
        return controller
    controller.start()
    if state is ConnectionState.CONNECTING:
        return controller
    if state is ConnectionState.STOPPED:
        controller.stop()
        return controller
    if state is ConnectionState.RECONNECTING:
        controller.connection_closed("driven")
        return controller
    controller.connection_opened()
    if state is ConnectionState.CONNECTED:
        return controller
    controller.message_arrived()
    clock.now += 16.0
    controller.poll()
    assert controller.state is ConnectionState.DEGRADED
    return controller


@pytest.mark.parametrize("to_state", TO_STATES, ids=lambda s: f"to-{s.value}")
@pytest.mark.parametrize(
    "from_state", FROM_STATES, ids=lambda s: f"from-{'none' if s is None else s.value}"
)
def test_every_cell_of_the_transition_table(
    from_state: ConnectionState | None, to_state: ConnectionState
) -> None:
    """Every allowed move emits one event; every forbidden move raises and changes
    nothing."""
    published: list = []
    clock = FakeClock()
    controller = drive_to(from_state, published, clock)
    assert controller.state is from_state
    published.clear()

    if (from_state, to_state) in SPEC_TABLE:
        controller.transition(to_state, "a reason")

        assert controller.state is to_state
        transitions = [e for e in published if isinstance(e, FeedConnection)]
        assert len(transitions) == 1
        (event,) = transitions
        assert event.from_state is from_state
        assert event.to_state is to_state
        assert event.reason == "a reason"
        assert event.adapter == "SCRIPT"
        assert event.instrument is None
    else:
        with pytest.raises(IllegalTransition):
            controller.transition(to_state, "a reason")

        assert controller.state is from_state
        assert [e for e in published if isinstance(e, FeedConnection)] == []


# --- driven by the script ---------------------------------------------------------

SYMBOL = "C-BTC-77600-040926"
BOOK_FRAME = {
    "type": "ob_l2",
    "sy": SYMBOL,
    "ts": 1_788_430_765_832_299,
    "lts": 1_788_430_765_000_000,
    "a": [["125", "12"]],
    "b": [["120", "10"]],
}


class ScriptClock:
    """The clock a script's silences consume, with the staleness timer riding on it.

    `Silence(20.0)` advances twenty seconds in poll-sized steps and fires the controller's
    timer at each one, which is what twenty seconds of wall clock would do — and it costs
    the suite nothing.
    """

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step
        self.controller: ConnectionController | None = None

    def read(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = min(end, self.now + self.step)
            if self.controller is not None:
                self.controller.poll(self.now)


#: `run_script`'s backoff, distinct from its poll cadence so `ScriptSleep` can tell the
#: two apart. Nothing asserts on the number; only that it is not `poll_seconds`.
SCRIPT_BACKOFF = 7.0


class ScriptSleep:
    """The controller's `sleep` under a `ScriptClock`. Two waits, two answers.

    **The poll timer parks.** The script's own clock is the timer in these tests, and
    the controller's watchdog must not also advance it or two timers race over one fake
    clock. Parking rather than returning is not a detail: a `sleep` that returns without
    awaiting anything never yields to the loop, so the timer spins and nothing else
    runs.

    **The backoff ends the run**, and that is #59's addition. The staleness watchdog now
    cuts the socket when it calls a connection silent, so a script with a long silence
    reaches the dial loop's backoff — which the old parking timer would have sat in
    forever, turning a changed rule into a hung suite rather than a failed assertion.
    The wait is recorded and the controller stopped, so exactly one redial is
    observable and the fake does not walk its whole script a second time.
    """

    def __init__(self, poll_seconds: float, backoff: float) -> None:
        self.poll_seconds = poll_seconds
        self.backoff = backoff
        self.backoffs: list[float] = []
        self.controller: ConnectionController | None = None

    async def __call__(self, seconds: float) -> None:
        if seconds != self.backoff:
            await asyncio.Event().wait()
        self.backoffs.append(seconds)
        assert self.controller is not None
        self.controller.stop()


def run_script(script, **kwargs):
    """Run one script through a controller and hand back what reached the bus."""
    clock = ScriptClock()
    published: list[tuple[float, Any]] = []
    adapter = ScriptedAdapter(script=script, sleep=clock.sleep)
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    adapter.subscribe([instrument])
    kwargs.setdefault("retry_delay", SCRIPT_BACKOFF)
    sleep = ScriptSleep(kwargs.get("poll_seconds", 1.0), kwargs["retry_delay"])
    controller = ConnectionController(
        adapter,
        lambda event: published.append((clock.now, event)),
        clock=clock.read,
        sleep=sleep,
        degraded_after=15.0,
        reconnect_after=45.0,
        **kwargs,
    )
    sleep.controller = controller
    clock.controller = controller
    asyncio.run(controller.run())
    return controller, published


def moves(published) -> list[tuple[float, str | None, str, str]]:
    return [
        (
            at,
            None if e.from_state is None else e.from_state.value,
            e.to_state.value,
            e.reason,
        )
        for at, e in published
        if isinstance(e, FeedConnection)
    ]


def test_frames_then_silence_degrades_at_the_interval_and_nothing_else() -> None:
    controller, published = run_script([Frames("ob_l2", [BOOK_FRAME]), Silence(20.0)])

    assert moves(published) == [
        (0.0, None, "connecting", "start"),
        (0.0, "connecting", "connected", "open"),
        # 15 s after the last frame, to the second the timer resolves — and nothing
        # between. The trailing stop is the script running out, not a sixth state.
        (15.0, "connected", "degraded", "stale"),
        (20.0, "degraded", "stopped", "stopped"),
    ]
    assert controller.state is ConnectionState.STOPPED
    assert 20.0 <= (controller.last_message_age(20.0) or 0.0) <= 20.0


def test_a_frame_after_the_silence_brings_it_back() -> None:
    _controller, published = run_script(
        [Frames("ob_l2", [BOOK_FRAME]), Silence(20.0), Frames("ob_l2", [BOOK_FRAME])]
    )

    assert moves(published) == [
        (0.0, None, "connecting", "start"),
        (0.0, "connecting", "connected", "open"),
        (15.0, "connected", "degraded", "stale"),
        (20.0, "degraded", "connected", "message"),
        (20.0, "connected", "stopped", "stopped"),
    ]


def test_frames_then_close_reconnects_with_reason_closed() -> None:
    controller, published = run_script([Frames("ob_l2", [BOOK_FRAME]), Close("1006")])

    assert moves(published) == [
        (0.0, None, "connecting", "start"),
        (0.0, "connecting", "connected", "open"),
        (0.0, "connected", "reconnecting", "closed"),
        # The fake's `Close` drops and comes back, so the way home is visible too.
        (0.0, "reconnecting", "connecting", "backoff"),
        (0.0, "connecting", "connected", "open"),
        (0.0, "connected", "stopped", "stopped"),
    ]
    assert controller.transitions == 6


def test_a_reconnect_that_replays_nothing_is_visible() -> None:
    """The silent failure the resubscribe-everything rule exists to prevent.

    A controller cannot see inside a replay, so this asserts the fake's own snapshot: an
    adapter that had subscribed nothing replays an empty registry, and one that had
    subscribed replays it in full.
    """
    controller, _published = run_script([Frames("ob_l2", [BOOK_FRAME]), Close()])
    adapter = controller.adapter
    assert adapter.replays == [
        {"ticker": {SYMBOL}, "ob_l2": {SYMBOL}},
        {"ticker": {SYMBOL}, "ob_l2": {SYMBOL}},
    ]


def test_heartbeats_arrive_on_cadence_and_the_age_resets_on_a_frame() -> None:
    _controller, published = run_script(
        [
            Frames("ob_l2", [BOOK_FRAME]),
            Silence(30.0),
            Frames("ob_l2", [BOOK_FRAME]),
            Silence(20.0),
        ],
        heartbeat_every=10.0,
    )
    beats = [(at, e) for at, e in published if isinstance(e, Heartbeat)]

    # On cadence: the first poll answers immediately, then every ten seconds.
    assert [at for at, _ in beats] == [1.0, 11.0, 21.0, 31.0, 41.0]
    # Growing through the silence, and reset by the frame at t=30.
    assert [e.last_message_age_seconds for _, e in beats] == [
        1.0,
        11.0,
        21.0,
        1.0,
        11.0,
    ]
    # Every heartbeat carries the state it was beaten in, whatever that state is.
    assert [e.state.value for _, e in beats] == [
        "connected",
        "connected",
        "degraded",
        "connected",
        "connected",
    ]
    assert all(e.adapter == "SCRIPT" and e.instrument is None for _, e in beats)


# --- the timer, and the log line ---------------------------------------------------


def test_run_drives_the_staleness_timer_on_its_own_clock() -> None:
    """**The one test that uses the real timer.**

    Every script test above parks the controller's watchdog and polls from the script's
    clock, which proves the machine but not the wiring. This proves the wiring: real
    `asyncio.sleep`, real `time.monotonic`, and intervals small enough that the whole
    thing costs a fifth of a second.
    """
    published: list = []
    adapter = ScriptedAdapter(script=[Frames("ob_l2", [BOOK_FRAME]), Silence(0.25)])
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    adapter.subscribe([instrument])
    controller = ConnectionController(
        adapter,
        published.append,
        degraded_after=0.05,
        reconnect_after=10.0,
        heartbeat_every=10.0,
        poll_seconds=0.01,
    )

    asyncio.run(controller.run())

    assert [
        (e.from_state, e.to_state, e.reason)
        for e in published
        if isinstance(e, FeedConnection)
    ] == [
        (None, ConnectionState.CONNECTING, "start"),
        (ConnectionState.CONNECTING, ConnectionState.CONNECTED, "open"),
        (ConnectionState.CONNECTED, ConnectionState.DEGRADED, "stale"),
        (ConnectionState.DEGRADED, ConnectionState.STOPPED, "stopped"),
    ]


def test_every_transition_logs_one_line(caplog: pytest.LogCaptureFixture) -> None:
    """One line per transition, through the existing logger. #42 replaces the format;
    this pins that a transition is never silent."""
    caplog.set_level(logging.INFO, logger="deltapayoff.controller")
    controller = ConnectionController(ScriptedAdapter(), lambda _event: None)

    controller.start()
    controller.connection_opened()
    controller.connection_closed("1006 abnormal closure")

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "deltapayoff.controller"
    ]
    assert lines == [
        "feed connection SCRIPT: start -> connecting (start)",
        "feed connection SCRIPT: connecting -> connected (open)",
        "feed connection SCRIPT: connected -> reconnecting (closed) 1006 abnormal "
        "closure",
    ]


def test_the_venues_own_words_stay_out_of_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`reason` is a short stable name #40 badges on and #42 greps. The venue's sentence
    belongs in the log line, where a person reads it, and nowhere else."""
    caplog.set_level(logging.INFO, logger="deltapayoff.controller")
    published: list = []
    controller = ConnectionController(ScriptedAdapter(), published.append)
    controller.start()
    controller.connection_opened()

    controller.connection_closed("sent 1011 (internal error) keepalive ping timeout")

    (event,) = [
        e
        for e in published
        if isinstance(e, FeedConnection)
        and e.to_state is ConnectionState.RECONNECTING
    ]
    assert event.reason == "closed"
    assert "keepalive ping timeout" not in event.reason
    assert "keepalive ping timeout" in caplog.records[-1].getMessage()


def test_a_stop_before_a_start_still_stops_the_adapter() -> None:
    """There is no state to leave, but there is an adapter that must not open a socket.

    `ScriptedAdapter.stream` honours a `stop()` made before it runs by opening no
    connection at all, which is `DeltaFeed.run`'s own behaviour — so a controller that
    transitioned but forgot to pass the stop on would leave a feed running behind a
    machine that said `stopped`.
    """
    published: list = []
    adapter = ScriptedAdapter(script=[Frames("ob_l2", [BOOK_FRAME])])
    controller = ConnectionController(adapter, published.append)

    controller.stop()

    assert controller.state is None
    assert published == []
    asyncio.run(adapter.stream(published.append))
    assert adapter.connections == 0
    assert published == []


def test_a_resume_does_not_inherit_the_age_from_before_the_pause() -> None:
    """**Time spent paused is our silence, not the venue's.**

    Staleness is measured from the last message, and a connection resumed after ten
    quiet minutes would otherwise arrive already stale — `reconnecting` on the first
    timer tick, against a socket nobody had given a chance to speak. A reconnect is the
    opposite case and keeps its age, because that gap really was the venue's.
    """
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = 10.0
    controller.stop()

    clock.now = 600.0
    controller.start()
    published.clear()
    controller.poll()

    assert controller.state is ConnectionState.CONNECTING
    assert controller.last_message_age() is None
    assert [e for e in published if isinstance(e, FeedConnection)] == []


def test_a_reconnect_keeps_the_age_because_that_gap_was_the_venues() -> None:
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = 30.0
    controller.connection_closed("dropped")
    controller.connection_opened()

    assert controller.last_message_age() == 30.0


def test_a_reopened_socket_gets_the_grace_a_fresh_start_gets() -> None:
    """The rule "a reconnect keeps its age" is right while reconnecting and wrong the
    instant the replay completes.

    Every outage longer than `reconnect_after` ends with a socket that is open and
    resubscribed but has not yet delivered a frame. Measuring that socket's staleness
    from a message that predates the drop demotes it on the very next poll, and the
    machine flaps `connected -> reconnecting -> connecting -> connected` for as long as
    the market stays quiet — three spurious `feed.connection` events per second, on the
    badge #40 draws, during the exact incident an operator is watching.
    """
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1_000.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()

    clock.now = 45.0
    controller.poll()  # degraded -> reconnecting, the drop the bound exists to catch
    clock.now = 46.0
    controller.connection_opened()  # the socket is back and resubscribed
    published.clear()

    for tick in (47.0, 48.0, 49.0, 55.0, 60.0):
        clock.now = tick
        controller.poll()

    assert controller.state is ConnectionState.CONNECTED
    assert [(e.from_state, e.to_state, e.reason) for e in published] == []


def test_a_reopened_socket_that_delivers_nothing_still_goes_stale_on_its_own_clock() -> (
    None
):
    """The grace is a rebase, not an exemption. A replayed socket that stays silent
    reaches `degraded` one `degraded_after` after the open and `reconnecting` one
    `reconnect_after` after it — measured from the open, because that is when this
    socket's silence began."""
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1_000.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = 45.0
    controller.poll()
    clock.now = 46.0
    controller.connection_opened()
    published.clear()

    clock.now = 61.0
    controller.poll()
    assert controller.state is ConnectionState.DEGRADED

    clock.now = 91.0
    controller.poll()
    assert controller.state is ConnectionState.RECONNECTING
    assert [
        (e.to_state, e.reason) for e in published if isinstance(e, FeedConnection)
    ] == [
        (ConnectionState.DEGRADED, "stale"),
        (ConnectionState.RECONNECTING, "silent"),
    ]


def test_the_heartbeat_still_reports_the_venues_own_silence_across_a_reconnect() -> None:
    """The grace moves what staleness is measured from. It does not move what the
    heartbeat reports: a badge that said "0 s since the last message" one second after a
    socket reopened would be the plausible-and-wrong answer again, one layer up."""
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    clock.now = 45.0
    controller.poll()
    clock.now = 46.0
    controller.connection_opened()
    published.clear()

    clock.now = 47.0
    controller.poll()

    (beat,) = [e for e in published if isinstance(e, Heartbeat)]
    assert beat.state is ConnectionState.CONNECTED
    assert beat.last_message_age_seconds == 47.0


def _reasons(published: list) -> list[str]:
    return [e.reason for e in published if isinstance(e, FeedConnection)]


def test_a_raising_consumer_does_not_kill_the_staleness_watchdog() -> None:
    """This module's own thesis, reintroduced one layer up.

    `poll()` calls `_publish`, which is the bus and, from #39, a consumer's code. An
    exception out of it takes the timer task down; `gather(..., return_exceptions=True)`
    retrieves it, so asyncio never warns either. The watchdog dies with no log, no event
    and no symptom, `run()` returns cleanly, and the connection sits in `connected`
    through a silence five times past the reconnect bound — a healthy badge over a dead
    feed, which is the failure this file exists to refuse.
    """
    published: list = []

    def publish(event: Any) -> None:
        if isinstance(event, Heartbeat):
            raise RuntimeError("a consumer's own bug")
        published.append(event)

    adapter = ScriptedAdapter(script=[Silence(0.5)])
    controller = ConnectionController(
        adapter,
        publish,
        degraded_after=0.05,
        reconnect_after=0.2,
        heartbeat_every=0.05,
        poll_seconds=0.02,
        # **A budget of nought, since #59, and it is what keeps this test bounded.** The
        # watchdog now cuts the socket when it calls a connection silent, and the fake
        # walks its whole script again on every redial — so with a budget the run would
        # loop through the backoff ladder rather than ending. Nought means the first
        # drop is the one that stops, which is the same five moves as before and no
        # wall clock spent proving a rule this test is not about.
        reconnect_budget=0,
    )

    asyncio.run(controller.run())

    assert _reasons(published) == ["start", "open", "stale", "silent", "stopped"]


def test_a_watchdog_that_keeps_failing_says_so_out_loud() -> None:
    """Logging forever is what a broken consumer would get away with. Three consecutive
    failures is not a blip, and an `alert` is the event a person is meant to see —
    `events.md` says the controller emits one, and this is the first place it does."""
    published: list = []

    def publish(event: Any) -> None:
        if isinstance(event, Heartbeat):
            raise RuntimeError("a consumer's own bug")
        published.append(event)

    adapter = ScriptedAdapter(script=[Silence(0.3)])
    controller = ConnectionController(
        adapter,
        publish,
        degraded_after=10.0,
        reconnect_after=10.0,
        heartbeat_every=0.01,
        poll_seconds=0.02,
    )

    asyncio.run(controller.run())

    alerts = [e for e in published if isinstance(e, Alert)]
    assert [(a.code, a.severity, a.adapter) for a in alerts] == [
        ("poll_failing", "error", "SCRIPT")
    ]


def test_a_connection_gone_silent_is_an_alert_and_not_only_a_badge() -> None:
    """`events.md` lists "a connection has gone stale" among the things an `alert` is
    for. `degraded` is not it — fifteen quiet seconds is a badge, and an alert per quiet
    minute is the flood an alert exists to stand out from. Silence past the reconnect
    bound is."""
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1_000.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()

    clock.now = 20.0
    controller.poll()
    assert [e for e in published if isinstance(e, Alert)] == []

    clock.now = 45.0
    controller.poll()

    (alert,) = [e for e in published if isinstance(e, Alert)]
    assert (alert.code, alert.severity, alert.adapter) == (
        "connection_silent",
        "error",
        "SCRIPT",
    )
    assert "45.0s" in alert.detail


def test_a_controller_can_be_taken_off_the_adapter_it_wrapped() -> None:
    """Registering is a construction side effect and there was no way to undo it, so a
    controller that was replaced or discarded stayed strongly referenced and went on
    receiving signals — one live socket driving two machines, the second of which is
    dead and answers a signal by raising `IllegalTransition` inside the socket reader.
    #39 holds controllers across a lifespan and is the first to hit it."""
    adapter = ScriptedAdapter()
    published: list = []
    controller = ConnectionController(adapter, published.append)
    controller.start()

    controller.detach()

    assert adapter._listeners == []
    published.clear()
    adapter._signal(ConnectionSignal.CLOSED, "a socket this controller no longer owns")
    assert published == []
    assert controller.state is ConnectionState.CONNECTING


def test_detaching_twice_is_not_an_error() -> None:
    """A supervisor that tidies up on both the normal and the failed path must be able
    to call it twice."""
    adapter = ScriptedAdapter()
    controller = ConnectionController(adapter, [].append)

    controller.detach()
    controller.detach()

    assert adapter._listeners == []


def test_run_leaves_nothing_registered_on_the_adapter() -> None:
    """The leak closes itself on the ordinary path: a controller whose stream has
    returned is finished with that adapter, and holds on to nothing."""
    adapter = ScriptedAdapter(script=[Frames("ob_l2", [])])
    controller = ConnectionController(
        adapter, [].append, heartbeat_every=1_000.0, poll_seconds=0.01
    )

    asyncio.run(controller.run())

    assert adapter._listeners == []


def test_a_restarted_controller_is_registered_again() -> None:
    """Detaching on the way out would be a trap if a resumed controller stayed deaf, so
    `start()` puts it back. #41's resume command is the caller that needs this."""
    adapter = ScriptedAdapter()
    published: list = []
    controller = ConnectionController(adapter, published.append)
    controller.detach()

    controller.start()
    published.clear()
    adapter._signal(ConnectionSignal.OPENED, "back")

    assert controller.state is ConnectionState.CONNECTED
    assert len(adapter._listeners) == 1


def test_an_open_while_already_connected_rebases_rather_than_vanishing() -> None:
    """No move is allowed out of `connected`, so the state does not change — but the
    signal is not nothing. A socket that says it reopened is a socket that stopped and
    started, and going on measuring its silence against a message the socket before it
    delivered is the same mistake the reconnect grace exists to undo."""
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1_000.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()

    clock.now = 14.0
    controller.connection_opened("a second open nobody announced a close for")
    published.clear()

    clock.now = 20.0
    controller.poll()

    assert controller.state is ConnectionState.CONNECTED
    assert _reasons(published) == []


# --- the reconnect that moved here from the feed (#39) ----------------------------
#
# Backoff, the lifetime budget and the decision to redial were inside `DeltaFeed.run`'s
# `while` loop until #39. They are the controller's now, so their tests are here — and
# the two that need a socket drive the real `DeltaFeed` through a scripted connection,
# exactly as `test_feed.py` used to, because a reconnect asserted only against a double
# that reconnects itself proves nothing about the code that dials.


async def _drive(controller, seconds: float = 0.3) -> None:
    """Run a controller against a live-ish adapter, then take it down."""
    task = asyncio.create_task(controller.run())
    await asyncio.sleep(seconds)
    controller.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _delta_over(connect):
    """A real `DeltaAdapter` over a real `DeltaFeed` over a scripted connection."""
    adapter = DeltaAdapter(
        feed_factory=lambda sink, **kw: DeltaFeed(sink, connect=connect, **kw)
    )
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    adapter.subscribe([instrument])
    return adapter


def test_frames_close_frames_reconnects_and_replays_everything() -> None:
    """The ticket's own script, and the whole shape of a reconnect in three assertions.

    The connection goes down, comes back through `connecting` rather than jumping
    straight to `connected`, and **every subscription is replayed** — which the fake
    records, because a controller cannot see inside a replay and an empty snapshot is
    the healthy-connection-zero-messages failure made visible.
    """
    controller, published = run_script(
        [Frames("ob_l2", [BOOK_FRAME]), Close("1006"), Frames("ob_l2", [BOOK_FRAME])]
    )

    assert moves(published) == [
        (0.0, None, "connecting", "start"),
        (0.0, "connecting", "connected", "open"),
        (0.0, "connected", "reconnecting", "closed"),
        (0.0, "reconnecting", "connecting", "backoff"),
        (0.0, "connecting", "connected", "open"),
        (0.0, "connected", "stopped", "stopped"),
    ]
    assert controller.adapter.replays == [
        {"ticker": {SYMBOL}, "ob_l2": {SYMBOL}},
        {"ticker": {SYMBOL}, "ob_l2": {SYMBOL}},
    ], "a reconnect that replayed a subset is the failure with no error"
    assert controller.reconnects == 1


def test_a_spent_budget_stops_the_connection_out_loud(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**The loudest thing the engine says.** A budget of two allows two reconnects; the
    third drop ends the connection rather than leaving it retrying forever.

    Checked before spending rather than after, so that "two" means two. And it is not a
    quiet exit: one `alert` on the bus and one error-level log record, because a feed
    that has given up produces no other symptom — the screens simply stop moving.
    """
    with caplog.at_level(logging.ERROR, logger="deltapayoff.controller"):
        controller, published = run_script(
            [Close("one"), Close("two"), Close("three")], reconnect_budget=2
        )

    assert moves(published)[-1] == (0.0, "reconnecting", "stopped", "stopped")
    assert controller.state is ConnectionState.STOPPED
    assert controller.budget_remaining == 0
    assert controller.reconnects == 3

    alerts = [e for _at, e in published if isinstance(e, Alert)]
    assert [a.code for a in alerts] == ["reconnect_budget_spent"]
    assert alerts[0].severity == "error"

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "budget of 2 is spent" in errors[0].getMessage()


def test_two_drops_inside_a_budget_of_two_do_not_stop_it() -> None:
    """The other side of the boundary, so the off-by-one cannot pass unnoticed."""
    controller, published = run_script([Close("one"), Close("two")], reconnect_budget=2)

    assert controller.reconnects == 2
    assert controller.budget_remaining == 0
    # The script running out always ends in `stopped`, so the state at the end says
    # nothing. The alert is what a spent budget produces and nothing else does.
    assert [e for _at, e in published if isinstance(e, Alert)] == []
    assert moves(published)[-3:-1] == [
        (0.0, "reconnecting", "connecting", "backoff"),
        (0.0, "connecting", "connected", "open"),
    ], "the second drop still came back"


def test_a_message_restores_the_whole_budget() -> None:
    """OpenAlgo's recorded bug, inverted into a guard.

    A cumulative counter that never resets kills a feed reconnecting once a day after a
    month, with no failure anywhere to point at. Delivering data proves the endpoint
    works, so it restores the budget **in full** — and with a budget of two, three drops
    with a frame between them stop nothing.
    """
    controller, published = run_script(
        [Close("one"), Close("two"), Frames("ob_l2", [BOOK_FRAME]), Close("three")],
        reconnect_budget=2,
    )

    assert controller.reconnects == 3
    assert [e for _at, e in published if isinstance(e, Alert)] == []
    assert controller.budget_remaining == 1, "one spent since the frame, of two"


def test_a_dropped_connection_is_redialled_and_resubscribed() -> None:
    """**The move, end to end, over the real socket owner.**

    `DeltaFeed.run` is one connection since #39, so the second socket exists only
    because the controller dialled it — and it must be sent the complete registry, not a
    subset and not nothing. This is the test `test_feed.py` used to own; the half that
    stayed there is that every open replays, and the half that is here is that there is
    a second open at all.
    """
    first = FakeSocket([ticker_frame(SYMBOL, 579, 584)], close_after=0)
    second = FakeSocket([])
    adapter = _delta_over(connector([first, second]))
    published: list = []
    controller = ConnectionController(
        adapter, published.append, retry_delay=0.01, heartbeat_every=1_000.0
    )

    asyncio.run(_drive(controller))

    resent = [
        entry
        for message in second.sent
        if message.get("type") == "subscribe"
        for entry in message["payload"]["channels"]
    ]
    assert {c["name"] for c in resent} == {"ticker", "ob_l2"}
    for channel in resent:
        assert channel["symbols"] == [SYMBOL]
    assert adapter.feed.connections == 2
    # The trailing `stopped` is this test taking the controller down, not a sixth state.
    assert _reasons(published)[-4:-1] == ["closed", "backoff", "open"]


def test_the_attempt_is_announced_when_it_begins_not_when_it_succeeds() -> None:
    """#38 could only reach `connecting` at the instant a socket opened, because it did
    not own the dial. It does now, so `reconnecting -> connecting` is emitted when the
    redial starts — the difference between a badge showing `reconnecting` for the whole
    of an outage and one showing a try in progress.

    The second dial here never opens, so the only way `connecting` can appear is if the
    controller announced the attempt itself.
    """
    first = FakeSocket([ticker_frame(SYMBOL, 579, 584)], close_after=0)

    def connect(url):
        if first.closed:
            raise ConnectionRefusedError("the endpoint is down")
        return first

    adapter = _delta_over(connect)
    published: list = []
    controller = ConnectionController(
        adapter, published.append, retry_delay=0.01, heartbeat_every=1_000.0
    )

    asyncio.run(_drive(controller, seconds=0.15))

    announced = [
        event
        for event in published
        if isinstance(event, FeedConnection) and event.reason == "backoff"
    ]
    assert announced, "the redial was never announced"
    assert announced[0].to_state is ConnectionState.CONNECTING
    assert adapter.feed.connections == 1, "the second dial was refused, as scripted"


def test_a_silence_the_venue_never_closed_is_cut_and_redialled() -> None:
    """**#59's adoption from Nautilus Trader: the watchdog pulls the lever itself.**

    Silence past `reconnect_after` used to mark the state, raise the alert and stop
    there. Nothing came down and nothing was redialled, because a socket the venue never
    closed is still open and dialling a second one over it is the failure
    `reconnect.md` §4 refused. So the connection sat in `reconnecting` — badge red,
    alert raised, no data — until the venue finally ended the socket or a person sent
    `POST /feed/DELTA/reconnect`. At 02:00 there is no person.

    Nautilus's read task breaks its own connection on the idle timeout and its
    controller redials
    (`crates/network/src/websocket/client.rs`, `idle_timeout_exceeded`), and Delta's own
    documentation says the client "should exit the existing connection and try to
    reconnect". #41 already built the lever — `_cut` cancels the stream, which unwinds
    the socket's `async with` — and nothing pulled it.

    So the watchdog now does exactly what an operator's `reconnect` does: cut, report
    the drop, back off, dial. The invariant `reconnect.md` §4 protects is untouched,
    because the redial still turns on the adapter's own signal — the socket really is
    gone by the time the loop reads it, and **we** are the ones who ended it, which is
    what `adapter.closes == 0` below says.

    **Supersedes two tests.** `test_silence_past_the_longer_bound_reconnects` asserted
    the four moves below and then that the run simply ended, and
    `test_a_silence_the_venue_never_closed_is_not_redialled` asserted that no backoff
    ran at all — the rule this ticket reverses. Both facts are asserted here, the second
    inverted.
    """
    clock = ScriptClock()
    published: list[tuple[float, Any]] = []
    adapter = ScriptedAdapter(
        script=[Frames("ob_l2", [BOOK_FRAME]), Silence(60.0)], sleep=clock.sleep
    )
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    adapter.subscribe([instrument])
    BACKOFF = 7.0  # distinctive, so the backoff is not the poll timer
    backoffs: list[float] = []
    holder: list[ConnectionController] = []

    async def sleep(seconds: float) -> None:
        if seconds != BACKOFF:
            # The poll timer. The script's own clock drives polls here, so this one must
            # park rather than return, or it spins without yielding.
            await asyncio.Event().wait()
        # The backoff, recorded and then ended: the script would otherwise restart from
        # the top and this test would measure the fake rather than the controller.
        backoffs.append(seconds)
        holder[0].stop()

    controller = ConnectionController(
        adapter,
        lambda event: published.append((clock.now, event)),
        clock=clock.read,
        sleep=sleep,
        degraded_after=15.0,
        reconnect_after=45.0,
        retry_delay=BACKOFF,
        heartbeat_every=1_000.0,
    )
    holder.append(controller)
    clock.controller = controller

    asyncio.run(controller.run())

    assert backoffs == [BACKOFF], "the silence produced no redial"
    assert adapter.closes == 0, "the venue never closed it — the watchdog did"
    assert controller.reconnects == 1, "a cut we made is still a drop and still counts"
    assert controller.budget_remaining == 9
    assert moves(published)[:4] == [
        (0.0, None, "connecting", "start"),
        (0.0, "connecting", "connected", "open"),
        (15.0, "connected", "degraded", "stale"),
        (45.0, "degraded", "reconnecting", "silent"),
    ]


def test_a_socket_that_dies_after_going_silent_still_spends_the_budget() -> None:
    """**A drop is a drop, whatever state the machine was already in.**

    The staleness watchdog can reach `reconnecting` on its own, over a socket the venue
    has not closed yet. When that socket then really does die, the close arrives at a
    machine already in `reconnecting` — and a budget spent only on the *transition* would
    not be spent at all. That is an unbounded reconnect loop in exactly the case the
    budget exists for: a connection that goes quiet and dies, over and over, spending a
    venue's connection allowance with a full budget on the books the whole time.

    **Two drops here since #59, not one, and the arithmetic is the point.** The silence
    itself now cuts the socket and reports that drop, so the poll below spends one; the
    venue's own close arriving afterwards at a machine already in `reconnecting` spends
    the second. In production a cut stream reports no close and the second never comes —
    this drives it by hand because the rule is what stops the loop being unbounded, and
    a rule only one code path can reach is a rule one refactor from being lost.
    """
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        clock=clock.read,
        degraded_after=15.0,
        reconnect_after=45.0,
        heartbeat_every=1_000.0,
        reconnect_budget=2,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()

    clock.now = 60.0
    controller.poll()
    assert controller.state is ConnectionState.RECONNECTING, "the silence, not the close"
    assert controller.reconnects == 1, "the watchdog's own cut is a drop"

    controller.connection_closed("1006")

    assert controller.reconnects == 2
    assert controller.budget_remaining == 0, "the second drop was free"


def test_an_announced_redial_is_not_immediately_called_silent() -> None:
    """**The flood #39 introduced, and the reason `_opened_at` exists pointed one step
    earlier.**

    `_attempt_forever` announces `reconnecting -> connecting` when a dial *begins*, which
    is new in #39 — under #38 `connecting` was only ever entered by `connection_opened`,
    and that sets `_opened_at`. Nothing rebased the staleness clock on the announcement,
    and `_check_staleness` includes `connecting` and measures from the last message or
    the last open, both of which are from before the drop.

    So in any outage longer than `reconnect_after` every single attempt was announced and
    then, on the very next poll, demoted with reason `silent` — and an
    `ALERT_CONNECTION_SILENT` was raised about a socket that does not exist. Once per
    attempt, for the whole outage: roughly five or six extra alerts and a dozen extra
    transitions in a ten-minute one, inflating `transitions` on `/health` and flipping
    #40's badge back within a second of each try.

    It is the same family as the flap #38 fixed, and it contradicts `controller.md`'s own
    rule that alerts must not flood during the incident an operator is watching.

    Scripted as one socket that delivers a frame and closes, and an endpoint that refuses
    every dial after it — so every `connecting` here is an announcement and none of them
    is a socket.
    """
    first = FakeSocket([ticker_frame(SYMBOL, 579, 584)], close_after=0)

    class HangingDial:
        """A dial that takes time and then fails, which is what an outage looks like.

        `websockets.connect` carries a 20 s `open_timeout`, so an attempt against a dead
        endpoint occupies `connecting` for many polls. A fake that refuses *instantly*
        returns to `reconnecting` before the next poll can see it and hides this bug
        completely — which is exactly what the first draft of this test did.

        The hang is **shorter than `reconnect_after` and longer than `poll_seconds`**,
        which is the real ratio: the production dial gives up at 20 s and the bound is
        45 s, so a dial in flight can never legitimately outlive the bound. Scripting a
        hang longer than the bound would test something else — a dial the machine is
        entitled to call silent.
        """

        async def __aenter__(self):
            await asyncio.sleep(0.025)
            raise ConnectionRefusedError("the endpoint is down")

        async def __aexit__(self, *_):
            return False

    def connect(url):
        return HangingDial() if first.closed else first

    adapter = _delta_over(connect)
    published: list = []
    controller = ConnectionController(
        adapter,
        published.append,
        retry_delay=0.01,
        degraded_after=0.02,
        reconnect_after=0.05,
        poll_seconds=0.005,
        heartbeat_every=1_000.0,
    )

    asyncio.run(_drive(controller, seconds=0.5))

    announced = [
        event
        for event in published
        if isinstance(event, FeedConnection)
        and event.to_state is ConnectionState.CONNECTING
        and event.reason == "backoff"
    ]
    assert announced, "the outage produced no announced redial, so this proves nothing"

    demoted = [
        event
        for event in published
        if isinstance(event, FeedConnection)
        and event.from_state is ConnectionState.CONNECTING
        and event.reason == "silent"
    ]
    assert demoted == [], (
        f"{len(demoted)} announced attempts were called silent on the next poll, for a "
        "socket that was never open"
    )

    alerts = [
        event
        for event in published
        if isinstance(event, Alert) and event.code == "connection_silent"
    ]
    assert alerts == [], (
        f"{len(alerts)} silence alerts fired during one outage — an alert per redial is "
        "the flood an alert exists to stand out from"
    )


def test_a_message_arriving_after_a_stop_does_not_restore_the_budget() -> None:
    """**A stopped connection is stopped, and nothing a socket says changes that.**

    `message_arrived` restored the budget, the backoff and the age before it looked at
    the state at all, so a frame off a socket winding down — or, from #41, a frame that
    arrives between a stop and the reader noticing — silently handed a `stopped`
    connection its whole lifetime budget back. The transitions were guarded and the
    counters were not, which is the more dangerous half: `/health` would report a feed
    that had given up as having a full budget in hand, and #41's resume would start from
    a number nobody spent.
    """
    clock = FakeClock()
    published: list = []
    controller = ConnectionController(
        ScriptedAdapter(),
        published.append,
        reconnect_budget=1,
        clock=clock.read,
        heartbeat_every=1_000.0,
    )
    controller.start()
    controller.connection_opened()
    controller.message_arrived()
    controller.connection_closed("1006")
    controller.connection_closed("1006")

    assert controller.state is ConnectionState.STOPPED
    assert controller.budget_remaining == 0

    clock.now = 5.0
    controller.message_arrived()

    assert controller.state is ConnectionState.STOPPED
    assert controller.budget_remaining == 0, (
        "a stopped connection was handed its lifetime budget back by a stray frame"
    )


# --- #42: structured logging ------------------------------------------------------


def test_frames_silence_frames_logs_stale_warning_then_transition_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The acceptance scenario named in #42: frames, a 20 s silence, frames.

    Entering `degraded` is `feed.stale` at warning with `conn_state: degraded`; coming
    back is an ordinary `feed.transition` at info. This is the record that would have
    made the three-day hole in the store visible the day it started.
    """
    caplog.set_level(logging.DEBUG, logger="deltapayoff.controller")

    run_script(
        [Frames("ob_l2", [BOOK_FRAME]), Silence(20.0), Frames("ob_l2", [BOOK_FRAME])]
    )

    records = [r for r in caplog.records if r.name == "deltapayoff.controller"]
    stale = [r for r in records if r.event == "feed.stale"]
    assert len(stale) == 1, [r.getMessage() for r in records]
    assert stale[0].levelno == logging.WARNING
    assert stale[0].conn_state == "degraded"
    assert stale[0].venue == "SCRIPT"

    # The very next controller record is the recovery, back at info, as an ordinary
    # transition rather than a second special-cased event.
    after = records[records.index(stale[0]) + 1]
    assert after.event == "feed.transition"
    assert after.levelno == logging.INFO
    assert after.conn_state == "connected"


def test_a_reconnect_is_a_warning_not_an_info(caplog: pytest.LogCaptureFixture) -> None:
    """Every move touching `reconnecting` — the drop and the redial — logs at warning
    under `feed.reconnect`, distinct from the ordinary `feed.transition` info line."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.controller")

    run_script([Frames("ob_l2", [BOOK_FRAME]), Close("1006")])

    records = [r for r in caplog.records if r.name == "deltapayoff.controller"]
    by_event = [(r.event, r.levelno) for r in records]
    assert (log_events.FEED_RECONNECT, logging.WARNING) in by_event
    # The close and the redial both count; the initial connect and the final stop do not.
    assert by_event.count((log_events.FEED_RECONNECT, logging.WARNING)) == 2
    assert (log_events.FEED_TRANSITION, logging.INFO) in by_event


def test_a_spent_budget_is_logged_as_a_single_error_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#42's "error when stopped by budget", folded into the one `feed.transition`
    record `_spend_reconnect` already made loud — not a second log line beside it."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.controller")

    run_script([Close("one"), Close("two"), Close("three")], reconnect_budget=2)

    records = [r for r in caplog.records if r.name == "deltapayoff.controller"]
    errors = [r for r in records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert errors[0].event == log_events.FEED_TRANSITION
    assert errors[0].conn_state == "stopped"
    assert "budget of 2 is spent" in errors[0].getMessage()


def test_every_alert_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """`_alert` always writes a warning-level record, whatever the alert's own
    `severity` — #42's "every alert (warning)"."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.controller")

    run_script([Frames("ob_l2", [BOOK_FRAME]), Silence(60.0)])

    records = [r for r in caplog.records if r.name == "deltapayoff.controller"]
    # `getattr`, because not every record here is a structured one: `_cut` writes a
    # plain info line, and since #59 the staleness watchdog cuts the socket, so that
    # line is now on this path. A bare `r.event` raised `AttributeError` on it.
    alerts = [r for r in records if getattr(r, "event", None) == log_events.ALERT]
    assert len(alerts) == 1, [r.getMessage() for r in records]
    assert alerts[0].levelno == logging.WARNING
    assert alerts[0].code == "connection_silent"


def test_a_reconnect_only_logging_scenario_produces_a_bounded_number_of_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**Volume, not just presence.** A single drop-and-recover produces a handful of
    records, not a flood — the same shape `fanout`'s "every drop is counted" argues for
    on the bus, applied to what a reconnect costs the log."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.controller")

    run_script([Frames("ob_l2", [BOOK_FRAME]), Close("1006")])

    records = [r for r in caplog.records if r.name == "deltapayoff.controller"]
    # start, open, closed->reconnecting, backoff->connecting, open, stopped: six moves,
    # six records — one reconnect costs six lines, not sixty.
    assert len(records) == 6, [r.getMessage() for r in records]
