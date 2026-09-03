"""The socket owner: one connection to Delta, decoded and fanned out.

Four jobs, and the third and fourth are where the real failures live.

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

Nothing here computes anything. It publishes decoded records; `fanout.FanOut` decides who
sees them and what happens when someone falls behind.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .wire import decode_ob_l2, decode_ticker

PUBLIC_WS = "wss://public-socket.india.delta.exchange"

#: The only channels Delta's public endpoint accepts. It retired `v2/ticker`,
#: `l1_orderbook` and `l2_orderbook` on 31 July 2026 and now rejects them as invalid, so
#: the old names are refused here rather than producing a silently empty stream.
CHANNELS = ("ticker", "ob_l2")

HEARTBEAT_SECONDS = 30.0
RETRY_DELAY_SECONDS = 1.0
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_RETRIES = 10


class Quote(BaseModel):
    """One contract's prices at one moment, from whichever channel carried them.

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

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


class DeltaFeed:
    """Owns the connection. Publishes `Quote`s to a `FanOut`."""

    def __init__(
        self,
        fanout,
        connect: Callable[[str], Any] | None = None,
        url: str = PUBLIC_WS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        retry_delay: float = RETRY_DELAY_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.fanout = fanout
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
        self.started_at: float | None = None
        #: Why the last connection ended. `None` means it has not ended yet. Without
        #: this a persistently failing feed is indistinguishable from a quiet healthy
        #: one: `messages` simply stops moving and nothing says why.
        self.last_error: str | None = None

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

    def _to_quote(self, message: dict[str, Any]) -> Quote | None:
        """One frame to one `Quote`, or `None` if it is control traffic."""
        kind = message.get("type")
        if kind == "ticker":
            symbol, leg = decode_ticker(message)
            return Quote(
                symbol=symbol,
                channel="ticker",
                bid=leg.bid,
                ask=leg.ask,
                received_at=time.time(),
            )
        if kind == "ob_l2":
            symbol, bid, ask = decode_ob_l2(message)
            return Quote(
                symbol=symbol,
                channel="ob_l2",
                bid=bid,
                ask=ask,
                received_at=time.time(),
            )
        # `subscriptions`, `error` and anything else is control traffic. Publishing it
        # would put a record with no prices on the bus.
        return None

    async def _pump(self, socket) -> None:
        """Read until the connection ends. Publishes; never computes."""
        payload = self._subscribe_payload()
        if payload is not None:
            await socket.send(payload)

        heartbeat = asyncio.create_task(self._heartbeat(socket))
        try:
            while not self._stopping:
                raw = await socket.recv()
                self.messages += 1
                self.bytes_read += len(raw)
                try:
                    message = json.loads(raw)
                    quote = self._to_quote(message)
                except Exception:
                    # One bad frame must not end ingestion. Counted, not swallowed —
                    # a malformed count that stays at zero is the useful signal.
                    self.malformed += 1
                    continue
                if quote is not None:
                    self.fanout.publish(quote)
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
