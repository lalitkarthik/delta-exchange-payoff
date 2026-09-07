"""Delta Exchange India, behind the adapter protocol.

**Everything venue-specific is in this file or in the three modules it owns** — the socket
(`delta_socket.py`), the REST client (`delta_client.py`) and the wire layout (`wire.py`).
What leaves here is canonical: `Instrument`s and catalogued `Event`s, never Delta JSON.

Three mappings, and they are the whole of it:

    ob_l2  frame  ->  md.option_quote
    ticker frame  ->  md.option_reference
    ticker frame  ->  md.index_quote      one per frame, since #37

**`null` is not `0`, and this is the boundary that holds it.** Delta spells an absent
quote three ways — `"0"`, `""` and `null` — and all three become `None` on a price, a size
or an implied volatility, because rendering one as `0.0` would claim somebody bid zero.
A real zero in open interest or in a greek stays `0.0`, because zero is a true value for
those. `convert.to_quote_number` and `convert.to_number` are that split, and `wire.py`
already applies the right one field by field; the adapter's job is to be the only place
where the venue's spelling is read at all. The events themselves cannot enforce this — a
string `"0"` handed to a pydantic `float` field is coerced to `0.0` — which is exactly why
`docs/design/lld/events.md` assigns the rule here.

**A non-finite number is treated as absent, and counted.** `Event` refuses `NaN` and
`Infinity` outright, because pydantic would otherwise serialise them to JSON `null` and a
garbage number would arrive downstream indistinguishable from a quote that was never
there. If that refusal reached the socket reader it would end the connection, so the
adapter converts a non-finite value to `None` *before* building the event and increments
`non_finite`. Python's `json.loads` accepts the bare tokens `NaN` and `Infinity`, which
standard JSON does not, so this is reachable from a venue and not merely defensive.
`convert.to_number` already maps the *string* spellings — `"nan"`, `"null"`, `"-"` — to
`None`; this closes the float-shaped hole beside it.

**Reconnect stays here for this ticket.** Backoff, the lifetime budget, subscription
replay and the reason a connection ended all live in `delta_socket.DeltaFeed`, which this
class owns and drives. #38 lifts them into a connection controller wrapped around the
protocol. Moving them early would mean writing the state machine twice.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from pydantic import ValidationError

from ..chain import build_chain, build_expiries
from ..delta_client import DeltaClient
from ..events import Event, IndexQuote, Instrument, OptionQuote, OptionReference, Right
from ..models import ChainResponse, ExpiriesResponse
from ..wire import decode_ob_l2_top, decode_ticker, decode_ticker_extras
from .base import ConnectionListener, ConnectionSignal, Publish
from .delta_socket import BOOK_CHANNEL, TICKER_CHANNEL, DeltaFeed, VenueMessage

logger = logging.getLogger(__name__)

#: The venue's short name. Every `Instrument` this adapter builds carries it, and it is
#: the `source` on every event, so a log line and a cache key agree without a lookup.
VENUE = "DELTA"

#: Delta's symbol is `RIGHT-UNDERLYING-STRIKE-DDMMYY`, e.g. `C-BTC-77600-040926`. Four
#: parts, and the expiry is the only place a frame carries a date at all.
_SYMBOL_PARTS = 4
_EXPIRY_FORMAT = "%d%m%y"


def instrument_from_symbol(symbol: str, venue: str = VENUE) -> Instrument | None:
    """Delta's symbol to a canonical `Instrument`, or `None` if it is not one.

    **`None` rather than an exception**, and counted by the caller, for the reason
    `bars.py` gives about the same parse: one odd symbol must not end ingestion, and a
    count that stays at zero is the useful signal. The canonical string's own parser
    raises instead, because there the string is ours and a bad one is a bug.

    `%y` reads `26` as 2026 — Python's cutoff is 69 — which is right until Delta lists a
    contract expiring after 2068.
    """
    parts = (symbol or "").split("-")
    if len(parts) != _SYMBOL_PARTS:
        return None
    right_text, underlying, strike_text, expiry_text = parts
    try:
        right = Right(right_text)
        expiry: date = datetime.strptime(expiry_text, _EXPIRY_FORMAT).date()
        strike = Decimal(strike_text)
    except (ValueError, InvalidOperation):
        return None
    # `Decimal("NaN")` and `Decimal("Infinity")` parse without raising and are not
    # strikes. Refused before the record is built, as `Instrument.from_canonical` does.
    if not strike.is_finite():
        return None
    try:
        return Instrument(
            venue=venue,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            venue_symbol=symbol,
        )
    except ValidationError:
        # An empty or hyphenated `underlying` — neither reachable from a four-part split,
        # but the record is the authority on what it accepts and not this function.
        return None


class _FrameSink:
    """What `DeltaFeed` publishes into. One adapter, one sink, and not a bus.

    It has `publish` because that is the socket owner's whole contract — synchronous,
    never blocking — and it is a named object rather than a lambda so a traceback from
    inside the decode says where it came from.
    """

    __slots__ = ("_adapter",)

    def __init__(self, adapter: DeltaAdapter) -> None:
        self._adapter = adapter

    def publish(self, message: VenueMessage) -> None:
        self._adapter.handle(message)


class DeltaAdapter:
    """Delta Exchange India. Implements `adapters.base.Adapter`."""

    def __init__(
        self,
        client: DeltaClient | None = None,
        *,
        underlyings: Sequence[str] = ("BTC",),
        feed_factory: Any = DeltaFeed,
        **feed_kwargs: Any,
    ) -> None:
        """`feed_factory` is the socket owner's constructor, called with the sink.

        It is a parameter rather than a hard reference so the application module keeps the
        seam its lifespan tests already drive — they replace `main.DeltaFeed` with a stub
        that registers subscriptions and never dials out. `connect=` and the retry
        settings pass through in `feed_kwargs`, which is the seam `tests/test_feed.py`
        drives.
        """
        self._client = client if client is not None else DeltaClient()
        self._underlyings = tuple(name.strip().upper() for name in underlyings if name)
        self._sink = _FrameSink(self)
        self._feed = feed_factory(self._sink, **feed_kwargs)
        self._publish: Publish | None = None

        #: `(listener, on_open, on_close)` per `on_connection` call. The two closures
        #: are the only handles on what was put on the feed, and `off_connection` is
        #: handed nothing but the listener, so the pair is kept beside it.
        self._translated: list[tuple[Any, Any, Any]] = []

        #: Events handed to `publish`. The adapter's own throughput, independent of the
        #: socket's message count, because one ticker frame is two events and a book
        #: frame is one.
        self.emitted = 0
        #: Frames that parsed as JSON and then made no sense — the count `feed.malformed`
        #: used to carry before the decode moved here. Never zero-by-omission: a frame
        #: that raises anywhere in the decode lands here and is dropped whole.
        self.undecodable = 0
        #: Symbols that are not Delta option symbols, from a frame's `sy` or from a row
        #: of the venue's listing. Such a frame produces no events at all, because an
        #: event with no instrument would be a quote about nothing.
        self.unparseable_symbols = 0
        #: Numbers that arrived as `NaN` or `Infinity` and were carried as absent.
        self.non_finite = 0

    # --- describe itself ---------------------------------------------------------

    @property
    def venue(self) -> str:
        return VENUE

    @property
    def underlyings(self) -> tuple[str, ...]:
        return self._underlyings

    @property
    def feed(self) -> Any:
        """The socket owner, for the counters `/health` will read in #39."""
        return self._feed

    # --- the feed ----------------------------------------------------------------

    async def instruments(self, underlying: str) -> list[Instrument]:
        """Every BTC (or ETH) option Delta lists, as canonical instruments.

        Read from `/v2/tickers` with no expiry filter, which is the same call `/expiries`
        makes — Delta has no instrument-listing endpoint, so the ticker snapshot is the
        listing.
        """
        rows = await self._client.tickers(underlying, None)
        instruments = []
        for row in rows:
            instrument = instrument_from_symbol(row.get("symbol") or "")
            if instrument is None:
                self.unparseable_symbols += 1
                continue
            instruments.append(instrument)
        return instruments

    def subscribe(self, instruments: Iterable[Instrument]) -> None:
        """Register these contracts on **both** channels.

        Both, always, and not narrowed to a watched expiry: `docs/ingestion.md` records
        that the narrowing was built and reverted, and the full subscription is what buys
        instant expiry switching with no subscribe round trip. `ob_l2` carries everything
        the pricing needs and refreshes 9.8x faster than `ticker`; `ticker` carries spot,
        open interest and Delta's own reference columns.
        """
        symbols = [
            instrument.venue_symbol
            for instrument in instruments
            if instrument.venue_symbol
        ]
        if not symbols:
            return
        self._feed.subscribe(TICKER_CHANNEL, symbols)
        self._feed.subscribe(BOOK_CHANNEL, symbols)

    def on_connection(self, listener: ConnectionListener) -> None:
        """Translate the socket owner's two facts into the protocol's vocabulary.

        `DeltaFeed` reports "opened" and "closed" as bare callbacks because it must not
        import the adapter package that imports it. Naming those two facts
        `ConnectionSignal.OPENED` and `.CLOSED` is this class's job, in the same way
        naming `sy` an `Instrument` is.

        **The two closures are remembered against the listener that asked for them**,
        because they are the only handles on them and `off_connection` is given nothing
        but the listener: a translation layer that forgot what it built could register
        but never remove.
        """

        def on_open(detail: str) -> None:
            listener(ConnectionSignal.OPENED, detail)

        def on_close(detail: str) -> None:
            listener(ConnectionSignal.CLOSED, detail)

        self._translated.append((listener, on_open, on_close))
        self._feed.on_open(on_open)
        self._feed.on_close(on_close)

    def off_connection(self, listener: ConnectionListener) -> None:
        """Take this listener, and the pair of closures built for it, back off the feed.

        One registration, matching by equality, and quiet about a listener that was
        never registered — the protocol's rule, so that a supervisor tidying up twice is
        not handed a failure by the tidy-up.
        """
        for index, (registered, on_open, on_close) in enumerate(self._translated):
            if registered == listener:
                del self._translated[index]
                self._feed.off_open(on_open)
                self._feed.off_close(on_close)
                return

    async def stream(self, publish: Publish) -> None:
        """Run the socket until stopped, publishing canonical events.

        `publish` is held for the duration and cleared on the way out, so an adapter that
        has returned cannot publish into a bus the caller has finished with.
        """
        self._publish = publish
        try:
            await self._feed.run()
        finally:
            self._publish = None

    def stop(self) -> None:
        self._feed.stop()

    # --- the venue's REST reads --------------------------------------------------

    async def expiries(self, underlying: str) -> ExpiriesResponse:
        """Every expiry Delta lists for one underlying, ascending."""
        return build_expiries(underlying, await self._client.tickers(underlying, None))

    async def chain_snapshot(self, underlying: str, expiry: str) -> ChainResponse:
        """The pivoted ladder as Delta currently reports it.

        **Raw, not enriched.** Our implied volatility and Greeks are the pricing core's
        work, not the venue's, and an adapter that returned them would be answering a
        question about us.
        """
        return build_chain(
            underlying, expiry, await self._client.tickers(underlying, expiry)
        )

    # --- frames in, events out ---------------------------------------------------

    def handle(self, message: VenueMessage) -> None:
        """One frame off the socket: decode once, publish the events.

        **The decode happens before any consumer sees anything**, so a frame that makes no
        sense is dropped whole rather than reaching a consumer with a broken payload
        inside it. That is what `feed.py` did before the decode moved here, and the chain
        cache would otherwise raise on it on every pass for as long as the frame stayed
        the newest for its contract.
        """
        try:
            events = self._decode(message)
        except Exception:
            self.undecodable += 1
            if self.undecodable == 1:
                # **The first one only.** A systematic decode bug would otherwise zero
                # the whole event stream while `feed.messages` kept climbing, and
                # `undecodable` is not on `/health` until #39 — a silent failure with a
                # counter nobody reads. Logging every frame would flood at 1,323 msg/s,
                # so the first says what happened and the counter carries the rest.
                logger.warning(
                    "the first undecodable %s frame for %r; the count carries the rest",
                    message.channel,
                    message.symbol,
                    exc_info=True,
                )
            return

        if self._publish is None:
            return
        for event in events:
            self.emitted += 1
            self._publish(event)

    def events_from_frame(
        self, channel: str, frame: dict[str, Any], received_at: float
    ) -> list[Event]:
        """The decode, as a function of three plain values. **The seam the tests drive.**

        No socket and no bus: a captured frame goes in and canonical events come out,
        which is what lets every fixture in `tests/fixtures/ws-*.json` be run through the
        real boundary. Raises nothing — a frame that cannot be read is counted and
        produces no events, exactly as it does on the live path.
        """
        try:
            events = self._decode(
                VenueMessage(
                    channel=channel,
                    symbol=frame.get("sy") or "",
                    frame=frame,
                    received_at=received_at,
                )
            )
        except Exception:
            self.undecodable += 1
            return []
        return events

    def _decode(self, message: VenueMessage) -> list[Event]:
        """The events for one frame. Raises on a frame that makes no sense.

        **One decode per frame, and one place that reads Delta's array offsets.** A book
        frame is one `md.option_quote`; a ticker frame is one `md.option_reference` and
        one `md.index_quote`. Since #37 every field either consumer needs is on one of
        those three, so nothing carries the frame onward and the venue's layout ends here.
        """
        frame = message.frame or {}
        instrument = instrument_from_symbol(message.symbol)
        if instrument is None:
            self.unparseable_symbols += 1

        stamps = {
            "ts_venue": _venue_time(frame.get("ts")),
            "ts_received": datetime.fromtimestamp(message.received_at, tz=timezone.utc),
        }

        if message.channel == BOOK_CHANNEL:
            _, top = decode_ob_l2_top(frame)
            # **A size without its price is not a quote**, and the guard has to be
            # applied after the non-finite check as well as inside `decode_ob_l2_top`:
            # a `NaN` bid that became `None` here would otherwise keep its size and
            # describe an order at no price.
            bid, bid_size = self._finite_quote(top.bid, top.bid_size)
            ask, ask_size = self._finite_quote(top.ask, top.ask_size)
            if instrument is None:
                return []
            quote = OptionQuote(
                source=VENUE,
                instrument=instrument,
                bid=bid,
                bid_size=bid_size,
                ask=ask,
                ask_size=ask_size,
                # Delta's own last-trade stamp on the book frame. **Carried, never
                # bucketed on** — the quote bars store it in `last_lts` and it decides
                # nothing. It travels on the event since #37 because once the frame stops
                # travelling there is nowhere else for it to live.
                lts=_venue_time(frame.get("lts")),
                **stamps,
            )
            return [quote]

        if message.channel != TICKER_CHANNEL:
            return []

        _, leg = decode_ticker(frame)
        extras = decode_ticker_extras(frame)
        if instrument is None:
            return []

        events: list[Event] = [
            OptionReference(
                source=VENUE,
                instrument=instrument,
                # Delta's numeric id for the contract. Neither a price nor an opinion: it
                # reaches the browser as `models.Leg.product_id`, and this frame is the
                # only place the websocket transport carries it.
                product_id=leg.product_id,
                mark=self._finite(leg.mark),
                last_price=self._finite(extras.last_traded_price),
                oi=self._finite(leg.oi),
                # The ticker channel carries **no** USD notional. Absent, never derived:
                # `wire.py` records that `oi[1]` is the six-hour change and not a
                # notional, checked against the REST snapshot on all 136 symbols.
                oi_value_usd=None,
                oi_change_usd_6h=self._finite(leg.oi_change_usd_6h),
                # Delta's ticker frame carries no tick size either. REST does; the
                # websocket does not, so the field is absent rather than invented.
                tick_size=None,
                bid_iv=self._finite(leg.bid_iv),
                ask_iv=self._finite(leg.ask_iv),
                mark_iv=self._finite(leg.mark_iv),
                delta=self._finite(leg.delta),
                gamma=self._finite(leg.gamma),
                theta=self._finite(leg.theta),
                vega=self._finite(leg.vega),
                rho=self._finite(leg.rho),
                # Traded value over Delta's rolling window, `to[0]`. The reference bars
                # store it and this frame is where it lives; `null` for a contract that
                # never traded, never `0`.
                turnover=self._finite(extras.turnover),
                # **This channel's own top of book.** The fallback quote the quote bars
                # use when a contract's book stays silent for a whole minute, and the base
                # the live ladder overrides wholesale with the book's when it has one.
                # See `docs/design/events.md`, *A note on provenance*.
                bid=self._finite(leg.bid),
                ask=self._finite(leg.ask),
                **stamps,
            )
        ]
        index = self._index_quote(instrument.underlying, extras.spot, stamps)
        if index is not None:
            events.append(index)
        return events

    def _index_quote(
        self, underlying: str, spot: float | None, stamps: dict[str, Any]
    ) -> IndexQuote | None:
        """One spot observation per ticker frame.

        **Measured**: all 136 frames captured inside a 0.06 s window carried an identical
        `sp` of 77651.9 (`tools/capture_ws.py`, 2026-09-03, recorded in `wire.py`). #36
        emitted this only when the value **changed**, so that 135 copies of one fact did
        not travel the bus.

        **#37 removed the suppression**, and the reason is the store rather than the
        screen. The spot bars count observations: `spot_ticks` is `measured` ~7,056 a
        minute against an individual contract's 118, and it is the one column that says
        whether the ingester was actually running. A deduplicated stream cannot say how
        long a price held, so a suppressed re-observation would be a row this engine had
        to invent. The four price columns are unaffected either way — suppression only
        ever dropped a value identical to the one immediately before it.

        The cost is `derived` ≈118 extra events a second, one per ticker frame, against
        a `measured` 1,322.9 msg/s feed (`tools/measure_feed.py`, 2026-09-03).

        A frame with no readable spot yields nothing: an absent spot is not an
        observation of absence. `wire.decode_ticker_extras` reads `sp` with
        `to_quote_number` since #37, so a spot spelled `"0"` is absent and not a price of
        zero — the gap `docs/design/lld/adapter.md` §6 recorded, closed on both paths at
        once so the event and the stored spot row cannot disagree about one frame.
        """
        checked = self._finite(spot)
        if checked is None:
            return None
        return IndexQuote(source=VENUE, underlying=underlying, spot=checked, **stamps)

    def _finite(self, value: float | None) -> float | None:
        """`NaN` and `Infinity` become `None`, counted. See the module docstring."""
        if isinstance(value, float) and not isfinite(value):
            self.non_finite += 1
            return None
        return value

    def _finite_quote(
        self, price: float | None, size: float | None
    ) -> tuple[float | None, float | None]:
        """One book level, guarded as a pair. A size outlives its price nowhere."""
        checked = self._finite(price)
        return checked, None if checked is None else self._finite(size)


def _venue_time(stamp: Any) -> datetime | None:
    """Delta's `ts`, microseconds since the epoch, as an aware UTC datetime.

    `None` where the venue gives none or gives something unreadable, which the envelope
    allows: a venue that does not stamp a frame is ordinary, and inventing our own clock
    here would destroy the arrival-lag column, whose whole content is the disagreement
    between the two stamps.
    """
    if stamp is None or isinstance(stamp, bool):
        return None
    try:
        microseconds = int(stamp)
    except (TypeError, ValueError):
        return None
    try:
        # Divided as integers, never through a float. A microsecond epoch already spends
        # more than a double's 15-16 significant digits on its integer part, so
        # `fromtimestamp(us / 1e6)` rounds the last digit away — and since #37 this stamp
        # is what the bar writer buckets on, exactly as `bars._to_utc` has always done.
        seconds, remainder = divmod(microseconds, 1_000_000)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=remainder
        )
    except (OverflowError, OSError, ValueError):
        return None
