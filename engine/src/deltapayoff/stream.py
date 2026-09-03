"""The live chain: the latest frame each contract sent, rebuilt into a ladder on demand.

**This computes nothing.** It is a cache with a filter. One frame per `(channel, symbol)`
is kept, and building a chain hands the relevant ones to `wire.chain_from_frames` — the
same decoder the REST path's tests already cover.

Two things it adds. **Which frames belong to the chain a browser asked for**, since one
connection carries every listed expiry and every underlying while a chain screen shows
one of each. And **the answer that there is no chain yet**, which is not the same as an
empty one: a `ChainResponse` with no rows renders as a blank ladder and reads as "Delta
lists nothing", when the truth is that the socket has not spoken yet.

It sits behind the `FanOut`, so a slow render or a stalled browser cannot reach the
socket. Falling behind costs stale prices and nothing else — the cache only ever holds
the newest frame per contract anyway, which is exactly what a dropped older one was.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .chain import expiry_from_symbol
from .models import ChainResponse
from .wire import chain_from_frames


class ChainStream:
    """Latest frames in, a `ChainResponse` out."""

    def __init__(self) -> None:
        # `ticker` carries spot, mark, open interest and Delta's reference Greeks and
        # implied vols. `ob_l2` carries the top of book and nothing else, so the two are
        # kept apart rather than merged on arrival — `chain_from_frames` layers the
        # fresher book over the ticker when it builds.
        self._ticker: dict[str, dict[str, Any]] = {}
        self._book: dict[str, dict[str, Any]] = {}
        self._subscription = None
        self.applied = 0

    def attach(self, fanout, maxsize: int = 10_000, name: str = "chain-stream"):
        """Take a queue on the bus. `run` drains it."""
        self._subscription = fanout.subscribe(name, maxsize=maxsize)
        return self._subscription

    def apply(self, quote) -> None:
        """Record one quote's frame as the newest for its contract."""
        if quote.frame is None or not quote.symbol:
            return
        target = self._book if quote.channel == "ob_l2" else self._ticker
        target[quote.symbol] = quote.frame
        self.applied += 1

    async def run(self) -> None:
        """Drain the subscription forever. Cancel to stop."""
        if self._subscription is None:
            raise RuntimeError("attach() the stream to a FanOut before running it")
        while True:
            self.apply(await self._subscription.queue.get())

    def symbols(self, underlying: str, expiry: str) -> list[str]:
        """Contracts seen on `ticker` for this underlying and expiry, sorted."""
        prefix = f"-{underlying.upper()}-"
        return sorted(
            symbol
            for symbol in self._ticker
            if prefix in symbol and expiry_from_symbol(symbol) == expiry
        )

    def chain(self, underlying: str, expiry: str) -> ChainResponse | None:
        """The ladder for one underlying and expiry, or `None` if nothing has arrived.

        A row needs its `ticker` frame: `ob_l2` carries no spot, no Greeks and no open
        interest, so a chain built from books alone would render as mostly empty lines
        rather than as quotes. Books are layered on top of the tickers that exist.
        """
        wanted = set(self.symbols(underlying, expiry))
        if not wanted:
            return None

        return chain_from_frames(
            underlying.upper(),
            expiry,
            {symbol: self._ticker[symbol] for symbol in wanted},
            {
                symbol: frame
                for symbol, frame in self._book.items()
                if symbol in wanted
            },
            fetched_at=datetime.now(timezone.utc),
        )


async def pump_forever(stream: ChainStream) -> None:
    """`stream.run()` with cancellation treated as an ordinary shutdown."""
    try:
        await stream.run()
    except asyncio.CancelledError:
        pass
