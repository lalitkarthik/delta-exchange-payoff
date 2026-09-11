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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import log_events
from .chain import EXPIRY_FORMAT, chain_from_legs
from .compute import enrich
from .events import IndexQuote, Instrument, OptionQuote, OptionReference
from .logging_setup import log_event
from .models import ChainResponse, Leg

logger = logging.getLogger(__name__)

#: How often the recompute loop drains the dirty set. Frames arrive at ~1,323 a second
#: and a full pass over every listed expiry is ~10 ms of arithmetic, so at 100 ms the
#: ceiling is roughly 10% of one core while every number on screen stays at most a
#: tenth of a second old — against Delta's own 5,001 ms republish.
RECOMPUTE_INTERVAL_SECONDS = 0.1

#: How long a pair keeps being solved after its last viewer leaves. `assumed` 30 s: long
#: enough that flipping between two expiries — the thing a trader does constantly — never
#: waits for a first solve, short enough that a closed tab stops costing anything within
#: the minute. No measurement fixes it; it is a judgement about how people use the screen.
GRACE_SECONDS = 30.0

#: The second cadence. Once a minute every dirty expiry is solved whether or not anyone
#: is watching, because the store's table C — and therefore the volatility screen — must
#: cover every listed expiry regardless of who has a browser open. `derived` from the
#: bar grain: one row a minute per contract is what the table holds, so one pass a minute
#: is exactly enough and any more would be discarded by `bars.ComputedAggregator`'s fold.
MINUTE_PASS_INTERVAL_SECONDS = 60.0

#: How far **before** the minute boundary the minute pass runs. The chain it produces is
#: stamped with the instant it was computed and bucketed on that stamp, so a pass that
#: ran at the boundary would land its rows in the minute that is just *opening* rather
#: than the one it means to close — and the writer may already have sealed the closing
#: one by then. Half a second is `assumed`: comfortably longer than a full pass over
#: sixteen expiries (see `docs/design/lld/chain-cache.md` §7) and far short of a minute.
MINUTE_PASS_LEAD_SECONDS = 0.5


@dataclass
class Watch:
    """One `(underlying, expiry)` pair's interest: how many browsers, and since when not.

    **A count and not a flag.** Two tabs on one expiry are two viewers, and the first of
    them closing must not stop the solve for the second — which a boolean cannot express
    and which is the ordinary case, since a trader with the chain open in two windows is
    not unusual.

    `released_at` is set when the count reaches zero and cleared when it leaves zero. It
    is a monotonic reading, because it is only ever used as the start of a duration.
    """

    viewers: int = 0
    released_at: float | None = None


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

    def __init__(self, grace_seconds: float = GRACE_SECONDS) -> None:
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
        #: Underlying to the expiries this cache has seen contracts for. Kept so a spot
        #: that moves can mark them dirty without walking every instrument; see `apply`.
        self._expiries: dict[str, set[str]] = {}
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

        #: Interest per pair — #44. What the **live** pass solves, and what `/health`
        #: reports. A pair with no entry here is solved once a minute and not otherwise.
        self._watch: dict[tuple[str, str], Watch] = {}
        self.grace_seconds = grace_seconds
        #: Wall clock of the newest arrival per pair, used by the minute pass to decide
        #: which pairs had a frame **inside the minute it is closing**. Wall and not
        #: monotonic because it is compared against a minute boundary, which is a
        #: wall-clock fact. One `time.time()` and one dict write per message: `derived`
        #: ~100 ns against the `measured` 1,323 messages a second the feed delivers.
        self._arrived_at: dict[tuple[str, str], float] = {}
        #: Minute passes run, and how many pairs each of them skipped as having had no
        #: arrival in the minute being closed. Counted because "the minute pass quietly
        #: stopped covering half the board" must be readable rather than discovered.
        self.minute_passes = 0
        self.minute_pass_skipped = 0

        #: **Two clocks, deliberately.** The grace window is a duration and is measured
        #: on `monotonic`, which cannot step backwards under an NTP correction and make a
        #: window that has not elapsed look elapsed. Arrival times are compared against a
        #: minute boundary, which only exists on the wall clock. Both are attributes so a
        #: test drives them rather than sleeping.
        self.clock: Callable[[], float] = time.monotonic
        self.wall: Callable[[], float] = time.time

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
            if event.spot is None:
                # An absent spot is not an observation of absence, and it is still a
                # record this cache made nothing of.
                self.skipped += 1
                return
            underlying = event.underlying.upper()
            moved = self._spot.get(underlying) != event.spot
            self._spot[underlying] = event.spot
            self.applied += 1
            if moved:
                # **A spot that moves schedules work, and this is not optional.** Spot
                # sets the ATM strike, the forward and therefore every implied volatility
                # on the ladder, so an expiry left clean would keep serving a chain priced
                # against the previous spot for as long as no contract of its own ticked.
                # `md.index_quote` carries no instrument by design — it is a fact about
                # the underlying — so nothing else marks it.
                #
                # **On change and not on arrival**, because this event now arrives once
                # per reference frame (`derived` ~118 a second) while spot moves far less
                # often. Marking on arrival would make every expiry dirty on every pass
                # and burn a core reproducing numbers that had not changed, which is the
                # thing the dirty set exists to prevent.
                self.dirty.update(
                    (underlying, expiry)
                    for expiry in self._expiries.get(underlying, ())
                )
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
        pair = _pair(instrument)
        expiries_for_underlying = self._expiries.setdefault(pair[0], set())
        # **Debug, and deliberately not on every event.** Every one of ~1,323 messages a
        # second touches `dirty`, which #42 explicitly rules off the per-message path.
        # The set of expiries this cache has ever seen a contract for changes only when
        # one lists for the first time — rare, and the fact worth a quiet line rather
        # than a flood: `compute.recompute_set`.
        if pair[1] not in expiries_for_underlying:
            log_event(
                logger,
                logging.DEBUG,
                log_events.COMPUTE_RECOMPUTE_SET,
                "%s %s joins the recompute set",
                pair[0],
                pair[1],
                instrument=key,
            )
        expiries_for_underlying.add(pair[1])
        self.dirty.add(pair)
        # **When**, not just that. The minute pass closes one minute and must not write a
        # row into a minute that had no frames — see `recompute_closing_minute`.
        self._arrived_at[pair] = self.wall()

    # ---------------------------------------------------------------- interest

    def watch(self, underlying: str, expiry: str) -> int:
        """Register one viewer's interest in a pair. Returns the new count.

        Called by `/ws/chain` on accept. **The count is the mechanism**: a second tab on
        the same expiry must not be able to stop the first one's solve when it closes.

        Re-entering a pair inside its grace window clears the release rather than
        starting a second timer, which is what makes flipping back instant — the cached
        ladder was never allowed to go stale in the first place.
        """
        pair = (underlying.upper(), expiry)
        entry = self._watch.get(pair)
        if entry is None:
            entry = self._watch[pair] = Watch()
        entry.viewers += 1
        entry.released_at = None
        if entry.viewers == 1:
            self._log_recompute_set(pair, "watched")
        return entry.viewers

    def unwatch(self, underlying: str, expiry: str) -> int:
        """Release one viewer's interest. Returns the count left.

        At zero the pair is **not** dropped: the grace clock starts, and the live pass
        keeps solving it until the window elapses. A release with no matching `watch` is
        ignored rather than driving the count negative — the handler's `finally` runs on
        every exit path including ones that never registered, and a negative count would
        make a later `watch` fail to start the solve.
        """
        pair = (underlying.upper(), expiry)
        entry = self._watch.get(pair)
        if entry is None or entry.viewers == 0:
            return 0
        entry.viewers -= 1
        if entry.viewers == 0:
            entry.released_at = self.clock()
            self._log_recompute_set(pair, "released")
        return entry.viewers

    def _grace_remaining(self, entry: Watch, now: float) -> float | None:
        """Seconds of grace left, or `None` if it is watched or the grace has elapsed."""
        if entry.viewers > 0 or entry.released_at is None:
            return None
        left = self.grace_seconds - (now - entry.released_at)
        return left if left > 0 else None

    def _solved_live(self, pair: tuple[str, str], now: float) -> bool:
        """Whether the live pass owes this pair a solve on every tick."""
        entry = self._watch.get(pair)
        if entry is None:
            return False
        return entry.viewers > 0 or self._grace_remaining(entry, now) is not None

    def watching(
        self, now: float | None = None
    ) -> list[tuple[str, str, int, float | None]]:
        """The watched set for `/health`: pair, viewers, and grace left if in grace.

        **Read-only, and it drops nothing.** A report is not the place to expire a
        window — a monitor polling `/health` twice a second would then be the thing
        driving the recompute set, and one that stopped polling would leave expired
        entries alive. Expiry belongs to the live pass; this filters.
        """
        now = self.clock() if now is None else now
        out: list[tuple[str, str, int, float | None]] = []
        # Keyed explicitly: sorting bare `(pair, Watch)` tuples would fall through
        # to comparing two `Watch` objects on a tie, and `Watch` is not orderable.
        # Dict keys cannot tie, so it is safe today and would stop being safe the
        # day this is keyed by anything else.
        for (underlying, expiry), entry in sorted(
            self._watch.items(), key=lambda item: item[0]
        ):
            if entry.viewers > 0:
                out.append((underlying, expiry, entry.viewers, None))
                continue
            remaining = self._grace_remaining(entry, now)
            if remaining is not None:
                out.append((underlying, expiry, 0, round(remaining, 3)))
        return out

    def prune_watches(self, now: float | None = None) -> int:
        """Drop every pair whose grace has elapsed. Returns how many went.

        Called from the live pass, so the set shrinks on the engine's own clock and not
        on whether anyone asked `/health`.
        """
        now = self.clock() if now is None else now
        expired = [
            pair
            for pair, entry in self._watch.items()
            if entry.viewers == 0 and self._grace_remaining(entry, now) is None
        ]
        for pair in expired:
            del self._watch[pair]
            self._log_recompute_set(pair, "grace elapsed")
        return len(expired)

    def _log_recompute_set(self, pair: tuple[str, str], why: str) -> None:
        """One debug line whenever the set the live pass solves changes.

        Debug and not info: a browser opening a tab is routine, and #42 rules routine
        volume off info. Not on a per-message path — this fires once per connection per
        end, against the `measured` 1,323 messages a second the feed delivers.
        """
        log_event(
            logger,
            logging.DEBUG,
            log_events.COMPUTE_RECOMPUTE_SET,
            "%s %s %s",
            pair[0],
            pair[1],
            why,
        )

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

    def expiries(self, underlying: str) -> list[str]:
        """The distinct expiries represented by reference events, date-sorted."""
        symbol = underlying.upper()
        dates = {
            instrument.expiry
            for instrument, _event in self._reference.values()
            if instrument.underlying.upper() == symbol
        }
        return [expiry.strftime(EXPIRY_FORMAT) for expiry in sorted(dates)]

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
        # **Read off the first instrument, not a constant.** `keys` is non-empty here
        # (checked above), and every contract on one underlying and expiry was built
        # by the same adapter, so any one of them answers for the chain's currency.
        # Taking it from the event rather than a hard-coded default is what keeps this
        # cache venue-neutral the way its own module docstring promises — a second
        # venue's instruments would carry their own `quote_currency` with nothing here
        # to change.
        quote_currency = self._reference[keys[0]][0].quote_currency
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
            quote_currency=quote_currency,
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

    def live_computed_chains(self) -> list[ChainResponse]:
        """The computed ladders for the pairs the **live** pass is keeping fresh.

        What the bar writer's periodic sample takes, since #44. `computed_chains()`
        beside it still hands back everything and is what a reader wanting the whole
        cache should call.

        **Why the writer takes the narrower list.** Its sample runs six times a minute
        and buckets each ladder on the instant that ladder was computed. An expiry
        nobody is watching is now solved once a minute, so five of those six samples
        would meet the same ladder again, in a minute already sealed, and each would be
        counted as `late` — thousands a minute, on a counter whose whole job is to say
        how many real observations were lost. The unwatched board reaches the table
        through the minute pass instead, which hands its ladders over directly.

        A ladder that stops being refreshed while its pair is still watched is still
        sampled and still refused as late, which is exactly the frozen-cache detection
        this store already had.
        """
        now = self.clock()
        return [
            chain
            for key, chain in self._computed.items()
            if self._solved_live(key, now)
        ]

    def _compute(self, key: tuple[str, str]) -> ChainResponse | None:
        """Build the raw chain for `key` and enrich it. `None` if nothing has arrived."""
        raw = self.raw_chain(*key)
        return None if raw is None else enrich(raw)

    def recompute_dirty(self) -> int:
        """Recompute every expiry that has had an event since its last pass.

        Returns how many were recomputed. Synchronous and CPU-bound by design — it is
        called from its own task, never from the socket reader, and a full pass over
        every listed expiry is milliseconds of arithmetic.

        **This is no longer what the live pass calls.** Since #44 the 100 ms loop calls
        `recompute_watched`, which does this for the pairs a browser is looking at; this
        is the whole-board pass, and it is what the minute cadence is built on.
        """
        if not self.dirty:
            return 0

        # Taken as a snapshot: `apply` may add to the set while this runs, and those
        # arrivals belong to the *next* pass rather than being silently cleared by it.
        pending, self.dirty = self.dirty, set()
        return self._drain(pending)

    def recompute_watched(self, now: float | None = None) -> int:
        """The live pass. Recompute only the dirty pairs somebody is looking at.

        **Work now scales with viewers rather than with the venue's listing.** Before
        #44 this loop solved every listed expiry on every tick whether or not any
        browser existed — sixteen of them once ETH joined, on a 100 ms tick, forever.
        An expiry nobody is watching now costs one solve a minute, taken by
        `recompute_closing_minute` so the record still covers it.

        A pair inside its grace window counts as watched, which is what makes flipping
        back to an expiry instant rather than a first solve.

        **An unwatched dirty pair stays dirty.** It is not solved and it is not cleared:
        the minute pass needs it still marked, and `chain()` needs it marked so a reader
        asking for an expiry nobody registered gets a fresh ladder rather than a cached
        one. Only the pairs actually solved here leave the set.
        """
        now = self.clock() if now is None else now
        self.prune_watches(now)
        if not self.dirty:
            return 0
        pending = {key for key in self.dirty if self._solved_live(key, now)}
        if not pending:
            return 0
        self.dirty -= pending
        return self._drain(pending)

    def recompute_closing_minute(
        self, now: float | None = None
    ) -> list[ChainResponse]:
        """The minute pass. Solve every dirty expiry that had a frame **this minute**.

        Returns the ladders it produced, for the caller to hand to the computed-bars
        aggregator. It runs whether or not a browser is open: the volatility screen
        reads a day of stored implied volatility in `measured` 6.8 ms where solving that
        day on demand takes seconds, and that store has to be written by something that
        does not depend on anyone looking.

        **The minute filter is the no-forward-fill rule, kept.** A ladder is bucketed on
        the instant it was computed, so a pass that solved every dirty pair regardless
        would write a row into the minute it is running in for an expiry whose last
        frame arrived in an earlier one — one manufactured bar per expiry every time a
        feed goes quiet. A pair whose newest arrival is older than this minute is
        therefore **skipped and left dirty**: not solved, not cleared, and counted. A
        later pass picks it up when it has an arrival of its own, and until then the
        store simply has nothing to say about it, which is the truth.

        Watched pairs are ordinarily clean by the time this runs — the 100 ms loop has
        already solved them — so in practice this pass is the unwatched board.
        """
        now = self.wall() if now is None else now
        self.minute_passes += 1
        if not self.dirty:
            return []

        minute_start = now - (now % MINUTE_PASS_INTERVAL_SECONDS)
        pending = set()
        for key in self.dirty:
            if self._arrived_at.get(key, 0.0) >= minute_start:
                pending.add(key)
            else:
                self.minute_pass_skipped += 1
        if not pending:
            return []

        self.dirty -= pending
        self._drain(pending)
        return [self._computed[key] for key in pending if key in self._computed]

    def _drain(self, pending: set[tuple[str, str]]) -> int:
        """Solve each key in `pending`, caching what succeeds. Returns how many did.

        **A key that fails goes back on the dirty set**, and one failure does not
        abandon the rest of the pass. Clearing the set up front and letting an exception
        escape would drop every remaining expiry silently: they would leave `dirty`
        while `_computed` still held their old chains, so `chain()` would serve that
        stale cache indefinitely on any expiry that received no further events. A
        screen showing last minute's volatility with nothing to say so is exactly the
        plausible-and-wrong failure this project keeps refusing.
        """
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
            stream.recompute_watched()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the loop must outlive anything
            stream.recompute_errors += 1


async def recompute_every_minute(
    stream: ChainStream,
    sink: Callable[[list[ChainResponse]], Any] | None = None,
    interval: float = MINUTE_PASS_INTERVAL_SECONDS,
    lead: float = MINUTE_PASS_LEAD_SECONDS,
) -> None:
    """The second cadence: close each minute for every expiry, watched or not.

    **This is the task that makes the volatility screen independent of the chain page.**
    The live pass follows viewers; nothing followed the record until this existed, and
    with the live pass narrowed to watched pairs an expiry nobody had open would simply
    stop appearing in the store.

    `sink` is handed the ladders the pass produced and is where they reach
    `bars.BarWriter`. **Handed rather than left to be sampled**, and that is not
    decoration: the writer's own sampling runs on its drain loop, which may seal the
    closing minute before it next looks, so a ladder computed for minute *M* could be
    refused as late and the minute would silently carry no row. Handing it over in the
    same call puts the tick in the bucket the pass meant it for.

    **Aligned to the wall clock, and early.** It wakes `lead` seconds before each minute
    boundary so the ladder it produces is stamped inside the minute it is closing. A
    plain `sleep(60)` would drift, and drift here moves rows between minutes.
    """
    while True:
        now = stream.wall()
        delay = interval - (now % interval) - lead
        if delay <= 0:
            delay += interval
        await asyncio.sleep(delay)
        try:
            produced = stream.recompute_closing_minute()
            if sink is not None and produced:
                sink(produced)
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
