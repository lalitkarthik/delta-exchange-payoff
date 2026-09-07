"""One adapter's connection, as a state machine that says so out loud.

**The failure this exists to refuse.** A socket that is open and silent is
indistinguishable over TCP from a socket that is open and busy in a quiet market: the
operating system keeps a dead connection alive for minutes and nothing raises. Today the
feed reconnects inside its own loop, `/health` says `ok` either way, and a browser
looking at a ladder that stopped moving cannot tell which it is looking at. That is the
plausible-and-wrong failure, and it is the default.

So this module turns *nothing has arrived* into a **state**, and every change of state
into an **event**. Five states, thirteen allowed moves, one `feed.connection` per move
and one log line beside it. Nothing else may change the state, and a move that is not in
the table raises rather than happening quietly — the whole value of a state machine is
what it forbids.

---

## The four causes

The machine is **synchronous** and driven by one method per cause, which is what makes it
testable without a clock or an event loop:

* `message_arrived()` — an event came off the adapter.
* `connection_opened()` / `connection_closed(reason)` — the adapter's socket signal.
* `poll(now)` — the staleness timer fired; also the heartbeat's cadence.
* `stop(reason)` / `start()` — stopped or started by request.

`run()` wires the ones that need a clock to a real one and is the only async thing here.
A test drives `poll` itself, and a twenty-second silence costs it nothing.

## What is *not* here, and for how long

**Backoff, the lifetime reconnect budget and subscription replay stay inside
`feed.DeltaFeed`,** where they are correct and tested, until #39 lifts them. This ticket
is the machine and the staleness that drives it. The consequence is visible in one place:
the controller learns that a reconnect attempt was made only when it **succeeds**, so the
`reconnecting -> connecting` move is emitted at the instant the socket opens rather than
at the instant the attempt began. #39, owning backoff, can emit it when the attempt
starts, and this module's table does not change.

## The connection signal

`adapters.base.Adapter` grew two members for this ticket — `on_connection(listener)` and
`off_connection(listener)` — because the protocol carried no way for an adapter to say
its socket had come or gone, and then no way to stop listening. Two signals, `OPENED` and
`CLOSED`; synchronous and non-blocking for the same reason `Publish` is, since the socket
reader calls it between reads. `docs/design/lld/connection-signal.md` is the design.

The full table and every number are in `docs/design/lld/controller.md`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .adapters.base import Adapter, ConnectionSignal
from .events import Alert, ConnectionState, Event, FeedConnection, Heartbeat

logger = logging.getLogger(__name__)

#: `Event.source` for everything this module emits. The component, not the venue: the
#: adapter's name travels in the payload's `adapter` field, so a consumer can tell a
#: transition the controller decided from a quote the venue sent.
SOURCE = "controller"

State = ConnectionState

# --- the table ---------------------------------------------------------------------

#: `hld.md` section 3, with its two "any" rows expanded.
#:
#: **"any -> reconnecting" excludes `stopped`**, because a stopped connection is not
#: trying: the table's own resume row takes it to `connecting`, and a `stopped ->
#: reconnecting` move would contradict it. **"any -> stopped" likewise excludes
#: `stopped`.** Neither expansion includes a state moving to itself: a `feed.connection`
#: whose `from_state` equals its `to_state` describes no change, and #40 would put it on
#: the wire as a badge that did not move.
ALLOWED: frozenset[tuple[ConnectionState | None, ConnectionState]] = frozenset(
    {
        (None, State.CONNECTING),  # adapter start
        (State.STOPPED, State.CONNECTING),  # a resume command (#41)
        (State.CONNECTING, State.CONNECTED),  # socket open, subscriptions replayed
        (State.CONNECTED, State.DEGRADED),  # no message for the staleness interval
        (State.DEGRADED, State.CONNECTED),  # the next message arrives
        (State.DEGRADED, State.RECONNECTING),  # silence beyond the longer bound
        (State.CONNECTING, State.RECONNECTING),  # "any" -> reconnecting
        (State.CONNECTED, State.RECONNECTING),
        (State.RECONNECTING, State.CONNECTING),  # backoff elapsed, an attempt is made
        (State.RECONNECTING, State.STOPPED),  # budget exhausted (#39)
        (State.CONNECTING, State.STOPPED),  # "any" -> stopped, a pause command
        (State.CONNECTED, State.STOPPED),
        (State.DEGRADED, State.STOPPED),
    }
)

#: Short, stable names, because #40 badges on them and #42 greps them. A reason is not a
#: sentence: the sentence goes in the log line, where a human reads it.
REASON_START = "start"
REASON_RESUME = "resume"
REASON_OPEN = "open"
REASON_MESSAGE = "message"
REASON_STALE = "stale"
REASON_SILENT = "silent"
REASON_CLOSED = "closed"
REASON_BACKOFF = "backoff"
REASON_STOPPED = "stopped"


class IllegalTransition(RuntimeError):
    """A move that is not in the table. Raised, never logged and continued.

    A machine that quietly ignored a forbidden move would be a machine with more states
    than it admits to, and the one guarantee this class offers — a connection is in
    exactly one of five states, and got there by an allowed route — would be untestable.
    """

    def __init__(
        self, from_state: ConnectionState | None, to_state: ConnectionState
    ) -> None:
        name = "nothing" if from_state is None else from_state.value
        super().__init__(
            f"{name} -> {to_state.value} is not a transition in hld.md section 3"
        )
        self.from_state = from_state
        self.to_state = to_state


# --- the numbers -------------------------------------------------------------------

#: Three ticker refreshes at `measured` 5001 ms (`feed.py`). See
#: `docs/design/lld/controller.md` section 4 for the live measurement beside it.
DEGRADED_AFTER_SECONDS = 15.0
#: Three degraded intervals, and under Delta's documented 60 s idle disconnect so that we
#: notice before the venue would drop us. `assumed`.
RECONNECT_AFTER_SECONDS = 45.0
#: `assumed`. Fast enough that `/health` and the badge are never a minute behind, slow
#: enough that a day of heartbeats is 8,640 events per adapter and not a flood.
HEARTBEAT_SECONDS = 10.0
#: The staleness timer's resolution: a 15 s bound is observed to the nearest second.
POLL_SECONDS = 1.0
#: Consecutive failed polls before the watchdog stops merely logging and emits an
#: `alert`. `assumed`. One failure is a blip a log line covers; three in a row at
#: `poll_seconds` is a consumer that is going to keep raising, and a staleness timer
#: whose every tick fails is a watchdog that is not watching.
POLL_FAILURES_BEFORE_ALERT = 3

#: **Moved here from `adapters/delta_socket.py` in #39, unchanged in value.** The first
#: wait after a drop, doubling per consecutive failure to the ceiling below, and restored
#: to this the moment a connection delivers anything.
RETRY_DELAY_SECONDS = 1.0
#: The ceiling the doubling stops at. A minute is long enough that a venue outage costs
#: one dial a minute rather than a flood, and short enough that a feed is back within a
#: minute of the venue returning.
MAX_RETRY_DELAY_SECONDS = 60.0
#: **The lifetime reconnect budget**, spent one per drop and **restored in full the
#: moment a message arrives**. Delivering data, not connecting, is what proves the
#: endpoint works: Delta can accept a handshake and close immediately, and a budget
#: restored on merely connecting never exhausts at all. It is a budget rather than an
#: infinite retry because a feed that has failed eleven times running without ever
#: delivering a frame is not going to succeed on the twelfth, and Delta's connection
#: allowance is 150 per five minutes.
RECONNECT_BUDGET = 10

#: `alert` codes. Short and stable, for the same reason a `reason` is.
ALERT_CONNECTION_SILENT = "connection_silent"
ALERT_POLL_FAILING = "poll_failing"
#: The spent budget. **The loudest thing this engine says**: the feed has given up and
#: will not come back without a `resume`, and every screen downstream is about to go
#: quiet with no other symptom.
ALERT_RECONNECT_BUDGET = "reconnect_budget_spent"


class ConnectionController:
    """Wraps one adapter. **The only thing that may change its connection's state.**"""

    def __init__(
        self,
        adapter: Adapter,
        publish: Callable[[Event], None],
        *,
        degraded_after: float = DEGRADED_AFTER_SECONDS,
        reconnect_after: float = RECONNECT_AFTER_SECONDS,
        heartbeat_every: float = HEARTBEAT_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        retry_delay: float = RETRY_DELAY_SECONDS,
        reconnect_budget: int = RECONNECT_BUDGET,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        """Two clocks, on purpose, for the reason `feed.py` records.

        `clock` measures **elapsed** time and is monotonic: `time.time()` steps backwards
        under an NTP correction, which would make an age negative and a staleness bound
        meaningless. `wall_clock` stamps `ts_received` on the events, which is a wall
        clock because that is what the envelope means by it. Both are injected so a test
        can hold time still.
        """
        self._adapter = adapter
        self._publish = publish
        self.degraded_after = degraded_after
        self.reconnect_after = reconnect_after
        self.heartbeat_every = heartbeat_every
        self.poll_seconds = poll_seconds
        self.retry_delay = retry_delay
        self.reconnect_budget = reconnect_budget
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep

        self._state: ConnectionState | None = None
        #: When the current state was entered, on the monotonic clock. What staleness is
        #: measured from before the first message ever arrives — a connection that opens
        #: and delivers nothing is stale from the moment it opened, not never.
        self._entered_at = clock()
        self._last_message_at: float | None = None
        #: When a socket was last reported open **and resubscribed**. What staleness is
        #: measured from when it is more recent than the last message: a replayed socket
        #: has not had a chance to speak yet, and the silence before it is not its own.
        self._opened_at: float | None = None
        #: `None` until the first poll. That poll beats whenever it comes — under
        #: `run()`, one `poll_seconds` after the start rather than at the instant of it.
        self._last_beat_at: float | None = None

        #: Transitions since construction, for `/health` in #39.
        self.transitions = 0
        #: Times this connection has entered `reconnecting`, for `/health`. A count and
        #: not a rate: a reader comparing two polls of `/health` gets the rate, and a
        #: rate computed here would need a window nobody agreed on.
        self.reconnects = 0
        #: Budget spent, never earned back except by a message arriving. The remaining
        #: figure is what `/health` reports; this is what the arithmetic is done on, so
        #: that "spent 3 of 10" and "7 left" cannot drift apart.
        self._budget_spent = 0
        #: The next backoff wait. Doubles per failed attempt, restored by a message.
        self._delay = retry_delay
        #: Whether the **adapter** last said its socket was gone. Not the same question
        #: as `state is RECONNECTING`, which the staleness watchdog can also reach with
        #: a socket that is still open and merely silent — and the difference decides
        #: whether `run()` redials when `stream` returns. See `_attempt_forever`.
        self._socket_closed = False

        #: Whether this controller is on the adapter's connection register. Tracked so
        #: that attaching and detaching are both idempotent.
        self._attached = False
        self._attach()

    # --- being on the adapter, and coming off it ------------------------------------

    def _attach(self) -> None:
        """Listen to the adapter's socket. Idempotent."""
        if self._attached:
            return
        self._adapter.on_connection(self.connection_signal)
        self._attached = True

    def detach(self) -> None:
        """Come off the adapter's connection register. **Idempotent.**

        Registering was a construction side effect with no way to undo it, so a
        controller that was replaced or discarded stayed strongly referenced by the
        adapter and went on being told about a socket it no longer owned: one live
        socket driving two machines, the dead one answering a signal by raising
        `IllegalTransition` inside the socket reader. `run()` calls this on the way out
        and `start()` puts it back, so the ordinary path needs no bookkeeping; #39's
        supervisor, which holds controllers across a lifespan and may replace one, is
        the caller that needs to say so itself.
        """
        if not self._attached:
            return
        self._adapter.off_connection(self.connection_signal)
        self._attached = False

    # --- what it reports ----------------------------------------------------------

    @property
    def adapter(self) -> Adapter:
        """The adapter this controller wraps. For #39's supervisor and `/health`."""
        return self._adapter

    @property
    def adapter_name(self) -> str:
        return self._adapter.venue

    @property
    def state(self) -> ConnectionState | None:
        """`None` only before `start()`. Every other moment is one of the five."""
        return self._state

    @property
    def last_message_at(self) -> float | None:
        return self._last_message_at

    def last_message_age(self, now: float | None = None) -> float | None:
        """Seconds since the last message, or `None` if none has ever arrived.

        `None` is an **unknown** age and not an age of zero, which is the rule the
        `heartbeat` event's own field carries.
        """
        if self._last_message_at is None:
            return None
        return (self._clock() if now is None else now) - self._last_message_at

    @property
    def budget_remaining(self) -> int:
        """Reconnects left before this connection gives up. Never below zero.

        Reported by `/health` because a feed two drops from `stopped` and a feed that has
        never dropped are the same green badge, and the difference is the whole point of
        having a budget.
        """
        return max(0, self.reconnect_budget - self._budget_spent)

    # --- the causes ---------------------------------------------------------------

    def start(self) -> None:
        """Adapter start, or a resume out of `stopped`. Both reach `connecting`.

        **The age of the last message is forgotten here, and only here.** Time spent
        stopped is our silence, not the venue's: a connection resumed after ten quiet
        minutes would otherwise arrive already past the reconnect bound and be marked
        `reconnecting` on the first timer tick, against a socket nobody had given a
        chance to speak. A **reconnect** is the opposite case and keeps its age — it
        does not pass through here — because that gap really was the venue's, and it is
        the gap the whole staleness bound exists to catch.
        """
        reason = REASON_RESUME if self._state is State.STOPPED else REASON_START
        self._last_message_at = None
        self._opened_at = None
        # A controller `run()` detached on its way out is deaf until it is put back, and
        # a resumed connection that never heard its socket open would sit in
        # `connecting` until the staleness bound moved it. #41's resume is the caller.
        self._attach()
        self.transition(State.CONNECTING, reason)

    def stop(self, reason: str = REASON_STOPPED, detail: str = "") -> None:
        """Stopped by request, and the adapter is asked to stop with it.

        Idempotent: stopping a stopped connection is not an error and emits nothing,
        because a `stopped -> stopped` event describes no change. **The adapter is asked
        to stop either way** — including before a `start()`, where there is no state to
        move out of but there is still an adapter that must not go on to open a socket.
        """
        self._adapter.stop()
        if self._state is None or self._state is State.STOPPED:
            return
        self.transition(State.STOPPED, reason, detail)

    def message_arrived(self, now: float | None = None) -> None:
        """An event came off the adapter. Resets the age, the budget and the backoff.

        **Delivering data is what restores the budget**, and it is restored in full
        rather than by one. OpenAlgo's comment records the bug the other way round: a
        cumulative counter that never resets kills a feed reconnecting once a day after a
        month, with no failure anywhere to point at. The inverse — restoring on the
        socket merely opening — is worse, because Delta can accept a handshake and close
        immediately and a budget restored every pass never exhausts at all.
        """
        self._last_message_at = self._clock() if now is None else now
        self._budget_spent = 0
        self._delay = self.retry_delay
        if self._state is State.RECONNECTING:
            # A frame off a socket the controller believed gone. The table forbids
            # `reconnecting -> connected`, so it takes the same two steps an open does.
            self.transition(State.CONNECTING, REASON_BACKOFF)
        if self._state is State.CONNECTING or self._state is State.DEGRADED:
            self.transition(State.CONNECTED, REASON_MESSAGE)

    def connection_opened(self, detail: str = "") -> None:
        """The adapter's socket is up and every subscription has been replayed.

        **The replayed socket's silence starts here.** A reconnect keeps the venue's age
        while it is reconnecting — that gap is what the bound exists to catch — but once
        the replay is done there is a new socket that has not been given a chance to
        speak, and measuring it against a message from before the drop demotes it on the
        next poll. See `docs/design/lld/controller.md` §8.

        An open reported while the connection is **already** running is not dropped on
        the floor: no move is allowed out of `connected`, but the grace is rebased and
        the fact is logged, because a socket that says it reopened is a socket that
        stopped and started, and forgetting that would leave the machine measuring
        against a connection that no longer exists.
        """
        self._opened_at = self._clock()
        self._socket_closed = False
        if self._state in (State.CONNECTED, State.DEGRADED):
            logger.info(
                "feed connection %s: open while already %s; the staleness clock is "
                "rebased and the state is unchanged%s",
                self.adapter_name,
                self._state.value,
                f" ({detail})" if detail else "",
            )
            return
        if self._state is State.RECONNECTING:
            self.transition(State.CONNECTING, REASON_BACKOFF, detail)
        if self._state is State.CONNECTING:
            self.transition(State.CONNECTED, REASON_OPEN, detail)

    def connection_closed(self, detail: str = "") -> None:
        """The adapter's socket is gone. The venue's own words go in `detail`.

        They do not go in `reason`, which stays the short stable `closed` so that #40 can
        badge on it and #42 can grep it without matching a thousand distinct sentences.

        **This is where the lifetime budget is spent**, since #39: a drop is what a
        reconnect budget counts, and this is the one place a drop is known. When there is
        nothing left to spend the connection does not go on to `reconnecting` and sit
        there — it is taken to `stopped` through it, which is the move #38 left in the
        table for exactly this, with one `alert` and one error-level log beside it.
        """
        self._socket_closed = True
        if self._state is None or self._state is State.STOPPED:
            return
        # **The budget is spent on the drop, not on the transition**, and the two are not
        # the same event. The staleness watchdog reaches `reconnecting` on its own, over
        # a socket the venue has not closed yet; when that socket then really dies, the
        # close arrives at a machine already in `reconnecting`. Counting only the
        # transition made that drop free — an unbounded reconnect loop in precisely the
        # case the budget exists for, with a full budget on the books throughout.
        if self._state is not State.RECONNECTING:
            self.transition(State.RECONNECTING, REASON_CLOSED, detail)
        self.reconnects += 1
        self._spend_reconnect(detail)

    def _spend_reconnect(self, detail: str) -> None:
        """Take one off the budget, or stop if there was none to take.

        Checked **before** spending rather than after, so that a budget of two allows two
        reconnects and the third drop is the one that stops — "two reconnects" meaning
        two, which is the only reading of the number that a person setting it would
        expect.
        """
        if self.budget_remaining > 0:
            self._budget_spent += 1
            return
        spent = (
            f"the reconnect budget of {self.reconnect_budget} is spent after "
            f"{self.reconnects} drops; this connection will not come back without a "
            f"resume ({detail})"
            if detail
            else f"the reconnect budget of {self.reconnect_budget} is spent after "
            f"{self.reconnects} drops; this connection will not come back without a "
            f"resume"
        )
        # An error record and not a warning, and the only one this module logs at error
        # besides a dead watchdog. A feed that has given up produces no other symptom:
        # the screens simply stop moving.
        logger.error("feed connection %s: %s", self.adapter_name, spent)
        self._alert(ALERT_RECONNECT_BUDGET, spent)
        # `stop()` rather than a bare transition, because the adapter must be told too —
        # a controller that gave up while its adapter went on dialling would be spending
        # a venue's connection allowance on a feed nobody is watching.
        self.stop(REASON_STOPPED, spent)

    def connection_signal(self, signal: ConnectionSignal, detail: str = "") -> None:
        """The adapter's `on_connection` listener. Registered in `__init__`."""
        if signal is ConnectionSignal.OPENED:
            self.connection_opened(detail)
        else:
            self.connection_closed(detail)

    def sink(self, event: Event) -> None:
        """What to hand `adapter.stream`: note the arrival, then publish onward.

        **Every event off the adapter passes through here**, which is how "a message
        arrived" is observed without the adapter having to say so — the arrival of data
        *is* the signal, and an adapter that had to report it separately could report it
        wrongly.
        """
        self.message_arrived()
        self._publish(event)

    def poll(self, now: float | None = None) -> None:
        """The staleness timer fired. Ages the connection, then beats.

        Staleness before the heartbeat, so a heartbeat published in the same poll carries
        the state the connection is in **now** rather than the one it just left.
        """
        moment = self._clock() if now is None else now
        self._check_staleness(moment)
        self._beat(moment)

    # --- staleness and the heartbeat ----------------------------------------------

    def _check_staleness(self, now: float) -> None:
        """Silence, measured from the last message — or, before there has ever been one,
        from the moment the current state was entered.

        **A connection that opens and delivers nothing is stale from the open**, not
        never: an age of `None` treated as "not yet old" is exactly the healthy-socket,
        zero-messages failure `feed.py` records.

        **And the last message is not the whole story.** A socket reported open and
        resubscribed since that message is a *new* socket whose own silence began at the
        open, so the later of the two is what this measures from. Without it, every
        outage longer than `reconnect_after` ends in a flap: the reopened socket is
        demoted on the next poll for a gap that belonged to the socket before it. The
        age the heartbeat reports is untouched — that one is the venue's own silence and
        stays true across the reconnect.
        """
        if self._state not in (State.CONNECTING, State.CONNECTED, State.DEGRADED):
            return
        since = self._last_message_at
        if since is None:
            since = self._entered_at
        if self._opened_at is not None and self._opened_at > since:
            since = self._opened_at
        age = now - since
        if age >= self.reconnect_after:
            detail = f"{age:.1f}s silent"
            self.transition(State.RECONNECTING, REASON_SILENT, detail)
            # `events.md` lists "a connection has gone stale" among the things an alert
            # is for, and this is that moment. **Not `degraded`:** fifteen quiet seconds
            # is a badge and a heartbeat, and an alert on every quiet minute is exactly
            # the flood an alert exists to stand out from.
            self._alert(ALERT_CONNECTION_SILENT, detail)
        elif age >= self.degraded_after and self._state is State.CONNECTED:
            self.transition(State.DEGRADED, REASON_STALE, f"{age:.1f}s silent")

    def _alert(self, code: str, detail: str, severity: str = "error") -> None:
        """Publish one `alert`, and never let publishing it be the thing that raises.

        The guard is not decoration: the caller most likely to need an alert is the poll
        that just failed **because `_publish` raised**, and an alert that re-raised into
        the watchdog would kill the watchdog with the report of its own illness.
        """
        try:
            self._publish(
                Alert(
                    source=SOURCE,
                    ts_received=self._wall_clock(),
                    adapter=self.adapter_name,
                    severity=severity,
                    code=code,
                    detail=detail,
                )
            )
        except Exception:
            logger.exception("publishing an alert raised; the alert is lost")

    def _beat(self, now: float) -> None:
        """One `heartbeat` per cadence, whatever the state. Not a message from the venue.

        **The first poll after a start beats, whenever that poll comes** — which under
        `run()` is one `poll_seconds` after the start and not at the instant of it. So
        `/health` and the badge have an answer a second in rather than a cadence in;
        they do not have one the moment `run()` is awaited.
        """
        if self._state is None:
            return
        if (
            self._last_beat_at is not None
            and now - self._last_beat_at < self.heartbeat_every
        ):
            return
        self._last_beat_at = now
        self._publish(
            Heartbeat(
                source=SOURCE,
                ts_received=self._wall_clock(),
                adapter=self.adapter_name,
                state=self._state,
                last_message_age_seconds=self.last_message_age(now),
            )
        )

    # --- running it ----------------------------------------------------------------

    async def run(self) -> None:
        """Start, dial the adapter until it is done, and tick the staleness timer beside.

        Returns when the adapter is finished — stopped by request, or out of budget — and
        leaves the connection `stopped`, because an adapter that is no longer streaming
        is not connecting.

        The timer is a **separate task**, not a timeout on the read: a controller that
        only woke when a message arrived could never notice that none had.
        """
        self.start()
        timer = asyncio.ensure_future(self._tick_forever())
        try:
            await self._attempt_forever()
        finally:
            timer.cancel()
            # **The result is read, not discarded.** `return_exceptions=True` retrieves
            # the exception, which is what stops asyncio's "Task exception was never
            # retrieved" warning from firing — so a timer that died of anything other
            # than its own cancellation would otherwise leave no trace anywhere.
            (ended,) = await asyncio.gather(timer, return_exceptions=True)
            if isinstance(ended, BaseException) and not isinstance(
                ended, asyncio.CancelledError
            ):
                logger.error(
                    "the staleness timer for %s ended in an exception; staleness was "
                    "not being watched",
                    self.adapter_name,
                    exc_info=ended,
                )
                self._alert(
                    ALERT_POLL_FAILING, f"the staleness timer stopped: {ended!r}"
                )
            self.stop(detail="the adapter's stream returned")
            # Finished with this adapter, and holding on to nothing of it. `start()`
            # puts the listener back, so a resume is not left deaf.
            self.detach()

    async def _attempt_forever(self) -> None:
        """One connection at a time, with the backoff between them. **#39's move.**

        `adapter.stream` is one connection since #39 — dial, replay, pump, return — so
        the loop that decides there is another attempt lives here, where the state
        machine can see it. That is the whole reason it moved: *we gave up* was a `while`
        condition inside the socket owner, one layer below anything that could observe it
        or say so, and now it is a transition to `stopped` with an alert attached.

        **A returned stream is only a reason to redial if the adapter said its socket was
        gone.** Two other endings reach the same line and neither is ours to retry: a
        `stop()` returns without reporting a close, on purpose, because a stop is not a
        drop; and a `reconnecting` the *staleness watchdog* reached — a socket the venue
        never closed and merely stopped speaking on — is a state, not a dead socket, and
        redialling it would be dialling over a connection that is still open. Forcing
        that one down needs a member the protocol does not have; it is #41's `reconnect`
        command, and `docs/design/lld/controller.md` §9 records the gap.
        """
        while True:
            await self._adapter.stream(self.sink)
            if self._state is State.STOPPED or not self._socket_closed:
                return
            # The wait grows per consecutive failed attempt and is restored in full by
            # the first message off a connection, which is the same rule the budget
            # follows and for the same reason.
            await self._sleep(self._delay)
            self._delay = min(self._delay * 2, MAX_RETRY_DELAY_SECONDS)
            if self._state is State.STOPPED:
                return
            if self._state is State.RECONNECTING:
                # **Emitted when the attempt begins, not when it succeeds.** #38 could
                # only move here at the instant a socket opened, because it did not own
                # the dial; a badge that showed `reconnecting` for the whole of a
                # thirty-second outage and never showed a try in progress is the
                # difference.
                self.transition(
                    State.CONNECTING, REASON_BACKOFF, f"redialling, {self._delay:.0f}s"
                )

    async def _tick_forever(self) -> None:
        """Poll until cancelled. **A poll that raises must not end the watchdog.**

        `poll()` reaches `_publish`, which is the bus and, from #39, a consumer's code.
        Letting that exception out killed the timer silently — `run()`'s `gather` caught
        it, so not even asyncio's unretrieved-exception warning fired — and left the
        connection sitting in `connected` through any silence at all. A watchdog that
        dies of a consumer's bug is the plausible-and-wrong failure this module is
        against, so it logs, counts, and keeps ticking; and once the failures are a
        pattern rather than a blip, it says so on the bus as well.
        """
        failures = 0
        while True:
            await self._sleep(self.poll_seconds)
            try:
                self.poll()
            except Exception:
                failures += 1
                logger.exception(
                    "the staleness poll for %s raised (%d in a row); the watchdog "
                    "keeps ticking",
                    self.adapter_name,
                    failures,
                )
                if failures == POLL_FAILURES_BEFORE_ALERT:
                    self._alert(
                        ALERT_POLL_FAILING,
                        f"{failures} staleness polls in a row raised",
                    )
            else:
                failures = 0

    # --- the one place the state changes ------------------------------------------

    def transition(
        self, to_state: ConnectionState, reason: str, detail: str = ""
    ) -> FeedConnection:
        """The only place the state changes, and the only place the event is built.

        Public because the machine **is** the interface: #39 drives
        `reconnecting -> stopped` when the lifetime budget is spent, and #41 drives the
        pause, resume and reconnect commands. Both are moves this ticket has no cause
        method for, and neither should reach around the table to make them.
        """
        from_state = self._state
        if (from_state, to_state) not in ALLOWED:
            raise IllegalTransition(from_state, to_state)
        self._state = to_state
        self._entered_at = self._clock()
        self.transitions += 1
        event = FeedConnection(
            source=SOURCE,
            ts_received=self._wall_clock(),
            adapter=self.adapter_name,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
        logger.info(
            "feed connection %s: %s -> %s (%s)%s",
            self.adapter_name,
            "start" if from_state is None else from_state.value,
            to_state.value,
            reason,
            f" {detail}" if detail else "",
        )
        self._publish(event)
        return event
