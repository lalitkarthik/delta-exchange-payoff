"""The store: hive-partitioned Parquet, read back in Polars.

Two assertions carry this file. **Row count** — the number of rows read back must equal
the number of minutes that actually contained ticks, which is "no invented rows" made
executable. And **types**, because a store whose columns come back as strings makes every
reader remember units and parse them, and this one is meant to outlive the memory of
whoever wrote it.

Partition pruning is tested as *behaviour*: two dates and two underlyings written, a
filtered scan asserted to return only the matching rows. Not as configuration, and not as
a read-time threshold — a timing assertion would be flaky and would not be the point.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from deltapayoff.bars import (
    BarAggregator,
    ComputedAggregator,
    ComputedBar,
    ComputedTick,
    QuoteBar,
    ReferenceAggregator,
    ReferenceBar,
    ReferenceTick,
    SpotAggregator,
    SpotBar,
    SpotTick,
    Tick,
)
from deltapayoff.chain import EXPIRY_FORMAT, nearest_strike
from deltapayoff.compute import MODEL_VERSION, enrich
from deltapayoff.fanout import FanOut
from deltapayoff.feed import Quote
from deltapayoff.forward import DAYS_PER_YEAR, SETTLEMENT_HOUR_UTC
from deltapayoff.models import ChainResponse, ChainRow, ComputedLeg, Leg
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
)
from deltapayoff.wire import chain_from_frames, decode_ob_l2, decode_ticker

MINUTE_US = int(datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1e6)
MINUTE = 60_000_000


def bar(
    symbol: str = "C-BTC-77600-040926",
    underlying: str = "BTC",
    minute: datetime | None = None,
    option_type: str = "C",
    strike: float = 77600.0,
    expiry: str = "04-09-2026",
    last_lts: datetime | None = None,
    from_book: bool = True,
) -> QuoteBar:
    return QuoteBar(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute or datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        bid_open=70.0,
        bid_high=75.5,
        bid_low=68.0,
        bid_close=73.0,
        bid_ticks=118,
        ask_open=72.0,
        ask_high=78.0,
        ask_low=71.0,
        ask_close=74.0,
        ask_ticks=118,
        mid_open=71.0,
        mid_high=76.75,
        mid_low=69.5,
        mid_close=73.5,
        mid_ticks=118,
        from_book=from_book,
        last_lts=last_lts,
    )


def test_a_bar_round_trips_through_parquet_with_its_values_and_its_types(
    tmp_path: Path,
) -> None:
    """Values, **types** and row count. The types are half the point: 64-bit prices
    because a five-figure BTC price with decimals already spends six of a 32-bit float's
    seven significant digits, and microsecond UTC because that is Delta's own resolution
    and any other unit is a conversion waiting to be forgotten."""
    store = BarStore(tmp_path)
    store.add([bar(last_lts=datetime(2026, 9, 4, 9, 0, 8, 123456, tzinfo=timezone.utc))])
    assert store.flush() == 1

    frame = store.scan().collect()
    schema = frame.collect_schema()

    assert frame.height == 1
    for column in (
        "strike",
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
        "mid_open",
        "mid_high",
        "mid_low",
        "mid_close",
    ):
        assert schema[column] == pl.Float64, column
    for column in ("bid_ticks", "ask_ticks", "mid_ticks"):
        assert schema[column] == pl.UInt32, column
    for column in ("symbol", "expiry", "option_type", "underlying"):
        assert schema[column] == pl.Categorical, column
    assert schema["minute"] == pl.Datetime("us", "UTC")
    assert schema["last_lts"] == pl.Datetime("us", "UTC")
    assert schema["date"] == pl.Date

    row = frame.row(0, named=True)
    assert row["symbol"] == "C-BTC-77600-040926"
    assert row["strike"] == 77600.0
    assert row["expiry"] == "04-09-2026"
    assert row["option_type"] == "C"
    assert row["minute"] == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    assert row["date"] == date(2026, 9, 4)
    assert (row["bid_open"], row["bid_high"], row["bid_low"], row["bid_close"]) == (
        70.0,
        75.5,
        68.0,
        73.0,
    )
    assert row["mid_high"] == 76.75
    assert row["mid_ticks"] == 118
    # Microseconds survive. A timestamp routed through a float would round the last
    # digits away, and nothing would say so.
    assert row["last_lts"] == datetime(2026, 9, 4, 9, 0, 8, 123456, tzinfo=timezone.utc)


def test_the_row_count_equals_the_minutes_that_actually_had_ticks(
    tmp_path: Path,
) -> None:
    """"No invented rows", made executable, through the whole path.

    Twenty minutes of wall time, ticks in six of them. The store must hold **six** rows.
    Delta's `/v2/history/candles` would hold twenty, fourteen of them fabricated from the
    last trade — `C-BTC-60000-270624` returns 801 daily bars of which 797 are invented.
    """
    aggregator = BarAggregator()
    quiet = {3, 4, 5, 9, 10, 11, 12, 14, 15, 16, 17, 18, 19, 20}
    busy = [minute for minute in range(1, 21) if minute not in quiet]
    for minute in busy:
        for second in (5, 25, 45):
            aggregator.add(
                Tick(
                    symbol="C-BTC-77600-040926",
                    exchange_us=MINUTE_US + minute * MINUTE + second * 1_000_000,
                    bid=70.0 + minute,
                    ask=72.0 + minute,
                )
            )

    store = BarStore(tmp_path)
    store.add(aggregator.seal((MINUTE_US + 25 * MINUTE) / 1e6))
    written = store.flush()

    frame = store.scan().collect().sort("minute")
    assert written == len(busy) == 6
    assert frame.height == 6, "the store grew rows for minutes that had no ticks"
    assert frame["minute"].dt.minute().to_list() == busy
    assert frame["bid_ticks"].to_list() == [3] * 6


def test_a_filtered_scan_returns_only_the_matching_partition(tmp_path: Path) -> None:
    """Partition pruning as behaviour. Two dates and two underlyings; a filter on one
    date and one underlying must bring back only its rows.

    The read-time difference partitioning buys is a **measurement** for the findings, not
    an assertion here — a timing threshold would be flaky and would not be the point.
    """
    store = BarStore(tmp_path)
    store.add(
        [
            bar(underlying="BTC", minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)),
            bar(
                symbol="C-ETH-3000-040926",
                underlying="ETH",
                strike=3000.0,
                minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
            ),
            bar(underlying="BTC", minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)),
            bar(
                symbol="C-ETH-3000-050926",
                underlying="ETH",
                strike=3000.0,
                expiry="05-09-2026",
                minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    assert store.flush() == 4

    everything = store.scan().collect()
    assert everything.height == 4

    one = (
        store.scan()
        .filter(pl.col("date") == date(2026, 9, 4))
        .filter(pl.col("underlying") == "BTC")
        .collect()
    )
    assert one.height == 1
    assert one.row(0, named=True)["symbol"] == "C-BTC-77600-040926"

    # The directories the filter answers, without opening a file.
    written = {
        path.relative_to(store.path).as_posix()
        for path in store.path.rglob("*.parquet")
    }
    assert {part.rsplit("/", 1)[0] for part in written} == {
        "date=2026-09-04/underlying=BTC",
        "date=2026-09-04/underlying=ETH",
        "date=2026-09-05/underlying=BTC",
        "date=2026-09-05/underlying=ETH",
    }


def test_expiry_strike_and_option_type_are_columns_not_partition_levels(
    tmp_path: Path,
) -> None:
    """Expiry as a partition level explodes into thousands of tiny directories and makes
    Parquet slower than CSV — each small file carries header and footer overhead and a
    reader has to open all of them. So the directory tree is `date/underlying` and
    nothing else, and three expiries share one file."""
    store = BarStore(tmp_path)
    store.add(
        [
            bar(symbol="C-BTC-77600-040926", expiry="04-09-2026"),
            bar(symbol="C-BTC-77600-110926", expiry="11-09-2026", strike=77600.0),
            bar(symbol="P-BTC-70000-181226", expiry="18-12-2026", option_type="P"),
        ]
    )
    store.flush()

    directories = {
        path.relative_to(store.path).parent.as_posix()
        for path in store.path.rglob("*.parquet")
    }
    assert directories == {"date=2026-09-04/underlying=BTC"}
    assert len(list(store.path.rglob("*.parquet"))) == 1

    frame = store.scan().collect()
    assert sorted(frame["expiry"].to_list()) == ["04-09-2026", "11-09-2026", "18-12-2026"]
    assert sorted(frame["option_type"].unique().to_list()) == ["C", "P"]


def test_a_second_flush_adds_to_a_partition_rather_than_overwriting_it(
    tmp_path: Path,
) -> None:
    """Hourly flushes land in the same directory all day.

    Polars names a partitioned write's file `00000000.parquet` every time, so letting it
    lay out the tree would have the 10:00 flush silently overwrite the 09:00 one and the
    day would end holding its last hour. The loss would be invisible: the file is
    perfectly valid, just short.
    """
    store = BarStore(tmp_path)
    store.add([bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc))])
    store.flush()
    store.add([bar(minute=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc))])
    store.flush()

    files = list(store.path.rglob("*.parquet"))
    assert len(files) == 2, "the second flush overwrote the first"
    frame = store.scan().collect().sort("minute")
    assert frame.height == 2
    assert frame["minute"].dt.hour().to_list() == [9, 10]


def test_an_empty_store_scans_to_no_rows_rather_than_raising(tmp_path: Path) -> None:
    """A reader opening the store before the first flush should get an empty frame with
    the real schema, not an exception about a missing directory. Absence is a legitimate
    answer here — it is the same discipline as a minute with no row."""
    store = BarStore(tmp_path)
    frame = store.scan().collect()

    assert frame.height == 0
    assert frame.collect_schema()["minute"] == pl.Datetime("us", "UTC")
    assert frame.collect_schema()["underlying"] == pl.Categorical
    assert store.flush() == 0


def test_a_null_last_lts_round_trips_as_null(tmp_path: Path) -> None:
    """`lts` is absent on some frames and its meaning is unverified anyway. An absent one
    must read back absent rather than as an epoch zero that looks like 1970."""
    store = BarStore(tmp_path)
    store.add([bar(last_lts=None)])
    store.flush()

    assert store.scan().collect()["last_lts"].to_list() == [None]


def test_the_writer_subscribes_losslessly(tmp_path: Path) -> None:
    """Drop-oldest systematically shaves the highs and lows, because drops happen under
    load and load is when price moves fastest. That is a bias, not noise, and it is
    invisible in the output — so the writer takes the other policy."""
    bus = FanOut()
    writer = BarWriter(BarStore(tmp_path))
    subscription = writer.attach(bus)

    assert subscription.lossless is True
    assert bus.stats()[subscription.name]["lossless"] is True


def test_the_writer_turns_bus_quotes_into_parquet_bars(tmp_path: Path) -> None:
    """The tracer bullet, end to end: bus -> aggregation -> seal -> partitioned write ->
    read back. Driven by a fake clock, so nothing here waits on a real one."""

    async def scenario():
        now = MINUTE_US / 1e6

        def clock() -> float:
            return now

        store = BarStore(tmp_path)
        writer = BarWriter(store, clock=clock, flush_seconds=3600.0, tick_seconds=0.01)
        bus = FanOut()
        writer.attach(bus)
        task = asyncio.create_task(writer.run())

        for second in (5, 25, 45):
            bus.publish(
                Quote(
                    symbol="C-BTC-77600-040926",
                    channel="ob_l2",
                    bid=70.0 + second,
                    ask=72.0 + second,
                    received_at=now,
                    frame={
                        "sy": "C-BTC-77600-040926",
                        "ts": MINUTE_US + second * 1_000_000,
                        "lts": MINUTE_US + second * 1_000_000 - 300_000,
                    },
                )
            )
        # A ticker frame on the same bus. It must not reach the quote bars: its `ts` runs
        # a median 3,176 ms behind arrival and it belongs to a table of its own.
        bus.publish(
            Quote(
                symbol="C-BTC-77600-040926",
                channel="ticker",
                bid=1.0,
                ask=2.0,
                received_at=now,
                frame={"sy": "C-BTC-77600-040926", "ts": MINUTE_US + 30_000_000},
            )
        )

        await asyncio.sleep(0.05)
        now = (MINUTE_US + 2 * MINUTE) / 1e6  # past the boundary and the grace period
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        return store

    store = asyncio.run(scenario())
    frame = store.scan().collect()

    assert frame.height == 1
    row = frame.row(0, named=True)
    assert row["bid_ticks"] == 3, "the ticker frame contaminated the quote bar"
    assert row["bid_open"] == 75.0 and row["bid_close"] == 115.0
    assert row["ask_high"] == 117.0
    assert row["minute"] == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)


def test_a_slow_flush_cannot_block_the_socket_reader(tmp_path: Path) -> None:
    """The failure this whole architecture exists to prevent.

    If a disk flush ran on the event loop, the socket reader could not be scheduled while
    it worked, the operating system's receive buffer would fill, and **Delta would close
    the connection** — nobody having enforced a limit; we simply failed to keep up.

    So the store is given a flush that blocks for a quarter of a second, and a stand-in
    for the reader — a task that does nothing but `await` and publish, exactly as
    `DeltaFeed._pump` awaits `socket.recv()` — measures the longest it ever went without
    being scheduled. **Measuring the time around `bus.publish` is not enough and was the
    first version of this test: `publish` is synchronous and returns instantly whether or
    not the loop behind it is wedged.** The gap between the reader's turns is the thing
    that kills a connection, so that is what is asserted.
    """

    class SlowStore(BarStore):
        calls = 0

        def flush(self) -> int:
            import time as _time

            type(self).calls += 1
            _time.sleep(0.25)  # a blocking write, exactly what must not be on the loop
            return super().flush()

    async def scenario():
        now = MINUTE_US / 1e6
        loop = asyncio.get_running_loop()
        longest = 0.0

        store = SlowStore(tmp_path)
        writer = BarWriter(
            store, clock=lambda: now, flush_seconds=0.0, tick_seconds=0.01
        )
        bus = FanOut()
        subscription = writer.attach(bus)

        async def socket_reader():
            nonlocal longest
            await asyncio.sleep(0)
            last = loop.time()
            published = 0
            while True:
                await asyncio.sleep(0)
                turn = loop.time()
                longest = max(longest, turn - last)
                last = turn
                if published < 200:
                    bus.publish(
                        Quote(
                            symbol="C-BTC-77600-040926",
                            channel="ob_l2",
                            bid=float(published),
                            ask=float(published) + 1,
                            received_at=now,
                            frame={
                                "sy": "C-BTC-77600-040926",
                                "ts": MINUTE_US + (published % 60) * 1_000_000,
                            },
                        )
                    )
                    published += 1

        writing = asyncio.create_task(writer.run())
        reading = asyncio.create_task(socket_reader())
        await asyncio.sleep(0.6)  # long enough for two of the slow flushes
        reading.cancel()
        writing.cancel()
        await asyncio.gather(reading, writing, return_exceptions=True)
        return longest, subscription, writer

    longest, subscription, writer = asyncio.run(scenario())

    assert longest < 0.05, f"the reader went {longest:.3f}s without a turn"
    assert subscription.dropped == 0
    assert writer.aggregator.ticks == 200, "the writer lost ticks while flushing"
    assert type(writer.store).calls >= 2, "the slow flush never ran"


def test_the_store_root_is_created_on_demand(tmp_path: Path) -> None:
    """The engine should come up against an empty disk without a setup step."""
    root = tmp_path / "does" / "not" / "exist"
    store = BarStore(root)
    store.add([bar()])
    store.flush()

    assert store.path.exists()
    assert store.scan().collect().height == 1


def test_flushing_an_empty_buffer_writes_no_file(tmp_path: Path) -> None:
    """An hourly flush over a quiet hour must not leave an empty Parquet file behind.
    Thousands of tiny files are the thing the buffering exists to avoid, and an empty one
    is the worst of them: all overhead, no rows."""
    store = BarStore(tmp_path)

    assert store.flush() == 0
    assert store.flushes == 0, "an empty flush must not consume a file ordinal"
    assert list(store.path.rglob("*.parquet")) == []


@pytest.mark.parametrize("bad", [None, ""])
def test_the_store_refuses_a_bar_it_cannot_partition(tmp_path: Path, bad) -> None:
    """`date` and `underlying` are the directory names. A row missing either cannot be
    placed, and placing it under a guess would put quotes in a day they did not happen
    in."""
    store = BarStore(tmp_path)
    broken = bar()
    object.__setattr__(broken, "underlying", bad)

    with pytest.raises(ValueError):
        store.add([broken])




# --- table B: reference bars on disk ----------------------------------------------


def reference_store(root: Path) -> BarStore:
    return BarStore(root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA)


def spot_store(root: Path) -> BarStore:
    return BarStore(root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA)


def reference(
    symbol: str = "C-BTC-77600-040926",
    underlying: str = "BTC",
    minute: datetime | None = None,
    expiry: str = "04-09-2026",
    strike: float = 77600.0,
    option_type: str = "C",
    ltp_ticks: int = 12,
    ltp: float | None = 1082.0,
) -> ReferenceBar:
    return ReferenceBar(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute or datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        mark_open=1059.85780065,
        mark_high=1071.5,
        mark_low=1050.25,
        mark_close=1060.125,
        mark_ticks=12,
        ltp_open=ltp,
        ltp_high=ltp,
        ltp_low=ltp,
        ltp_close=ltp,
        ltp_ticks=ltp_ticks,
        oi_contracts=1997.0,
        oi_change_usd_6h=-41302.35,
        turnover=411134.7807,
        venue_delta=-0.73938982,
        venue_gamma=0.00024511,
        venue_rho=-1.7038088,
        venue_theta=-202.29182089,
        venue_vega=13.60933495,
        venue_bid_iv=0.3110054,
        venue_ask_iv=0.32129313,
        venue_mark_iv=0.31623765,
    )


def spot(
    underlying: str = "BTC",
    minute: datetime | None = None,
    close: float = 77651.9,
    ticks: int = 7056,
) -> SpotBar:
    return SpotBar(
        underlying=underlying,
        minute=minute or datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        spot_open=77600.0,
        spot_high=77700.5,
        spot_low=77590.25,
        spot_close=close,
        spot_ticks=ticks,
    )


def test_a_reference_bar_round_trips_through_parquet_with_its_values_and_its_types(
    tmp_path: Path,
) -> None:
    """Values, **types** and row count for table B.

    The `venue_` prefix is asserted here rather than left to the reader: #5's table C
    stores our computed Greeks under the bare names, and a store holding two columns
    called `delta` is one careless join away from measuring how well we imitate Delta
    instead of what the prices imply.
    """
    store = reference_store(tmp_path)
    store.add([reference()])
    assert store.flush() == 1

    frame = store.scan().collect()
    schema = frame.collect_schema()

    assert frame.height == 1
    for column in (
        "strike",
        "mark_open",
        "mark_high",
        "mark_low",
        "mark_close",
        "ltp_open",
        "ltp_high",
        "ltp_low",
        "ltp_close",
        "oi_contracts",
        "oi_change_usd_6h",
        "turnover",
        "venue_delta",
        "venue_gamma",
        "venue_rho",
        "venue_theta",
        "venue_vega",
        "venue_bid_iv",
        "venue_ask_iv",
        "venue_mark_iv",
    ):
        assert schema[column] == pl.Float64, column
    for column in ("mark_ticks", "ltp_ticks"):
        assert schema[column] == pl.UInt32, column
    for column in ("symbol", "expiry", "option_type", "underlying"):
        assert schema[column] == pl.Categorical, column
    assert schema["minute"] == pl.Datetime("us", "UTC")
    assert schema["date"] == pl.Date

    row = frame.row(0, named=True)
    assert row["minute"] == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    assert row["date"] == date(2026, 9, 4)
    assert row["mark_open"] == 1059.85780065
    assert row["mark_high"] >= max(row["mark_open"], row["mark_close"])
    assert row["mark_low"] <= min(row["mark_open"], row["mark_close"])
    assert row["venue_delta"] == -0.73938982
    assert row["venue_mark_iv"] == 0.31623765
    # Negative, which is what says this is a six-hour change and not a USD notional.
    assert row["oi_change_usd_6h"] == -41302.35


def test_the_reference_table_stores_no_usd_open_interest_and_none_of_the_dropped_fields(
    tmp_path: Path,
) -> None:
    """Two separate refusals, asserted together because both are about what is *absent*.

    `oi_value_usd` is missing because the ticker channel does not carry one: `oi[1]` is
    Delta's `oi_change_usd_6h`, verified on all 136 captured symbols against the REST
    snapshot taken beside them. Deriving a notional from contracts, contract size and
    spot was rejected — that is a calculation, not an observation, and it would sit in
    a column readers would take for something Delta published.

    The price band, the 24-hour mark change, the symbol echo, the product id and the
    ticker's own bid and ask are missing because #11 drops them.

    **Spot is missing for the strongest reason of all.** It belongs to the underlying,
    so a copy on 588 contract rows a minute would let two contracts whose frames
    straddled a boundary disagree about what spot was.
    """
    store = reference_store(tmp_path)
    store.add([reference()])
    store.flush()

    columns = set(store.scan().collect().columns)

    for banned in (
        "oi_value_usd",
        "price_band",
        "pb",
        "mark_change_24h",
        "m24hc",
        "product_id",
        "bid",
        "ask",
        "bid_open",
        "ask_open",
        "spot",
        "spot_open",
        "spot_close",
        "ohlc_open",
        "ohlc_high",
        "ohlc_low",
    ):
        assert banned not in columns, banned
    assert "oi_contracts" in columns and "oi_change_usd_6h" in columns


def test_a_contract_that_never_traded_reads_back_with_null_ltp_and_a_zero_count(
    tmp_path: Path,
) -> None:
    """Delta sends `ohlc: [null, null, null, null]` for a contract with no trades — 16 of
    the 136 captured symbols. A zero would read as "it last traded at nothing", which is
    a price nobody paid; `ltp_ticks = 0` is the honest record."""
    store = reference_store(tmp_path)
    store.add([reference(ltp=None, ltp_ticks=0)])
    store.flush()

    row = store.scan().collect().row(0, named=True)

    assert row["ltp_open"] is None and row["ltp_close"] is None
    assert row["ltp_ticks"] == 0
    assert row["mark_ticks"] == 12, "the mark still moves; only the trades are absent"


def test_the_reference_row_count_equals_the_minutes_that_actually_had_frames(
    tmp_path: Path,
) -> None:
    """"No invented rows" for table B, through the whole path.

    Twenty minutes of wall time, ticker frames in six of them. A far-dated strike's mark
    would forward-fill beautifully and read as a live valuation nobody published — the
    exact defect in Delta's own history, where `C-BTC-60000-270624` returns 801 daily
    bars of which 797 are fabricated.
    """
    aggregator = ReferenceAggregator()
    quiet = {3, 4, 5, 9, 10, 11, 12, 14, 15, 16, 17, 18, 19, 20}
    busy = [minute for minute in range(1, 21) if minute not in quiet]
    for minute in busy:
        for second in (5, 25, 45):
            aggregator.add(
                ReferenceTick(
                    symbol="C-BTC-77600-040926",
                    exchange_us=MINUTE_US + minute * MINUTE + second * 1_000_000,
                    mark=1000.0 + minute,
                    last_traded_price=1082.0,
                    oi_contracts=1997.0,
                    oi_change_usd_6h=-41302.35,
                    turnover=411134.7807,
                    venue_delta=-0.74,
                    venue_gamma=0.00024,
                    venue_rho=-1.70,
                    venue_theta=-202.29,
                    venue_vega=13.61,
                    venue_bid_iv=0.311,
                    venue_ask_iv=0.321,
                    venue_mark_iv=0.316,
                )
            )

    store = reference_store(tmp_path)
    store.add(aggregator.seal((MINUTE_US + 25 * MINUTE) / 1e6))
    written = store.flush()

    frame = store.scan().collect().sort("minute")
    assert written == len(busy) == 6
    assert frame.height == 6, "the store grew rows for minutes that had no frames"
    assert frame["minute"].dt.minute().to_list() == busy
    assert frame["mark_ticks"].to_list() == [3] * 6


def test_the_reference_table_partitions_on_date_and_underlying_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Table B follows table A's layout exactly: `date/underlying` in the directory
    names, expiry and strike and option type as **columns**.

    Pruning asserted as behaviour — two dates, two underlyings, a filter on one of each
    returns only its row — because expiry as a partition level would explode into
    thousands of directories holding a handful of rows and make Parquet slower than CSV.
    """
    store = reference_store(tmp_path)
    store.add(
        [
            reference(expiry="04-09-2026"),
            reference(symbol="C-BTC-77600-110926", expiry="11-09-2026"),
            reference(
                symbol="C-ETH-3000-040926", underlying="ETH", strike=3000.0
            ),
            reference(
                symbol="P-BTC-70000-181226",
                expiry="18-12-2026",
                option_type="P",
                minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    assert store.flush() == 4

    directories = {
        path.relative_to(store.path).parent.as_posix()
        for path in store.path.rglob("*.parquet")
    }
    assert directories == {
        "date=2026-09-04/underlying=BTC",
        "date=2026-09-04/underlying=ETH",
        "date=2026-09-05/underlying=BTC",
    }

    one = (
        store.scan()
        .filter(pl.col("date") == date(2026, 9, 4))
        .filter(pl.col("underlying") == "BTC")
        .collect()
    )
    assert one.height == 2, "two expiries share one partition, as they must"
    assert sorted(one["expiry"].to_list()) == ["04-09-2026", "11-09-2026"]
    assert sorted(one["option_type"].unique().to_list()) == ["C"]


# --- table D: spot bars on disk ---------------------------------------------------


def test_a_spot_bar_round_trips_through_parquet_with_its_values_and_its_types(
    tmp_path: Path,
) -> None:
    """Table D carries five columns and **no contract identity at all**, which is the
    whole point of giving it a table: the symbol that happened to carry the frame is not
    a fact about spot."""
    store = spot_store(tmp_path)
    store.add([spot()])
    assert store.flush() == 1

    frame = store.scan().collect()
    schema = frame.collect_schema()

    assert frame.height == 1
    for column in ("spot_open", "spot_high", "spot_low", "spot_close"):
        assert schema[column] == pl.Float64, column
    assert schema["spot_ticks"] == pl.UInt32
    assert schema["minute"] == pl.Datetime("us", "UTC")
    assert schema["underlying"] == pl.Categorical
    assert schema["date"] == pl.Date

    assert set(frame.columns) == {
        "minute",
        "spot_open",
        "spot_high",
        "spot_low",
        "spot_close",
        "spot_ticks",
        "date",
        "underlying",
    }

    row = frame.row(0, named=True)
    assert row["underlying"] == "BTC"
    assert row["spot_close"] == 77651.9
    assert row["spot_high"] >= max(row["spot_open"], row["spot_close"])
    assert row["spot_low"] <= min(row["spot_open"], row["spot_close"])
    # Roughly 7,056 observations a bar, because every contract's frame carries spot.
    assert row["spot_ticks"] == 7056


def test_the_spot_row_count_equals_the_minutes_that_actually_had_frames(
    tmp_path: Path,
) -> None:
    """"No invented rows" for table D, and the acceptance criterion stated as arithmetic:
    the spot table's row count for a session equals the number of minutes that actually
    contained ticker frames.

    A forward-filled spot would be the worst row in the store. Spot is the best-sampled
    series in the feed, so a gap in it is a gap in the **ingester**, and a fabricated row
    would hide precisely the outage a reader most needs to see.
    """
    aggregator = SpotAggregator()
    busy = [1, 2, 6, 7, 8, 13]
    for minute in busy:
        for second in (5, 25, 45):
            aggregator.add(
                SpotTick(
                    symbol="C-BTC-77600-040926",
                    exchange_us=MINUTE_US + minute * MINUTE + second * 1_000_000,
                    spot=77650.0 + minute,
                )
            )

    store = spot_store(tmp_path)
    store.add(aggregator.seal((MINUTE_US + 25 * MINUTE) / 1e6))
    written = store.flush()

    frame = store.scan().collect().sort("minute")
    assert written == 6
    assert frame.height == 6, "the store grew rows for minutes that had no frames"
    assert frame["minute"].dt.minute().to_list() == busy
    assert frame["spot_ticks"].to_list() == [3] * 6


def test_a_filtered_scan_returns_only_the_matching_spot_partition(tmp_path: Path) -> None:
    """Partition pruning as behaviour for table D. Two dates and two underlyings; a
    filter on one of each brings back only its row, and the directory names are the
    thing that answered the filter."""
    store = spot_store(tmp_path)
    store.add(
        [
            spot(underlying="BTC", close=77651.9),
            spot(underlying="ETH", close=3000.5),
            spot(
                underlying="BTC",
                minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                close=78000.0,
            ),
            spot(
                underlying="ETH",
                minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                close=3100.0,
            ),
        ]
    )
    assert store.flush() == 4

    one = (
        store.scan()
        .filter(pl.col("date") == date(2026, 9, 4))
        .filter(pl.col("underlying") == "BTC")
        .collect()
    )

    assert one.height == 1
    assert one.row(0, named=True)["spot_close"] == 77651.9

    directories = {
        path.relative_to(store.path).parent.as_posix()
        for path in store.path.rglob("*.parquet")
    }
    assert directories == {
        "date=2026-09-04/underlying=BTC",
        "date=2026-09-04/underlying=ETH",
        "date=2026-09-05/underlying=BTC",
        "date=2026-09-05/underlying=ETH",
    }


def test_the_three_tables_are_three_dataset_roots_that_do_not_share_a_file(
    tmp_path: Path,
) -> None:
    """A single root with a `table` partition key was rejected: it forces every scan to
    carry a filter a directory should have answered, and it puts three schemas in one
    dataset for Parquet's own metadata to reconcile on every read.

    They still share the **same** partition keys, so a reader joins spot to quotes on
    `date` and `underlying` with no schema translation.
    """
    quotes = BarStore(tmp_path)
    quotes.add([bar()])
    quotes.flush()
    references = reference_store(tmp_path)
    references.add([reference()])
    references.flush()
    spots = spot_store(tmp_path)
    spots.add([spot()])
    spots.flush()

    roots = {store.path.name for store in (quotes, references, spots)}
    assert roots == {"quote-bars", "reference-bars", "spot-bars"}
    assert len(list(tmp_path.rglob("*.parquet"))) == 3
    for store in (quotes, references, spots):
        assert store.scan().collect().height == 1

    joined = (
        quotes.scan()
        .join(spots.scan(), on=["date", "underlying", "minute"], how="inner")
        .collect()
    )
    assert joined.height == 1
    assert joined.row(0, named=True)["spot_close"] == 77651.9


# --- the provenance flag, on disk -------------------------------------------------


def test_the_provenance_flag_round_trips_as_a_boolean(tmp_path: Path) -> None:
    """A bar sampled 118 times and one sampled 12 times are different objects, and a
    tick count alone cannot tell a quiet book from no book at all. So the column is a
    real `Boolean` on disk and not a string that a reader has to interpret."""
    store = BarStore(tmp_path)
    store.add(
        [
            bar(from_book=True),
            bar(symbol="P-BTC-75600-040926", option_type="P", from_book=False),
        ]
    )
    store.flush()

    frame = store.scan().collect().sort("symbol")

    assert frame.collect_schema()["from_book"] == pl.Boolean
    assert frame["from_book"].to_list() == [True, False]
    assert frame["from_book"].null_count() == 0


# --- the writer, filling three tables from one bus --------------------------------


def ticker_frame(symbol: str, exchange_us: int, spot_price: str = "77651.9") -> dict:
    """A `ticker` payload shaped exactly as Delta sends one.

    Written out in full rather than trimmed to the fields under test, because the point
    of the end-to-end path is that `wire` reads the real array layout: `q` interleaves
    prices and sizes, `g` is five Greeks in one order and `qiv` three implied vols in
    another, and a shortened fixture would let a transposed index pass.
    """
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": spot_price,
        "ts": exchange_us,
        "d": [
            {
                "g": [
                    "-0.73938982",
                    "0.00024511",
                    "-1.70380880",
                    "-202.29182089",
                    "13.60933495",
                ],
                "i": 148290,
                "m": "1059.85780065",
                "m24hc": "-48.3655",
                "ohlc": [2051.0, 2243.0, 750.0, 1082.0],
                "oi": ["1997", "74608.2000"],
                "pb": ["0.1", "2514.52568587"],
                "q": ["1080", "5425", "1066", "8096", None],
                "qiv": ["0.32129313", "0.3110054", "0.31623765"],
                "s": symbol,
                "to": [411134.7807, 411134.7807],
            }
        ],
    }


def test_the_writer_fills_all_three_tables_from_one_bus(tmp_path: Path) -> None:
    """The whole of #11 end to end: one bus, one drain loop, three partitioned tables.

    Two contracts. The first gets book frames **and** a ticker frame, so its quote bar
    must come from the book and say so. The second gets only a ticker frame, so its quote
    bar exists at all only because of the fallback and must say **that**. Both get a
    reference row; the two frames together make one spot row, because spot is per
    underlying.
    """
    booked = "C-BTC-77600-040926"
    quiet = "P-BTC-75600-040926"

    async def scenario():
        now = MINUTE_US / 1e6

        def clock() -> float:
            return now

        store = BarStore(tmp_path)
        writer = BarWriter(store, clock=clock, flush_seconds=3600.0, tick_seconds=0.01)
        bus = FanOut()
        writer.attach(bus)
        task = asyncio.create_task(writer.run())

        for second in (5, 25, 45):
            bus.publish(
                Quote(
                    symbol=booked,
                    channel="ob_l2",
                    bid=70.0 + second,
                    ask=72.0 + second,
                    received_at=now,
                    frame={
                        "sy": booked,
                        "ts": MINUTE_US + second * 1_000_000,
                        "lts": MINUTE_US + second * 1_000_000 - 300_000,
                    },
                )
            )
        for symbol in (booked, quiet):
            frame = ticker_frame(symbol, MINUTE_US + 30_000_000)
            bus.publish(
                Quote(
                    symbol=symbol,
                    channel="ticker",
                    bid=1066.0,
                    ask=1080.0,
                    received_at=now,
                    frame=frame,
                )
            )

        await asyncio.sleep(0.05)
        now = (MINUTE_US + 2 * MINUTE) / 1e6  # past the boundary and the grace period
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        return writer

    writer = asyncio.run(scenario())

    quotes = writer.store.scan().collect().sort("symbol")
    assert quotes.height == 2
    rows = {row["symbol"]: row for row in quotes.iter_rows(named=True)}

    assert rows[booked]["from_book"] is True
    assert rows[booked]["bid_ticks"] == 3, "the ticker frame contaminated a booked bar"
    assert rows[booked]["bid_open"] == 75.0 and rows[booked]["bid_close"] == 115.0

    assert rows[quiet]["from_book"] is False, "a book-less contract claimed a book"
    assert rows[quiet]["bid_ticks"] == 1
    assert rows[quiet]["bid_close"] == 1066.0 and rows[quiet]["ask_close"] == 1080.0

    references = writer.reference_store.scan().collect().sort("symbol")
    assert references.height == 2
    reference_row = {
        row["symbol"]: row for row in references.iter_rows(named=True)
    }[booked]
    assert reference_row["mark_close"] == 1059.85780065
    assert reference_row["ltp_close"] == 1082.0, "the 24-hour close is the LTP"
    assert reference_row["venue_delta"] == -0.73938982
    assert reference_row["venue_mark_iv"] == 0.31623765
    assert reference_row["oi_contracts"] == 1997.0
    assert reference_row["turnover"] == 411134.7807
    assert reference_row["minute"] == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

    spots = writer.spot_store.scan().collect()
    assert spots.height == 1, "spot was stored per contract instead of per underlying"
    spot_row = spots.row(0, named=True)
    assert spot_row["underlying"] == "BTC"
    assert spot_row["spot_close"] == 77651.9
    assert spot_row["spot_ticks"] == 2, "both contracts' frames carried the same spot"


def test_the_writers_three_stores_share_the_root_they_were_given(tmp_path: Path) -> None:
    """A test that hands the writer a temporary directory must not have two of its three
    tables quietly write into the repository's own `data/`. The sibling stores are
    derived from the one it was given, never defaulted separately."""
    writer = BarWriter(BarStore(tmp_path))

    assert writer.reference_store.root == tmp_path
    assert writer.spot_store.root == tmp_path
    assert writer.reference_store.dataset == REFERENCE_DATASET
    assert writer.spot_store.dataset == SPOT_DATASET


def test_the_writer_counts_a_bus_record_it_can_use_for_nothing(tmp_path: Path) -> None:
    """"The writer ignored most of the bus" should be a number rather than a discovery.

    A ticker frame with no `ts` cannot be bucketed on anything but our arrival time,
    which is the one thing this design refuses, so it is refused whole and counted.
    """
    writer = BarWriter(BarStore(tmp_path))
    writer.ingest(
        Quote(
            symbol="C-BTC-77600-040926",
            channel="ticker",
            bid=1.0,
            ask=2.0,
            received_at=MINUTE_US / 1e6,
            frame={"sy": "C-BTC-77600-040926", "d": [{}]},
        )
    )

    stats = writer.stats()
    assert stats["skipped"] == 1
    assert stats["ticks"] == 0
    assert stats["reference"]["ticks"] == 0
    assert stats["spot"]["ticks"] == 0


# --- the app wiring -------------------------------------------------------------


class _StubDeltaClient:
    """Stands in for `DeltaClient` inside the lifespan. Opens nothing."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def tickers(self, underlying: str, expiry=None):
        return [{"symbol": "C-BTC-77600-040926"}]

    async def aclose(self) -> None:
        return None


class _StubFeed:
    """Stands in for `DeltaFeed`. Registers subscriptions and never dials out."""

    def __init__(self, fanout) -> None:
        self.fanout = fanout
        self.registry: dict[str, list[str]] = {}

    def subscribe(self, channel: str, symbols) -> None:
        self.registry.setdefault(channel, []).extend(symbols)

    async def run(self) -> None:
        await asyncio.Event().wait()


def test_the_app_attaches_the_writer_to_the_bus_losslessly(monkeypatch, tmp_path) -> None:
    """The writer is the bus's second consumer — the seam #3 built and nothing has used
    since — and it must not share `ChainStream`'s drop-oldest policy."""
    from fastapi.testclient import TestClient

    from deltapayoff import main

    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    with TestClient(main.app):
        assert isinstance(main.app.state.writer, BarWriter)
        stats = main.app.state.fanout.stats()
        assert stats["bar-writer"]["lossless"] is True
        assert stats["chain-stream"]["lossless"] is False


def test_the_lifespan_runs_the_writer_and_flushes_the_open_minute_on_shutdown(
    monkeypatch, tmp_path
) -> None:
    """The tracer bullet through the real application.

    A quote published on the live bus becomes a row on disk, and the row only exists
    because shutdown flushed a bar that was still open — which is the ticket's "a partial
    bar at process stop is kept with its true counts", asserted rather than described.
    """
    from fastapi.testclient import TestClient

    from deltapayoff import main

    monkeypatch.setenv("DELTA_LIVE_FEED", "1")
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    monkeypatch.setattr(main, "DeltaFeed", _StubFeed)
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        names = {task.get_name() for task in main.app.state.tasks}
        assert "bar-writer" in names

        now = time.time()
        exchange_us = int(now * 1e6)
        for offset in (0, 1, 2):
            main.app.state.fanout.publish(
                Quote(
                    symbol="C-BTC-77600-040926",
                    channel="ob_l2",
                    bid=70.0 + offset,
                    ask=72.0 + offset,
                    received_at=now,
                    frame={
                        "sy": "C-BTC-77600-040926",
                        "ts": exchange_us + offset * 1000,
                        "lts": exchange_us + offset * 1000 - 300_000,
                    },
                )
            )
        # Let the writer's task drain the queue before the lifespan tears it down.
        time.sleep(0.2)

    frame = BarStore(tmp_path).scan().collect()
    assert frame.height == 1, "the open minute was discarded at shutdown"
    row = frame.row(0, named=True)
    assert row["bid_ticks"] == 3, "the partial bar lost its true tick count"
    assert row["bid_open"] == 70.0 and row["bid_close"] == 72.0
    assert row["symbol"] == "C-BTC-77600-040926"


def test_the_running_app_writes_all_three_tables(monkeypatch, tmp_path) -> None:
    """#11 delivered through the real application, not through a hand-built writer.

    A quote and a ticker frame published on the live bus become rows in three separate
    hive-partitioned datasets, and they exist only because shutdown flushed bars that
    were still open — the ticket's "a partial bar at process stop is kept with its true
    counts", asserted for all three tables at once.
    """
    from fastapi.testclient import TestClient

    from deltapayoff import main

    monkeypatch.setenv("DELTA_LIVE_FEED", "1")
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    monkeypatch.setattr(main, "DeltaFeed", _StubFeed)
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    symbol = "C-BTC-77600-040926"
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        # One writer, one subscription, three tables.
        assert main.app.state.writer.reference_store.root == tmp_path
        assert main.app.state.writer.spot_store.root == tmp_path

        now = time.time()
        exchange_us = int(now * 1e6)
        main.app.state.fanout.publish(
            Quote(
                symbol=symbol,
                channel="ob_l2",
                bid=70.0,
                ask=72.0,
                received_at=now,
                frame={"sy": symbol, "ts": exchange_us, "lts": exchange_us - 300_000},
            )
        )
        main.app.state.fanout.publish(
            Quote(
                symbol=symbol,
                channel="ticker",
                bid=1066.0,
                ask=1080.0,
                received_at=now,
                frame=ticker_frame(symbol, exchange_us + 1000),
            )
        )
        time.sleep(0.2)  # let the writer drain before the lifespan tears it down

    quotes = BarStore(tmp_path).scan().collect()
    references = BarStore(
        tmp_path, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA
    ).scan().collect()
    spots = BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA).scan().collect()

    assert quotes.height == 1, "the open quote minute was discarded at shutdown"
    assert quotes.row(0, named=True)["from_book"] is True
    assert quotes.row(0, named=True)["bid_ticks"] == 1, "the ticker frame contaminated it"

    assert references.height == 1, "table B is not produced by the running app"
    assert references.row(0, named=True)["mark_close"] == 1059.85780065
    assert references.row(0, named=True)["ltp_close"] == 1082.0

    assert spots.height == 1, "table D is not produced by the running app"
    assert spots.row(0, named=True)["spot_close"] == 77651.9
    assert spots.row(0, named=True)["underlying"] == "BTC"


def test_the_writer_task_reports_a_clean_shutdown_as_cancelled(tmp_path: Path) -> None:
    """`main._report_finished_task` distinguishes a cancelled task from a returned one,
    and logs an error for the second. A `run` that swallowed its own cancellation would
    therefore log an error on every orderly stop, until nobody read the log at all."""

    async def scenario():
        writer = BarWriter(BarStore(tmp_path), tick_seconds=0.01)
        writer.attach(FanOut())
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled() is True


def test_the_writer_refuses_to_run_before_it_is_attached(tmp_path: Path) -> None:
    """A writer with no subscription would sit in a loop draining nothing and writing
    nothing, which looks exactly like a quiet market."""

    async def scenario():
        with pytest.raises(RuntimeError):
            await BarWriter(BarStore(tmp_path)).run()

    asyncio.run(scenario())


# --- table C: our computed values ------------------------------------------------


def computed_bar(
    symbol: str = "C-BTC-77600-040926",
    minute: datetime | None = None,
    *,
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    strike: float = 77600.0,
    option_type: str = "C",
    iv: float | None = 0.43212345,
    iv_leg: str | None = "call",
    iv_reason: str | None = None,
) -> ComputedBar:
    greeks: dict[str, float | None] = dict(
        delta=0.51234567, gamma=0.00012345, vega=31.41592653, theta=-8.2, rho=1.9
    )
    if iv is None:
        greeks = dict.fromkeys(greeks)
    return ComputedBar(
        symbol=symbol,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute or datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        iv=iv,
        iv_leg=iv_leg,
        iv_reason=iv_reason,
        forward=77590.43210987,
        discount=0.99997892,
        years_to_expiry=0.00114155,
        forward_method="F1+assumed-rate",
        model_version=MODEL_VERSION,
        **greeks,
    )


def test_a_computed_bar_round_trips_through_parquet_with_its_values_and_its_types(
    tmp_path: Path,
) -> None:
    """Values, **types** and the model stamp. The stamp is dictionary-encoded because it
    is one short string repeated on every row of every day, and it is the column that
    makes a later change to the model visible instead of silent."""
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add([computed_bar()])
    assert store.flush() == 1

    frame = store.scan().collect()
    schema = frame.collect_schema()
    row = frame.row(0, named=True)

    assert frame.height == 1
    for column in (
        "strike",
        "iv",
        "delta",
        "gamma",
        "vega",
        "theta",
        "rho",
        "forward",
        "discount",
        "years_to_expiry",
    ):
        assert schema[column] == pl.Float64, column
    for column in (
        "symbol",
        "expiry",
        "option_type",
        "underlying",
        "iv_leg",
        "iv_reason",
        "forward_method",
        "model_version",
    ):
        assert schema[column] == pl.Categorical, column
    assert schema["minute"] == pl.Datetime("us", "UTC")
    assert schema["date"] == pl.Date

    # Full 64-bit precision, not a 32-bit float's seven digits.
    assert row["iv"] == 0.43212345
    assert row["vega"] == 31.41592653
    assert row["forward"] == 77590.43210987
    assert row["model_version"] == MODEL_VERSION
    assert row["forward_method"] == "F1+assumed-rate"
    assert row["date"] == date(2026, 9, 4)
    assert row["underlying"] == "BTC"


def test_a_computed_row_without_a_volatility_carries_no_greeks_and_nulls_not_zeros(
    tmp_path: Path,
) -> None:
    """Absence is null and never zero. A zero delta is a real and meaningful number —
    a deep out-of-the-money option has one — so writing zeros for "we could not solve
    this" would put five plausible figures in the store that describe nothing."""
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add([computed_bar(iv=None, iv_reason="the solver did not converge")])
    store.flush()

    row = store.scan().collect().row(0, named=True)

    assert row["iv"] is None
    for greek in ("delta", "gamma", "vega", "theta", "rho"):
        assert row[greek] is None, greek
    assert row["iv_reason"] == "the solver did not converge"


def test_the_computed_table_stores_none_of_delta_s_own_figures(tmp_path: Path) -> None:
    """The separation that makes any agreement between us and the venue evidence rather
    than construction. Delta's vols and Greeks are table B's `venue_` columns; a second
    column called `delta` in this table would be one careless join away from measuring
    how well we imitate them."""
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add([computed_bar()])
    store.flush()

    columns = set(store.scan().collect_schema().names())

    assert not [name for name in columns if name.startswith("venue_")]
    for absent in ("mark_close", "mark_iv", "bid_iv", "ask_iv", "oi_contracts", "spot"):
        assert absent not in columns, absent
    # ...and ours are there under the bare names.
    assert {"iv", "delta", "gamma", "vega", "theta", "rho"} <= columns


def test_the_computed_row_count_equals_the_minutes_that_actually_had_a_chain(
    tmp_path: Path,
) -> None:
    """"No invented rows" made executable at the file layer: minutes 0 and 4 were
    computed, 1, 2 and 3 were not, and the file holds two rows rather than five."""
    aggregator = ComputedAggregator()
    for minute in (0, 4):
        aggregator.add(
            computed_tick(MINUTE_US + minute * MINUTE + 30 * 1_000_000, iv=0.4 + minute)
        )

    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add(aggregator.seal((MINUTE_US + 5 * MINUTE) / 1e6 + 3600.0))
    store.flush()

    frame = store.scan().collect().sort("minute")

    assert frame.height == 2
    assert frame["minute"].to_list() == [
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 9, 4, tzinfo=timezone.utc),
    ]


def computed_tick(exchange_us: int, iv: float | None = 0.43) -> ComputedTick:
    return ComputedTick(
        symbol="C-BTC-77600-040926",
        exchange_us=exchange_us,
        iv=iv,
        iv_leg="call",
        iv_reason=None,
        delta=0.5,
        gamma=0.0001,
        vega=31.4,
        theta=-8.2,
        rho=1.9,
        forward=77590.4,
        discount=0.99997892,
        years_to_expiry=0.00114155,
        forward_method="F1",
        model_version=MODEL_VERSION,
    )


def test_a_filtered_scan_returns_only_the_matching_computed_partition(
    tmp_path: Path,
) -> None:
    """Pruning as behaviour rather than as configuration: two dates and two underlyings
    written, a filtered scan asserted to hold only the matching rows."""
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add(
        [
            computed_bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)),
            computed_bar(minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)),
            computed_bar(
                symbol="C-ETH-4000-040926",
                underlying="ETH",
                strike=4000.0,
                minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
            ),
        ]
    )
    store.flush()

    frame = (
        store.scan()
        .filter(pl.col("date") == date(2026, 9, 4), pl.col("underlying") == "BTC")
        .collect()
    )

    assert frame.height == 1
    assert frame.row(0, named=True)["symbol"] == "C-BTC-77600-040926"


def sampled_chain(minute: int, second: int, iv: float | None = 0.43) -> ChainResponse:
    """A two-leg enriched chain stamped inside minute `minute`, as the recompute loop
    would have left it in `ChainStream._computed`."""
    stamp = datetime.fromtimestamp(
        (MINUTE_US + minute * MINUTE + second * 1_000_000) / 1e6, tz=timezone.utc
    )
    block = ComputedLeg(
        iv=iv,
        iv_leg="call",
        delta=0.5,
        gamma=0.0001,
        vega=31.4,
        theta=-8.2,
        rho=1.9,
    )
    return ChainResponse(
        underlying="BTC",
        expiry="04-09-2026",
        spot=77568.2,
        atm_strike=77600.0,
        fetched_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        rows=[
            ChainRow(
                strike=77600.0,
                call=Leg(symbol="C-BTC-77600-040926", computed=block),
                put=Leg(symbol="P-BTC-77600-040926", computed=block),
            )
        ],
        forward=77590.4,
        discount=0.99997892,
        years_to_expiry=0.00114155,
        forward_method="F1+assumed-rate",
    )


def test_the_writer_samples_the_chain_cache_at_each_minute_boundary(
    tmp_path: Path,
) -> None:
    """Table C is **sampled**, not folded from the bus. The computed surface is produced
    by the recompute loop rather than arriving on the wire, so the writer reads the cache
    once as each minute closes and stores the state the screen was showing."""
    held: list[ChainResponse] = []

    async def scenario():
        now = MINUTE_US / 1e6 + 30.0

        def clock() -> float:
            return now

        writer = BarWriter(
            BarStore(tmp_path),
            clock=clock,
            flush_seconds=3600.0,
            tick_seconds=0.01,
            chains=lambda: list(held),
        )
        writer.attach(FanOut())
        task = asyncio.create_task(writer.run())

        held.append(sampled_chain(0, 50, iv=0.40))
        await asyncio.sleep(0.05)
        now = (MINUTE_US + MINUTE) / 1e6 + 0.5
        await asyncio.sleep(0.05)

        held[:] = [sampled_chain(1, 50, iv=0.44)]
        now = (MINUTE_US + 2 * MINUTE) / 1e6 + 0.5
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        return writer

    writer = asyncio.run(scenario())
    frame = writer.computed_store.scan().collect().sort("minute", "symbol")

    assert frame.height == 4, "two contracts across two closed minutes"
    assert frame["minute"].to_list() == [
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc),
    ]
    assert frame["iv"].to_list() == [0.40, 0.40, 0.44, 0.44]
    assert set(frame["model_version"].to_list()) == {MODEL_VERSION}
    assert set(frame["symbol"].to_list()) == {
        "C-BTC-77600-040926",
        "P-BTC-77600-040926",
    }


def test_a_minute_with_no_computed_chain_gets_no_computed_row(tmp_path: Path) -> None:
    """**The deliberate-silence test, at the file layer.**

    Five minutes; exactly one of them was computed. Minutes 0 and 1 pass with the cache
    holding nothing at all — the recompute loop has produced no chain yet, which is what
    process start and an expiry nobody has quoted both look like. Minute 2 is computed.
    Then the feed stops and the cache keeps answering with that same chain forever, which
    is what a dead socket looks like from here.

    Only minute 2 may appear. Not a row of nulls for the others, and above all not minute
    2's volatility carried into 3 and 4 — that is the defect this project documented in
    Delta's own `/v2/history/candles`, where 797 of 801 daily bars are the last trade
    repeated. It would be invisible: plausible rows describing a market nobody observed.

    **Sabotage-verified.** With `_sample_computed` stamping each tick with the minute it
    is closing instead of with the instant the chain was computed — the plausible mistake,
    since the row *is* attributed to that minute — this fails with three minutes.
    """
    held: list[ChainResponse] = []

    async def scenario():
        now = MINUTE_US / 1e6 + 30.0

        def clock() -> float:
            return now

        writer = BarWriter(
            BarStore(tmp_path),
            clock=clock,
            flush_seconds=3600.0,
            tick_seconds=0.01,
            chains=lambda: list(held),
        )
        writer.attach(FanOut())
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.05)

        # Minutes 0 and 1 close with nothing computed at all.
        for minute in (1, 2):
            now = (MINUTE_US + minute * MINUTE) / 1e6 + 0.5
            await asyncio.sleep(0.03)

        # Minute 2 is computed, and then the feed stops: the cache goes on holding this
        # one chain while minutes 3 and 4 close over it.
        held.append(sampled_chain(2, 50, iv=0.48))
        for minute in (3, 4, 5):
            now = (MINUTE_US + minute * MINUTE) / 1e6 + 0.5
            await asyncio.sleep(0.03)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        return writer

    writer = asyncio.run(scenario())
    frame = writer.computed_store.scan().collect().sort("symbol")
    minutes = sorted(set(frame["minute"].to_list()))

    assert minutes == [datetime(2026, 9, 4, 9, 2, tzinfo=timezone.utc)]
    for silent in (0, 1, 3, 4):
        stamp = datetime(2026, 9, 4, 9, silent, tzinfo=timezone.utc)
        assert stamp not in minutes, f"minute {silent} was invented"
    assert frame.height == 2, "two contracts, one minute, and nothing else"
    assert frame["iv"].to_list() == [0.48, 0.48]


def test_a_cache_that_stops_being_recomputed_stops_producing_rows(
    tmp_path: Path,
) -> None:
    """The same rule, with the refusal counted rather than merely observed.

    `ChainStream._computed` keeps answering after the socket dies — it holds the last
    chain it managed to build, forever. Re-sampling it every minute would write identical
    rows for a market that was not observed. The sample is bucketed on the instant the
    chain was **computed**, so a frozen cache is late rather than fresh, and a discarded
    observation with no counter behind it is the same lie as a silent drop.
    """
    frozen = sampled_chain(0, 50, iv=0.40)

    async def scenario():
        now = MINUTE_US / 1e6 + 30.0

        def clock() -> float:
            return now

        writer = BarWriter(
            BarStore(tmp_path),
            clock=clock,
            flush_seconds=3600.0,
            tick_seconds=0.01,
            chains=lambda: [frozen],
        )
        writer.attach(FanOut())
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.05)

        for minute in (1, 2, 3, 4, 5):
            now = (MINUTE_US + minute * MINUTE) / 1e6 + 0.5
            await asyncio.sleep(0.03)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        return writer

    writer = asyncio.run(scenario())
    frame = writer.computed_store.scan().collect()

    assert frame.height == 2, "a dead feed's last chain was stored again"
    assert set(frame["minute"].to_list()) == {
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    }
    assert writer.computed.late >= 8, "a refused sample was discarded silently"


def test_the_writers_computed_store_shares_the_root_it_was_given(
    tmp_path: Path,
) -> None:
    """The fourth table is derived from the quote store's root like the other two, so a
    test handed a temporary directory does not have one of its four tables quietly write
    into the repository's own `data/`."""
    writer = BarWriter(BarStore(tmp_path))

    assert writer.computed_store.root == tmp_path
    assert writer.computed_store.dataset == COMPUTED_DATASET
    assert writer.stats()["computed"]["rows_written"] == 0


def test_a_writer_with_no_chain_source_writes_no_computed_rows(tmp_path: Path) -> None:
    """`chains` is optional. A writer with nothing to sample stores nothing rather than
    storing empty minutes, and the other three tables are unaffected."""
    writer = BarWriter(BarStore(tmp_path))

    writer._sample_computed(MINUTE_US / 1e6)
    writer._sample_computed((MINUTE_US + MINUTE) / 1e6)

    assert writer.computed.stats()["ticks"] == 0
    assert writer.computed_store.buffered == 0


def test_the_running_app_stores_our_computed_values_too(monkeypatch, tmp_path) -> None:
    """The fourth table through the real application.

    A ticker frame on the live bus is enriched by the recompute loop, and the writer
    samples that loop's own cache rather than the queue — which is the whole structural
    difference between this table and the other three. The row lands because shutdown
    takes a final sample of the open minute, exactly as it flushes a partial quote bar.

    The assertion is on **content**, not on a row count, because a real minute boundary
    may fall inside the window and legitimately split the sample across two minutes.
    Whether silence produces rows is pinned by the deliberate-silence tests above, on an
    injected clock where it can be asserted exactly.
    """
    from fastapi.testclient import TestClient

    from deltapayoff import main

    monkeypatch.setenv("DELTA_LIVE_FEED", "1")
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    monkeypatch.setattr(main, "DeltaFeed", _StubFeed)
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    symbol = "C-BTC-77600-040926"
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        writer, stream = main.app.state.writer, main.app.state.stream
        assert writer.computed_store.root == tmp_path
        assert writer.chains == stream.computed_chains, "the writer samples nothing"

        now = time.time()
        main.app.state.fanout.publish(
            Quote(
                symbol=symbol,
                channel="ticker",
                bid=1066.0,
                ask=1080.0,
                received_at=now,
                frame=ticker_frame(symbol, int(now * 1e6)),
            )
        )
        time.sleep(0.4)  # the recompute loop runs every 100 ms
        assert stream.computed_chains(), "the loop computed nothing to sample"

    computed = (
        BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
        .scan()
        .collect()
    )

    assert computed.height >= 1, "table C is not produced by the running app"
    assert set(computed["symbol"].to_list()) == {symbol}
    assert set(computed["model_version"].to_list()) == {MODEL_VERSION}
    assert set(computed["underlying"].to_list()) == {"BTC"}
    # Delta's own figures stay in table B. Nothing of theirs is written here.
    assert not [
        name for name in computed.collect_schema().names() if name.startswith("venue_")
    ]


def test_a_stored_row_reproduces_offline_from_the_quote_bar_beside_it(
    tmp_path: Path, ws_ticker_frames, ws_book_frames
) -> None:
    """**What makes `model_version` verifiable rather than decorative.**

    The captured 136-symbol chain is published on the bus and enriched exactly as it
    would be live, so table A gets its quote bars and table C gets our volatility and
    Greeks for the same minute. Then the chain is **rebuilt from the stored bytes alone**
    — bid and ask closes from table A, spot from table D, and the snapshot instant
    recovered by inverting table C's own `years_to_expiry` — and `compute.enrich` is run
    over it offline. What it produces must be what table C already holds.

    **What this proves.** That the store carries every input our model needs, that the
    columns mean what their names say, that the types survive Parquet without losing
    precision, and that a reader with nothing but these files can re-derive the numbers
    rather than having to trust them. It is not tautological: the two tables are written
    by different paths — one folds ticks off the bus, the other samples the recompute
    loop's cache — and the comparison is between values that have both been through the
    file layer.

    **What it does not prove.** Each contract gets exactly one tick in this minute, so
    `bid_close` *is* the tick that was live when the chain was computed and the agreement
    is exact. On a busy minute it is not: a bar's close is the last tick of the minute
    while the sample was taken from whatever chain the 100 ms loop had last produced, and
    those are usually but not always the same quote. So this establishes that the model
    is reproducible from the stored schema — **not** that every historical row will
    re-derive to the last decimal from its own bar. It also says nothing about whether
    the model is right; it says the store is honest about which model ran.
    """
    minute_start = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    taken = minute_start + timedelta(seconds=50)
    live = enrich(
        chain_from_frames(
            "BTC", "04-09-2026", ws_ticker_frames, ws_book_frames, fetched_at=taken
        )
    )

    async def scenario():
        now = MINUTE_US / 1e6 + 55.0

        def clock() -> float:
            return now

        writer = BarWriter(
            BarStore(tmp_path),
            clock=clock,
            flush_seconds=3600.0,
            tick_seconds=0.01,
            chains=lambda: [live],
        )
        bus = FanOut()
        writer.attach(bus)
        task = asyncio.create_task(writer.run())
        await asyncio.sleep(0.05)

        stamp = MINUTE_US + 50_000_000
        for symbol, frame in ws_ticker_frames.items():
            _, leg = decode_ticker(frame)
            bus.publish(
                Quote(
                    symbol=symbol,
                    channel="ticker",
                    bid=leg.bid,
                    ask=leg.ask,
                    received_at=now,
                    frame={**frame, "ts": stamp},
                )
            )
        for symbol, frame in ws_book_frames.items():
            _, bid, ask = decode_ob_l2(frame)
            bus.publish(
                Quote(
                    symbol=symbol,
                    channel="ob_l2",
                    bid=bid,
                    ask=ask,
                    received_at=now,
                    frame={**frame, "ts": stamp},
                )
            )

        await asyncio.sleep(0.1)
        now = (MINUTE_US + MINUTE) / 1e6 + 10.0  # past every grace period
        await asyncio.sleep(0.1)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()

    asyncio.run(scenario())

    quotes = BarStore(tmp_path).scan().collect()
    spots = BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA).scan().collect()
    computed = (
        BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
        .scan()
        .collect()
    )
    stored = {row["symbol"]: row for row in computed.iter_rows(named=True)}

    assert stored, "table C is empty; there is nothing to reproduce"
    assert sum(1 for row in stored.values() if row["iv"] is not None) >= 20, (
        "almost nothing solved, so an agreement below would prove very little"
    )

    # --- the offline reconstruction, from the stored bytes and nothing else ---
    sample = next(iter(stored.values()))
    # `fetched_at` is not a column, and does not need to be: `years_to_expiry` pins it
    # exactly against a settlement time the expiry already names, and #5 asks that
    # nothing trivially derivable be stored.
    settles = datetime.strptime(sample["expiry"], EXPIRY_FORMAT).replace(
        hour=SETTLEMENT_HOUR_UTC, tzinfo=timezone.utc
    )
    recovered = settles - timedelta(
        seconds=round(sample["years_to_expiry"] * DAYS_PER_YEAR * 86_400)
    )
    assert recovered == taken, "the snapshot instant cannot be recovered from the store"

    legs: dict[float, dict[str, Leg]] = {}
    for row in quotes.iter_rows(named=True):
        side = "call" if row["option_type"] == "C" else "put"
        legs.setdefault(row["strike"], {})[side] = Leg(
            symbol=row["symbol"], bid=row["bid_close"], ask=row["ask_close"]
        )
    spot = spots.row(0, named=True)["spot_close"]
    rebuilt = ChainResponse(
        underlying="BTC",
        expiry=sample["expiry"],
        spot=spot,
        atm_strike=nearest_strike(list(legs), spot),
        fetched_at=recovered.strftime("%Y-%m-%dT%H:%M:%SZ"),
        rows=[
            ChainRow(strike=strike, call=sides.get("call"), put=sides.get("put"))
            for strike, sides in sorted(legs.items())
        ],
    )

    offline = enrich(rebuilt)

    assert offline.forward_method == sample["forward_method"]
    assert offline.forward == sample["forward"]
    assert offline.discount == sample["discount"]
    assert offline.years_to_expiry == sample["years_to_expiry"]

    checked = 0
    for row in offline.rows:
        for leg in (row.call, row.put):
            if leg is None:
                continue
            block, kept = leg.computed, stored[leg.symbol]
            assert block.iv == kept["iv"], leg.symbol
            assert (block.iv_reason or None) == kept["iv_reason"], leg.symbol
            for greek in ("delta", "gamma", "vega", "theta", "rho"):
                assert getattr(block, greek) == kept[greek], f"{leg.symbol} {greek}"
            checked += 1

    assert checked == len(stored) == quotes.height
