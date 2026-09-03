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
from .compute import enrich
from .models import ChainResponse
from .wire import chain_from_frames

#: How often the recompute loop drains the dirty set. Frames arrive at ~1,323 a second
#: and a full pass over every listed expiry is ~10 ms of arithmetic, so at 100 ms the
#: ceiling is roughly 10% of one core while every number on screen stays at most a
#: tenth of a second old — against Delta's own 5,001 ms republish.
RECOMPUTE_INTERVAL_SECONDS = 0.1


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

        #: `(underlying, expiry)` pairs that have received a frame since their last
        #: recompute. **Arrival is the only thing that schedules work.** A timer that
        #: recomputed regardless would burn a core reproducing unchanged numbers.
        self.dirty: set[tuple[str, str]] = set()
        #: The enriched chain per `(underlying, expiry)`. What `chain()` serves.
        self._computed: dict[tuple[str, str], ChainResponse] = {}
        self.recomputes = 0
        #: Passes that raised. A silent failure here would look exactly like a quiet
        #: market: the numbers simply stop moving and nothing says why.
        self.recompute_errors = 0

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

        key = self._key_for(quote.symbol)
        if key is not None:
            self.dirty.add(key)

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

    def raw_chain(self, underlying: str, expiry: str) -> ChainResponse | None:
        """The ladder as Delta sent it, before enrichment. `None` if nothing has arrived.

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

    @staticmethod
    def _key_for(symbol: str) -> tuple[str, str] | None:
        """`C-BTC-77600-040926` to `("BTC", "04-09-2026")`, or `None` if unparseable.

        A frame carries neither underlying nor expiry as a field; both live only in the
        symbol. An unparseable symbol marks nothing dirty rather than raising — one odd
        frame must not stop ingestion.
        """
        parts = (symbol or "").split("-")
        expiry = expiry_from_symbol(symbol)
        if len(parts) < 4 or expiry is None:
            return None
        return parts[1].upper(), expiry

    def chain(self, underlying: str, expiry: str) -> ChainResponse | None:
        """The computed ladder for one underlying and expiry, or `None` if nothing yet.

        Serves the cache, but **recomputes whenever this expiry is dirty**. Correctness
        therefore never depends on the background loop having run: a frame that arrived
        a millisecond ago is reflected in the very next call, and a caller that reads
        faster than the loop still sees current prices rather than the last pass's.

        The loop is what keeps this a cache hit almost always — it computes once for
        every reader rather than once per reader — and what bounds staleness for a
        screen nobody is currently looking at. It is an optimisation, not the mechanism.
        """
        key = (underlying.upper(), expiry)
        cached = self._computed.get(key)
        if cached is not None and key not in self.dirty:
            return cached

        computed = self._compute(key)
        if computed is not None:
            self._computed[key] = computed
            self.dirty.discard(key)
        return computed

    def computed_chains(self) -> list[ChainResponse]:
        """Every chain the recompute loop has produced, as it currently stands.

        **What #5's table C is sampled from.** Our implied volatility and Greeks are not
        on the wire — they are made here, every 100 ms, and until this ticket they lived
        exactly as long as the process did. The bar writer reads this once a minute and
        stores the result beside the quote bars for the same minute.

        **Deliberately not `chain()`.** That method recomputes a dirty expiry
        synchronously so a reader never sees a stale ladder; calling it from the writer's
        drain loop would move a chain build onto a pass that has to stay short and would
        duplicate work `recompute_forever` is already doing. This hands back what has
        *already* been computed, which is also exactly what "the state the screen was
        showing" means.

        A **list**, not the live dictionary. The writer walks it while this loop may be
        replacing entries, and a dict mutated during iteration raises. Each value is a
        `ChainResponse` that recompute *replaces* rather than mutates, so a snapshot of
        references is stable for as long as the caller holds it.

        Every chain carries the instant it was computed in `fetched_at`, which is what
        lets a chain the loop has stopped refreshing be recognised as stale rather than
        stored again — see `bars.ComputedAggregator`.
        """
        return list(self._computed.values())

    def _compute(self, key: tuple[str, str]) -> ChainResponse | None:
        """Build the raw chain for `key` and enrich it. `None` if nothing has arrived."""
        raw = self.raw_chain(*key)
        return None if raw is None else enrich(raw)

    def recompute_dirty(self) -> int:
        """Recompute every expiry that has had a frame since its last pass.

        Returns how many were recomputed. Synchronous and CPU-bound by design — it is
        called from its own task, never from the socket reader, and a full pass over
        every listed expiry is milliseconds of arithmetic.

        **A key that fails goes back on the dirty set**, and one failure does not
        abandon the rest of the pass. Clearing the set up front and letting an exception
        escape would drop every remaining expiry silently: they would leave `dirty`
        while `_computed` still held their old chains, so `chain()` would serve that
        stale cache indefinitely on any expiry that received no further frames. A
        screen showing last minute's volatility with nothing to say so is exactly the
        plausible-and-wrong failure this project keeps refusing.
        """
        if not self.dirty:
            return 0

        # Taken as a snapshot: `apply` may add to the set while this runs, and those
        # arrivals belong to the *next* pass rather than being silently cleared by it.
        pending, self.dirty = self.dirty, set()
        recomputed = 0
        for key in pending:
            try:
                computed = self._compute(key)
            except Exception:
                # Put it back so the next pass retries, and count it. A chain that
                # cannot be enriched is a fact worth surfacing, not a reason to stop.
                self.dirty.add(key)
                self.recompute_errors += 1
                continue
            if computed is not None:
                self._computed[key] = computed
                recomputed += 1
        self.recomputes += recomputed
        return recomputed


async def recompute_forever(
    stream: ChainStream, interval: float = RECOMPUTE_INTERVAL_SECONDS
) -> None:
    """Drain the dirty set on a fixed tick until cancelled.

    **This runs in its own task, never in the socket reader.** `recompute_dirty` is
    synchronous CPU work, so calling it from the reader would stop us draining the
    connection while it ran — the operating system's receive buffer would fill and Delta
    would close us, with nothing having enforced a limit. The fan-out exists to keep
    those two apart and this is the second consumer it was built for.

    An exception here must not kill the loop. `recompute_dirty` already isolates a
    single failing chain and re-queues it, so this guard is for the unexpected — but a
    loop that dies leaves the screen frozen with no error, which is worse than a loop
    that retries something hopeless.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            stream.recompute_dirty()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the loop must outlive anything
            stream.recompute_errors += 1


async def pump_forever(stream: ChainStream) -> None:
    """`stream.run()` with cancellation treated as an ordinary shutdown."""
    try:
        await stream.run()
    except asyncio.CancelledError:
        pass
