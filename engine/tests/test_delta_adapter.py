"""Frames in, canonical events out. No socket, no bus, no clock but the one passed in.

**The boundary this file tests is the only place the venue's spelling is read.** Delta's
absent-quote spellings, its array offsets, its `DDMMYY` symbols and its microsecond
stamps all stop here; everything downstream sees `Instrument`s and catalogued events.

Every captured frame in `tests/fixtures/ws-*.json` is run through the real decoder, so
these are not three hand-written examples — they are 136 contracts of a live chain, on
both channels, asserted one by one.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from deltapayoff.adapters import (
    Adapter,
    ConnectionSignal,
    DeltaAdapter,
    instrument_from_symbol,
)
from deltapayoff.adapters.delta import VENUE, _venue_time
from deltapayoff.adapters.delta_socket import BOOK_CHANNEL, TICKER_CHANNEL
from deltapayoff.events import IndexQuote, OptionQuote, OptionReference, Right

#: An arrival stamp this file chose, so nothing here reads a clock.
ARRIVED_AT = 1_788_430_800.5


class _StubFeed:
    """Stands in for the socket owner. Registers subscriptions and never dials out."""

    def __init__(self, sink, **_kwargs) -> None:
        self.sink = sink
        self.registry: dict[str, list[str]] = {}
        self.ran = False
        self.stopped = False
        self.open_listeners: list = []
        self.close_listeners: list = []

    def subscribe(self, channel: str, symbols) -> None:
        self.registry.setdefault(channel, []).extend(symbols)

    def on_open(self, listener) -> None:
        self.open_listeners.append(listener)

    def on_close(self, listener) -> None:
        self.close_listeners.append(listener)

    def off_open(self, listener) -> None:
        if listener in self.open_listeners:
            self.open_listeners.remove(listener)

    def off_close(self, listener) -> None:
        if listener in self.close_listeners:
            self.close_listeners.remove(listener)

    async def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        self.stopped = True


class _StubClient:
    """Delta's REST answers, from a committed fixture. Nothing dials out."""

    def __init__(self, rows) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str | None]] = []

    async def tickers(self, underlying: str, expiry=None):
        self.calls.append((underlying, expiry))
        return self.rows


def adapter(client=None, **kwargs) -> DeltaAdapter:
    return DeltaAdapter(client=client, feed_factory=_StubFeed, **kwargs)


def ticker_frame(symbol: str, **body) -> dict:
    """A `ticker` frame in Delta's own shape, with the arrays a caller wants set."""
    payload = {
        "s": symbol,
        "i": 1,
        "m": "580.6",
        "q": ["584", "100", "579", "200", None],
        "qiv": ["0.31", "0.29", "0.30"],
        "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
        "oi": ["100", "200"],
        "ohlc": ["1", "2", "3", "4"],
        "to": ["5", "5"],
    }
    payload.update(body)
    return {
        "type": TICKER_CHANNEL,
        "sy": symbol,
        "sp": "77651.9",
        "ts": 1_788_430_765_832_299,
        "d": [payload],
    }


def book_frame(symbol: str, bids=(("120", "10"),), asks=(("125", "12"),)) -> dict:
    return {
        "type": BOOK_CHANNEL,
        "sy": symbol,
        "ts": 1_788_430_765_832_299,
        "lts": 1_788_430_765_000_000,
        "a": [list(level) for level in asks],
        "b": [list(level) for level in bids],
    }


# --- the symbol, and the instrument it becomes ------------------------------------


def test_delta_s_symbol_becomes_a_canonical_instrument() -> None:
    """`C-BTC-77600-040926` is a call on BTC at 77600 expiring 4 September 2026.

    The venue's string rides along verbatim so a request back to Delta needs no reverse
    lookup, and it is deliberately not part of the canonical string.
    """
    instrument = instrument_from_symbol("C-BTC-77600-040926")

    assert instrument is not None
    assert instrument.venue == VENUE
    assert instrument.underlying == "BTC"
    assert instrument.expiry == date(2026, 9, 4)
    assert instrument.strike == Decimal("77600")
    assert instrument.right is Right.CALL
    assert instrument.venue_symbol == "C-BTC-77600-040926"
    # #60 (I1): both currencies are the adapter's own doing, not the class default —
    # a test per field, per the ticket's acceptance criterion.
    assert instrument.quote_currency == "USD"
    assert instrument.settlement_currency == "USD"
    assert instrument.canonical() == "DELTA-BTC-20260904-77600-C-USD"


def test_eth_s_symbol_becomes_a_canonical_instrument_too() -> None:
    """The decode reads the underlying out of the symbol; it never assumed BTC. #43
    changes which underlyings are subscribed, not this boundary — this pins that the
    boundary already had no BTC-only assumption baked into it to find."""
    instrument = instrument_from_symbol("P-ETH-3600-080926")

    assert instrument is not None
    assert instrument.underlying == "ETH"
    assert instrument.expiry == date(2026, 9, 8)
    assert instrument.strike == Decimal("3600")
    assert instrument.right is Right.PUT
    assert instrument.canonical() == "DELTA-ETH-20260908-3600-P-USD"


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "C-BTC-77600",
        "C-BTC-77600-040926-EXTRA",
        "X-BTC-77600-040926",
        "C-BTC-notanumber-040926",
        "C-BTC-77600-993926",
        "C-BTC-NaN-040926",
    ],
)
def test_a_symbol_that_is_not_a_contract_is_none_rather_than_a_guess(symbol) -> None:
    """`underlying` becomes a partition directory name downstream, so a wrong guess
    files quotes under an asset they did not happen in. `None`, counted by the caller,
    for the reason `bars.py` gives about the same parse: one odd symbol must not end
    ingestion."""
    assert instrument_from_symbol(symbol) is None


# --- the two channels, over every captured frame ----------------------------------


def test_every_captured_book_frame_yields_one_option_quote(ws_book_frames) -> None:
    """136 contracts of a live chain, on `ob_l2`, through the real decoder."""
    delta = adapter()

    assert ws_book_frames, "the book fixture is empty"
    for symbol, frame in ws_book_frames.items():
        events = delta.events_from_frame(BOOK_CHANNEL, frame, ARRIVED_AT)

        assert len(events) == 1, symbol
        quote = events[0]
        assert isinstance(quote, OptionQuote)
        assert quote.type == "md.option_quote"
        assert quote.source == VENUE
        assert quote.instrument is not None
        assert quote.instrument.venue_symbol == symbol
        assert quote.instrument.canonical().startswith("DELTA-BTC-2026")

    assert delta.undecodable == 0
    assert delta.unparseable_symbols == 0


def test_every_captured_ticker_frame_yields_a_reference(ws_ticker_frames) -> None:
    """The venue's own view of a contract: its mark, its open interest, its greeks and
    its implied vols — reference columns, never inputs."""
    delta = adapter()

    assert ws_ticker_frames, "the ticker fixture is empty"
    for symbol, frame in ws_ticker_frames.items():
        events = delta.events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)

        references = [e for e in events if isinstance(e, OptionReference)]
        assert len(references) == 1, symbol
        assert references[0].instrument is not None
        assert references[0].instrument.venue_symbol == symbol
        assert references[0].source == VENUE

    assert delta.undecodable == 0


def test_the_index_quote_is_emitted_once_per_frame(ws_ticker_frames) -> None:
    """One spot observation per frame, and **not** one per change.

    **Measured**: all 136 captured frames were taken inside a 0.06 s window and carry an
    identical `sp` of 77651.9. #36 emitted only the first of those, so that 135 copies of
    one fact did not travel the bus; #37 emits all 136, because the spot bars count
    observations and `spot_ticks` is the column that says whether the ingester was
    running at all. A deduplicated stream cannot say how long a price held.

    `instrument` stays `null` on every one of them: spot is a property of BTC and not of
    the contract whose frame happened to carry it, and storing the messenger would invite
    a reader to join on it.
    """
    delta = adapter()

    index_quotes = [
        event
        for frame in ws_ticker_frames.values()
        for event in delta.events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)
        if isinstance(event, IndexQuote)
    ]

    assert len(index_quotes) == len(ws_ticker_frames) == 136
    assert {event.underlying for event in index_quotes} == {"BTC"}
    assert {event.instrument for event in index_quotes} == {None}
    assert {round(event.spot, 1) for event in index_quotes} == {77651.9}


def test_an_unchanged_spot_is_emitted_again() -> None:
    """The suppression #36 had is gone, and this is the test that would have caught it.

    A re-observation of the same price at a later instant is a real observation: it is
    what a `spot_ticks` of ~7,056 a minute is made of, and it is the difference between
    a quiet market and a dead ingester.
    """
    delta = adapter()
    symbol = "C-BTC-77600-040926"

    first = delta.events_from_frame(TICKER_CHANNEL, ticker_frame(symbol), ARRIVED_AT)
    moved = ticker_frame(symbol)
    moved["sp"] = "77700.0"
    second = delta.events_from_frame(TICKER_CHANNEL, moved, ARRIVED_AT)
    again = delta.events_from_frame(TICKER_CHANNEL, moved, ARRIVED_AT)

    assert [type(e).__name__ for e in first] == ["OptionReference", "IndexQuote"]
    assert [type(e).__name__ for e in second] == ["OptionReference", "IndexQuote"]
    assert [type(e).__name__ for e in again] == ["OptionReference", "IndexQuote"]
    assert [e.spot for e in again if isinstance(e, IndexQuote)] == [77700.0]


def test_a_spot_spelled_zero_is_absent_and_not_a_price_of_zero() -> None:
    """`null` is not `0`, on the one field #36 left reading it as a number.

    `docs/design/lld/adapter.md` §6 recorded this as a known gap and handed it to #37 with
    the instruction to fix `wire.decode_ticker_extras` at the same time, since the spot
    bars read `sp` through that function and an event disagreeing with a stored row about
    one frame is worse than either being wrong alone.
    """
    frame = ticker_frame("C-BTC-77600-040926")
    frame["sp"] = "0"

    events = adapter().events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)

    assert not [e for e in events if isinstance(e, IndexQuote)]


def test_a_last_price_spelled_zero_is_absent_too() -> None:
    """`ohlc[3]` has the same shape as `sp`: a price nobody paid is not a price of zero.

    Sixteen of the 136 captured contracts have never traded and send `ohlc` all-null; a
    seventeenth spelling it `"0"` must land the same way.
    """
    frame = ticker_frame("C-BTC-77600-040926", ohlc=["1", "2", "3", "0"])

    reference = adapter().events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)[0]

    assert reference.last_price is None


def test_a_frame_with_no_spot_yields_no_index_quote() -> None:
    """An absent spot is not an observation of absence."""
    frame = ticker_frame("C-BTC-77600-040926")
    frame["sp"] = None

    events = adapter().events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)

    assert not [e for e in events if isinstance(e, IndexQuote)]


# --- the four fields the shim existed for ----------------------------------------
#
# #36's shim carried the venue's whole frame past the adapter, because four things the
# consumers read had no field in the catalogue: `lts`, turnover, `product_id` and the
# ticker channel's own bid and ask. #37 added all four to the events and deleted the shim.
# These are the tests that say the deletion did not lose them.


def test_every_book_event_carries_the_venue_last_trade_stamp(ws_book_frames) -> None:
    """`lts` feeds the quote bars' `last_lts` column, populated on all 2,460 rows of #36's
    410 s live run. Carried on the event and **never bucketed on**: `ts_venue` alone
    decides which minute a tick belongs to."""
    delta = adapter()

    quotes = [
        delta.events_from_frame(BOOK_CHANNEL, frame, ARRIVED_AT)[0]
        for frame in ws_book_frames.values()
    ]

    assert len(quotes) == 136
    assert all(quote.lts is not None for quote in quotes), "a bar would lose its last_lts"
    for quote, frame in zip(quotes, ws_book_frames.values(), strict=True):
        assert quote.lts == _venue_time(frame["lts"])
        assert quote.lts != quote.ts_venue, "lts and ts are two different stamps"


def test_the_reference_event_carries_turnover_and_the_product_id(
    ws_ticker_frames,
) -> None:
    """`to[0]` is a stored column and `i` reaches the browser as `Leg.product_id`. Neither
    is derivable from anything else on the bus, so both travel on the event."""
    delta = adapter()
    symbol = "P-BTC-78500-040926"
    frame = ws_ticker_frames[symbol]
    body = frame["d"][0]

    reference = delta.events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)[0]

    assert reference.product_id == body["i"]
    assert reference.turnover == body["to"][0]


def test_a_contract_that_never_traded_carries_no_turnover_and_no_last_price(
    ws_ticker_frames,
) -> None:
    """**Sixteen of the 136 captured contracts have never traded** and send `ohlc` and
    `to` all-null. Absent stays absent: a zero would read as "it turned over nothing and
    last traded at zero", which is two claims nobody made."""
    delta = adapter()
    untraded = [
        delta.events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)[0]
        for frame in ws_ticker_frames.values()
        if frame["d"][0]["ohlc"][3] is None
    ]

    assert len(untraded) == 16, "the capture no longer holds the never-traded contracts"
    assert all(event.last_price is None for event in untraded)
    assert all(event.turnover is None for event in untraded)


def test_the_reference_event_carries_the_channels_own_top_of_book(
    ws_ticker_frames, ws_book_frames
) -> None:
    """**The fallback quote, and the reason `from_book` needs no channel on the bus.**

    The ticker channel publishes its own bid and ask; they are the quote bars' fallback
    for a contract whose book stays silent for a whole minute, and the base the live
    ladder overrides wholesale when a book quote exists. A consumer decides provenance by
    *which event* a price came from, so no venue channel has to re-enter the catalogue.
    """
    delta = adapter()
    symbol = "P-BTC-78500-040926"
    body = ws_ticker_frames[symbol]["d"][0]

    reference = delta.events_from_frame(
        TICKER_CHANNEL, ws_ticker_frames[symbol], ARRIVED_AT
    )[0]
    book = delta.events_from_frame(BOOK_CHANNEL, ws_book_frames[symbol], ARRIVED_AT)[0]

    assert reference.bid == float(body["q"][2])
    assert reference.ask == float(body["q"][0])
    # The two describe the same top of book at two instants — the book republishes every
    # 508 ms against the ticker channel's 5,001 ms — so they are close and not equal.
    # That gap is exactly why one overrides the other **wholesale**: taking the bid from
    # one and the ask from the other would report a spread neither channel ever quoted.
    assert book.bid is not None and book.ask is not None
    assert abs(book.bid - reference.bid) / reference.bid < 0.05
    assert (book.bid, book.ask) != (reference.bid, reference.ask)


# --- null is not 0, at the one boundary that knows -------------------------------


def test_the_three_absent_spellings_become_null_and_a_real_zero_survives(
    absent_quote_tickers,
) -> None:
    """**The rule this whole boundary exists for**, driven by the hand-built fixture.

    `tests/fixtures/tickers-absent-quotes.json` carries the three spellings Delta uses
    for "nobody is quoting" — `"0"`, `""` and `null` — because live snapshots quote every
    strike and the edge cases have to be constructed. The spellings are lifted out of it
    verbatim and put on the wire in the websocket's own array layout, so this asserts the
    adapter and not a restatement of the fixture.

    A real `0` in open interest and in a greek stays `0.0`, because zero is a true value
    for those: the ladder that shows `0` open interest is reporting a fact, and a `null`
    there would read as "we do not know".

    The ticker frame's own `q` bid and ask are asserted **through the shim**, because
    `md.option_reference` carries no bid or ask — putting the spellings in `q` and only
    looking at the event would decode them and throw them away, which would assert
    nothing.
    """
    by_symbol = {row["symbol"]: row for row in absent_quote_tickers}
    row = by_symbol["C-BTC-59000-040926"]
    quotes, greeks = row["quotes"], row["greeks"]

    # The three spellings, as the fixture holds them, before anything touches them.
    assert quotes["best_bid"] == "0"
    assert quotes["best_ask"] == ""
    assert quotes["ask_iv"] is None
    assert row["oi_contracts"] == "0"

    delta = adapter()
    frame = ticker_frame(
        row["symbol"],
        q=[quotes["best_ask"], quotes["ask_size"], quotes["best_bid"], "0", None],
        qiv=[quotes["ask_iv"], quotes["bid_iv"], quotes["mark_iv"]],
        oi=[row["oi_contracts"], row["oi_change_usd_6h"]],
        g=[
            greeks["delta"],
            greeks["gamma"],
            greeks["rho"],
            greeks["theta"],
            greeks["vega"],
        ],
    )
    reference = delta.events_from_frame(TICKER_CHANNEL, frame, ARRIVED_AT)[0]

    # `"0"` on a quote field, `""` on another, `null` on a third: all absent.
    assert reference.bid_iv is None, 'a bid_iv spelled "0" is nobody quoting'
    assert reference.ask_iv is None, "a null ask_iv is nobody quoting"

    # A real zero survives, on open interest and on a greek.
    assert reference.oi == 0.0
    assert reference.gamma == pytest.approx(0.00000239)

    book = delta.events_from_frame(
        BOOK_CHANNEL,
        book_frame(
            row["symbol"],
            bids=((quotes["best_bid"], quotes["bid_size"]),),
            asks=((quotes["best_ask"], quotes["ask_size"]),),
        ),
        ARRIVED_AT,
    )[0]

    assert book.bid is None, 'a best_bid spelled "0" is nobody bidding'
    assert book.ask is None, 'a best_ask spelled "" is nobody offering'
    assert book.bid_size is None, "a size without its price is not a quote"
    assert book.ask_size is None

    # The same two spellings on the ticker channel, where since #37 the quote reaches
    # `md.option_reference`'s own bid and ask rather than a record carrying a channel.
    assert reference.bid is None, 'a ticker `q` bid spelled "0" is nobody quoting'
    assert reference.ask is None, 'a ticker `q` ask spelled "" is nobody quoting'


def test_a_zero_that_is_really_zero_survives_on_a_second_fixture_row(
    absent_quote_tickers,
) -> None:
    """The row whose gamma is the bare string `"0"` and whose mark_iv is `"0.00000000"`.

    One is a real zero on a greek and stays; the other is a quote field and does not. The
    two spellings are almost identical and the meanings are opposite, which is the whole
    reason `to_number` and `to_quote_number` are two functions.
    """
    row = {r["symbol"]: r for r in absent_quote_tickers}["P-BTC-89000-040926"]
    greeks, quotes = row["greeks"], row["quotes"]

    assert greeks["gamma"] == "0"
    assert quotes["mark_iv"] == "0.00000000"

    reference = adapter().events_from_frame(
        TICKER_CHANNEL,
        ticker_frame(
            row["symbol"],
            qiv=[quotes["ask_iv"], quotes["bid_iv"], quotes["mark_iv"]],
            g=[
                greeks["delta"],
                greeks["gamma"],
                greeks["rho"],
                greeks["theta"],
                greeks["vega"],
            ],
            oi=[row["oi_contracts"], row["oi_change_usd_6h"]],
        ),
        ARRIVED_AT,
    )[0]

    assert reference.gamma == 0.0, "a greek of zero is a real number"
    assert reference.mark_iv is None, "an implied vol of exactly zero is a missing one"
    assert reference.oi == 0.0


def test_the_book_top_carries_its_sizes() -> None:
    """`bid_size` and `ask_size` were never decoded before this ticket; the offsets are
    read in `wire.py` beside every other Delta offset and nowhere else."""
    quote = adapter().events_from_frame(
        BOOK_CHANNEL,
        book_frame("C-BTC-77600-040926", bids=(("120", "10"),), asks=(("125", "12"),)),
        ARRIVED_AT,
    )[0]

    assert (quote.bid, quote.bid_size) == (120.0, 10.0)
    assert (quote.ask, quote.ask_size) == (125.0, 12.0)


# --- the two stamps ---------------------------------------------------------------


def test_the_venue_s_stamp_and_ours_are_both_carried() -> None:
    """The arrival-lag column is `ts_received - ts_venue`, so both have to survive.

    Delta's `ts` is microseconds since the epoch. Neither stamp is corrected against the
    other: the disagreement is the data.
    """
    quote = adapter().events_from_frame(
        BOOK_CHANNEL, book_frame("C-BTC-77600-040926"), ARRIVED_AT
    )[0]

    assert quote.ts_venue == datetime.fromtimestamp(
        1_788_430_765_832_299 / 1e6, tz=timezone.utc
    )
    assert quote.ts_received == datetime.fromtimestamp(ARRIVED_AT, tz=timezone.utc)


def test_a_frame_with_no_venue_stamp_still_produces_an_event() -> None:
    """A venue that does not stamp a frame is ordinary; the envelope allows `null`.
    Substituting our own clock would destroy the lag column, whose whole content is the
    disagreement between the two."""
    frame = book_frame("C-BTC-77600-040926")
    del frame["ts"]

    quote = adapter().events_from_frame(BOOK_CHANNEL, frame, ARRIVED_AT)[0]

    assert quote.ts_venue is None
    assert quote.ts_received is not None


# --- what must not kill the feed --------------------------------------------------


def test_a_malformed_frame_yields_no_events_and_is_counted() -> None:
    """One bad frame must not end ingestion. This is the count `feed.malformed` carried
    before the decode moved here."""
    delta = adapter()

    events = delta.events_from_frame(
        TICKER_CHANNEL, {"sy": "C-BTC-77600-040926", "d": "not a list"}, ARRIVED_AT
    )

    assert events == []
    assert delta.undecodable == 1


def test_a_non_finite_price_is_carried_as_absent_rather_than_raising() -> None:
    """`Event` refuses `NaN` outright, because pydantic would serialise it to JSON
    `null` and a garbage number would arrive indistinguishable from a quote that was
    never there. That refusal must not reach the socket reader, so the adapter makes the
    value absent first — and counts it, because a silent conversion is a lie.

    Reachable from a venue and not merely defensive: `json.loads` accepts the bare token
    `NaN`, which standard JSON does not.
    """
    delta = adapter()
    frame = book_frame("C-BTC-77600-040926")
    frame["b"] = [[float("nan"), "10"]]

    quote = delta.events_from_frame(BOOK_CHANNEL, frame, ARRIVED_AT)[0]

    assert quote.bid is None
    assert quote.bid_size is None, "a size outlived the price it belonged to"
    assert (quote.ask, quote.ask_size) == (125.0, 12.0)
    assert delta.non_finite >= 1


def test_a_frame_whose_symbol_is_not_a_contract_produces_no_events() -> None:
    """An event with no instrument would be a quote about nothing."""
    delta = adapter()

    events = delta.events_from_frame(BOOK_CHANNEL, book_frame("not-a-symbol"), ARRIVED_AT)

    assert events == []
    assert delta.unparseable_symbols == 1
    assert delta.undecodable == 0, "an unreadable symbol is not an unreadable frame"


def test_a_channel_the_adapter_does_not_know_produces_nothing() -> None:
    """Control traffic never reaches here, but a channel added to `feed.CHANNELS` and
    not to the decoder would otherwise be silently dropped as if it were data."""
    assert adapter().events_from_frame("subscriptions", {"sy": ""}, ARRIVED_AT) == []


# --- the protocol, and the rest of it ---------------------------------------------


def test_the_delta_adapter_satisfies_the_protocol() -> None:
    """Structural, not nominal: `isinstance` against the protocol fails when a member
    goes missing, where an explicit subclass would inherit a `...`-bodied stub and pass.
    See `events/bus.py`, which records the same choice being reverted once."""
    assert isinstance(adapter(), Adapter)


def test_the_protocol_check_can_actually_fail() -> None:
    """**The conformance test above is worth nothing unless this one holds.**

    `events/bus.py` records writing `class FanOut(Bus)` and reverting it, because
    `isinstance` against a nominal subclass is `True` whatever the class contains — the
    check becomes unfalsifiable and a missing method starts returning `None` silently.
    This asserts the structural check still discriminates: drop one member and it fails.
    """

    class MissingStream:
        venue = "X"
        underlyings = ()

        async def instruments(self, underlying):
            return []

        def subscribe(self, instruments):
            return None

        def on_connection(self, listener):
            return None

        def off_connection(self, listener):
            return None

        def stop(self):
            return None

        async def expiries(self, underlying):
            return None

        async def chain_snapshot(self, underlying, expiry):
            return None

    class Complete(MissingStream):
        async def stream(self, publish):
            return None

    assert not isinstance(MissingStream(), Adapter)
    assert isinstance(Complete(), Adapter)


def test_the_adapter_describes_itself() -> None:
    """The recorded set is configuration, read at start-up and passed in by the caller.
    `DeltaAdapter`'s own constructor default is BTC alone — a safe fallback for a caller
    that builds one without saying — which is a class default, not `main.py`'s: since #43
    `main.live_underlyings()` always passes both explicitly."""
    delta = adapter(underlyings=("btc", "eth"))

    assert delta.venue == VENUE
    assert delta.underlyings == ("BTC", "ETH")
    assert adapter().underlyings == ("BTC",)


def test_listing_instruments_reads_the_venue_s_snapshot(chain_tickers) -> None:
    """Delta has no instrument endpoint, so the ticker snapshot is the listing."""
    client = _StubClient(chain_tickers)
    delta = adapter(client=client)

    instruments = asyncio.run(delta.instruments("BTC"))

    assert len(instruments) == len(chain_tickers)
    assert {i.underlying for i in instruments} == {"BTC"}
    assert client.calls == [("BTC", None)]


def test_subscribing_registers_both_channels_for_every_contract() -> None:
    """Both, always. Narrowing `ob_l2` to the watched expiry was built and reverted once
    already — see `docs/ingestion.md` — and is not reopened here."""
    delta = adapter()
    symbols = ["C-BTC-77600-040926", "P-BTC-77600-040926"]

    delta.subscribe([instrument_from_symbol(symbol) for symbol in symbols])

    assert delta.feed.registry[TICKER_CHANNEL] == symbols
    assert delta.feed.registry[BOOK_CHANNEL] == symbols


def test_the_two_venue_reads_come_off_the_same_client(chain_tickers) -> None:
    """`/expiries` and `/chain` are answered from the venue's own snapshot, and a second
    venue answers them from its own — so the client belongs inside the adapter."""
    client = _StubClient(chain_tickers)
    delta = adapter(client=client)

    expiries = asyncio.run(delta.expiries("BTC"))
    chain = asyncio.run(delta.chain_snapshot("BTC", "04-09-2026"))

    assert expiries.expiries == ["04-09-2026"]
    assert chain.underlying == "BTC"
    assert chain.rows, "the ladder is empty"


def test_the_chain_snapshot_is_the_venue_s_and_not_ours(chain_tickers) -> None:
    """Raw, not enriched. Our implied volatility and greeks are the pricing core's work,
    and an adapter returning them would be answering a question about us."""
    chain = asyncio.run(adapter(client=_StubClient(chain_tickers)).chain_snapshot(
        "BTC", "04-09-2026"
    ))

    legs = [leg for row in chain.rows for leg in (row.call, row.put) if leg is not None]

    assert legs, "the ladder is empty"
    assert all(leg.computed is None for leg in legs)
    assert chain.forward is None and chain.forward_method is None


def test_streaming_runs_the_socket_owner_and_stopping_stops_it() -> None:
    """The adapter owns the connection for this ticket; #38 lifts it into a controller."""
    delta = adapter()

    asyncio.run(delta.stream(lambda event: None))
    delta.stop()

    assert delta.feed.ran is True
    assert delta.feed.stopped is True


def test_the_sink_decodes_once_and_publishes_the_events() -> None:
    """The whole live path, one frame at a time: the socket owner's sink decodes once and
    publishes the canonical events, and nothing else leaves the adapter.

    **Two events from the ticker frame, not one.** The reference carries the contract; the
    index quote carries spot, which belongs to the underlying.
    """
    delta = adapter()
    published: list = []
    delta._publish = published.append

    delta.feed.sink.publish(_message(BOOK_CHANNEL, book_frame("C-BTC-77600-040926")))
    delta.feed.sink.publish(_message(TICKER_CHANNEL, ticker_frame("C-BTC-77600-040926")))

    assert [type(event).__name__ for event in published] == [
        "OptionQuote",
        "OptionReference",
        "IndexQuote",
    ]
    assert (published[0].bid, published[0].ask) == (120.0, 125.0)
    assert (published[1].bid, published[1].ask) == (579.0, 584.0)
    assert delta.emitted == 3


def test_a_non_finite_price_is_absent_and_takes_its_size_with_it() -> None:
    """`NaN` must not reach a consumer as a quote, and must not reach one as a size.

    Pydantic serialises `NaN` to JSON `null`, so a garbage number would arrive downstream
    indistinguishable from a quote that was never there. Refused before the event is built
    — if the refusal reached the socket reader it would end the connection.
    """
    delta = adapter()
    published: list = []
    delta._publish = published.append

    frame = book_frame("C-BTC-77600-040926")
    frame["b"] = [[float("nan"), "10"]]
    delta.feed.sink.publish(_message(BOOK_CHANNEL, frame))

    assert published[0].bid is None
    assert published[0].bid_size is None, "a size without its price is not a quote"
    assert published[0].ask == 125.0
    assert delta.non_finite == 1


def test_a_malformed_frame_reaches_no_consumer() -> None:
    """Dropped whole, as the socket owner dropped it before the decode moved here.

    Letting it through would leave the chain cache holding something it raises on, once
    every recompute pass, for as long as it stayed the newest for its contract — a screen
    that stops updating with nothing saying why.
    """
    delta = adapter()
    published: list = []
    delta._publish = published.append

    delta.feed.sink.publish(
        _message(TICKER_CHANNEL, {"sy": "C-BTC-77600-040926", "d": "not a list"})
    )

    assert published == []
    assert delta.undecodable == 1


def _message(channel: str, frame: dict):
    from deltapayoff.adapters.delta_socket import VenueMessage

    return VenueMessage(
        channel=channel,
        symbol=frame.get("sy") or "",
        frame=frame,
        received_at=ARRIVED_AT,
    )


def test_the_adapter_translates_the_sockets_two_facts() -> None:
    """`DeltaFeed` reports "opened" and "closed" as bare callbacks, because importing the
    adapter protocol into the socket owner would be a cycle. Naming those two facts in
    the protocol's vocabulary is this class's job, like naming `sy` an `Instrument`."""
    delta = adapter()
    seen: list[tuple[ConnectionSignal, str]] = []

    delta.on_connection(lambda signal, detail: seen.append((signal, detail)))
    delta.feed.open_listeners[0]("wss://public-socket.india.delta.exchange")
    delta.feed.close_listeners[0]("ConnectionResetError: scripted drop")

    assert seen == [
        (ConnectionSignal.OPENED, "wss://public-socket.india.delta.exchange"),
        (ConnectionSignal.CLOSED, "ConnectionResetError: scripted drop"),
    ]


def test_the_signal_is_a_register_and_not_a_slot() -> None:
    """Two listeners, both told. The controller and a future recorder can listen without
    either knowing about the other."""
    delta = adapter()
    first: list = []
    second: list = []

    delta.on_connection(lambda signal, detail: first.append(signal))
    delta.on_connection(lambda signal, detail: second.append(signal))
    for listener in delta.feed.open_listeners:
        listener("up")

    assert first == [ConnectionSignal.OPENED]
    assert second == [ConnectionSignal.OPENED]


def test_a_connection_listener_can_be_taken_off_again() -> None:
    """`on_connection` appends **two** closures per call, and there was no way to remove
    either — so a discarded controller stayed strongly referenced and kept being told
    about a socket it no longer owned. `off_connection` removes the pair it registered,
    which is why the pair is remembered against the listener that asked for it."""
    delta = adapter()
    seen: list = []

    def listener(signal, detail):
        seen.append(signal)

    delta.on_connection(listener)
    assert len(delta.feed.open_listeners) == 1
    assert len(delta.feed.close_listeners) == 1

    delta.off_connection(listener)

    assert delta.feed.open_listeners == []
    assert delta.feed.close_listeners == []
    assert seen == []


def test_removing_one_listener_leaves_the_others_registered() -> None:
    """The register keeps being a register. Removing the controller must not deafen the
    recorder beside it."""
    delta = adapter()
    kept: list = []
    dropped: list = []

    def goes(signal, detail):
        dropped.append(signal)

    def stays(signal, detail):
        kept.append(signal)

    delta.on_connection(goes)
    delta.on_connection(stays)
    delta.off_connection(goes)
    for listener in delta.feed.open_listeners:
        listener("up")

    assert kept == [ConnectionSignal.OPENED]
    assert dropped == []


def test_removing_a_listener_that_was_never_registered_is_not_an_error() -> None:
    """A supervisor tidying up on both the normal and the failed path calls this twice.
    `DeltaFeed.off_open` and `off_close` are the same: quiet about what is not there."""
    delta = adapter()

    delta.off_connection(lambda signal, detail: None)

    assert delta.feed.open_listeners == []
