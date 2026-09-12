"""`index-bars`: the backfilled `.DEXBTUSD` index candle table. R1, R2 pinned here.

Unlike the four live tables in `test_store.py`, nothing here comes off the bus — a row
is written only by `tools/backfill_index_bars.py`, so this suite constructs `IndexBar`
rows by hand and drives them through the same `BarStore.add()` / `BarStore.flush()` path
table D uses. `tmp_path` only; nothing here reads or writes the repository's real `data/`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from deltapayoff.bars import IndexBar
from deltapayoff.store import INDEX_DATASET, INDEX_SCHEMA, BarStore, read_index_bars

MINUTE = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)


def bar(
    *,
    underlying: str = "BTC",
    minute: datetime = MINUTE,
    symbol: str = ".DEXBTUSD",
    index_open: float | None = 80_000.0,
    index_high: float | None = 80_010.0,
    index_low: float | None = 79_990.0,
    index_close: float | None = 80_005.0,
) -> IndexBar:
    return IndexBar(
        underlying=underlying,
        minute=minute,
        symbol=symbol,
        index_open=index_open,
        index_high=index_high,
        index_low=index_low,
        index_close=index_close,
    )


def index_store(root: Path) -> BarStore:
    return BarStore(root, dataset=INDEX_DATASET, schema=INDEX_SCHEMA)


def test_the_dataset_name_is_index_bars() -> None:
    assert INDEX_DATASET == "index-bars"


def test_an_index_bar_round_trips_through_parquet_with_its_values_and_its_types(
    tmp_path: Path,
) -> None:
    """Persisted only through `add()` + `flush()` — R2 — so the schema and the layout
    are both proven by writing a real file rather than asserted from the constant."""
    store = index_store(tmp_path)
    store.add([bar()])
    assert store.flush() == 1

    frame = store.scan().collect()
    schema = frame.collect_schema()

    assert frame.height == 1
    assert schema["minute"] == pl.Datetime("us", "UTC")
    assert schema["symbol"] == pl.String
    for column in ("index_open", "index_high", "index_low", "index_close"):
        assert schema[column] == pl.Float64, column

    row = frame.row(0, named=True)
    assert row["symbol"] == ".DEXBTUSD"
    assert row["index_close"] == 80_005.0
    assert row["index_high"] >= max(row["index_open"], row["index_close"])
    assert row["index_low"] <= min(row["index_open"], row["index_close"])


def test_the_layout_is_underlying_then_date_with_no_second_partition_writer(
    tmp_path: Path,
) -> None:
    """R1/R2: born in `underlying=.../date=.../` through the hand-built writer, never
    through `pl.write_parquet(partition_by=...)`. The directory names are the proof."""
    store = index_store(tmp_path)
    store.add(
        [
            bar(minute=MINUTE),
            bar(minute=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc)),
        ]
    )
    assert store.flush() == 2

    directories = {
        path.relative_to(store.path).parent.as_posix()
        for path in store.path.rglob("*.parquet")
    }
    assert directories == {
        "underlying=BTC/date=2026-09-04",
        "underlying=BTC/date=2026-09-05",
    }
    # Each flush names its own file — no writer laid the tree out by partition key.
    assert all(path.name.endswith(".parquet") for path in store.path.rglob("*.parquet"))


def test_index_bars_is_not_part_of_the_four_live_tables(tmp_path: Path) -> None:
    """R1: nightly compaction and `tools/migrate_store.py` keep exactly the four
    recording tables. `all_stores()` must not have grown a fifth."""
    from deltapayoff.store import all_stores

    datasets = {store.dataset for store in all_stores(tmp_path)}
    assert INDEX_DATASET not in datasets
    assert len(datasets) == 4


def test_read_index_bars_returns_ascending_realised_vol_bars(tmp_path: Path) -> None:
    store = index_store(tmp_path)
    later = MINUTE.replace(minute=1)
    store.add([bar(minute=later, index_close=80_100.0), bar(minute=MINUTE)])
    store.flush()

    rows = read_index_bars(store, "BTC")

    assert [row.at for row in rows] == [MINUTE, later]
    assert rows[0].close == 80_005.0
    assert rows[1].close == 80_100.0


def test_read_index_bars_drops_a_row_with_any_null_ohlc_value(tmp_path: Path) -> None:
    """Never substitute zero or another minute's price for a missing one — the row is
    dropped outright, exactly as `read_spot_bars` drops a null spot price."""
    store = index_store(tmp_path)
    complete = MINUTE
    holed = MINUTE.replace(minute=1)
    store.add([bar(minute=complete), bar(minute=holed, index_high=None)])
    store.flush()

    rows = read_index_bars(store, "BTC")

    assert [row.at for row in rows] == [complete]


def test_read_index_bars_preserves_a_missing_minute_as_missing(tmp_path: Path) -> None:
    """R4, read side: a bucket that was never written stays absent — never forward-filled
    and never interpolated from its neighbours."""
    store = index_store(tmp_path)
    first = MINUTE
    third = MINUTE.replace(minute=2)  # minute 1 never arrives
    store.add([bar(minute=first), bar(minute=third)])
    store.flush()

    rows = read_index_bars(store, "BTC")

    assert [row.at for row in rows] == [first, third]


def test_read_index_bars_applies_inclusive_start_and_end_filters(tmp_path: Path) -> None:
    store = index_store(tmp_path)
    early = MINUTE
    middle = MINUTE.replace(minute=1)
    late = MINUTE.replace(minute=2)
    store.add([bar(minute=early), bar(minute=middle), bar(minute=late)])
    store.flush()

    rows = read_index_bars(store, "BTC", start=middle, end=middle)
    assert [row.at for row in rows] == [middle]

    inclusive = read_index_bars(store, "BTC", start=early, end=late)
    assert [row.at for row in inclusive] == [early, middle, late]


def test_read_index_bars_optionally_filters_symbol(tmp_path: Path) -> None:
    store = index_store(tmp_path)
    store.add(
        [
            bar(symbol=".DEXBTUSD", index_close=80_000.0),
            bar(symbol=".DEXBTUSDT", index_close=81_000.0),
        ]
    )
    store.flush()

    rows = read_index_bars(store, "BTC", symbol=".DEXBTUSD")
    assert [row.close for row in rows] == [80_000.0]


def test_read_index_bars_filters_by_underlying(tmp_path: Path) -> None:
    store = index_store(tmp_path)
    store.add([bar(underlying="BTC"), bar(underlying="ETH", index_close=3_000.0)])
    store.flush()

    rows = read_index_bars(store, "BTC")
    assert len(rows) == 1
    assert rows[0].close == 80_005.0


def test_read_index_bars_on_an_empty_store_returns_nothing(tmp_path: Path) -> None:
    store = index_store(tmp_path)
    assert read_index_bars(store, "BTC") == []
