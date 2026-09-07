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

from deltapayoff.adapters import instrument_from_symbol
from deltapayoff.controller import ConnectionController, IllegalTransition
from deltapayoff.events import ConnectionState, FeedConnection, Heartbeat
from fakes.scripted_adapter import Close, Frames, ScriptedAdapter, Silence


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


async def never(_seconds: float) -> None:
    """A timer that never fires.

    The script's clock is the timer in these tests; the controller's own watchdog must
    not also advance it, or two timers would race over one fake clock.
    """
    await asyncio.Event().wait()


def run_script(script, **kwargs):
    """Run one script through a controller and hand back what reached the bus."""
    clock = ScriptClock()
    published: list[tuple[float, Any]] = []
    adapter = ScriptedAdapter(script=script, sleep=clock.sleep)
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    adapter.subscribe([instrument])
    controller = ConnectionController(
        adapter,
        lambda event: published.append((clock.now, event)),
        clock=clock.read,
        sleep=never,
        degraded_after=15.0,
        reconnect_after=45.0,
        **kwargs,
    )
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


def test_silence_past_the_longer_bound_reconnects() -> None:
    _controller, published = run_script([Frames("ob_l2", [BOOK_FRAME]), Silence(60.0)])

    assert moves(published) == [
        (0.0, None, "connecting", "start"),
        (0.0, "connecting", "connected", "open"),
        (15.0, "connected", "degraded", "stale"),
        (45.0, "degraded", "reconnecting", "silent"),
        (60.0, "reconnecting", "stopped", "stopped"),
    ]


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
