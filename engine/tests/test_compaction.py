"""Compaction: a day's hourly fragments into one file, without ever losing the day.

**This is the only part of the store that deletes anything, so it is the only part that
can lose something permanently.** Everywhere else a bug writes a wrong file and the right
one is still recoverable from the feed or from its neighbours; here a bug removes the
inputs and there is nothing to re-run from. The tests are shaped around that asymmetry.

Three properties carry this file, and only the first is about the happy path:

**Nothing is deleted before the output verifies.** Asserted by sabotage - make the
verification fail and assert that every input file is still on disk and still readable.

**An interruption at any stage reruns to a correct store.** Parametrised over every entry
in `COMPACTION_STAGES`, and `test_the_crash_tests_cover_every_compaction_stage` fails if
a stage is ever added without a crash test to go with it. The interruption is raised from
*inside* the real code path rather than reconstructed afterwards, because hand-built
wreckage only ever tests what the test's author imagined a crash would leave.

**A partition never reads back doubled.** The window between publishing and deleting is
where a doubling would live, and the ordering here is chosen so that window shows a gap
instead. A gap is visible and recoverable; a silent doubling is invention, which this
store refuses everywhere else.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from deltapayoff.bars import ComputedBar, QuoteBar, ReferenceBar, SpotBar
from deltapayoff.store import (
    COMPACT_PREFIX,
    COMPACTION_STAGES,
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    MANIFEST_NAME,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    TMP_SUFFIX,
    BarStore,
    CompactionInterrupted,
    CompactionUnsound,
    all_stores,
    compact_all,
)

DAY = "2026-09-03"
NEXT_DAY = "2026-09-04"
START = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)


def quote_bar(minute: datetime, strike: float, underlying: str = "BTC") -> QuoteBar:
    option = "C" if int(strike) % 200 == 0 else "P"
    return QuoteBar(
        symbol=f"{option}-{underlying}-{int(strike)}-030926",
        underlying=underlying,
        expiry="03-09-2026",
        strike=strike,
        option_type=option,
        minute=minute,
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
        from_book=True,
        last_lts=None,
    )


def write_hours(
    store: BarStore,
    *,
    hours: int = 24,
    per_hour: int = 3,
    underlying: str = "BTC",
    start: datetime = START,
) -> int:
    """One flush per hour, exactly as the writer produces them. Returns rows written."""
    written = 0
    for hour in range(hours):
        bars = [
            quote_bar(
                start + timedelta(hours=hour, minutes=index),
                77000.0 + index * 100,
                underlying,
            )
            for index in range(per_hour)
        ]
        store.add(bars)
        written += store.flush()
    return written


def partition(store: BarStore, day: str = DAY, underlying: str = "BTC") -> Path:
    return store.path / f"date={day}" / f"underlying={underlying}"


def parquet_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.parquet"))


def rows_on_disk(store: BarStore) -> pl.DataFrame:
    """Everything the store reads back, ordered so two reads can be compared."""
    return store.scan().collect().sort("symbol", "minute")


# -- the happy path --------------------------------------------------------------------


def test_a_days_hourly_files_become_one_file_per_table_per_partition(
    tmp_path: Path,
) -> None:
    store = BarStore(tmp_path)
    written = write_hours(store)

    assert len(parquet_files(partition(store))) == 24, "the day should start as 24 files"

    result = store.compact_partition(DAY, "BTC")

    assert result.compacted is True
    assert result.files_before == 24
    assert result.files_after == 1
    assert result.rows == written
    assert len(parquet_files(partition(store))) == 1


def test_the_compacted_file_holds_every_row_the_inputs_held_and_no_others(
    tmp_path: Path,
) -> None:
    """Row count *and* content. A count alone would pass on a file of the right size
    holding the wrong day."""
    store = BarStore(tmp_path)
    write_hours(store)
    before = rows_on_disk(store)

    store.compact_partition(DAY, "BTC")
    after = rows_on_disk(store)

    assert after.height == before.height
    assert after.equals(before)


def test_the_compacted_file_reads_back_with_the_declared_types(tmp_path: Path) -> None:
    """A compaction that silently widened a `UInt32` count or narrowed a `Float64` price
    would be a schema change nobody asked for, applied to a year of history."""
    store = BarStore(tmp_path)
    write_hours(store)
    store.compact_partition(DAY, "BTC")

    frame = pl.read_parquet(parquet_files(partition(store))[0])
    assert dict(frame.schema) == dict(pl.DataFrame(schema=store.schema).schema)


def test_compacting_an_already_compacted_partition_is_a_no_op_and_not_a_doubling(
    tmp_path: Path,
) -> None:
    """The failure this guards is not an error message, it is a partition that quietly
    holds every row twice."""
    store = BarStore(tmp_path)
    write_hours(store)
    first = store.compact_partition(DAY, "BTC")
    only = parquet_files(partition(store))[0]
    stamp = only.stat().st_mtime_ns

    second = store.compact_partition(DAY, "BTC")

    assert second.compacted is False
    assert second.files_before == second.files_after == 1
    assert second.rows == first.rows
    assert parquet_files(partition(store)) == [only]
    assert only.stat().st_mtime_ns == stamp, "a no-op must not rewrite the file"
    assert rows_on_disk(store).height == first.rows


def test_hourly_files_written_after_a_compaction_fold_into_the_next_one(
    tmp_path: Path,
) -> None:
    """A late flush - a backfill, a restart, a partition compacted before the day was
    truly closed - must end as one file, not as a compacted file with satellites."""
    store = BarStore(tmp_path)
    write_hours(store, hours=20)
    store.compact_partition(DAY, "BTC")
    write_hours(store, hours=4, start=START + timedelta(hours=20))
    expected = rows_on_disk(store)

    assert len(parquet_files(partition(store))) == 5

    result = store.compact_partition(DAY, "BTC")

    assert result.compacted is True
    assert result.files_before == 5
    assert len(parquet_files(partition(store))) == 1
    assert rows_on_disk(store).equals(expected)


def test_compacting_a_partition_that_does_not_exist_is_a_no_op(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    result = store.compact_partition("2020-01-01", "BTC")
    assert result.compacted is False
    assert result.rows == 0


def test_a_scan_across_a_compacted_and_an_uncompacted_partition_sees_each_row_once(
    tmp_path: Path,
) -> None:
    store = BarStore(tmp_path)
    write_hours(store, hours=4)
    write_hours(store, hours=4, start=datetime(2026, 9, 4, tzinfo=timezone.utc))
    expected = rows_on_disk(store)

    store.compact_partition(DAY, "BTC")

    assert rows_on_disk(store).equals(expected)
    assert len(parquet_files(partition(store, DAY))) == 1
    assert len(parquet_files(partition(store, NEXT_DAY))) == 4


# -- what compaction is allowed to delete ----------------------------------------------


def test_nothing_is_deleted_when_the_compacted_file_fails_to_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage. A bad write is simulated by making the inputs' own row counts disagree
    with what the output actually contains, which is exactly the shape of the failure the
    verification exists to catch - and the whole point is what is still on disk after."""
    store = BarStore(tmp_path)
    written = write_hours(store)
    before = {path.name: path.read_bytes() for path in parquet_files(partition(store))}

    monkeypatch.setattr(BarStore, "_rows", staticmethod(lambda path: 10_000))

    with pytest.raises(CompactionUnsound):
        store.compact_partition(DAY, "BTC")

    monkeypatch.undo()
    after = {path.name: path.read_bytes() for path in parquet_files(partition(store))}
    assert after == before, "an input was touched before the output verified"
    assert rows_on_disk(store).height == written


def test_a_schema_that_does_not_match_the_files_fails_before_any_delete(
    tmp_path: Path,
) -> None:
    """The same gate, reached the other way: the output's schema is checked against the
    store's, so a store pointed at the wrong dataset refuses rather than rewrites."""
    write_hours(BarStore(tmp_path))
    wrong = BarStore(tmp_path, schema=SPOT_SCHEMA)
    directory = partition(wrong)

    with pytest.raises(CompactionUnsound):
        wrong.compact_partition(DAY, "BTC")

    assert len(parquet_files(directory)) == 24
    assert not list(directory.glob("*" + TMP_SUFFIX)), "a failed run left its tmp behind"


def test_compaction_touches_nothing_but_the_partition_it_was_given(
    tmp_path: Path,
) -> None:
    """Pre-compaction files in *this* partition are the only thing ever deleted."""
    store = BarStore(tmp_path)
    write_hours(store, hours=3)
    write_hours(store, hours=3, underlying="ETH")
    write_hours(store, hours=3, start=datetime(2026, 9, 4, tzinfo=timezone.utc))
    other = BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA)
    other.path.mkdir(parents=True, exist_ok=True)
    stray = partition(store) / "README.txt"
    stray.write_text("not a parquet file", encoding="utf-8")

    store.compact_partition(DAY, "BTC")

    assert len(parquet_files(partition(store, DAY, "BTC"))) == 1
    assert len(parquet_files(partition(store, DAY, "ETH"))) == 3
    assert len(parquet_files(partition(store, NEXT_DAY, "BTC"))) == 3
    assert stray.exists(), "compaction deleted a file it did not write"


def test_the_open_day_is_left_alone_by_default(tmp_path: Path) -> None:
    """`flush` writes straight to its final name, so today's partition can be half a file
    at any instant. Compaction waits for the day to close rather than racing it."""
    store = BarStore(tmp_path)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    closed = today - timedelta(days=2)
    write_hours(store, hours=3, start=closed)
    write_hours(store, hours=3, start=today)

    results = {result.date: result for result in store.compact(before=None)}

    assert results[closed.strftime("%Y-%m-%d")].compacted is True
    assert today.strftime("%Y-%m-%d") not in results
    assert len(parquet_files(partition(store, today.strftime("%Y-%m-%d")))) == 3


# -- crash safety ----------------------------------------------------------------------


@pytest.mark.parametrize("stage", COMPACTION_STAGES)
def test_a_compaction_interrupted_at_any_stage_reruns_to_a_correct_store(
    tmp_path: Path, stage: str
) -> None:
    """Red-line test. Kill the compaction at `stage`, run it again, and the store must
    hold exactly the rows it started with - in one file, with no wreckage left."""
    store = BarStore(tmp_path)
    write_hours(store)
    expected = rows_on_disk(store)

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at=stage)

    result = store.compact_partition(DAY, "BTC")

    directory = partition(store)
    assert len(parquet_files(directory)) == 1, f"{stage}: not one file"
    assert rows_on_disk(store).equals(expected), f"{stage}: the day changed"
    assert result.rows == expected.height
    assert not (directory / MANIFEST_NAME).exists(), f"{stage}: manifest left behind"
    assert not list(directory.glob("*" + TMP_SUFFIX)), f"{stage}: tmp left behind"


@pytest.mark.parametrize("stage", COMPACTION_STAGES)
def test_a_compaction_interrupted_at_any_stage_never_reads_back_a_doubled_row(
    tmp_path: Path, stage: str
) -> None:
    """The window between publishing the output and deleting the inputs is where a
    doubling would live. The inputs are deleted *first*, so the worst a reader can see
    mid-crash is a day that is short - never one that is twice itself."""
    store = BarStore(tmp_path)
    write_hours(store)
    expected = rows_on_disk(store)

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at=stage)

    stranded = rows_on_disk(store)
    assert stranded.height <= expected.height, f"{stage}: the store read back doubled"
    assert stranded.unique().height == stranded.height, f"{stage}: a row appeared twice"
    # Whatever survived must be a *subset* of what was written, value for value. Short is
    # allowed and doubled is not; invented is not either.
    surviving = expected.join(
        stranded.select("symbol", "minute"), on=["symbol", "minute"], how="semi"
    ).sort("symbol", "minute")
    assert stranded.equals(surviving), f"{stage}: a row appeared that was never written"


@pytest.mark.parametrize("stage", COMPACTION_STAGES)
def test_a_compaction_interrupted_at_any_stage_is_recoverable_twice_over(
    tmp_path: Path, stage: str
) -> None:
    """Idempotence is not "runs twice"; it is "runs any number of times". A third and a
    fourth run after a crash must still leave one file and the same rows."""
    store = BarStore(tmp_path)
    write_hours(store, hours=6)
    expected = rows_on_disk(store)

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at=stage)
    for _ in range(3):
        store.compact_partition(DAY, "BTC")

    assert len(parquet_files(partition(store))) == 1
    assert rows_on_disk(store).equals(expected)


def test_the_crash_tests_cover_every_compaction_stage() -> None:
    """A standing assertion, not a formality: a stage added to `compact_partition`
    without a crash test to go with it is a stage nobody has ever interrupted."""
    covered = {
        mark.args[1]
        for test in (
            test_a_compaction_interrupted_at_any_stage_reruns_to_a_correct_store,
            test_a_compaction_interrupted_at_any_stage_never_reads_back_a_doubled_row,
            test_a_compaction_interrupted_at_any_stage_is_recoverable_twice_over,
        )
        for mark in test.pytestmark
    }
    assert covered == {COMPACTION_STAGES}
    assert len(COMPACTION_STAGES) == 6


def test_a_flush_that_lands_mid_compaction_is_not_deleted_by_it(tmp_path: Path) -> None:
    """Only the names the manifest recorded are removed. A file that appeared after the
    input list was taken is not one of them, so an hourly flush racing a compaction
    survives it and is folded in by the next run."""
    store = BarStore(tmp_path)
    write_hours(store, hours=6)

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at="after-manifest")

    write_hours(store, hours=1, start=START + timedelta(hours=6))
    expected = 6 * 3 + 3

    store.compact_partition(DAY, "BTC")

    assert len(parquet_files(partition(store))) == 1
    assert rows_on_disk(store).height == expected


def test_a_stale_tmp_from_an_uncommitted_run_is_cleared_rather_than_published(
    tmp_path: Path,
) -> None:
    """Before the manifest exists, the tmp is worth nothing: every input it was built
    from is still on disk. Publishing it on the next run would be publishing a file no
    manifest ever vouched for."""
    store = BarStore(tmp_path)
    write_hours(store, hours=4)

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at="after-write")

    directory = partition(store)
    assert list(directory.glob("*" + TMP_SUFFIX)), "the interruption left no tmp to clear"
    assert len(parquet_files(directory)) == 4, "an input was deleted before the commit"

    store.compact_partition(DAY, "BTC")

    assert not list(directory.glob("*" + TMP_SUFFIX))
    assert len(parquet_files(directory)) == 1


def test_a_recovery_refuses_to_delete_inputs_for_an_output_that_no_longer_verifies(
    tmp_path: Path,
) -> None:
    """Recovery re-verifies before it deletes, exactly as the first pass does.

    The manifest already says the output verified once, but a recovery runs by definition
    after something went wrong - so a tmp that has been damaged since must stop the
    recovery, not be published over a day whose inputs it then removes.
    """
    store = BarStore(tmp_path)
    write_hours(store, hours=4)

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at="after-manifest")

    stranded = next(iter(partition(store).glob("*" + TMP_SUFFIX)))
    stranded.write_bytes(b"this is no longer a parquet file")

    with pytest.raises(CompactionUnsound):
        store.compact_partition(DAY, "BTC")

    assert len(parquet_files(partition(store))) == 4, "an input was deleted anyway"
    assert rows_on_disk(store).height == 4 * 3


def test_the_manifest_names_the_files_it_is_about(tmp_path: Path) -> None:
    """The manifest is the commit record and the recovery instruction, so what it holds
    is a contract: the output, the exact inputs it was verified to contain, and the row
    count that verification checked."""
    store = BarStore(tmp_path)
    written = write_hours(store, hours=4)
    inputs = [path.name for path in parquet_files(partition(store))]

    with pytest.raises(CompactionInterrupted):
        store.compact_partition(DAY, "BTC", interrupt_at="after-manifest")

    manifest = json.loads(
        (partition(store) / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["inputs"] == inputs
    assert manifest["rows"] == written
    assert manifest["output"].startswith(COMPACT_PREFIX)


# -- the whole store -------------------------------------------------------------------


def reference_bar(minute: datetime, strike: float) -> ReferenceBar:
    return ReferenceBar(
        symbol=f"C-BTC-{int(strike)}-030926",
        underlying="BTC",
        expiry="03-09-2026",
        strike=strike,
        option_type="C",
        minute=minute,
        mark_open=71.0,
        mark_high=72.0,
        mark_low=70.0,
        mark_close=71.5,
        mark_ticks=12,
        ltp_open=None,
        ltp_high=None,
        ltp_low=None,
        ltp_close=None,
        ltp_ticks=0,
        oi_contracts=1234.0,
        oi_change_usd_6h=-50.0,
        turnover=99.5,
        venue_delta=0.5,
        venue_gamma=0.0001,
        venue_rho=0.02,
        venue_theta=-3.5,
        venue_vega=12.0,
        venue_bid_iv=0.42,
        venue_ask_iv=0.44,
        venue_mark_iv=0.43,
    )


def spot_bar(minute: datetime) -> SpotBar:
    return SpotBar(
        underlying="BTC",
        minute=minute,
        spot_open=77651.9,
        spot_high=77700.0,
        spot_low=77600.0,
        spot_close=77680.0,
        spot_ticks=7056,
    )


def computed_bar(minute: datetime, strike: float) -> ComputedBar:
    return ComputedBar(
        symbol=f"C-BTC-{int(strike)}-030926",
        underlying="BTC",
        expiry="03-09-2026",
        strike=strike,
        option_type="C",
        minute=minute,
        iv=0.4312,
        iv_leg="call",
        iv_reason=None,
        delta=0.51,
        gamma=0.00012,
        vega=11.9,
        theta=-3.4,
        rho=0.021,
        forward=77712.0,
        discount=0.9998,
        years_to_expiry=0.0027,
        forward_method="F1",
        model_version="F1+assumed-6.5 / S1-newton / ACT365 / mid-OTM",
    )


def test_a_full_day_of_all_four_tables_compacts_and_reads_back_with_no_invented_rows(
    tmp_path: Path,
) -> None:
    """End to end, across all four tables, with a deliberate silence in the middle.

    Twenty-four hourly flushes per table, three of the twenty-four hours left empty on
    purpose, then one compaction of the lot. The row count read back must equal the
    minutes that actually had something in them - **the same no-invention assertion the
    rest of the store carries, asked of the component whose job is to delete files.**
    """
    quotes, reference, spot, computed = all_stores(tmp_path)
    silent = {5, 11, 17}
    strikes = [77000.0, 77100.0]
    minutes = 0

    for hour in range(24):
        if hour not in silent:
            for index in range(3):
                when = START + timedelta(hours=hour, minutes=index)
                quotes.add(quote_bar(when, strike) for strike in strikes)
                reference.add(reference_bar(when, strike) for strike in strikes)
                computed.add(computed_bar(when, strike) for strike in strikes)
                spot.add([spot_bar(when)])
                minutes += 1
        for store in (quotes, reference, spot, computed):
            store.flush()

    for store in (quotes, reference, spot, computed):
        assert len(parquet_files(partition(store))) == 24 - len(silent)

    results = compact_all(tmp_path, before=NEXT_DAY)

    assert [result.compacted for result in results] == [True] * 4
    for store, schema in (
        (quotes, quotes.schema),
        (reference, REFERENCE_SCHEMA),
        (spot, SPOT_SCHEMA),
        (computed, COMPUTED_SCHEMA),
    ):
        files = parquet_files(partition(store))
        assert len(files) == 1, store.dataset
        frame = store.scan().collect()
        expected_rows = minutes if store.dataset == SPOT_DATASET else minutes * 2
        assert frame.height == expected_rows, store.dataset
        assert dict(pl.read_parquet(files[0]).schema) == dict(
            pl.DataFrame(schema=schema).schema
        ), store.dataset
        # No invented minutes: the silent hours produced no file and must produce no row.
        stored_hours = {
            value.hour for value in frame.get_column("minute").to_list()
        }
        assert stored_hours.isdisjoint(silent), store.dataset


def test_all_four_tables_are_compacted_together_and_none_is_forgotten(
    tmp_path: Path,
) -> None:
    """`all_stores` exists so the fourth table cannot be left out of a whole-store job,
    which is exactly the mistake a hand-written list of three makes once."""
    for store in all_stores(tmp_path):
        assert store.dataset in {
            "quote-bars",
            REFERENCE_DATASET,
            SPOT_DATASET,
            COMPUTED_DATASET,
        }
    assert len({store.dataset for store in all_stores(tmp_path)}) == 4
    assert all(store.root == tmp_path for store in all_stores(tmp_path))


def test_partitions_are_read_off_the_directory_names(tmp_path: Path) -> None:
    store = BarStore(tmp_path)
    write_hours(store, hours=1)
    write_hours(store, hours=1, underlying="ETH")
    write_hours(store, hours=1, start=datetime(2026, 9, 4, tzinfo=timezone.utc))

    assert store.partitions() == [
        (DAY, "BTC"),
        (DAY, "ETH"),
        (NEXT_DAY, "BTC"),
    ]


def test_an_empty_store_has_no_partitions_and_compacts_to_nothing(
    tmp_path: Path,
) -> None:
    assert BarStore(tmp_path).partitions() == []
    assert compact_all(tmp_path, before=NEXT_DAY) == []
