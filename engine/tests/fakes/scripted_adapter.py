"""An adapter driven by a script instead of a socket. **The one new test seam.**

Everything above the adapter — the connection controller (#38), the supervisor (#39),
`/health`, the websocket badge (#40) — has to be testable without a network, and the way
to do that is a double that implements the same protocol as the real thing and does what
it is told. This is that double. It is built here, in #36, because #38's tests need it on
day one.

---

## The script language

A script is a sequence of steps, walked in order by `stream`. Four verbs:

* **`Frames(channel, frames)`** — these venue frames arrived on this channel. The events
  they decode to are published, and `frames_replayed` grows.
* **`Close(reason)`** — the connection dropped and came back. `closes` and `connections`
  grow, and **every subscription is replayed**: a snapshot lands in `replays`.
* **`Silence(seconds)`** — nothing arrives for this long. `silences` and `silent_seconds`
  grow and the clock advances; **nothing is published**.
* **`Resume()`** — the feed comes back with the book it went away with. The most recent
  `Frames` step is published again, through a **fresh decoder**, so a ticker replay
  produces its `md.index_quote` again rather than deduplicating it into silence.

So the ticket's own sentence — *frames, close, silence 20 s, resume* — is:

    ScriptedAdapter([Frames("ob_l2", captured), Close(), Silence(20.0), Resume()])

and it produces the events, then nothing, then the events again.

**`Resume` replays the last `Frames` rather than doing nothing**, because a verb with no
observable effect is not a verb. A feed that reconnects and re-sends the current book is
what Delta actually does — a fresh subscription is answered with the state of the book,
not with silence until the next change — so replaying is also the honest behaviour.

**`Silence` does not really wait.** The clock is injected: pass a `sleep` that records its
argument and returns, and a twenty-second silence costs a test nothing while still being
assertable as twenty seconds. That is what makes staleness detection testable at all —
#38's degraded bound is 15 s (`assumed`, `hld.md` §5), and no suite may sit through it.

**`stream` returns when the script is exhausted.** A real adapter's `stream` returns when
it is stopped or when its reconnect budget is spent; a script running out is the same kind
of ending, and it means a test can simply `await adapter.stream(publish)` and then assert.

---

## What it does not do

**It emits no `feed.connection` events.** Connection state is the controller's to decide
and #38 owns it; a double that pre-empted the state machine would make #38's tests assert
against this file rather than against the code under test. `Close` is observable through
`closes`, `connections` and `replays` instead, which is what a controller wrapped around
this adapter would have to react to.

**What it does report, since #38, is the protocol's `on_connection` signal** — the two
facts about a socket, `OPENED` and `CLOSED`, and not a word about what they mean. `Close`
fires `CLOSED` and then `OPENED`, in that order, because that is the shape of the verb:
the connection dropped and came back.

**It decodes with the real Delta decoder by default**, so replaying
`tests/fixtures/ws-*.json` produces genuine `md.option_quote` and `md.option_reference`
events rather than hand-written stand-ins. Pass `decode=` to script a different venue's
frames, or to hand back events directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from deltapayoff.adapters.base import ConnectionListener, ConnectionSignal
from deltapayoff.events import Event, Instrument
from fakes.decoder import delta_decoder

# --- the four verbs ---------------------------------------------------------------


@dataclass(frozen=True)
class Frames:
    """Venue frames arriving on one channel."""

    channel: str
    frames: Sequence[dict[str, Any]]
    #: Our arrival stamp for every frame in this step. Fixed rather than clock-read, so a
    #: test that asserts on `ts_received` is deterministic.
    received_at: float = 1_788_430_800.0


@dataclass(frozen=True)
class Close:
    """The connection dropped. Everything subscribed is replayed on the way back."""

    reason: str = "scripted close"


@dataclass(frozen=True)
class Silence:
    """Nothing arrives for this many seconds. The clock is the injected one."""

    seconds: float


@dataclass(frozen=True)
class Resume:
    """The feed comes back with the book it went away with."""


Step = Frames | Close | Silence | Resume


# --- the double -------------------------------------------------------------------


@dataclass
class ScriptedAdapter:
    """An `adapters.base.Adapter` that does what its script says."""

    script: Sequence[Step] = ()
    venue: str = "SCRIPT"
    underlyings: tuple[str, ...] = ("BTC",)
    #: What `instruments` answers, per underlying. Empty means it answers nothing, which
    #: is a real case: a venue that lists no contracts for an asset.
    listings: dict[str, list[Instrument]] = field(default_factory=dict)
    #: What the two REST reads answer. `None` means the caller did not need them, and
    #: asking raises rather than returning an empty shape that reads as a real answer.
    expiries_response: Any = None
    chain_response: Any = None
    #: `(channel, frame, received_at) -> list[Event]`. The real Delta decoder by
    #: default, so captured fixtures produce genuine events.
    decode: Callable[[str, dict[str, Any], float], list[Event]] | None = None
    #: Injected so a twenty-second silence costs a test nothing.
    sleep: Callable[[float], Any] = asyncio.sleep

    def __post_init__(self) -> None:
        if self.decode is None:
            self.decode = delta_decoder()

        #: Channel to symbols, exactly as `DeltaFeed` keys it. **Never cleared**,
        #: because that is what a reconnect replays.
        self.registry: dict[str, set[str]] = {}
        #: One snapshot of the registry per replay, oldest first. A reconnect that
        #: replayed a subset would show up here as a smaller set, which is the silent
        #: failure the socket owner exists to prevent, made assertable.
        self.replays: list[dict[str, set[str]]] = []

        self.connections = 0
        self.closes = 0
        self.silences = 0
        #: Total seconds of scripted silence, so "it went quiet for the staleness bound"
        #: is one assertion.
        self.silent_seconds = 0.0
        self.frames_replayed = 0
        self.published = 0

        self._last_frames: Frames | None = None
        self._stopping = False
        #: Everyone told when the socket comes or goes. A list rather than one slot,
        #: because the protocol says a register.
        self._listeners: list[ConnectionListener] = []

    # --- describe itself ---------------------------------------------------------

    async def instruments(self, underlying: str) -> list[Instrument]:
        return list(self.listings.get(underlying.upper(), []))

    def subscribe(self, instruments: Iterable[Instrument]) -> None:
        symbols = {
            instrument.venue_symbol
            for instrument in instruments
            if instrument.venue_symbol
        }
        if not symbols:
            return
        for channel in ("ticker", "ob_l2"):
            self.registry.setdefault(channel, set()).update(symbols)

    def on_connection(self, listener: ConnectionListener) -> None:
        self._listeners.append(listener)

    def off_connection(self, listener: ConnectionListener) -> None:
        """One registration off, and quiet about one that was never on — the protocol's
        rule, so a supervisor that tidies up twice is not handed a failure by it."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _signal(self, signal: ConnectionSignal, detail: str) -> None:
        for listener in self._listeners:
            listener(signal, detail)

    # --- the script --------------------------------------------------------------

    async def stream(self, publish: Callable[[Event], None]) -> None:
        """Walk the script once, then return.

        `stop()` is honoured between steps, which is as often as a real adapter checks
        it: `DeltaFeed` tests its own flag once per frame and once per reconnect.

        **A `stop()` before `stream` means no connection is opened at all**, and the flag
        is not reset on the way in. That is `DeltaFeed.run`'s own behaviour — it tests the
        flag before connecting and never clears it — so an adapter that has been stopped
        stays stopped, here as there.
        """
        if self._stopping:
            return
        self._open_connection()

        for step in self.script:
            if self._stopping:
                break
            await self._run_step(step, publish)

    async def _run_step(self, step: Step, publish: Callable[[Event], None]) -> None:
        if isinstance(step, Frames):
            self._last_frames = step
            self._emit(step, publish)
        elif isinstance(step, Close):
            self.closes += 1
            self._signal(ConnectionSignal.CLOSED, step.reason)
            # A reconnect decodes with a fresh decoder, so no ticker state leaks across
            # a drop. #37 moved the builder into `fakes/decoder.py`; this is that call.
            self.decode = delta_decoder()
            self._open_connection()
        elif isinstance(step, Silence):
            self.silences += 1
            self.silent_seconds += step.seconds
            await self.sleep(step.seconds)
        elif isinstance(step, Resume):
            if self._last_frames is not None:
                self._emit(self._last_frames, publish)
        else:  # pragma: no cover - a step nobody defined is a test's own bug
            raise TypeError(f"{step!r} is not one of the four verbs")

    def _emit(self, step: Frames, publish: Callable[[Event], None]) -> None:
        assert self.decode is not None  # set in __post_init__
        for frame in step.frames:
            self.frames_replayed += 1
            for event in self.decode(step.channel, frame, step.received_at):
                self.published += 1
                publish(event)

    def _open_connection(self) -> None:
        """A fresh socket has forgotten every subscription, so the registry is replayed.

        Recorded rather than sent, because there is nothing to send it to. The point is
        that a reconnect that replayed **nothing** — a healthy connection carrying zero
        messages, the failure with no error — is visible as an empty snapshot here.
        """
        self.connections += 1
        self.replays.append(
            {channel: set(symbols) for channel, symbols in self.registry.items()}
        )
        # After the replay, never before: `OPENED` promises a socket that has been
        # resubscribed, and an adapter that claimed it earlier would be lying about the
        # one thing the signal is for.
        self._signal(ConnectionSignal.OPENED, "scripted connection")

    def stop(self) -> None:
        self._stopping = True

    # --- the venue's REST reads --------------------------------------------------

    async def expiries(self, underlying: str) -> Any:
        if self.expiries_response is None:
            raise NotImplementedError(
                "this ScriptedAdapter was not given an expiries_response"
            )
        return self.expiries_response

    async def chain_snapshot(self, underlying: str, expiry: str) -> Any:
        if self.chain_response is None:
            raise NotImplementedError(
                "this ScriptedAdapter was not given a chain_response"
            )
        return self.chain_response
