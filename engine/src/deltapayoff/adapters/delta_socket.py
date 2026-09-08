"""The socket owner: one connection to Delta, read and republished frame by frame.

**Inside `adapters/` since #37, and that is the whole reason it moved.** `ob_l2` and
`ticker` are Delta's words, and the acceptance test for retiring the old quote record is
that they appear nowhere but the Delta adapter. The socket owner *is* part of knowing a
venue — it subscribes by channel name — so it belongs in the package that owns one rather
than beside the modules that must never learn one. Nothing about it changed in the move
except the retirement of the `Quote` record it used to publish.

Five jobs, and the third and fourth are where the real failures live.

**Subscribe both channels.** `ob_l2` carries the top-of-book, refreshed every **508 ms**
per contract on a live chain (measured, `tools/measure_feed.py`), and everything the
pricing needs is in it — best bid, best ask, and the strike and expiry parsed out of the
symbol. `ticker` refreshes every **5001 ms** and carries spot, open interest, and Delta's
own Greeks and implied vols, which travel as **reference columns only** and are never
consumed as inputs. That 9.8x gap is the whole opportunity: Delta computes an implied
volatility from these prices and republishes it ten times more slowly than the prices
underneath it move.

**Heartbeat.** A quiet connection and a dead connection are indistinguishable over TCP.
Delta's documented 60 s idle disconnect did not reproduce in a 75 s test on this project,
so it is treated as unverified and pings are sent regardless — 30 s, OpenAlgo's interval.

**One connection, and since #39 only one.** `run()` dials once, replays the registry,
pumps until the socket ends, and returns. **Backoff, the lifetime reconnect budget and
the decision to redial left this module in #39** and belong to
`controller.ConnectionController`, with every value unchanged — the rules they encode did
not move, only the code that runs them:

* A budget that **resets on data, not on connecting.** A cumulative retry counter looks
  correct and dies after a month: OpenAlgo's comment records that a long-lived feed
  reconnecting once a day silently exhausts a lifetime budget and never comes back. So a
  connection that **delivered a message** restores it. Resetting on the connection merely
  opening is the same bug inverted, and worse — Delta can accept a handshake and close
  immediately, and a budget that resets every pass never exhausts at all. Measured before
  that was fixed: 21 attempts in 0.3 s with a budget of 3 and no sign of stopping.
* A delay that doubles to a minute, restored the moment data arrives.

They moved because the controller cannot own a state machine whose central move — *we
gave up* — was decided by a `while` condition one layer below it, where nothing could
observe it and nothing could say so. What is left here is the half that needs a socket:
the dial, the replay and the read.

**Resubscribe everything.** This is the one that produces no error. A reconnected socket
is a fresh, empty socket and Delta has forgotten every subscription; skip the replay and
you get a healthy connection, zero messages, and a screen that quietly stops updating. So
`registry` is **never cleared** and is replayed in full on every open. It is keyed per
symbol rather than per message for the reason OpenAlgo gives: a message-keyed registry
replays a whole batch when one symbol inside it is rejected.

**Since #51 the replay is not the only send.** Registering used to be the whole of
subscribing, so a contract the venue listed after the socket came up was never told to it
— invisible until a reconnect happened to replay it, and a healthy feed does not
reconnect. `subscribe` now sends the difference to the connection it is holding, if it is
holding one. Still additive, still never cleared: the replay above is undisturbed and only
ever carries more. `docs/design/lld/relisting.md` is the design.

**Nothing here decodes anything, since #36.** This module used to turn a frame into a
quote record by calling `wire`, which meant the venue's array offsets were read by the
socket owner — so "the wire layout lives behind the adapter" was not true of the code. It
publishes a `VenueMessage`: the frame verbatim, its channel, and the instant it arrived.
`adapters.delta.DeltaAdapter` is the only thing that reads inside one, and since #37 it is
the only thing that reads one at all — the record that used to carry a frame past it, to
the chain cache and the bar writer, is gone and those two take canonical events.

**Report the connection.** #38 added a fifth job, and it is the smallest: say when the
socket opened and when an attempt ended, through `on_open` and `on_close`. Two facts and
no interpretation — whether "opened" means `connected` or `reconnecting` is
`controller.ConnectionController`'s to decide, and a socket owner that answered that
question would be a second state machine disagreeing with the first.

That leaves this module with those five jobs and no knowledge of what Delta's payloads
mean, which is what let #38 lift it under a connection controller without carrying a
decoder along.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import log_events
from ..logging_setup import log_event

logger = logging.getLogger(__name__)

#: What an attempt that opened and delivered nothing is called, in the close it
#: reports and in `last_error`. One spelling, because the controller's budget rule
#: and an operator reading a log line are looking at the same fact.
NOTHING_DELIVERED = "connected but closed without delivering a message"

PUBLIC_WS = "wss://public-socket.india.delta.exchange"

#: Delta's two channel names, and **the only place either string appears** outside the
#: adapter's own dispatch. `bars.py` used to spell them again for its `Tick.source`; since
#: #37 a tick's provenance is the event type it came from, so the venue's vocabulary stops
#: at this package.
TICKER_CHANNEL = "ticker"
BOOK_CHANNEL = "ob_l2"

#: The only channels Delta's public endpoint accepts. It retired `v2/ticker`,
#: `l1_orderbook` and `l2_orderbook` on 31 July 2026 and now rejects them as invalid, so
#: the old names are refused here rather than producing a silently empty stream.
CHANNELS = (TICKER_CHANNEL, BOOK_CHANNEL)

HEARTBEAT_SECONDS = 30.0

#: **Moved to `controller.py` in #39, unchanged in value.** They are named here as
#: pointers and not as numbers, because two modules holding the same constant is how the
#: two quietly stop agreeing. `RETRY_DELAY_SECONDS = 1.0`,
#: `MAX_RETRY_DELAY_SECONDS = 60.0` and `MAX_RETRIES = 10` are now
#: `controller.RETRY_DELAY_SECONDS`, `controller.MAX_RETRY_DELAY_SECONDS` and
#: `controller.RECONNECT_BUDGET`.


@dataclass(frozen=True, slots=True)
class VenueMessage:
    """One frame off the socket, undecoded, with the channel and the arrival stamp.

    **What the socket owner publishes.** The frame is Delta's own JSON object, kept
    verbatim: reading inside it is the adapter's business, and this record exists so that
    the reader and the decoder can be two things.

    `received_at` is a **wall clock** stamp, taken once here, so that the arrival time of
    a frame is the instant it was read rather than the instant something got around to
    decoding it. It is deliberately not a latency clock — `time.time()` can step backwards
    under an NTP correction. Anything measuring elapsed time uses `timing.time_it`, which
    is built on `perf_counter`.
    """

    channel: str
    symbol: str
    frame: dict[str, Any]
    received_at: float


class DeltaFeed:
    """Owns the connection. Publishes `VenueMessage`s to whatever it was handed.

    The sink is anything with a non-blocking `publish`. In the running engine it is the
    Delta adapter's frame sink; in `tests/test_feed.py` it is a `FanOut`, so what the
    socket produced can be drained and asserted about.
    """

    def __init__(
        self,
        sink,
        connect: Callable[[str], Any] | None = None,
        url: str = PUBLIC_WS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        #: Anything with a non-blocking `publish`. Named `sink` rather than `fanout`
        #: since #36: the running engine passes the adapter's frame sink, and only
        #: `tests/test_feed.py` still hands this a `FanOut`.
        self.sink = sink
        self.url = url
        self.heartbeat_seconds = heartbeat_seconds
        self._connect = connect or self._default_connect

        #: Channel to symbols. **Never cleared.** This is the reconnect replay.
        self.registry: dict[str, set[str]] = {}
        self._stopping = False

        self.connections = 0
        self.messages = 0
        self.bytes_read = 0
        self.malformed = 0
        #: Attempts that opened a socket with an **empty registry**, and so were not
        #: announced as open. Counted rather than only logged because a feed whose every
        #: connection is empty is a feed nobody subscribed, and a counter stuck above
        #: zero is the signal that says so.
        self.empty_opens = 0
        #: Why the last connection ended. `None` means it has not ended yet. Without
        #: this a persistently failing feed is indistinguishable from a quiet healthy
        #: one: `messages` simply stops moving and nothing says why.
        self.last_error: str | None = None

        #: Who to tell when the socket comes and goes. **Added in #38**, because a
        #: connection controller cannot run a state machine over a connection it cannot
        #: observe, and this loop is the only thing that knows.
        #:
        #: Two plain registers of `(detail) -> None` rather than one carrying the
        #: adapter protocol's `ConnectionSignal`: importing that here would make the
        #: socket owner depend on `adapters`, which imports this module back, and this
        #: module's whole point is that it knows nothing above itself. `DeltaAdapter`
        #: translates these two facts into the protocol's vocabulary.
        self._on_open: list[Callable[[str], None]] = []
        self._on_close: list[Callable[[str], None]] = []

        #: The socket currently being pumped, or `None` between connections. **Added in
        #: #51**, and it exists for exactly one reason: `subscribe` has to be able to
        #: tell whether a symbol it has just registered still has an open connection to
        #: be told about. Before this, the registry was sent once per open and a contract
        #: listed after that sat unsubscribed until the next reconnect — which on a
        #: healthy feed never comes.
        self._socket: Any | None = None
        #: Set once the current connection's replay is on the wire. A live subscribe waits
        #: on it, which is what lets `_socket` be published *before* the replay is
        #: snapshotted — closing the window where a subscribe reached neither — without
        #: ever putting two coroutines on one socket's `send`.
        self._replayed: asyncio.Event | None = None
        #: In-flight live subscribe sends, held so the loop cannot garbage-collect a task
        #: nothing else refers to. Cancelled with the connection they belong to.
        #:
        #: **Not a complete record of what has been sent**: a task's own done callback
        #: removes it as soon as it finishes, so `_pump`'s final `gather` sees only what
        #: is still in flight. That is what it is for — the finished ones need no
        #: cancelling — and it is safe only because `_send_subscribe` swallows its own
        #: failures rather than leaving an exception for nobody to retrieve.
        self._sends: set[asyncio.Task] = set()

    @staticmethod
    def _default_connect(url: str):
        import websockets

        return websockets.connect(url, open_timeout=20)

    def subscribe(self, channel: str, symbols: list[str]) -> None:
        """Register symbols, and tell an open socket about the ones that are new.

        Accepting subscriptions before connecting removes a start-up race the caller
        would otherwise have to know about: they accumulate here and are sent on open.

        **Since #51 registering is not the whole of subscribing.** The registry used to
        be sent exactly once per connection, in `_pump`, so a contract the venue listed
        after the socket came up was registered and never subscribed — invisible until a
        reconnect happened to replay it, and a healthy feed does not reconnect. That is
        issue #51's second half: re-listing on a cadence discovers nothing if the
        discovery cannot reach the connection that is already up.

        **Additive, and only the difference goes out.** The registry is still never
        cleared and nothing is ever unsubscribed, so the replay a reconnect performs is
        undisturbed — it simply carries more. Sending the whole registry again would also
        work, but Delta answers a subscribe with the current book, so it would answer for
        every contract already on the socket: a burst of several hundred snapshot frames
        to say nothing new.

        Synchronous and never blocking, as the callers require. The send is a task,
        because a socket that is open is a socket a loop is running.
        """
        if channel not in CHANNELS:
            raise ValueError(
                f"channel must be one of {', '.join(CHANNELS)}; got {channel!r}. "
                "Delta retired v2/ticker, l1_orderbook and l2_orderbook on 31 July 2026."
            )
        registered = self.registry.setdefault(channel, set())
        fresh = sorted(set(symbols) - registered)
        registered.update(symbols)
        socket, replayed = self._socket, self._replayed
        if fresh and socket is not None and replayed is not None:
            task = asyncio.get_running_loop().create_task(
                self._send_subscribe(socket, replayed, channel, fresh)
            )
            self._sends.add(task)
            task.add_done_callback(self._sends.discard)

    async def _send_subscribe(
        self,
        socket,
        replayed: asyncio.Event,
        channel: str,
        symbols: list[str],
    ) -> None:
        """One subscribe frame for one channel, on a connection that is already up.

        **A failure here is a warning and nothing more.** The symbols are already in the
        registry, so the next open replays them whatever happens to this send; taking the
        connection down over a subscribe that missed would turn a recoverable gap into an
        outage. The socket is passed in rather than read off `self`, so a send scheduled
        against one connection can never be delivered to its successor — and so is
        `replayed`, which this waits on: a subscribe that arrived while the replay was
        still going out must follow it rather than write beside it.

        **Under `feed.instruments`, the same name `main` uses**, because this is the other
        way the discovery can fail to land — and a failure that cannot be filtered for is
        the failure #51 spent six hours being. It is the only `log_event` call in this
        package; every other record here is a detail of one connection, and this one is a
        gap in the record.
        """
        try:
            await replayed.wait()
            await socket.send(
                self._subscribe_frame([{"name": channel, "symbols": symbols}])
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                logger,
                logging.WARNING,
                log_events.FEED_INSTRUMENTS,
                "subscribing %d newly listed %s symbols on the open socket failed; "
                "they stay in the registry and go out on the next open",
                len(symbols),
                channel,
                listed=len(symbols),
                exc_info=True,
            )

    def on_open(self, listener: Callable[[str], None]) -> None:
        """Be told when the socket is up **and resubscribed**. Safe before it exists."""
        self._on_open.append(listener)

    def on_close(self, listener: Callable[[str], None]) -> None:
        """Be told when an attempt has ended, opened or not."""
        self._on_close.append(listener)

    def off_open(self, listener: Callable[[str], None]) -> None:
        """Stop telling this listener. **Quiet about one that is not registered.**

        A register with no way out keeps whatever was ever put in it alive for the life
        of the feed, and goes on calling it: a replaced controller would keep driving a
        state machine nobody reads, off a socket it no longer owns.
        """
        self._forget(self._on_open, listener)

    def off_close(self, listener: Callable[[str], None]) -> None:
        """Stop telling this listener. Quiet about one that is not registered."""
        self._forget(self._on_close, listener)

    @staticmethod
    def _forget(
        listeners: list[Callable[[str], None]], listener: Callable[[str], None]
    ) -> None:
        """Remove one registration, and only one, leaving any duplicate in place.

        Raising on an absent listener would make the tidy-up path of a supervisor that
        cleans up on both success and failure into a second failure.
        """
        try:
            listeners.remove(listener)
        except ValueError:
            pass

    @staticmethod
    def _tell(listeners: list[Callable[[str], None]], detail: str) -> None:
        """Tell every listener, and let none of them end the connection.

        A listener that raises is a bug in the listener, and a socket reader is not the
        place to discover it: swallowing here means a broken consumer cannot take the
        feed down, which is the rule `publish` already follows.
        """
        for listener in listeners:
            try:
                listener(detail)
            except Exception:  # pragma: no cover - a listener's own bug
                logger.exception("a connection listener raised")

    def stop(self) -> None:
        self._stopping = True

    def _subscribe_payload(self) -> str | None:
        """The whole registry, one channel entry each.

        **Measured** by `tools/probe_ws.py` on 2026-09-03: Delta accepted 300 symbols in
        a single subscribe message on both channels and acknowledged all 300, so a full
        chain needs no batching. OpenAlgo's `MAX_SYMBOLS_PER_FRAME[ob_l2] = 1` was true
        when they measured it and is not true now.
        """
        channels = [
            {"name": channel, "symbols": sorted(symbols)}
            for channel, symbols in self.registry.items()
            if symbols
        ]
        if not channels:
            return None
        return self._subscribe_frame(channels)

    @staticmethod
    def _subscribe_frame(channels: list[dict[str, Any]]) -> str:
        """The venue's subscribe message. One spelling, used by the replay and by #51's
        live additions, so the two cannot drift into two shapes of the same frame."""
        return json.dumps({"type": "subscribe", "payload": {"channels": channels}})

    @staticmethod
    def _to_message(message: dict[str, Any]) -> VenueMessage | None:
        """One frame to one `VenueMessage`, or `None` if it is control traffic.

        **The frame is not read beyond its `type` and `sy`.** Whether the payload makes
        sense is the adapter's question, asked once, where the array offsets live.

        `subscriptions`, `error` and anything else is control traffic and is dropped
        here: publishing it would put a record carrying no market data on the bus.
        """
        kind = message.get("type")
        if kind not in CHANNELS:
            return None
        return VenueMessage(
            channel=kind,
            symbol=message.get("sy") or "",
            frame=message,
            received_at=time.time(),
        )

    async def _pump(self, socket) -> None:
        """Read until the connection ends. Publishes; never computes."""
        # **Reachable before the snapshot is taken, and that ordering is a bug fix.**
        # The snapshot below is read, then awaited on the wire; a `subscribe` landing on
        # that await used to be too late for the snapshot and too early for the live send,
        # so it went out on neither — and `main.relist_instruments` had already recorded
        # the contract as known, so nothing would ever retry it. That is #51's own failure
        # inside #51's own fix. Publishing the socket first makes the worst case a symbol
        # sent *twice*, which costs one extra snapshot frame and nothing else.
        replayed = asyncio.Event()
        self._socket = socket
        self._replayed = replayed

        payload = self._subscribe_payload()
        if payload is None:
            # An empty registry sends no subscribe, so this socket is guaranteed to
            # deliver nothing. **It is not announced as open.** "Open" promises a socket
            # that has been resubscribed, and a green badge over a socket with no
            # subscriptions on it is precisely the healthy-connection-zero-messages
            # failure this module exists to prevent — the one case the resubscribe-
            # everything rule is written against. Staying quiet leaves the controller in
            # `connecting`, which reaches `reconnecting` at its own bound rather than
            # sitting green for as long as the process runs.
            self.empty_opens += 1
            logger.warning(
                "the socket opened with nothing subscribed; not reporting it as open"
            )
        else:
            await socket.send(payload)
            # After the replay, never before. Announcing an open before its
            # subscriptions have gone out would hide the same failure behind the same
            # green badge, one moment earlier.
            self._tell(self._on_open, self.url)

        # The replay is on the wire, so a live subscribe may follow it. Every send this
        # connection makes goes through here in order, which is what keeps two coroutines
        # off one socket: a live subscribe scheduled during the replay waits here rather
        # than writing beside it.
        replayed.set()

        heartbeat = asyncio.create_task(self._heartbeat(socket))
        try:
            while not self._stopping:
                raw = await socket.recv()
                self.messages += 1
                self.bytes_read += len(raw)
                try:
                    venue_message = self._to_message(json.loads(raw))
                except Exception:
                    # One bad frame must not end ingestion. Counted, not swallowed —
                    # a malformed count that stays at zero is the useful signal. Since
                    # #36 this counts only frames that are not JSON at all: a frame that
                    # parses but makes no sense is counted by the adapter, which is the
                    # only thing that looks inside one.
                    self.malformed += 1
                    continue
                if venue_message is not None:
                    self.sink.publish(venue_message)
        finally:
            # This connection is over, so nothing may be sent on it again: a live
            # subscribe still in flight would be writing to a closed socket, and one
            # scheduled after this point must wait for the next open's replay instead.
            self._socket = None
            self._replayed = None
            # A snapshot, because each task's own done callback removes it from the set.
            sends = list(self._sends)
            for send in sends:
                send.cancel()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, *sends, return_exceptions=True)

    async def _heartbeat(self, socket) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                # `ping()` returns a future that resolves when the pong arrives. It is
                # deliberately not awaited: waiting for a pong would delay the next
                # ping by the round trip, and the client library runs its own keepalive
                # which closes a peer that stops answering. This heartbeat exists to
                # keep the connection from looking idle to Delta, not to detect death.
                await socket.ping()
            except Exception:  # pragma: no cover - the read loop reports the close
                return

    async def run(self) -> None:
        """**One connection.** Dial, replay the registry, pump, return when it ends.

        Since #39 this returns after a single attempt rather than looping: whether there
        is another attempt, how long to wait for it and whether the budget allows one at
        all are `controller.ConnectionController`'s, because they are the questions the
        state machine is made of. `delivered` is still computed here — the counter is
        read across the whole attempt rather than returned by `_pump`, since a dropped
        connection leaves `_pump` by raising and a returned flag is lost on exactly the
        path that matters most — and it becomes the `CLOSED` signal's detail, which is
        what reaches a listener and the log. **The controller does not read `last_error`
        at all**: the budget is restored in `message_arrived`, by frames arriving through
        the sink, not by anything this module reports. An earlier draft of #39 said
        otherwise and the code never did it.

        A `stop()` before this runs opens nothing, and the flag is never cleared, so a
        stopped feed stays stopped.
        """
        if self._stopping:
            return
        before = self.messages
        # **This attempt's own error, in a local.** `last_error` is instance state that
        # survives across `run()` calls, and since #39 there are many calls where there
        # used to be one loop. Reading it below to describe *this* ending would let a
        # previous attempt's dial failure be reported as the reason this socket closed.
        # Nothing constructs that today — every ending here raises, so the assignment
        # below always lands first — but it is one refactor away, and a local costs
        # nothing to make it impossible.
        attempt_error: str | None = None
        try:
            async with self._connect(self.url) as socket:
                self.connections += 1
                await self._pump(socket)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt_error = f"{type(exc).__name__}: {exc}"[:300]
            self.last_error = attempt_error

        delivered = self.messages > before
        # **The diagnosis, resolved before anyone is told rather than after.** An attempt
        # that opened and delivered nothing is the healthy-socket-zero-messages failure
        # this whole module exists to refuse, and it is the fact the budget rule turns
        # on. It used to be computed below the `_tell`, so a listener heard only
        # `ConnectionResetError` — which is also what a healthy socket dropping in a
        # storm reports. Two very different incidents, one indistinguishable detail.
        if delivered:
            ending = attempt_error or "closed by the venue"
        elif attempt_error is None:
            ending = NOTHING_DELIVERED
        else:
            ending = f"{attempt_error}; {NOTHING_DELIVERED}"

        # The attempt has ended, whether it ever opened or the dial failed outright.
        # The failed dial is reported too: a controller told only about sockets that had
        # opened would sit in `connecting` for the length of an endpoint outage, which
        # reads on a badge as "starting up". A stop is not a drop, so a stop is not
        # reported as one — and the controller reads exactly this signal to tell the two
        # endings apart, so staying quiet on a stop is what stops it redialling.
        if not self._stopping:
            self._tell(self._on_close, ending)

        # Delivering data, not connecting, is what proves the endpoint works. Delta can
        # accept the handshake and close straight away — a rejected subscribe, a
        # throttled IP, an endpoint draining — and treating that as healthy resets the
        # budget every pass so nothing ever gives up. Measured before that was fixed: 21
        # attempts in 0.3 s with a budget of 3, still going. At the production
        # one-second delay that exhausts the 150-per-5-minutes connection budget in
        # about two and a half minutes and keeps hammering.
        if delivered:
            self.last_error = None
        elif self.last_error is None:
            self.last_error = NOTHING_DELIVERED
