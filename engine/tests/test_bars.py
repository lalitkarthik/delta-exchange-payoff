"""One-minute quote bars, as a pure function of ticks.

Every rule in #10 is reachable here without a socket, a clock or a filesystem, which is
the whole reason the aggregation is a separate module. `seal` takes the wall clock as an
argument rather than reading one, so lateness is a test parameter.

**A bar built with the wrong comparison produces numbers that are all plausible and all
wrong**, and nothing crashes. So these tests assert properties a plausible mistake would
break — that the high bounds the open and the close, that the mid is not the midpoint of
the bid and ask bars, that silence produces nothing — rather than restating constants
the implementation would restate identically.
"""

from __future__ import annotations

from datetime import datetime, timezone

from deltapayoff.bars import (
    BarAggregator,
    ReferenceAggregator,
    ReferenceTick,
    SpotAggregator,
    SpotTick,
    Tick,
    samples_from_ticker,
    tick_from_quote,
)
from deltapayoff.feed import Quote

SYMBOL = "C-BTC-77600-040926"

#: 2026-09-04T09:00:00Z, in microseconds. A round minute boundary, so every offset below
#: reads as "seconds into the minute".
MINUTE_US = int(datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1e6)
SECOND_US = 1_000_000
MINUTE = 60 * SECOND_US


def at(second: float, bid: float | None, ask: float | None, *, minute: int = 0) -> Tick:
    """A tick `second` seconds into minute `minute`, counted from `MINUTE_US`."""
    return Tick(
        symbol=SYMBOL,
        exchange_us=MINUTE_US + minute * MINUTE + int(second * SECOND_US),
        bid=bid,
        ask=ask,
    )


def wall_after(minutes: int) -> float:
    """A wall clock reading far enough past minute `minutes` to seal it and everything
    before it, whatever the grace period is."""
    return (MINUTE_US + minutes * MINUTE) / 1e6 + 3600.0


def test_one_minute_of_ticks_becomes_one_bar_with_ohlc_for_all_three_series() -> None:
    """The baseline, asserted as properties rather than as restated constants.

    The open is the first tick's value and the close the last; the high is at least both
    and the low at most both. A comparison written backwards passes a test that checks
    only "high == 78.0" if 78.0 happened to be first, and fails these.
    """
    aggregator = BarAggregator()
    for tick in (
        at(1, 70.0, 72.0),
        at(2, 75.0, 78.0),
        at(3, 68.0, 71.0),
        at(4, 73.0, 74.0),
    ):
        aggregator.add(tick)

    bars = aggregator.seal(wall_after(1))
    assert len(bars) == 1
    bar = bars[0]

    assert bar.minute == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    assert bar.symbol == SYMBOL
    assert bar.underlying == "BTC"
    assert bar.expiry == "04-09-2026"
    assert bar.strike == 77600.0
    assert bar.option_type == "C"

    assert (bar.bid_open, bar.bid_close) == (70.0, 73.0)
    assert (bar.ask_open, bar.ask_close) == (72.0, 74.0)
    assert bar.bid_high >= max(bar.bid_open, bar.bid_close)
    assert bar.bid_low <= min(bar.bid_open, bar.bid_close)
    assert bar.ask_high >= max(bar.ask_open, bar.ask_close)
    assert bar.ask_low <= min(bar.ask_open, bar.ask_close)
    assert bar.mid_high >= max(bar.mid_open, bar.mid_close)
    assert bar.mid_low <= min(bar.mid_open, bar.mid_close)

    assert bar.bid_ticks == bar.ask_ticks == bar.mid_ticks == 4


def test_a_minute_with_no_arrivals_produces_no_row_at_all() -> None:
    """**The most important test in the ticket.**

    Not a row of nulls, and never the previous bar's close. Delta's own
    `/v2/history/candles` pads empty buckets with the last trade and does not say so —
    `C-BTC-60000-270624` returns 801 daily bars of which 797 are fabricated. This is the
    standing assertion that the same defect cannot enter here.

    Ticks in minute 0 and minute 4. Minutes 1, 2 and 3 are a deliberate silence and must
    come back as *nothing* — no row, of any shape.
    """
    aggregator = BarAggregator()
    aggregator.add(at(10, 70.0, 72.0, minute=0))
    aggregator.add(at(20, 71.0, 73.0, minute=0))
    aggregator.add(at(10, 90.0, 92.0, minute=4))
    aggregator.add(at(20, 91.0, 93.0, minute=4))

    bars = aggregator.seal(wall_after(5))
    minutes = [bar.minute for bar in bars]

    assert len(bars) == 2, "a silent minute grew a row"
    assert minutes == [
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 9, 4, tzinfo=timezone.utc),
    ]
    for silent in (1, 2, 3):
        stamp = datetime(2026, 9, 4, 9, silent, tzinfo=timezone.utc)
        assert stamp not in minutes, f"minute {silent} was invented"
    # And the row that follows the silence opens on its own first tick, not on the last
    # close before the gap — the forward-fill this design refuses absolutely.
    assert bars[1].bid_open == 90.0
    assert bars[0].bid_close == 71.0


def test_the_mid_is_not_the_midpoint_of_the_bid_and_ask_bars() -> None:
    """The mid's own trap, constructed deliberately.

    The highest bid and the highest ask happen at **different ticks**, so the midpoint of
    the two bar highs is a price that never existed at any instant. A `mid_high` derived
    from `bid_high` and `ask_high` would be 130.0 here and the true answer is 120.5.

    This is the failure that would otherwise be found from a chart six months later.
    """
    aggregator = BarAggregator()
    aggregator.add(at(1, 100.0, 130.0))  # mid 115.0
    aggregator.add(at(2, 120.0, 112.0))  # mid 116.0 — the high bid, and a low ask
    aggregator.add(at(3, 101.0, 140.0))  # mid 120.5 — the high ask, and a low bid

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.bid_high == 120.0 and bar.ask_high == 140.0
    assert bar.mid_high == 120.5
    assert bar.mid_high != (bar.bid_high + bar.ask_high) / 2, "derived from the bars"
    assert bar.mid_low == 115.0
    assert bar.mid_low != (bar.bid_low + bar.ask_low) / 2

    # And the mid is a real observation: every value it reports was some tick's midpoint.
    observed = {115.0, 116.0, 120.5}
    assert {bar.mid_open, bar.mid_high, bar.mid_low, bar.mid_close} <= observed


def test_a_one_sided_tick_advances_only_its_own_series() -> None:
    """A tick carrying a bid and no ask has no mid, so it contributes to the bid OHLC
    and to **nothing else** — values and counts alike.

    Measured on a production snapshot all 588 BTC options were two-sided, so this path is
    rare. That is exactly why it is specified now rather than discovered in six months of
    data.
    """
    aggregator = BarAggregator()
    aggregator.add(at(1, 100.0, 110.0))  # two-sided: all three advance
    aggregator.add(at(2, 999.0, None))  # bid only
    aggregator.add(at(3, None, 5.0))  # ask only
    aggregator.add(at(4, 101.0, 111.0))  # two-sided again

    (bar,) = aggregator.seal(wall_after(1))

    assert (bar.bid_ticks, bar.ask_ticks, bar.mid_ticks) == (3, 3, 2)
    assert bar.bid_high == 999.0, "the one-sided bid must reach the bid high"
    assert bar.ask_low == 5.0, "the one-sided ask must reach the ask low"
    # The one-sided extremes must not have leaked into the mid. If they had, the mid
    # would show (999 + 110)/2 or (100 + 5)/2 — both prices that never existed.
    assert bar.mid_high == 106.0
    assert bar.mid_low == 105.0
    assert bar.mid_open == 105.0 and bar.mid_close == 106.0


def test_the_mid_open_is_not_the_midpoint_of_the_bid_and_ask_opens() -> None:
    """The same trap at the open, which one-sided ticks make reachable.

    The bid opens on the first tick and the mid cannot, because that tick had no ask. So
    `mid_open` is 105.0 while `(bid_open + ask_open)/2` is 60.0 — a price nobody quoted.
    """
    aggregator = BarAggregator()
    aggregator.add(at(1, 10.0, None))
    aggregator.add(at(2, 100.0, 110.0))

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.bid_open == 10.0 and bar.ask_open == 110.0
    assert bar.mid_open == 105.0
    assert bar.mid_open != (bar.bid_open + bar.ask_open) / 2


def test_a_tick_arriving_after_its_bar_was_sealed_is_counted_and_discarded() -> None:
    """Lateness is a policy, not an accident. The tick is refused **and counted** — a
    discarded observation with no counter is the same lie as a silent drop."""
    aggregator = BarAggregator()
    aggregator.add(at(10, 70.0, 72.0))
    (bar,) = aggregator.seal(wall_after(1))
    assert bar.bid_high == 70.0

    aggregator.add(at(20, 999.0, 1000.0))  # far too late, and an extreme

    assert aggregator.late == 1
    assert aggregator.stats()["late"] == 1
    assert aggregator.seal(wall_after(2)) == [], "the sealed minute reopened"
    assert aggregator.flush() == [], "the late tick was kept somewhere"


def test_the_watermark_advances_over_a_minute_that_was_never_open() -> None:
    """A quiet minute still seals.

    Otherwise a tick turning up ten minutes late for a bucket nothing was open for would
    start a fresh bar for a minute long since written, and the store would grow a row out
    of nothing — the no-invention rule broken by the back door.
    """
    aggregator = BarAggregator()
    assert aggregator.seal(wall_after(3)) == []

    aggregator.add(at(10, 70.0, 72.0, minute=1))

    assert aggregator.late == 1
    assert aggregator.flush() == []


def test_a_bar_is_not_sealed_before_its_grace_period_has_passed() -> None:
    """The watermark is the whole point of the grace period: a bar that sealed the
    instant its minute ended would refuse every tick still in flight, and measured lag on
    `ob_l2` runs to 510.3 ms."""
    aggregator = BarAggregator(grace_seconds=2.0)
    aggregator.add(at(10, 70.0, 72.0))
    minute_end = (MINUTE_US + MINUTE) / 1e6

    assert aggregator.seal(minute_end + 0.5) == [], "sealed inside the grace period"
    assert aggregator.seal(minute_end + 1.9) == []
    # A tick still in flight is admitted, because the bar is still open.
    aggregator.add(at(59, 80.0, 82.0))
    (bar,) = aggregator.seal(minute_end + 2.1)
    assert bar.bid_high == 80.0
    assert bar.bid_ticks == 2


def test_a_partial_bar_is_flushed_with_its_true_counts_and_no_flag() -> None:
    """Process stop. The counts already carry the shortness — nine ticks beside
    neighbours with a hundred and eighteen is self-evidently short — so there is no
    separate flag to mislead a reader into thinking it marks the only kind of incomplete
    bar."""
    aggregator = BarAggregator()
    for second in (5, 6, 7):
        aggregator.add(at(second, 70.0 + second, 80.0 + second))

    bars = aggregator.flush()

    assert len(bars) == 1
    (bar,) = bars
    assert (bar.bid_ticks, bar.ask_ticks, bar.mid_ticks) == (3, 3, 3)
    assert not any("partial" in name for name in bar.__slots__)
    assert aggregator.flush() == [], "flush must not re-emit"


def test_ticks_are_bucketed_on_the_venue_clock_and_never_on_our_arrival() -> None:
    """A tick stamped at 09:00:59.9 belongs to 09:00 however late it reaches us.

    Fed through `tick_from_quote`, so the `Quote` this asserts about carries a
    `received_at` in the *next* minute — the exact case where bucketing on arrival would
    move an event across a boundary and our network latency would decide which minute a
    price belonged to.
    """
    frame = {"sy": SYMBOL, "ts": MINUTE_US + 59_900_000, "lts": MINUTE_US + 59_600_000}
    quote = Quote(
        symbol=SYMBOL,
        channel="ob_l2",
        bid=70.0,
        ask=72.0,
        received_at=(MINUTE_US + MINUTE + 240_000) / 1e6,  # 09:01:00.24, the next minute
        frame=frame,
    )

    aggregator = BarAggregator()
    tick = tick_from_quote(quote)
    assert tick is not None
    aggregator.add(tick)
    (bar,) = aggregator.seal(wall_after(1))

    assert bar.minute == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)


def test_lts_is_carried_as_a_column_and_never_bucketed_on() -> None:
    """`lts` sits a median 377 ms before `ts` and ranges to 7,979.5 ms away from it
    (measured, `tools/measure_arrival_lag.py`, 2026-09-04). Its meaning is **unverified**,
    so it is stored and decides nothing.

    Here it points into the previous minute. Bucketing on it would put this bar a minute
    away from where the venue says it belongs.
    """
    aggregator = BarAggregator()
    aggregator.add(
        Tick(
            symbol=SYMBOL,
            exchange_us=MINUTE_US + 5_000_000,
            bid=70.0,
            ask=72.0,
            lts_us=MINUTE_US - 30_000_000,  # the *previous* minute
        )
    )
    aggregator.add(
        Tick(
            symbol=SYMBOL,
            exchange_us=MINUTE_US + 9_000_000,
            bid=71.0,
            ask=73.0,
            lts_us=MINUTE_US + 8_123_456,
        )
    )

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.minute == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    # The last one seen, by the venue's clock, with its microseconds intact.
    assert bar.last_lts == datetime(2026, 9, 4, 9, 0, 8, 123456, tzinfo=timezone.utc)


def test_the_open_and_close_follow_the_venue_clock_not_the_arrival_order() -> None:
    """Two ticks in one minute can reach us out of order — measured `ob_l2` lag spreads
    from 204.2 ms to 510.3 ms, most of a 508 ms republish interval. Taking the
    first-and-last *received* would let our network decide which price opened the
    minute."""
    aggregator = BarAggregator()
    aggregator.add(at(30, 50.0, 60.0))  # arrives first, stamped in the middle
    aggregator.add(at(10, 40.0, 55.0))  # stamped earlier: this is the open
    aggregator.add(at(50, 45.0, 58.0))  # stamped later: this is the close

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.bid_open == 40.0
    assert bar.bid_close == 45.0
    assert bar.ask_open == 55.0
    assert bar.ask_close == 58.0
    assert bar.bid_high == 50.0 and bar.bid_low == 40.0


def test_two_contracts_do_not_contaminate_each_other() -> None:
    """Bars are per contract per minute. One symbol's extreme must never reach another's,
    which a key on the minute alone would allow."""
    other = "P-BTC-75600-040926"
    aggregator = BarAggregator()
    aggregator.add(at(1, 100.0, 110.0))
    aggregator.add(
        Tick(symbol=other, exchange_us=MINUTE_US + 2_000_000, bid=5.0, ask=6.0)
    )

    bars = {bar.symbol: bar for bar in aggregator.seal(wall_after(1))}

    assert set(bars) == {SYMBOL, other}
    assert bars[SYMBOL].bid_low == 100.0
    assert bars[other].bid_high == 5.0
    assert bars[other].option_type == "P" and bars[other].strike == 75600.0


def test_a_symbol_that_cannot_be_parsed_is_refused_and_counted() -> None:
    """Underlying, expiry, strike and option type live only in the symbol. A row that
    cannot be partitioned or filtered is refused rather than stored under a guess — and
    counted, because a refusal nobody can see is a silent drop."""
    aggregator = BarAggregator()
    aggregator.add(Tick(symbol="BTCUSD", exchange_us=MINUTE_US, bid=1.0, ask=2.0))
    aggregator.add(Tick(symbol="", exchange_us=MINUTE_US, bid=1.0, ask=2.0))

    assert aggregator.unparseable == 2
    assert aggregator.flush() == []
    assert aggregator.stats()["ticks"] == 0


def test_a_tick_with_neither_price_advances_nothing() -> None:
    """A frame with an empty book is not a quote. It must not open a bar, because a bar
    that exists with three empty series is a row invented out of no observation."""
    aggregator = BarAggregator()
    aggregator.add(at(1, None, None))

    assert aggregator.empty == 1
    assert aggregator.flush() == []


def test_tick_from_quote_refuses_what_it_cannot_bucket() -> None:
    """The `ticker` channel and any frame without a `ts`.

    `ticker`'s stamp runs a median 3,176 ms and up to 5,298.8 ms behind arrival
    (measured, 2026-09-04) — a whole republish cycle — so it needs its own watermark and
    its own table. And a frame with no `ts` has nothing to bucket on but our arrival
    time, which is the one thing this module exists not to do.
    """
    ticker = Quote(
        symbol=SYMBOL,
        channel="ticker",
        bid=70.0,
        ask=72.0,
        received_at=MINUTE_US / 1e6,
        frame={"sy": SYMBOL, "ts": MINUTE_US},
    )
    stampless = Quote(
        symbol=SYMBOL,
        channel="ob_l2",
        bid=70.0,
        ask=72.0,
        received_at=MINUTE_US / 1e6,
        frame={"sy": SYMBOL},
    )

    assert tick_from_quote(ticker) is None
    assert tick_from_quote(stampless) is None


def test_flushing_also_seals_the_minutes_it_emitted() -> None:
    """A bar emitted by `flush` is as emitted as one sealed by the watermark.

    Without this, a tick arriving after a flush for a minute just written would open a
    *second* bar for it, and the store would end up holding two rows for one
    contract-minute with nothing to say which was whole. Emitted is emitted, however it
    was emitted.
    """
    aggregator = BarAggregator()
    aggregator.add(at(10, 70.0, 72.0))
    (bar,) = aggregator.flush()
    assert bar.bid_ticks == 1

    aggregator.add(at(20, 999.0, 1000.0))

    assert aggregator.late == 1
    assert aggregator.flush() == [], "the flushed minute reopened"


# --- table D: spot bars, at per-underlying grain ----------------------------------


PUT = "P-BTC-75600-040926"
FAR = "C-BTC-90000-111226"


def spot_at(
    second: float, spot: float, *, minute: int = 0, symbol: str = SYMBOL
) -> SpotTick:
    return SpotTick(
        symbol=symbol,
        exchange_us=MINUTE_US + minute * MINUTE + int(second * SECOND_US),
        spot=spot,
    )


def test_spot_is_one_row_per_underlying_per_minute_not_one_per_contract() -> None:
    """Every contract's ticker frame carries the same `sp`, so a naive design would
    store the same four numbers 588 times a minute.

    Worse than the waste: two contracts whose frames straddled a boundary could disagree
    about what spot was, and a store that contradicts itself about the underlying is
    worse than one that does not carry it. So the grain is the **underlying**, and three
    contracts' frames in one minute make **one** row with a tick count of three.
    """
    aggregator = SpotAggregator()
    aggregator.add(spot_at(1, 77650.0, symbol=SYMBOL))
    aggregator.add(spot_at(2, 77700.0, symbol=PUT))
    aggregator.add(spot_at(3, 77600.0, symbol=FAR))

    bars = aggregator.seal(wall_after(1))

    assert len(bars) == 1, "spot was stored per contract instead of per underlying"
    (bar,) = bars
    assert bar.underlying == "BTC"
    assert bar.minute == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    assert bar.spot_ticks == 3
    assert (bar.spot_open, bar.spot_close) == (77650.0, 77600.0)
    assert bar.spot_high >= max(bar.spot_open, bar.spot_close)
    assert bar.spot_low <= min(bar.spot_open, bar.spot_close)
    assert bar.spot_high == 77700.0 and bar.spot_low == 77600.0
    # The grain is the underlying, so a spot bar carries no contract identity at all.
    assert not any(
        name in bar.__slots__ for name in ("symbol", "strike", "expiry", "option_type")
    )


def test_two_underlyings_get_their_own_spot_rows() -> None:
    """The partition key already anticipates ETH. A BTC frame must never move an ETH
    spot bar, which a key on the minute alone would allow."""
    aggregator = SpotAggregator()
    aggregator.add(spot_at(1, 77650.0, symbol=SYMBOL))
    aggregator.add(spot_at(2, 3000.0, symbol="C-ETH-3000-040926"))

    bars = {bar.underlying: bar for bar in aggregator.seal(wall_after(1))}

    assert set(bars) == {"BTC", "ETH"}
    assert bars["BTC"].spot_close == 77650.0
    assert bars["ETH"].spot_close == 3000.0


def test_a_minute_with_no_spot_arrivals_produces_no_spot_row() -> None:
    """The no-invention rule, asserted separately for table D.

    Spot is the best-sampled series in the feed — roughly 7,056 observations a bar — so
    a gap in it is a gap in the *ingester*, and a forward-filled row would hide exactly
    the outage a reader most needs to see. Minutes 1, 2 and 3 are a deliberate silence
    and must come back as nothing at all.
    """
    aggregator = SpotAggregator()
    aggregator.add(spot_at(10, 77650.0, minute=0))
    aggregator.add(spot_at(20, 77660.0, minute=0))
    aggregator.add(spot_at(10, 78000.0, minute=4))

    bars = aggregator.seal(wall_after(5))
    minutes = [bar.minute for bar in bars]

    assert len(bars) == 2, "a silent minute grew a spot row"
    for silent in (1, 2, 3):
        stamp = datetime(2026, 9, 4, 9, silent, tzinfo=timezone.utc)
        assert stamp not in minutes, f"minute {silent} was invented"
    # The row after the silence opens on its own first tick, never on the last close
    # before the gap.
    assert bars[1].spot_open == 78000.0
    assert bars[0].spot_close == 77660.0


def test_spot_bars_are_bucketed_on_the_venue_clock_and_seal_on_the_ticker_watermark(
) -> None:
    """`ticker`'s stamp runs a median 2,882.5 ms and up to 4,696.6 ms behind arrival
    (measured, `tools/measure_arrival_lag.py`, 2026-09-04, 8,220 frames). Under
    `ob_l2`'s 2.0 s grace almost every spot frame would be late; under the ticker
    watermark it is admitted."""
    aggregator = SpotAggregator()
    aggregator.add(spot_at(59, 77650.0))
    minute_end = (MINUTE_US + MINUTE) / 1e6

    assert aggregator.grace_seconds >= 5.3, "the ticker watermark is too small to work"
    assert aggregator.seal(minute_end + 2.0) == [], "sealed on ob_l2's grace"
    # A frame stamped inside the minute but discovered 4.7 s later is still admitted.
    aggregator.add(spot_at(59.5, 77700.0))
    (bar,) = aggregator.seal(minute_end + aggregator.grace_seconds + 0.1)
    assert bar.spot_ticks == 2
    assert bar.spot_close == 77700.0


def test_a_ticker_frame_without_a_spot_advances_nothing() -> None:
    """`sp` missing is not a spot of zero. A bar with no observation behind it is a row
    invented out of nothing."""
    aggregator = SpotAggregator()
    aggregator.add(SpotTick(symbol=SYMBOL, exchange_us=MINUTE_US, spot=None))
    aggregator.add(SpotTick(symbol="BTCUSD", exchange_us=MINUTE_US, spot=77650.0))

    assert aggregator.empty == 1
    assert aggregator.unparseable == 1
    assert aggregator.flush() == []


# --- table B: reference bars ------------------------------------------------------


def reference_at(
    second: float,
    *,
    minute: int = 0,
    symbol: str = SYMBOL,
    mark: float | None = 1000.0,
    ltp: float | None = 1082.0,
    oi_contracts: float | None = 1997.0,
    oi_change_usd_6h: float | None = 74608.2,
    turnover: float | None = 411134.7807,
    venue_delta: float | None = -0.739,
    venue_mark_iv: float | None = 0.316,
) -> ReferenceTick:
    return ReferenceTick(
        symbol=symbol,
        exchange_us=MINUTE_US + minute * MINUTE + int(second * SECOND_US),
        mark=mark,
        last_traded_price=ltp,
        oi_contracts=oi_contracts,
        oi_change_usd_6h=oi_change_usd_6h,
        turnover=turnover,
        venue_delta=venue_delta,
        venue_gamma=0.000245,
        venue_rho=-1.7038,
        venue_theta=-202.29,
        venue_vega=13.609,
        venue_bid_iv=0.311,
        venue_ask_iv=0.321,
        venue_mark_iv=venue_mark_iv,
    )


def test_mark_and_the_last_traded_price_get_a_range_and_the_levels_do_not() -> None:
    """Mark and LTP are prices and they move, so a minute of them has a high and a low
    worth keeping. Open interest, turnover, the venue's Greeks and its implied vols are
    **levels**: an open/high/low/close of rho would be four numbers describing nothing.

    Asserted as properties — the high bounds the open and the close — rather than as
    restated constants, because a comparison written backwards passes an equality check
    whenever the extreme happened to arrive first.
    """
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(1, mark=1000.0, ltp=1082.0, venue_delta=-0.70))
    aggregator.add(reference_at(2, mark=1200.0, ltp=1100.0, venue_delta=-0.75))
    aggregator.add(reference_at(3, mark=900.0, ltp=1050.0, venue_delta=-0.80))
    aggregator.add(reference_at(4, mark=1100.0, ltp=1090.0, venue_delta=-0.85))

    (bar,) = aggregator.seal(wall_after(1))

    assert (bar.mark_open, bar.mark_close) == (1000.0, 1100.0)
    assert bar.mark_high >= max(bar.mark_open, bar.mark_close)
    assert bar.mark_low <= min(bar.mark_open, bar.mark_close)
    assert bar.mark_high == 1200.0 and bar.mark_low == 900.0
    assert bar.mark_ticks == 4

    assert (bar.ltp_open, bar.ltp_close) == (1082.0, 1090.0)
    assert bar.ltp_high == 1100.0 and bar.ltp_low == 1050.0
    assert bar.ltp_ticks == 4

    # The levels carry one sample and no range at all.
    assert bar.venue_delta == -0.85, "the last sample in the bar, not the first"
    assert not any(
        name.startswith(("venue_", "oi_", "turnover")) and name.endswith(
            ("_open", "_high", "_low", "_close")
        )
        for name in bar.__slots__
    )
    assert bar.symbol == SYMBOL and bar.underlying == "BTC"
    assert bar.expiry == "04-09-2026" and bar.strike == 77600.0
    assert bar.option_type == "C"


def test_the_reference_levels_come_from_one_frame_rather_than_from_several() -> None:
    """A row whose delta came from 09:00:12 and whose vega came from 09:00:47 is not a
    snapshot of anything.

    So last-value-in-bar means **the last frame's values, taken together**. Here the last
    frame carries a null mark IV; the bar reports the null rather than reaching back for
    an older one and pairing it with a newer delta.
    """
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(1, venue_delta=-0.70, venue_mark_iv=0.30))
    aggregator.add(reference_at(2, venue_delta=-0.90, venue_mark_iv=None))

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.venue_delta == -0.90
    assert bar.venue_mark_iv is None, "the levels were assembled from two frames"


def test_the_levels_are_taken_by_the_venue_clock_not_by_arrival_order() -> None:
    """Ticker frames can reach us out of order — measured lag spreads from 981.7 ms to
    4,696.6 ms, most of the channel's own 5,001 ms republish interval — so "last" has to
    mean last by Delta's stamp or our network picks the bar's reference values."""
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(40, venue_delta=-0.40))  # arrives first, stamped later
    aggregator.add(reference_at(10, venue_delta=-0.10))  # stamped earlier

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.venue_delta == -0.40


def test_a_contract_that_never_traded_has_no_last_traded_price_and_no_ltp_ticks() -> None:
    """Sixteen of the 136 captured symbols send `ohlc: [null, null, null, null]`.

    Their mark still moves — Delta prices them from a model — so the bar exists and is
    useful. Its LTP columns stay null and `ltp_ticks` stays zero, which is the honest
    record of "no trade has ever happened here". A zero price would claim someone paid
    nothing.
    """
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(1, mark=1000.0, ltp=None, turnover=None))
    aggregator.add(reference_at(2, mark=1010.0, ltp=None, turnover=None))

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.mark_ticks == 2 and bar.mark_close == 1010.0
    assert bar.ltp_ticks == 0
    assert (bar.ltp_open, bar.ltp_high, bar.ltp_low, bar.ltp_close) == (
        None,
        None,
        None,
        None,
    )
    assert bar.turnover is None


def test_a_minute_with_no_ticker_arrivals_produces_no_reference_row() -> None:
    """The no-invention rule, asserted separately for table B.

    A far-dated strike can go quiet for a whole session. Its mark would forward-fill
    beautifully and read as a live valuation that nobody published — which is exactly the
    defect in Delta's own `/v2/history/candles`, where `C-BTC-60000-270624` returns 801
    daily bars of which 797 are fabricated.
    """
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(10, minute=0, mark=1000.0))
    aggregator.add(reference_at(10, minute=4, mark=1500.0))

    bars = aggregator.seal(wall_after(5))
    minutes = [bar.minute for bar in bars]

    assert len(bars) == 2, "a silent minute grew a reference row"
    for silent in (1, 2, 3):
        stamp = datetime(2026, 9, 4, 9, silent, tzinfo=timezone.utc)
        assert stamp not in minutes, f"minute {silent} was invented"
    assert bars[1].mark_open == 1500.0, "the bar after the gap opened on a stale mark"


def test_a_ticker_frame_carrying_no_values_at_all_opens_no_reference_bar() -> None:
    """Found in review rather than predicted by the ticket.

    A frame whose `d` list is empty or whose body is — control traffic, a truncated
    payload, a contract Delta has stopped populating — decodes to a `ReferenceTick` of
    fifteen `None`s. Opening a bucket on it would emit a row where every column is null
    and `mark_ticks` is zero: **a row with no observation behind it**, which is the same
    fabrication as a forward-fill wearing different clothes. It is refused and counted.

    A frame with *some* values is a different thing entirely and is kept: a contract that
    has never traded has no LTP and its mark still moves, and that bar is real.
    """
    aggregator = ReferenceAggregator()
    aggregator.add(
        ReferenceTick(
            symbol=SYMBOL,
            exchange_us=MINUTE_US + SECOND_US,
            mark=None,
            last_traded_price=None,
            oi_contracts=None,
            oi_change_usd_6h=None,
            turnover=None,
            venue_delta=None,
            venue_gamma=None,
            venue_rho=None,
            venue_theta=None,
            venue_vega=None,
            venue_bid_iv=None,
            venue_ask_iv=None,
            venue_mark_iv=None,
        )
    )

    assert aggregator.empty == 1
    assert aggregator.ticks == 0
    assert aggregator.flush() == [], "a frame with no values grew a row of nulls"

    # ...while a frame carrying only one of the fifteen is a real observation.
    aggregator.add(reference_at(2, mark=None, ltp=None, turnover=None, oi_contracts=None,
                                oi_change_usd_6h=None, venue_delta=None,
                                venue_mark_iv=0.31))
    (bar,) = aggregator.flush()
    assert bar.venue_mark_iv == 0.31
    assert bar.mark_ticks == 0 and bar.ltp_ticks == 0


def test_the_reference_bar_stores_none_of_the_fields_the_ticket_drops() -> None:
    """Price band, 24-hour mark change, the symbol echo and the product id are static or
    derivable, and the ticker's own bid and ask are the book channel's job. None of them
    is a column here.

    Spot is absent too, and for a stronger reason: it belongs to the underlying, so
    putting it on 588 contract rows a minute would let two contracts whose frames
    straddled a boundary disagree about what spot was.
    """
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(1))
    (bar,) = aggregator.seal(wall_after(1))

    for banned in (
        "price_band",
        "pb",
        "mark_change_24h",
        "m24hc",
        "product_id",
        "bid",
        "ask",
        "spot",
        "ohlc_open",
        "ohlc_high",
        "ohlc_low",
    ):
        assert banned not in bar.__slots__, banned


def test_reference_bars_seal_on_the_ticker_watermark_and_not_on_the_books() -> None:
    """`ob_l2`'s 2.0 s grace would call almost every ticker frame late — the channel's
    stamp runs a median 2,882.5 ms and up to 4,696.6 ms behind arrival (measured,
    2026-09-04, 8,220 frames). That is the whole reason table B needed a watermark of
    its own."""
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(59))
    minute_end = (MINUTE_US + MINUTE) / 1e6

    assert aggregator.grace_seconds >= 5.3
    assert aggregator.seal(minute_end + 2.0) == [], "sealed on ob_l2's grace"
    assert len(aggregator.seal(minute_end + aggregator.grace_seconds + 0.1)) == 1


def test_a_reference_tick_arriving_after_its_bar_was_sealed_is_counted_and_discarded(
) -> None:
    """Lateness is a policy here too, and a discarded observation with no counter is the
    same lie as a silent drop."""
    aggregator = ReferenceAggregator()
    aggregator.add(reference_at(10, mark=1000.0))
    (bar,) = aggregator.seal(wall_after(1))
    assert bar.mark_close == 1000.0

    aggregator.add(reference_at(20, mark=9999.0))

    assert aggregator.late == 1
    assert aggregator.flush() == [], "the late tick was kept somewhere"


# --- the ticker frame, decoded once for all three tables --------------------------


def ticker_quote(frame: dict, bid: float | None = 1066.0, ask: float | None = 1080.0):
    return Quote(
        symbol=frame["sy"],
        channel="ticker",
        bid=bid,
        ask=ask,
        received_at=(MINUTE_US + 3_200_000) / 1e6,
        frame=frame,
    )


def test_a_real_ticker_frame_lands_in_all_three_tables(ws_ticker_frames) -> None:
    """One frame, one decode, three destinations — and the array offsets are read by
    `wire`, never re-indexed here.

    The values are checked against the frame's own payload rather than against constants
    written out again: `g[0]` is delta and `qiv[2]` is the mark IV because
    `tests/test_wire.py` pins those orderings against the REST snapshot captured
    alongside, and this test would move with them.
    """
    symbol = "P-BTC-78500-040926"
    frame = ws_ticker_frames[symbol]
    body = frame["d"][0]

    sample = samples_from_ticker(ticker_quote(frame))

    assert sample is not None
    assert sample.reference.symbol == symbol
    assert sample.reference.exchange_us == frame["ts"]
    assert sample.reference.mark == float(body["m"])
    assert sample.reference.last_traded_price == body["ohlc"][3]
    assert sample.reference.oi_contracts == float(body["oi"][0])
    assert sample.reference.oi_change_usd_6h == float(body["oi"][1])
    assert sample.reference.turnover == body["to"][0]
    assert sample.reference.venue_delta == float(body["g"][0])
    assert sample.reference.venue_vega == float(body["g"][4])
    assert sample.reference.venue_mark_iv == float(body["qiv"][2])

    assert sample.spot.spot == float(frame["sp"])
    assert sample.spot.exchange_us == frame["ts"]

    assert sample.quote is not None
    assert sample.quote.source == "ticker"
    assert (sample.quote.bid, sample.quote.ask) == (1066.0, 1080.0)


def test_a_ticker_frame_with_no_stamp_is_refused_whole() -> None:
    """Bucketing on our arrival time is the one thing this module exists not to do, and
    a reference row without a bucket cannot be salvaged."""
    assert samples_from_ticker(ticker_quote({"sy": SYMBOL, "d": [{}]})) is None


def test_samples_from_ticker_refuses_the_book_channel() -> None:
    """The two converters do not overlap. `tick_from_quote` owns `ob_l2` and this owns
    `ticker`; a frame reaching both would be counted twice in one bar."""
    book = Quote(
        symbol=SYMBOL,
        channel="ob_l2",
        bid=70.0,
        ask=72.0,
        received_at=MINUTE_US / 1e6,
        frame={"sy": SYMBOL, "ts": MINUTE_US},
    )

    assert samples_from_ticker(book) is None
    assert tick_from_quote(book) is not None


# --- the provenance flag on table A -----------------------------------------------


def from_ticker(
    second: float, bid: float | None, ask: float | None, *, minute: int = 0
) -> Tick:
    """The same tick, arriving on the slower channel."""
    return Tick(
        symbol=SYMBOL,
        exchange_us=MINUTE_US + minute * MINUTE + int(second * SECOND_US),
        bid=bid,
        ask=ask,
        source="ticker",
    )


def test_a_minute_of_book_quotes_is_flagged_as_coming_from_the_book() -> None:
    """The ordinary case, and the one the flag has to get right most often."""
    aggregator = BarAggregator()
    aggregator.add(at(1, 70.0, 72.0))
    aggregator.add(at(2, 71.0, 73.0))

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.from_book is True
    assert bar.bid_ticks == 2


def test_a_contract_with_no_book_falls_back_to_the_ticker_and_says_so() -> None:
    """A contract nobody is making a market in gets no `ob_l2` frames at all, and under
    #10 that minute produced no row — a silence indistinguishable from the ingester being
    down.

    The ticker channel still carries a quote for it, about ten times more slowly. So the
    bar exists, is built from those samples, and is flagged **False**: twelve ticks with
    `from_book=False` is "no book at all", while twelve with `from_book=True` is a quiet
    book, and a tick count alone cannot tell the two apart.
    """
    aggregator = BarAggregator()
    aggregator.add(from_ticker(5, 100.0, 110.0))
    aggregator.add(from_ticker(35, 102.0, 108.0))

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.from_book is False
    assert (bar.bid_open, bar.bid_close) == (100.0, 102.0)
    assert bar.bid_ticks == bar.ask_ticks == bar.mid_ticks == 2
    assert bar.mid_high == 105.0


def test_the_book_overrides_the_ticker_wholesale_when_both_arrive() -> None:
    """`wire.chain_from_frames` replaces a ticker quote with a book quote outright rather
    than averaging them, and a bar has to make the same choice or its provenance flag
    would be answering for a mixture.

    The ticker samples here carry extremes far outside the book's range. None of them may
    reach the bar: if they did, `bid_high` would be 999.0 and the row would claim a price
    the book never showed while also claiming to have come from the book.
    """
    aggregator = BarAggregator()
    aggregator.add(from_ticker(1, 999.0, 1000.0))
    aggregator.add(at(2, 70.0, 72.0))
    aggregator.add(at(3, 75.0, 78.0))
    aggregator.add(from_ticker(4, 1.0, 2.0))

    (bar,) = aggregator.seal(wall_after(1))

    assert bar.from_book is True
    assert bar.bid_ticks == 2, "the ticker samples were counted into the book's bar"
    assert bar.bid_high == 75.0 and bar.bid_low == 70.0
    assert bar.ask_high == 78.0 and bar.ask_low == 72.0
    assert (bar.bid_open, bar.bid_close) == (70.0, 75.0)


def test_one_contracts_fallback_does_not_flag_another_contracts_bar() -> None:
    """The flag is per contract per minute, like everything else in the table. A silent
    book on one strike says nothing about the strike beside it."""
    other = "P-BTC-75600-040926"
    aggregator = BarAggregator()
    aggregator.add(at(1, 70.0, 72.0))
    aggregator.add(
        Tick(
            symbol=other,
            exchange_us=MINUTE_US + 2 * SECOND_US,
            bid=5.0,
            ask=6.0,
            source="ticker",
        )
    )

    bars = {bar.symbol: bar for bar in aggregator.seal(wall_after(1))}

    assert bars[SYMBOL].from_book is True
    assert bars[other].from_book is False


def test_the_flag_follows_the_minute_and_not_the_contract() -> None:
    """A book that goes quiet for one minute and comes back must produce one `True` row
    and one `False` row, not a single verdict for the contract.

    This is what makes the column worth storing: it is a per-bar record of how that bar
    was observed, so a gap in the book is visible as a run of `False` rows rather than
    as a hole.
    """
    aggregator = BarAggregator()
    aggregator.add(at(10, 70.0, 72.0, minute=0))
    aggregator.add(from_ticker(10, 71.0, 73.0, minute=1))
    aggregator.add(at(10, 72.0, 74.0, minute=2))

    bars = aggregator.seal(wall_after(3))

    assert [bar.from_book for bar in bars] == [True, False, True]


def test_the_quote_bars_wait_long_enough_for_a_fallback_to_arrive() -> None:
    """The watermark change #11 forced, asserted rather than described.

    A ticker frame stamped inside a minute is discovered up to 5.3 s later (measured).
    Sealing table A on `ob_l2`'s 2.0 s grace would close the bar before its fallback
    could land, every fallback quote would be counted late, and `from_book` would be a
    constant `True` — a column that stores nothing.
    """
    aggregator = BarAggregator()
    minute_end = (MINUTE_US + MINUTE) / 1e6

    assert aggregator.grace_seconds >= 5.3, "a fallback quote can never be admitted"
    aggregator.add(at(59, 70.0, 72.0))
    assert aggregator.seal(minute_end + 2.0) == [], "sealed on the book's grace alone"

    # The ticker's copy of that same minute, discovered 5.3 s after the boundary.
    aggregator.add(from_ticker(59.5, 71.0, 73.0))
    (bar,) = aggregator.seal(minute_end + aggregator.grace_seconds + 0.1)

    assert aggregator.late == 0, "a fallback quote inside the watermark was called late"
    assert bar.from_book is True
    assert bar.bid_ticks == 1, "the fallback contaminated a bar the book had covered"
