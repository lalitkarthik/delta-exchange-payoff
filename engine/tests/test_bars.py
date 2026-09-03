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

from deltapayoff.bars import BarAggregator, Tick, tick_from_quote
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
