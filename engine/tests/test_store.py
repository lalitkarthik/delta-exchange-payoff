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
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from deltapayoff.bars import BarAggregator, QuoteBar, Tick
from deltapayoff.fanout import FanOut
from deltapayoff.feed import Quote
from deltapayoff.store import BarStore, BarWriter

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
