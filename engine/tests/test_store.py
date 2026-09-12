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
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from deltapayoff import store as store_module
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
from deltapayoff.events import (
    BarTable,
    ConnectionState,
    Heartbeat,
    Instrument,
    OptionBar,
    OptionQuote,
    Right,
)
from deltapayoff.fanout import FanOut
from deltapayoff.forward import DAYS_PER_YEAR, SETTLEMENT_HOUR_UTC
from deltapayoff.models import ChainResponse, ChainRow, ComputedLeg, Leg
from deltapayoff.redis_bus import Position, Span
from deltapayoff.store import (
    CHECKPOINT_VERSION,
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    FLUSH_STAGES,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
    Checkpoint,
    FlushInterrupted,
    Intent,
    read_checkpoint,
    read_intent,
    recover_intent,
    write_checkpoint,
    write_intent,
)
from deltapayoff.wire import chain_from_frames
from fakes.decoder import events_from_frame

MINUTE_US = int(datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1e6)
MINUTE = 60_000_000

FLUSH_MINUTES = (
    datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 12, 10, 1, tzinfo=timezone.utc),
)
FLUSH_INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 9, 27),
    strike=77600.0,
    right=Right.CALL,
    venue_symbol="C-BTC-77600-270926",
)


def test_default_root_uses_the_repository_data_directory_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("DELTA_STORE_ROOT", raising=False)

    assert store_module.default_root() == Path(__file__).resolve().parents[2] / "data"


def test_default_root_uses_the_configured_store_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DELTA_STORE_ROOT", str(tmp_path))

    assert store_module.default_root() == tmp_path


def test_default_root_ignores_an_empty_configured_store_directory(monkeypatch) -> None:
    monkeypatch.setenv("DELTA_STORE_ROOT", "")

    assert store_module.default_root() == Path(__file__).resolve().parents[2] / "data"


def test_checkpoint_and_intent_round_trip_atomically(tmp_path: Path) -> None:
    checkpoint = Checkpoint(
        generation=17,
        written_at=datetime(2026, 9, 12, 10, 5, 0, 123456, tzinfo=timezone.utc),
        group="store",
        recording=True,
        streams={
            "md.option_quote:DELTA:BTC": Position("1789163972987-4", 9123456)
        },
        sealed_through_us={
            "quote-bars": 1789163880000000,
            "reference-bars": 1789163880000000,
            "spot-bars": 1789163880000000,
            "computed-bars": 1789163940000000,
        },
        pauses=(Span({"md.option_quote:DELTA:BTC": "1789163972987-0"}, None),),
    )
    intent = Intent(
        generation=18,
        files=(
            "quote-bars/underlying=BTC/date=2026-09-12/"
            "20260912T100000Z-g00000018.parquet",
        ),
    )

    write_checkpoint(tmp_path, checkpoint)
    write_intent(tmp_path, intent)

    assert read_checkpoint(tmp_path) == checkpoint
    assert read_intent(tmp_path) == intent
    assert not list(tmp_path.glob("*.tmp"))
    assert CHECKPOINT_VERSION == 1


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
        "underlying=BTC/date=2026-09-04",
        "underlying=ETH/date=2026-09-04",
        "underlying=BTC/date=2026-09-05",
        "underlying=ETH/date=2026-09-05",
    }


def test_a_partition_filter_is_answered_by_the_paths_before_a_file_is_opened(
    tmp_path: Path,
) -> None:
    """Pruning is the whole reason the tree is shaped this way, and the test above cannot
    see it — a scan that opened all four files and then dropped three rows would pass it
    identically. This one reads Polars' own optimised plan and asserts the filtered scan
    carries **one** source where the unfiltered one carries four.

    It matters because `scan()` names `**/*.parquet` rather than the directory, and a
    glob is exactly the kind of change that could quietly stop the hive keys being read
    off the path.
    """
    store = BarStore(tmp_path)
    for day in (4, 5):
        for underlying, symbol, strike in (
            ("BTC", "C-BTC-77600-040926", 77600.0),
            ("ETH", "C-ETH-3000-040926", 3000.0),
        ):
            store.add(
                [
                    bar(
                        symbol=symbol,
                        underlying=underlying,
                        strike=strike,
                        minute=datetime(2026, 9, day, 9, 0, tzinfo=timezone.utc),
                    )
                ]
            )
        store.flush()

    def sources(plan: str) -> int:
        """How many files the plan will open. Polars abbreviates a long list as
        `first.parquet, ... N other sources`, so both spellings are counted."""
        listed = plan.count(".parquet")
        more = re.search(r"(\d+) other sources", plan)
        return listed + (int(more.group(1)) if more else 0)

    everything = store.scan().explain(optimized=True)
    pruned = (
        store.scan()
        .filter(pl.col("date") == date(2026, 9, 4), pl.col("underlying") == "BTC")
        .explain(optimized=True)
    )

    assert sources(everything) == 4, everything
    assert sources(pruned) == 1, pruned
    assert "underlying=BTC/date=2026-09-04" in pruned.replace("\\", "/")


def test_a_scan_ignores_a_file_in_the_tree_that_is_not_part_of_the_dataset(
    tmp_path: Path,
) -> None:
    """Regression, and a sharp one. Handed a bare directory, `scan_parquet` **raises**
    the moment that directory holds anything whose extension is not `.parquet` — it does
    not skip the file. Compaction puts two such things in a partition while it runs, its
    `.tmp` output and its manifest, so a bare-directory scan made every partition
    unreadable for the duration of a compaction and permanently unreadable after a crash.
    """
    store = BarStore(tmp_path)
    store.add([bar()])
    assert store.flush() == 1
    directory = store.path / "underlying=BTC" / "date=2026-09-04"
    (directory / "_compaction.json").write_text("{}", encoding="utf-8")
    (directory / "compact-000001.parquet.tmp").write_bytes(b"not a parquet file")
    (directory / "notes.txt").write_text("hello", encoding="utf-8")

    assert store.scan().collect().height == 1


def test_expiry_strike_and_option_type_are_columns_not_partition_levels(
    tmp_path: Path,
) -> None:
    """Expiry as a partition level explodes into thousands of tiny directories and makes
    Parquet slower than CSV — each small file carries header and footer overhead and a
    reader has to open all of them. So the directory tree is `underlying/date` and
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
    assert directories == {"underlying=BTC/date=2026-09-04"}
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


def test_a_flush_logs_table_rows_file_and_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#42's acceptance line: a flush on a temporary store produces an info record with
    table, rows, file and duration."""
    caplog.set_level(logging.INFO, logger="deltapayoff.store")
    store = BarStore(tmp_path)
    store.add([bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc))])

    written = store.flush()

    assert written == 1
    records = [r for r in caplog.records if r.event == "store.flush"]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    record = records[0]
    assert record.levelno == logging.INFO
    assert record.table == "quote-bars"
    assert record.rows == 1
    assert Path(record.file).exists()
    assert Path(record.file).suffix == ".parquet"
    assert record.duration_seconds >= 0.0


def test_a_quiet_flush_with_nothing_buffered_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty flush already writes no file; it must not write a log line either — a
    quiet five minutes must cost the log nothing, the same way it costs the disk
    nothing."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.store")
    store = BarStore(tmp_path)

    assert store.flush() == 0

    assert [r for r in caplog.records if r.event == "store.flush"] == []


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


async def wait_until(
    condition: Callable[[], bool], *, timeout: float = 2.0, poll: float = 0.005
) -> None:
    """Poll a real-time condition until it is true, or fail loudly past `timeout`.

    For synchronising a test with a `BarWriter` task driven by a **fake** clock: the
    condition is always something the writer sets after doing the real work (a row
    count, a buffer length, `writer.loops`), never a guess at how long that work takes.
    `timeout` is real wall-clock slack for a loaded machine to schedule the writer's
    task and, where a flush is involved, its worker thread — it does not move the fake
    clock, which the test alone controls.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(poll)


def wait_until_sync(
    condition: Callable[[], bool],
    *,
    timeout: float = 5.0,
    poll: float = 0.01,
    message: str = "condition not met",
) -> None:
    """`wait_until`'s sibling for a test with no event loop of its own to await on.

    `TestClient(main.app)` runs the real application, background tasks included, on its
    own event loop in another thread; a synchronous test body cannot `await` that loop,
    only poll it from outside. #83: a fixed `time.sleep` guessing how long two passes of
    a shortened background loop take is exactly the bet that fails under load -- the
    loop is real and asyncio-scheduled, so how many passes land inside a fixed sleep
    depends on how promptly the OS runs that other thread, not on anything the test
    controls. This polls the condition itself instead, real wall-clock slack behind it
    (`timeout`) for a loaded machine to schedule that thread, and fails on a timeout
    with `message` rather than on whatever counter the caller was really asking about.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message} (timed out after {timeout}s)")
        time.sleep(poll)


def test_the_writer_subscribes_losslessly(tmp_path: Path) -> None:
    """Drop-oldest systematically shaves the highs and lows, because drops happen under
    load and load is when price moves fastest. That is a bias, not noise, and it is
    invisible in the output — so the writer takes the other policy."""
    bus = FanOut()
    writer = BarWriter(BarStore(tmp_path))
    subscription = writer.attach(bus)

    assert subscription.lossless is True
    assert bus.stats()[subscription.name]["lossless"] is True


def test_the_writer_publishes_one_typed_bar_for_each_table(tmp_path: Path) -> None:
    published: list[OptionBar] = []
    writer = BarWriter(BarStore(tmp_path), publish=published.append)
    lts = datetime(2026, 9, 4, 9, 0, 8, 123456, tzinfo=timezone.utc)

    writer._hand_to_store(
        writer.store, [bar(last_lts=lts)], BarTable.QUOTE
    )
    writer._hand_to_store(
        writer.reference_store, [reference()], BarTable.REFERENCE
    )
    writer._hand_to_store(writer.spot_store, [spot()], BarTable.SPOT)
    writer._hand_to_store(
        writer.computed_store, [computed_bar()], BarTable.COMPUTED
    )

    assert [event.table for event in published] == list(BarTable)
    assert all(event.instrument is None for event in published)
    assert all(event.underlying == "BTC" for event in published)
    assert [set(event.columns) for event in published] == [
        set(store.schema)
        for store in writer.stores
    ]
    assert published[0].columns["last_lts"] == lts.isoformat()
    assert published[0].columns["bid_ticks"] == 118


def test_a_publisher_error_does_not_lose_the_bar(tmp_path: Path) -> None:
    def fail(_event: OptionBar) -> None:
        raise RuntimeError("publisher is down")

    writer = BarWriter(BarStore(tmp_path), publish=fail)
    writer._hand_to_store(writer.store, [bar()], BarTable.QUOTE)

    assert writer.store.buffered == 1
    assert writer.stats()["publish_errors"] == 1


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
            publish(
                bus,
                "ob_l2",
                book_frame(
                    "C-BTC-77600-040926",
                    MINUTE_US + second * 1_000_000,
                    bid=70.0 + second,
                    ask=72.0 + second,
                ),
            )
        # A reference event on the same bus. Its own quote is the *fallback* and must not
        # displace the book's: its stamp runs a median 3,176 ms behind arrival, and the
        # book spoke for this contract-minute.
        publish(
            bus,
            "ticker",
            ticker_frame("C-BTC-77600-040926", MINUTE_US + 30_000_000),
        )

        # All three events are already queued before the writer task gets a turn, so
        # one full drain pass is guaranteed to have ingested every one of them.
        await wait_until(lambda: writer.loops >= 1)
        # No clock advance here: `aclose()` flushes whatever is still open in the
        # aggregator unconditionally (see `_Watermarked.flush`), so whether `_seal`
        # ever gets to see a boundary crossing makes no difference to the bar this
        # test reads back. `_seal`'s own boundary and grace logic is pinned directly,
        # without a clock or a sleep, in `test_bars.py`.

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


def test_the_writer_flushes_on_the_default_five_minute_cadence(tmp_path: Path) -> None:
    """The guard the interval needs, because its failure mode is silent.

    Bars that are never written look exactly like bars that were: the aggregators keep
    sealing, the counters keep moving, and nothing goes wrong until the process dies and
    takes the whole unflushed stretch with it. So the interval is asserted rather than
    observed, and it is asserted on the **default** — the engine constructs its writer
    without passing `flush_seconds`, so a default left at an hour is the live regression.

    Both edges are pinned. Nothing on disk at four minutes fifty-nine, everything on disk
    a second past five minutes. The clock is a variable this test assigns, never a real
    one it waits on: a test that slept for five minutes would be no test at all, and one
    that read the wall clock would be the third time-bomb this suite has grown.

    **The wait is on the condition, not on an elapsed guess.** A fixed real sleep here
    was the fourth time-bomb: a flush is a thread hop plus a Parquet write, and on a
    loaded machine 50ms is not always enough for both to land before the counter is
    read — `measured` 2026-09-07, 1 failure in 8 runs of the unrepaired test under load.
    So every step below polls `writer.loops` (one full pass of the drain loop) or the
    store's own counters instead of guessing a duration, via `wait_until` above.
    """

    async def scenario():
        started = MINUTE_US / 1e6
        now = started

        def clock() -> float:
            return now

        store = BarStore(tmp_path)
        # No `flush_seconds`: the default is the thing under test.
        writer = BarWriter(store, clock=clock, tick_seconds=0.01)
        bus = FanOut()
        writer.attach(bus)
        task = asyncio.create_task(writer.run())
        # `_last_flush` is stamped once, before the loop's first pass.
        await wait_until(lambda: writer._last_flush is not None)

        publish(
            bus,
            "ob_l2",
            book_frame("C-BTC-77600-040926", MINUTE_US + 5_000_000),
        )
        now = started + 120.0  # past the boundary and the grace: the bar seals
        await wait_until(lambda: store.buffered == 1)
        buffered = store.buffered

        # A loop iteration that starts after `now` moves is guaranteed to read the new
        # value — the writer's only await points are `queue.get` and the flush itself,
        # so nothing can observe a stale `now` once a fresh pass begins. Snapshotting
        # `loops` first and waiting for it to advance is therefore "the writer has seen
        # this clock reading", not a guess at how long seeing it takes.
        passes = writer.loops
        now = started + 299.0  # four minutes fifty-nine
        await wait_until(lambda passes=passes: writer.loops > passes)
        early = store.rows_written

        now = started + 301.0  # a second past five minutes
        # The flush itself: a thread hop and a Parquet write. Wait on its result, not on
        # the loop noticing the clock — `rows_written` only moves once the write lands.
        await wait_until(lambda: store.rows_written == 1, timeout=5.0)
        late = store.rows_written

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return buffered, early, late, store

    buffered, early, late, store = asyncio.run(scenario())

    assert buffered == 1, "the bar never reached the store's buffer"
    assert early == 0, "the buffer was written before the interval elapsed"
    assert late == 1, "five minutes passed and the buffer was still not written"
    assert store.scan().collect().height == 1


def test_a_flush_lands_every_buffered_bar_exactly_once(tmp_path: Path) -> None:
    """What left the buffer is what is on disk: no bar written twice, none dropped.

    Twelve flushes of ten bars — an hour at the five-minute cadence — because the denser
    layout is where a duplicate or a drop would show. The assertion is on the identity of
    each row, `(symbol, minute)`, and not on a count: a count matches just as happily if
    one bar were written twice and another lost.
    """
    store = BarStore(tmp_path)
    start = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    expected: list[tuple[str, datetime]] = []

    for window in range(12):
        batch = [
            bar(
                symbol=f"C-BTC-{77600 + step}-040926",
                strike=float(77600 + step),
                minute=start + timedelta(minutes=5 * window + offset),
            )
            for offset in range(5)
            for step in (0, 100)
        ]
        assert store.add(batch) == len(batch)
        assert store.flush() == len(batch)
        assert store.buffered == 0, "a bar was left behind in the buffer"
        expected.extend((one.symbol, one.minute) for one in batch)

    frame = store.scan().collect()
    landed = sorted(
        zip(frame["symbol"].to_list(), frame["minute"].to_list(), strict=True)
    )

    assert len(landed) == len(set(landed)), "a bar was written twice"
    assert landed == sorted(expected), "disk does not match what left the buffer"
    assert store.rows_written == len(expected)
    assert len(list(store.path.rglob("*.parquet"))) == 12, "a flush wrote no file"


def test_planned_generation_paths_match_the_files_written(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    store.add(
        [
            bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)),
            bar(
                symbol="C-ETH-3000-040926",
                underlying="ETH",
                strike=3000.0,
                minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
            ),
            bar(minute=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)),
        ]
    )

    planned = [path.relative_to(tmp_path).as_posix() for path in store.planned_paths(18)]
    written = store.flush(generation=18)

    assert written == 3
    assert planned == [
        "quote-bars/underlying=BTC/date=2026-09-04/20260904T090000Z-g00000018.parquet",
        "quote-bars/underlying=BTC/date=2026-09-05/20260905T100000Z-g00000018.parquet",
        "quote-bars/underlying=ETH/date=2026-09-04/20260904T090000Z-g00000018.parquet",
    ]
    assert sorted(
        path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*.parquet")
    ) == planned
    assert not list(tmp_path.rglob("*.flushing"))


def test_a_failed_generation_flush_restores_every_bar_and_file(tmp_path: Path) -> None:
    class FailingStore(BarStore):
        calls = 0

        def _frame(self, bars):
            type(self).calls += 1
            if type(self).calls == 2:
                raise RuntimeError("injected second-file failure")
            return super()._frame(bars)

    store = FailingStore(tmp_path)
    store.add(
        [
            bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)),
            bar(minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)),
        ]
    )

    with pytest.raises(RuntimeError, match="second-file"):
        store.flush(generation=18)

    assert store.buffered == 2
    assert list(tmp_path.rglob("*g00000018*")) == []


def _generation_writer(root: Path) -> tuple[BarWriter, Counter]:
    """Ingest two fixture minutes into a store-mode writer before its first commit."""
    now = (FLUSH_MINUTES[-1] + timedelta(minutes=2)).timestamp()
    writer = BarWriter(
        BarStore(root),
        clock=lambda: now,
        checkpoint_root=root,
    )
    expected: Counter = Counter()
    for index, minute in enumerate(FLUSH_MINUTES):
        stamp = minute + timedelta(seconds=5)
        writer.ingest(
            OptionQuote(
                source="test",
                ts_venue=stamp,
                ts_received=stamp,
                instrument=FLUSH_INSTRUMENT,
                bid=100.0 + index,
                ask=101.0 + index,
            )
        )
        expected[(FLUSH_INSTRUMENT.venue_symbol, minute)] += 1
    writer._seal(now)
    return writer, expected


def _quote_keys(root: Path) -> Counter:
    frame = BarStore(root).scan().collect()
    return Counter(zip(frame["symbol"], frame["minute"], strict=True))


def _recover_generation(root: Path) -> Checkpoint | None:
    checkpoint = read_checkpoint(root)
    recover_intent(
        root, committed_generation=0 if checkpoint is None else checkpoint.generation
    )
    return checkpoint


@pytest.mark.parametrize("stage", FLUSH_STAGES)
def test_a_generation_flush_interrupted_at_any_stage_replays_each_minute_once(
    tmp_path: Path, stage: str
) -> None:
    """Every interrupted commit preserves the two fixture minutes exactly."""
    writer, expected = _generation_writer(tmp_path)

    with pytest.raises(FlushInterrupted, match=stage):
        writer._commit(interrupt_at=stage)

    checkpoint = _recover_generation(tmp_path)
    if checkpoint is None:
        replay, _ = _generation_writer(tmp_path)
        assert replay._commit() == len(expected)
    else:
        restarted = BarWriter(
            BarStore(tmp_path),
            clock=lambda: (FLUSH_MINUTES[-1] + timedelta(minutes=2)).timestamp(),
            checkpoint_root=tmp_path,
        )
        restarted.restore_checkpoint(checkpoint)

    landed = _quote_keys(tmp_path)
    assert landed == expected, f"{stage}: disk keys differ from ingested minutes"
    assert all(count == 1 for count in landed.values()), f"{stage}: duplicate minute"


@pytest.mark.parametrize("stage", FLUSH_STAGES)
def test_a_generation_flush_recovery_leaves_no_staging_or_intent(
    tmp_path: Path, stage: str
) -> None:
    writer, _expected = _generation_writer(tmp_path)

    with pytest.raises(FlushInterrupted, match=stage):
        writer._commit(interrupt_at=stage)

    _recover_generation(tmp_path)

    assert read_intent(tmp_path) is None, f"{stage}: intent left behind"
    assert not list(tmp_path.rglob("*.flushing")), f"{stage}: staging file left behind"


def test_the_crash_tests_cover_every_flush_stage() -> None:
    """A stage added to `_commit` without a crash test to go with it is untested."""
    covered = {
        mark.args[1]
        for test in (
            test_a_generation_flush_interrupted_at_any_stage_replays_each_minute_once,
            test_a_generation_flush_recovery_leaves_no_staging_or_intent,
        )
        for mark in test.pytestmark
    }
    assert covered == {FLUSH_STAGES}
    assert len(FLUSH_STAGES) == 6


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
                    publish(
                        bus,
                        "ob_l2",
                        book_frame(
                            "C-BTC-77600-040926",
                            MINUTE_US + (published % 60) * 1_000_000,
                            bid=float(published) + 1,
                            ask=float(published) + 2,
                        ),
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


def test_the_reference_table_partitions_on_underlying_and_date_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Table B follows table A's layout exactly: `underlying/date` in the directory
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
        "underlying=BTC/date=2026-09-04",
        "underlying=ETH/date=2026-09-04",
        "underlying=BTC/date=2026-09-05",
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
                    underlying="BTC",
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
        "underlying=BTC/date=2026-09-04",
        "underlying=ETH/date=2026-09-04",
        "underlying=BTC/date=2026-09-05",
        "underlying=ETH/date=2026-09-05",
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


def book_frame(
    symbol: str, exchange_us: int, bid: float = 70.0, ask: float = 72.0, lts: int = 0
) -> dict:
    """An `ob_l2` payload shaped exactly as the venue sends one."""
    frame = {
        "type": "ob_l2",
        "sy": symbol,
        "ts": exchange_us,
        "lts": lts or exchange_us - 300_000,
        "b": [[str(bid), "10"]],
        "a": [[str(ask), "10"]],
    }
    return frame


def publish(bus, channel: str, frame: dict) -> None:
    """Decode one frame the way the live path does and publish every event it produced.

    **The events are the adapter's, not this file's.** A hand-built event would be a
    second copy of the catalogue with no way to notice the producer drifting away from it,
    and the whole point of the writer taking events is that the two agree.
    """
    for event in events_from_frame(channel, frame):
        bus.publish(event)


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
            publish(
                bus,
                "ob_l2",
                book_frame(
                    booked,
                    MINUTE_US + second * 1_000_000,
                    bid=70.0 + second,
                    ask=72.0 + second,
                ),
            )
        for symbol in (booked, quiet):
            publish(bus, "ticker", ticker_frame(symbol, MINUTE_US + 30_000_000))

        # Everything above is already queued before the writer task gets a turn, so
        # one full drain pass is guaranteed to have ingested all of it.
        await wait_until(lambda: writer.loops >= 1)
        # No clock advance: see the identical note in
        # test_the_writer_turns_bus_quotes_into_parquet_bars — `aclose()` flushes the
        # aggregators' open bars unconditionally, so the boundary crossing this used to
        # wait on made no difference to what lands on disk.

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

    Two shapes reach `skipped`. A reference event with no venue stamp cannot be bucketed
    on anything but our arrival time, which is the one thing this design refuses. And an
    event of a type this writer does not store — a heartbeat, a connection transition —
    is not a defect either: the subscription is to the whole bus.
    """
    # No `ts` and no readable `sp`: one reference event whose `ts_venue` is null, and no
    # index quote at all, because an absent spot is not an observation of absence.
    (stampless,) = events_from_frame("ticker", {"sy": "C-BTC-77600-040926", "d": [{}]})
    heartbeat = Heartbeat(
        source="DELTA",
        ts_received=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        adapter="DELTA",
        state=ConnectionState.CONNECTED,
    )

    writer = BarWriter(BarStore(tmp_path))
    writer.ingest(stampless)
    writer.ingest(heartbeat)

    stats = writer.stats()
    assert stats["skipped"] == 2
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
        self._stopping = False

    def subscribe(self, channel: str, symbols) -> None:
        self.registry.setdefault(channel, []).extend(symbols)

    def on_open(self, listener) -> None:
        return None

    def on_close(self, listener) -> None:
        return None

    def off_open(self, listener) -> None:
        return None

    def off_close(self, listener) -> None:
        return None

    def stop(self) -> None:
        """**Needed since #39**: the supervisor stops each controller on the way down,
        and a controller stops the adapter under it. A double missing this member let
        the shutdown path raise where the real feed would not."""
        self._stopping = True

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
        stats = main.app.state.events.stats()
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
            publish(
                main.app.state.events,
                "ob_l2",
                book_frame(
                    "C-BTC-77600-040926",
                    exchange_us + offset * 1000,
                    bid=70.0 + offset,
                    ask=72.0 + offset,
                ),
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
        publish(main.app.state.events, "ob_l2", book_frame(symbol, exchange_us))
        publish(
            main.app.state.events, "ticker", ticker_frame(symbol, exchange_us + 1000)
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


def test_the_unflushed_buffer_reads_back_with_the_same_schema_as_the_disk(
    tmp_path: Path,
) -> None:
    """`pending()` is `scan()`'s twin for the rows that are not on disk yet.

    The two are concatenated by `smile.read_smile`, so a column present on one and absent
    from the other — or typed differently — would break the union rather than degrade it.
    The partition columns are the risk: `_frame` deliberately omits them because on disk
    they are directory names, so `pending` has to rebuild them from the bars.
    """
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add([computed_bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc))])

    pending = store.pending().collect()

    assert pending.height == 1
    assert pending.collect_schema() == store.scan().collect_schema()
    assert pending.row(0, named=True)["date"] == date(2026, 9, 4)
    assert pending.row(0, named=True)["underlying"] == "BTC"
    assert store.scan().collect().height == 0, "nothing has been written"


def test_a_flush_empties_what_pending_reports(tmp_path: Path) -> None:
    """The two halves of the union must never both hold a row: a bar is buffered or it is
    filed, and one counted twice would draw a strike twice on the curve."""
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    store.add([computed_bar()])
    assert store.pending().collect().height == 1

    store.flush()

    assert store.pending().collect().height == 0
    assert store.scan().collect().height == 1


def test_an_empty_buffer_still_reports_the_full_schema(tmp_path: Path) -> None:
    """An empty frame with the real schema, not a frame with no columns. The union is
    built before anything is known about whether either side holds rows."""
    store = BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)

    empty = store.pending().collect()

    assert empty.height == 0
    assert set(empty.collect_schema()) == set(COMPUTED_SCHEMA) | {"date", "underlying"}


def test_a_pending_source_frames_the_same_rows_as_the_local_buffer(
    tmp_path: Path,
) -> None:
    bars = [
        computed_bar(minute=datetime(2026, 9, 4, 9, minute, tzinfo=timezone.utc))
        for minute in (0, 1)
    ]
    local = BarStore(tmp_path / "local", dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    local.add(bars)
    remote = BarStore(
        tmp_path / "remote",
        dataset=COMPUTED_DATASET,
        schema=COMPUTED_SCHEMA,
        pending_source=lambda: list(bars),
    )

    assert remote.pending().collect().equals(local.pending().collect())
    assert remote.buffered == 0


def test_a_buffer_fed_store_is_typed_even_when_its_source_is_empty(
    tmp_path: Path,
) -> None:
    store = BarStore(
        tmp_path,
        dataset=COMPUTED_DATASET,
        schema=COMPUTED_SCHEMA,
        pending_source=lambda: [],
    )

    empty = store.pending().collect()

    assert empty.height == 0
    assert dict(empty.collect_schema()) == {
        **COMPUTED_SCHEMA,
        "underlying": pl.Categorical,
        "date": pl.Date,
    }


def test_a_buffer_fed_store_cannot_write(tmp_path: Path) -> None:
    store = BarStore(
        tmp_path,
        dataset=COMPUTED_DATASET,
        schema=COMPUTED_SCHEMA,
        pending_source=lambda: [],
    )

    message = "computed-bars.*buffer-fed store does not write"
    with pytest.raises(RuntimeError, match=message):
        store.add([computed_bar()])
    with pytest.raises(RuntimeError, match=message):
        store.flush()
    assert store.buffered == 0


def test_scan_and_pending_prefers_the_committed_disk_row(
    tmp_path: Path,
) -> None:
    disk = bar()
    buffer = bar()
    object.__setattr__(disk, "bid_close", 701.0)
    object.__setattr__(buffer, "bid_close", 999.0)
    writable = BarStore(tmp_path)
    writable.add([disk])
    writable.flush()
    readable = BarStore(
        tmp_path,
        pending_source=lambda: [buffer],
    )

    rows = store_module.scan_and_pending(
        readable, pl.lit(True)
    ).collect()

    assert rows.height == 1
    assert rows.row(0, named=True)["bid_close"] == 701.0


def test_scan_and_pending_deduplicates_a_spot_row_without_a_symbol(
    tmp_path: Path,
) -> None:
    disk = spot(close=701.0)
    buffer = spot(close=999.0)
    writable = spot_store(tmp_path)
    writable.add([disk])
    writable.flush()
    readable = BarStore(
        tmp_path,
        dataset=SPOT_DATASET,
        schema=SPOT_SCHEMA,
        pending_source=lambda: [buffer],
    )

    rows = store_module.scan_and_pending(readable, pl.lit(True)).collect()

    assert rows.height == 1
    assert rows.row(0, named=True)["spot_close"] == 701.0


def test_scan_and_pending_keeps_disjoint_disk_and_buffer_rows(tmp_path: Path) -> None:
    disk = bar(minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc))
    buffer = bar(minute=datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc))
    writable = BarStore(tmp_path)
    writable.add([disk])
    writable.flush()
    readable = BarStore(tmp_path, pending_source=lambda: [buffer])

    rows = store_module.scan_and_pending(readable, pl.lit(True)).collect()

    assert rows.height == 2
    assert rows.sort("minute")["minute"].to_list() == [
        disk.minute,
        buffer.minute,
    ]


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
        quote_currency="USD",
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
        # The first pass always samples — `_sampled_at` starts `None` — so one full
        # loop pass is the condition, not a guess at how long that pass takes.
        await wait_until(lambda: writer.loops >= 1)

        passes = writer.loops
        now = (MINUTE_US + MINUTE) / 1e6 + 0.5
        await wait_until(lambda passes=passes: writer.loops > passes)

        held[:] = [sampled_chain(1, 50, iv=0.44)]
        passes = writer.loops
        now = (MINUTE_US + 2 * MINUTE) / 1e6 + 0.5
        await wait_until(lambda passes=passes: writer.loops > passes)

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


def test_the_chain_cache_is_sampled_several_times_within_one_minute(
    tmp_path: Path,
) -> None:
    """Six observations of the minute, not one.

    One sample per minute is a single instant deciding a whole minute: if the cache's
    stamp falls a hair on the wrong side of the boundary the minute is refused outright,
    which is `measured` at 217 of 904 minutes on the live store for expiry 25-09-2026 on
    2026-09-04. `ComputedAggregator`'s own docstring describes "a handful of samples"
    being folded; this is what makes the handful exist.

    The clock is driven, never waited on. Six ten-second steps across one minute, with
    the cache recomputed at each, must reach the aggregator as six samples per contract.
    """
    held: list[ChainResponse] = []

    async def scenario():
        # Five seconds before the minute opens, so the boundary itself is one of the
        # steps below rather than something the first pass consumed.
        now = MINUTE_US / 1e6 - 5.0

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
        await wait_until(lambda: writer.loops >= 1)

        for second in (0, 10, 20, 30, 40, 50):
            held[:] = [sampled_chain(0, second, iv=0.40)]
            passes = writer.loops
            now = MINUTE_US / 1e6 + second
            await wait_until(lambda passes=passes: writer.loops > passes)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return writer

    writer = asyncio.run(scenario())

    assert writer.stats()["computed"]["ticks"] == 12, (
        "six samples of a two-leg chain, one every ten seconds"
    )


def test_a_minute_keeps_the_freshest_sample_taken_inside_it(tmp_path: Path) -> None:
    """The gap, reproduced: the cache moves on a fraction after the boundary.

    The recompute loop runs every 100 ms, so by the time the writer's pass notices the
    minute has turned, the cache commonly holds a chain stamped in the **new** minute.
    Sampling only at the boundary then attributes nothing to the minute that just
    closed — every one of those losses is exactly one minute long, which is what
    `run lengths: [(1, 217)]` says on the live store.

    Sampling inside the minute means the minute keeps the freshest observation taken
    while it was open: `_Last` on the chain's own clock, the fold the aggregator already
    documents. Nothing is invented — 0.47 is a chain that really was computed at 09:00:45.
    """
    held: list[ChainResponse] = []

    async def scenario():
        # Five seconds into the minute, which is where a writer that has been running
        # all day always is: the minute it is closing is one it was already inside.
        now = MINUTE_US / 1e6 + 5.0

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
        await wait_until(lambda: writer.loops >= 1)

        for second, iv in ((20, 0.41), (45, 0.47)):
            held[:] = [sampled_chain(0, second, iv=iv)]
            passes = writer.loops
            now = MINUTE_US / 1e6 + second
            await wait_until(lambda passes=passes: writer.loops > passes)

        # 09:01:00.2 — the recompute that lands just past the boundary. The writer's
        # pass at 09:01:00.5 now finds a chain belonging to minute 1, and minute 0 has
        # nothing left in the cache to be sampled from.
        held[:] = [sampled_chain(1, 0, iv=0.52)]
        passes = writer.loops
        now = (MINUTE_US + MINUTE) / 1e6 + 0.5
        await wait_until(lambda passes=passes: writer.loops > passes)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()
        return writer

    writer = asyncio.run(scenario())
    frame = writer.computed_store.scan().collect()
    opening = frame.filter(
        pl.col("minute") == datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    )

    assert opening.height == 2, "the minute whose quotes were captured has no curve"
    assert set(opening["iv"].to_list()) == {0.47}, "not the freshest sample in the minute"


def test_a_sample_refused_as_late_is_counted_where_an_operator_can_read_it(
    tmp_path: Path,
) -> None:
    """Sampling six times a minute refuses six times as often, so the refusals have to
    be readable rather than inferred from two tables compared by hand months later.

    `_Watermarked.late` already counts them and `BarWriter.stats()` already nests the
    aggregator's own dictionary under `computed`; this pins that path so the number
    cannot quietly stop being reachable. No new counter, no new mechanism.
    """
    frozen = sampled_chain(0, 50, iv=0.40)

    async def scenario():
        now = MINUTE_US / 1e6 - 5.0

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
        await wait_until(lambda: writer.loops >= 1)

        for minute in (1, 2):
            for second in (0, 20, 40):
                passes = writer.loops
                now = (MINUTE_US + minute * MINUTE) / 1e6 + second
                await wait_until(lambda passes=passes: writer.loops > passes)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return writer

    writer = asyncio.run(scenario())
    computed = writer.stats()["computed"]

    assert computed["late"] > 0, "a refused sample was discarded silently"
    assert computed["late"] == writer.computed.late


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
        await wait_until(lambda: writer.loops >= 1)

        # Minutes 0 and 1 close with nothing computed at all.
        for minute in (1, 2):
            passes = writer.loops
            now = (MINUTE_US + minute * MINUTE) / 1e6 + 0.5
            await wait_until(lambda passes=passes: writer.loops > passes)

        # Minute 2 is computed, and then the feed stops: the cache goes on holding this
        # one chain while minutes 3 and 4 close over it.
        held.append(sampled_chain(2, 50, iv=0.48))
        for minute in (3, 4, 5):
            passes = writer.loops
            now = (MINUTE_US + minute * MINUTE) / 1e6 + 0.5
            await wait_until(lambda passes=passes: writer.loops > passes)

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
        await wait_until(lambda: writer.loops >= 1)

        for minute in (1, 2, 3, 4, 5):
            passes = writer.loops
            now = (MINUTE_US + minute * MINUTE) / 1e6 + 0.5
            await wait_until(lambda passes=passes: writer.loops > passes)

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
    """The fourth table through the real application, **with no browser open at all**.

    A ticker frame on the live bus is enriched and reaches the writer without anything
    on the bus carrying it — which is the whole structural difference between this table
    and the other three. Since #44 the path is the **minute pass**: the 100 ms loop
    solves only what a `/ws/chain` connection has registered, and no connection is
    opened here, so if the minute pass were not running this table would stay empty
    however long the feed ran. That is the acceptance line "the volatility screen keeps
    working with no chain page open", driven at the seam.

    The pass is given a 0.2 s cadence rather than its real minute so the test does not
    wait one out; everything else about it is the code the application runs.

    The assertion is on **content**, not on a row count, because a real minute boundary
    may fall inside the window and legitimately split the sample across two minutes.
    Whether silence produces rows is pinned by the deliberate-silence tests above, on an
    injected clock where it can be asserted exactly.
    """
    from fastapi.testclient import TestClient

    from deltapayoff import main
    from deltapayoff import stream as stream_module

    monkeypatch.setenv("DELTA_LIVE_FEED", "1")
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    monkeypatch.setattr(main, "DeltaFeed", _StubFeed)
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    real_pass = stream_module.recompute_every_minute
    monkeypatch.setattr(
        main,
        "recompute_every_minute",
        lambda chain_stream, sink: real_pass(chain_stream, sink, interval=0.2, lead=0.0),
    )

    symbol = "C-BTC-77600-040926"
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        writer, stream = main.app.state.writer, main.app.state.stream
        assert writer.computed_store.root == tmp_path
        assert writer.chains == stream.live_computed_chains, "the writer samples nothing"
        assert client.get("/health").json()["watched"] == [], "something is watching"

        now = time.time()
        publish(main.app.state.events, "ticker", ticker_frame(symbol, int(now * 1e6)))
        # #83: this used to be a fixed time.sleep(0.6) betting on two 0.2 s passes
        # landing inside it -- a bet that failed under load because it does not move
        # any faster just because the loop behind it was scheduled promptly. Wait on
        # the passes themselves instead.
        wait_until_sync(
            lambda: stream.minute_passes >= 1,
            timeout=5.0,
            message="the minute pass never ran",
        )
        wait_until_sync(
            lambda: writer.computed.stats()["ticks"] > 0,
            timeout=5.0,
            message="nothing reached the aggregator",
        )

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
        # `chains=[live]` is constant from the start, and `_sampled_at` begins `None`,
        # so the first full pass always samples it — this is table C's data landing.
        await wait_until(lambda: writer.loops >= 1)

        stamp = MINUTE_US + 50_000_000
        for frame in ws_ticker_frames.values():
            publish(bus, "ticker", {**frame, "ts": stamp})
        for frame in ws_book_frames.values():
            publish(bus, "ob_l2", {**frame, "ts": stamp})

        # Every frame above is already queued before the writer's next turn, and one
        # pass drains a queue to empty regardless of how many messages are on it.
        passes = writer.loops
        await wait_until(lambda passes=passes: writer.loops > passes)
        # No wait after this: the run loop never gets another turn before `cancel()`
        # below, so nothing here needs `_seal` to see it. `aclose()` still reads it
        # through `self.clock()` for its own forced re-sample — a no-op given a
        # constant `chains` callable, since `_bucket` keys on the tick's own
        # `exchange_us`, not on this reading, and the entry it updates already exists.
        now = (MINUTE_US + MINUTE) / 1e6 + 10.0  # past every grace period

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
        quote_currency="USD",
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


def test_all_four_tables_flush_underlying_before_date(tmp_path: Path) -> None:
    """Every table uses the same underlying-first tree for two assets and two dates."""
    tables = (
        (
            BarStore(tmp_path),
            [
                bar(
                    underlying="BTC",
                    minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
                ),
                bar(
                    symbol="C-ETH-3000-040926",
                    underlying="ETH",
                    strike=3000.0,
                    minute=datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc),
                ),
                bar(
                    underlying="BTC",
                    minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                ),
                bar(
                    symbol="C-ETH-3000-050926",
                    underlying="ETH",
                    strike=3000.0,
                    expiry="05-09-2026",
                    minute=datetime(2026, 9, 5, 9, 1, tzinfo=timezone.utc),
                ),
            ],
        ),
        (
            reference_store(tmp_path),
            [
                reference(underlying="BTC"),
                reference(
                    symbol="C-ETH-3000-040926",
                    underlying="ETH",
                    strike=3000.0,
                ),
                reference(
                    underlying="BTC",
                    minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                ),
                reference(
                    symbol="C-ETH-3000-050926",
                    underlying="ETH",
                    strike=3000.0,
                    minute=datetime(2026, 9, 5, 9, 1, tzinfo=timezone.utc),
                ),
            ],
        ),
        (
            spot_store(tmp_path),
            [
                spot(underlying="BTC"),
                spot(underlying="ETH"),
                spot(
                    underlying="BTC",
                    minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                ),
                spot(
                    underlying="ETH",
                    minute=datetime(2026, 9, 5, 9, 1, tzinfo=timezone.utc),
                ),
            ],
        ),
        (
            BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
            [
                computed_bar(underlying="BTC"),
                computed_bar(
                    symbol="C-ETH-4000-040926",
                    underlying="ETH",
                    strike=4000.0,
                ),
                computed_bar(
                    underlying="BTC",
                    minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                ),
                computed_bar(
                    symbol="C-ETH-4000-050926",
                    underlying="ETH",
                    strike=4000.0,
                    minute=datetime(2026, 9, 5, 9, 1, tzinfo=timezone.utc),
                ),
            ],
        ),
    )
    expected = {
        "underlying=BTC/date=2026-09-04",
        "underlying=ETH/date=2026-09-04",
        "underlying=BTC/date=2026-09-05",
        "underlying=ETH/date=2026-09-05",
    }

    for store, bars in tables:
        store.add(bars)
        assert store.flush() == 4
        directories = {
            path.relative_to(store.path).parent.as_posix()
            for path in store.path.rglob("*.parquet")
        }
        assert directories == expected, store.dataset
        assert not any(
            child.is_dir() and child.name.startswith("date=")
            for child in store.path.iterdir()
        )


def test_all_four_tables_round_trip_every_column_in_the_underlying_first_tree(
    tmp_path: Path,
) -> None:
    """A fixed post-move minute preserves every table column and both path keys."""
    minute = datetime(2026, 9, 5, 12, 34, 56, 789, tzinfo=timezone.utc)
    tables = (
        (BarStore(tmp_path), bar(minute=minute)),
        (reference_store(tmp_path), reference(minute=minute)),
        (spot_store(tmp_path), spot(minute=minute)),
        (
            BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
            computed_bar(minute=minute),
        ),
    )

    for store, expected in tables:
        store.add([expected])
        assert store.flush() == 1
        row = store.scan().collect().row(0, named=True)

        for column in store.schema:
            assert row[column] == getattr(expected, column), f"{store.dataset}:{column}"
        assert row["underlying"] == expected.underlying
        assert row["date"] == minute.date()


def test_partitions_skip_non_directories_and_return_sorted_public_tuples(
    tmp_path: Path,
) -> None:
    store = BarStore(tmp_path)
    store.add(
        [
            bar(
                minute=datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
                underlying="BTC",
            ),
            bar(
                minute=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
                underlying="ETH",
                symbol="C-ETH-3000-040926",
                strike=3000.0,
            ),
        ]
    )
    store.flush()
    (store.path / "underlying=ignored-file").write_text("not a directory")
    (store.path / "date=ignored-root").mkdir()
    (store.path / "underlying=BTC" / "date=ignored-file").write_text(
        "not a directory"
    )

    assert store.partitions() == [
        ("2026-09-04", "ETH"),
        ("2026-09-05", "BTC"),
    ]
