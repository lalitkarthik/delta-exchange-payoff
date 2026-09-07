"""The socket owner: one connection to Delta, decoded and fanned out.

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

**Reconnect with a budget that resets on data, not on connecting.** A cumulative retry
counter looks correct and dies after a month: OpenAlgo's comment records that a
long-lived feed reconnecting once a day silently exhausts a lifetime budget and never
comes back. So a connection that **delivered a message** restores the counter.

Resetting on the connection merely opening is the same bug inverted, and it is worse —
Delta can accept a handshake and close immediately, and a budget that resets every pass
never exhausts at all. Measured before the fix: 21 attempts in 0.3 s with `max_retries=3`
and no sign of stopping.

**Resubscribe everything.** This is the one that produces no error. A reconnected socket
is a fresh, empty socket and Delta has forgotten every subscription; skip the replay and
you get a healthy connection, zero messages, and a screen that quietly stops updating. So
`registry` is **never cleared** and is replayed in full on every open. It is keyed per
symbol rather than per message for the reason OpenAlgo gives: a message-keyed registry
replays a whole batch when one symbol inside it is rejected.

**Nothing here decodes anything, since #36.** This module used to turn a frame into a
`Quote` by calling `wire`, which meant the venue's array offsets were read by the socket
owner — so "the wire layout lives behind the adapter" was not true of the code. It now
publishes a `VenueMessage`: the frame verbatim, its channel, and the instant it arrived.
`adapters.delta.DeltaAdapter` is the only thing that reads inside one.

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

from pydantic import BaseModel

logger = logging.getLogger(__name__)

PUBLIC_WS = "wss://public-socket.india.delta.exchange"

#: Delta's two channel names. `bars.py` spells them again for its own `Tick.source`; the
#: adapter reads them from here, because this is the module that subscribes to them.
TICKER_CHANNEL = "ticker"
BOOK_CHANNEL = "ob_l2"

#: The only channels Delta's public endpoint accepts. It retired `v2/ticker`,
#: `l1_orderbook` and `l2_orderbook` on 31 July 2026 and now rejects them as invalid, so
#: the old names are refused here rather than producing a silently empty stream.
CHANNELS = (TICKER_CHANNEL, BOOK_CHANNEL)

HEARTBEAT_SECONDS = 30.0
RETRY_DELAY_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_RETRIES = 10


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


class Quote(BaseModel):
    """One contract's prices at one moment, from whichever channel carried them.

    **Retired by #37.** Nothing produces this on the live path any more: the adapter
    emits canonical events and `adapters.shim` rebuilds this record for the two consumers
    that have not moved yet. It stays here, unchanged, because those consumers and their
    tests are typed against it and this ticket is the expand half of an expand–contract.

    A consumer should never have to know that the bid is `q[2]` on one channel and
    `b[0][0]` on the other. `channel` travels with the record because the two refresh at
    very different rates and a consumer may reasonably care which it is looking at.

    `received_at` is a **wall clock** stamp, for #5 to store beside the quote. It is
    deliberately not a latency clock: `time.time()` can step backwards under an NTP
    correction, which would make `now - received_at` negative. Anything measuring elapsed
    time uses `timing.time_it`, which is built on `perf_counter`. If #6 needs a monotonic
    arrival stamp it should be a second field rather than a change of meaning here.
    """

    symbol: str
    channel: str
    bid: float | None = None
    ask: float | None = None
    received_at: float
    #: The frame this came from, verbatim. Carried because the screen shows more than a
    #: quote — mark, open interest, and Delta's Greeks and implied vols as reference
    #: columns — and re-decoding downstream would duplicate `wire`'s array offsets in a
    #: second place. Consumers that only want prices ignore it.
    frame: dict[str, Any] | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


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
        retry_delay: float = RETRY_DELAY_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        #: Anything with a non-blocking `publish`. Named `sink` rather than `fanout`
        #: since #36: the running engine passes the adapter's frame sink, and only
        #: `tests/test_feed.py` still hands this a `FanOut`.
        self.sink = sink
        self.url = url
        self.heartbeat_seconds = heartbeat_seconds
        self.retry_delay = retry_delay
        self.max_retries = max_retries
        self._connect = connect or self._default_connect

        #: Channel to symbols. **Never cleared.** This is the reconnect replay.
        self.registry: dict[str, set[str]] = {}
        self._stopping = False

        self.connections = 0
        self.consecutive_failures = 0
        self.messages = 0
        self.bytes_read = 0
        self.malformed = 0
        #: Attempts that opened a socket with an **empty registry**, and so were not
        #: announced as open. Counted rather than only logged because a feed whose every
        #: connection is empty is a feed nobody subscribed, and a counter stuck above
        #: zero is the signal that says so.
        self.empty_opens = 0
        self.started_at: float | None = None
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

    @staticmethod
    def _default_connect(url: str):
        import websockets

        return websockets.connect(url, open_timeout=20)

    def subscribe(self, channel: str, symbols: list[str]) -> None:
        """Register symbols. Safe before the socket exists.

        Accepting subscriptions before connecting removes a start-up race the caller
        would otherwise have to know about: they accumulate here and are sent on open.
        """
        if channel not in CHANNELS:
            raise ValueError(
                f"channel must be one of {', '.join(CHANNELS)}; got {channel!r}. "
                "Delta retired v2/ticker, l1_orderbook and l2_orderbook on 31 July 2026."
            )
        self.registry.setdefault(channel, set()).update(symbols)

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
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

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
        """Connect, pump, reconnect. Returns when stopped or the budget is exhausted."""
        self.started_at = time.perf_counter()
        delay = self.retry_delay

        while not self._stopping and self.consecutive_failures <= self.max_retries:
            # Read the counter across the whole attempt rather than taking a return
            # value from `_pump`: a dropped connection leaves `_pump` by raising, so a
            # returned flag is lost on exactly the path that matters most.
            before = self.messages
            try:
                async with self._connect(self.url) as socket:
                    self.connections += 1
                    await self._pump(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]

            # One attempt has ended, whether it ever opened or the dial failed outright.
            # The failed dial is reported too: a controller told only about sockets that
            # had opened would sit in `connecting` for the length of an endpoint outage,
            # which reads on a badge as "starting up". A stop is not a drop, so a stop is
            # not reported as one.
            if not self._stopping:
                self._tell(self._on_close, self.last_error or "closed by the venue")

            # Delivering data, not connecting, is what proves the endpoint works. Delta
            # can accept the handshake and close straight away — a rejected subscribe, a
            # throttled IP, an endpoint draining — and treating that as healthy resets
            # the budget every pass so the loop never gives up. Measured before this
            # was fixed: 21 attempts in 0.3s with max_retries=3, still going. At the
            # production one-second delay that exhausts the 150-per-5-minutes connection
            # budget in about two and a half minutes and keeps hammering.
            delivered = self.messages > before
            if delivered:
                self.last_error = None
            elif self.last_error is None:
                self.last_error = "connected but closed without delivering a message"

            if self._stopping:
                break
            if delivered:
                # This connection carried data, so the endpoint works. Restoring the
                # budget is what stops a feed that reconnects daily from exhausting a
                # lifetime allowance and never returning — OpenAlgo's recorded bug.
                self.consecutive_failures = 0
                delay = self.retry_delay
            else:
                self.consecutive_failures += 1
                delay = min(delay * 2, MAX_RETRY_DELAY_SECONDS)
            await asyncio.sleep(delay)
