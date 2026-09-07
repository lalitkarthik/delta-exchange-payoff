"""The live chain: the latest event each contract sent, rebuilt into a ladder on demand.

**This computes nothing.** It is a cache with a filter. One `md.option_quote` and one
`md.option_reference` are kept per instrument, one spot per underlying, and building a
chain folds the relevant ones into the same `ChainResponse` the REST path returns.

**It reads canonical events and knows no venue.** Until #37 it kept whole Delta frames,
keyed by `(channel, symbol)`, and handed them to `wire.chain_from_frames` — so the chain
cache knew that Delta had two channels and which of them carried spot. It now keys on the
canonical instrument and dispatches on the event type. `docs/design/lld/chain-cache.md` is
the design; `docs/design/events.md` is the contract for what arrives.

Two things it adds. **Which events belong to the chain a browser asked for**, since one
connection carries every listed expiry and every underlying while a chain screen shows one
of each. And **the answer that there is no chain yet**, which is not the same as an empty
one: a `ChainResponse` with no rows renders as a blank ladder and reads as "the venue
lists nothing", when the truth is that the socket has not spoken yet.

It sits behind the bus's drop-oldest queue, so a slow render or a stalled browser cannot
reach the socket. Falling behind costs stale prices and nothing else — the cache only ever
holds the newest event per contract anyway, which is exactly what a dropped older one was.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .chain import EXPIRY_FORMAT, chain_from_legs
from .compute import enrich
from .events import IndexQuote, Instrument, OptionQuote, OptionReference
from .models import ChainResponse, Leg

#: How often the recompute loop drains the dirty set. Frames arrive at ~1,323 a second
#: and a full pass over every listed expiry is ~10 ms of arithmetic, so at 100 ms the
#: ceiling is roughly 10% of one core while every number on screen stays at most a
#: tenth of a second old — against Delta's own 5,001 ms republish.
RECOMPUTE_INTERVAL_SECONDS = 0.1


def leg_from_events(
    instrument: Instrument,
    reference: OptionReference,
    quote: OptionQuote | None,
) -> Leg:
    """One contract's two events folded into the `Leg` the browser contract carries.

    **The book wins wholesale, and is never merged.** Where a `md.option_quote` exists and
    quotes either side, both its prices replace the reference event's pair rather than
    filling in beside them. `measured` by `tools/measure_feed.py` on a live 136-symbol
    chain: the book republishes every 508 ms against the reference's 5,001 ms and the
    two carry the same top of book, so taking the book's copy makes every price here 9.8x
    fresher — and taking one side from each would produce a spread neither channel ever
    quoted.

    `symbol` stays the **venue's** spelling, because `docs/chain-contract.md` is the
    engine↔web authority and the browser reads that field. The canonical string is the
    cache's key, not the payload's.
    """
    bid, ask = reference.bid, reference.ask
    if quote is not None and (quote.bid is not None or quote.ask is not None):
        bid, ask = quote.bid, quote.ask

    return Leg(
        symbol=instrument.venue_symbol or instrument.canonical(),
        product_id=reference.product_id,
        bid=bid,
        ask=ask,
        bid_iv=reference.bid_iv,
        ask_iv=reference.ask_iv,
        mark_iv=reference.mark_iv,
        mark=reference.mark,
        delta=reference.delta,
        gamma=reference.gamma,
        theta=reference.theta,
        vega=reference.vega,
        rho=reference.rho,
        oi=reference.oi,
        oi_value_usd=reference.oi_value_usd,
        oi_change_usd_6h=reference.oi_change_usd_6h,
        tick_size=reference.tick_size,
    )


class ChainStream:
    """Latest events in, a `ChainResponse` out."""

    def __init__(self) -> None:
        # Keyed by the canonical instrument string, and the instrument is kept beside its
        # reference event because the ladder needs the strike and the side and neither is
        # on the payload — they are on the envelope's `instrument`, typed, so nothing here
        # parses a symbol.
        self._reference: dict[str, tuple[Instrument, OptionReference]] = {}
        self._quote: dict[str, OptionQuote] = {}
        #: Spot per underlying, from `md.index_quote`. **Per underlying and not per
        #: contract**: spot is a property of BTC, not of the contract whose frame carried
        #: it, and the event says so by leaving `instrument` null.
        self._spot: dict[str, float] = {}
        self._subscription = None
        self.applied = 0
        #: Bus records this cache makes nothing of — anything that is not one of the three
        #: market-data events. Counted rather than ignored, because "the chain cache
        #: ignored most of the bus" should be a number and not a discovery.
        self.skipped = 0

        #: `(underlying, expiry)` pairs that have received an event since their last
        #: recompute. **Arrival is the only thing that schedules work.** A timer that
        #: recomputed regardless would burn a core reproducing unchanged numbers.
        self.dirty: set[tuple[str, str]] = set()
        #: The enriched chain per `(underlying, expiry)`. What `chain()` serves.
        self._computed: dict[tuple[str, str], ChainResponse] = {}
        self.recomputes = 0
        #: Passes that raised. A silent failure here would look exactly like a quiet
        #: market: the numbers simply stop moving and nothing says why.
        self.recompute_errors = 0

    def attach(self, bus, maxsize: int = 10_000, name: str = "chain-stream"):
        """Take a queue on the bus. `run` drains it."""
        self._subscription = bus.subscribe(name, maxsize=maxsize)
        return self._subscription

    def apply(self, event) -> None:
        """Record one event as the newest of its kind for its instrument.

        **Dispatch is on the event type**, which is what replaced a channel name here.
        An event carrying no instrument where one is required is dropped rather than
        keyed under nothing; `md.index_quote` carries none by design and names its
        underlying instead.
        """
        if isinstance(event, IndexQuote):
            if event.spot is not None:
                self._spot[event.underlying.upper()] = event.spot
                self.applied += 1
            return

        if isinstance(event, OptionReference):
            target: dict = self._reference
        elif isinstance(event, OptionQuote):
            target = self._quote
        else:
            self.skipped += 1
            return

        instrument = event.instrument
        if instrument is None:
            self.skipped += 1
            return

        key = instrument.canonical()
        target[key] = (instrument, event) if target is self._reference else event
        self.applied += 1
        self.dirty.add(_pair(instrument))

    async def run(self) -> None:
        """Drain the subscription forever. Cancel to stop."""
        if self._subscription is None:
            raise RuntimeError("attach() the stream to a bus before running it")
        while True:
            self.apply(await self._subscription.queue.get())

    def instruments(self, underlying: str, expiry: str) -> list[str]:
        """Canonical strings for the contracts seen on this underlying and expiry, sorted.

        **Canonical and not the venue's spelling**, because this is the cache's own key
        and a second venue's ladder is enumerated by the same call.
        """
        pair = (underlying.upper(), expiry)
        return sorted(
            key
            for key, (instrument, _) in self._reference.items()
            if _pair(instrument) == pair
        )

    def raw_chain(self, underlying: str, expiry: str) -> ChainResponse | None:
        """The ladder as the venue sent it, before enrichment. `None` if nothing arrived.

        **A row needs its reference event.** `md.option_quote` carries a top of book and
        nothing else — no mark, no open interest, no venue greeks — so a chain built from
        quotes alone would render as mostly empty lines rather than as a ladder. Quotes
        are layered over the references that exist, which is the same precedence the two
        channels had before the events replaced them.
        """
        keys = self.instruments(underlying, expiry)
        if not keys:
            return None

        legs = []
        for key in keys:
            instrument, reference = self._reference[key]
            leg = leg_from_events(instrument, reference, self._quote.get(key))
            legs.append((float(instrument.strike), instrument.right.side, leg))

        return chain_from_legs(
            underlying.upper(),
            expiry,
            legs,
            self._spot.get(underlying.upper()),
            fetched_at=datetime.now(timezone.utc),
        )

    def chain(self, underlying: str, expiry: str) -> ChainResponse | None:
        """The computed ladder for one underlying and expiry, or `None` if nothing yet.

        Serves the cache, but **recomputes whenever this expiry is dirty**. Correctness
        therefore never depends on the background loop having run: an event that arrived
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

        **What the store's table C is sampled from.** Our implied volatility and Greeks
        are not on the wire — they are made here, every 100 ms, and until #5 they lived
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
        """Recompute every expiry that has had an event since its last pass.

        Returns how many were recomputed. Synchronous and CPU-bound by design — it is
        called from its own task, never from the socket reader, and a full pass over
        every listed expiry is milliseconds of arithmetic.

        **A key that fails goes back on the dirty set**, and one failure does not
        abandon the rest of the pass. Clearing the set up front and letting an exception
        escape would drop every remaining expiry silently: they would leave `dirty`
        while `_computed` still held their old chains, so `chain()` would serve that
        stale cache indefinitely on any expiry that received no further events. A
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


def _pair(instrument: Instrument) -> tuple[str, str]:
    """`(underlying, expiry)` in the spelling the routes and the browser use.

    The expiry is a calendar `date` on the instrument and `DD-MM-YYYY` on the wire, and
    the conversion is here rather than at every call site so there is one place it can be
    wrong. It is the venue's format only by coincidence: `docs/chain-contract.md` fixes it
    as the engine↔web spelling.
    """
    return instrument.underlying.upper(), instrument.expiry.strftime(EXPIRY_FORMAT)


async def recompute_forever(
    stream: ChainStream, interval: float = RECOMPUTE_INTERVAL_SECONDS
) -> None:
    """Drain the dirty set on a fixed tick until cancelled.

    **This runs in its own task, never in the socket reader.** `recompute_dirty` is
    synchronous CPU work, so calling it from the reader would stop us draining the
    connection while it ran — the operating system's receive buffer would fill and the
    venue would close us, with nothing having enforced a limit. The fan-out exists to keep
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
