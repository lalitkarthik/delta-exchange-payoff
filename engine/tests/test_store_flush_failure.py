"""What a store flush does when `write_parquet` raises.

Every other test in `test_store.py` drives the happy path of `write_parquet`, which is why
#101's defect survived: `_flush_legacy` emptied its buffer before it wrote anything, so a
raising write destroyed up to one flush interval of sealed bars. The aggregator has
already sealed those minutes and will not seal them again, so nothing could put them back.

The seam is `polars.DataFrame.write_parquet` itself -- the exact call the issue names, and
the one a full disk, a held file handle or a permission change actually raises from.
Writes before the chosen one land for real, so a partial flush is a partial flush and not
a simulation of one.

The second half of this file is #107, and the same seam answers it: what the *writer*
does about a failure it has rolled back. #101 left the two compositions with one flush
implementation and two error handlers, and the two then disagreed -- the monolith counted
and said nothing while the split logged and alerted. Those tests are parametrised over the
composition so the two cannot quietly part company again.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from deltapayoff import log_events
from deltapayoff.store import DATASET, BarStore, BarWriter
from test_store import bar

#: The message the fake failure carries, so a test can prove *which* error propagated.
DISK_FULL = "no space left on device"


def raising_write(monkeypatch, *, fail_on: int) -> list[Path]:
    """Let every `write_parquet` before `fail_on` land, then raise on that one.

    Returns the list of paths written to, which grows as the flush runs, so a test can
    assert how far the flush got before it failed rather than assuming.
    """
    real = pl.DataFrame.write_parquet
    calls: list[Path] = []

    def write_parquet(self, file, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(Path(file))
        if len(calls) == fail_on:
            raise OSError(DISK_FULL)
        return real(self, file, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", write_parquet)
    return calls


def eth_bar():
    """A second bar in a different partition, so one flush writes two files."""
    return bar(symbol="C-ETH-3000-040926", underlying="ETH", strike=3000.0)


def test_a_failed_flush_leaves_the_bar_in_the_buffer(tmp_path: Path, monkeypatch) -> None:
    """The criterion the ticket is named for. A sealed bar that did not reach disk is
    still owed to disk, and the buffer is the only place it exists."""
    store = BarStore(tmp_path)
    store.add([bar()])
    raising_write(monkeypatch, fail_on=1)

    with pytest.raises(OSError, match=DISK_FULL):
        store.flush()

    assert store.buffered == 1, "the sealed bar was destroyed by a failed flush"
    assert store.rows_written == 0, "a row that never reached disk was counted"
    assert list(tmp_path.rglob("*.parquet")) == []


def test_a_partial_flush_puts_the_written_group_back_too(
    tmp_path: Path, monkeypatch
) -> None:
    """Two partitions, the second write raises. The whole flush is rolled back -- the
    first group's file is deleted and its bars stay buffered -- which is what
    `_flush_generation` already does and the only answer that keeps `rows_written` and
    the directory agreeing with each other."""
    store = BarStore(tmp_path)
    store.add([bar(), eth_bar()])
    calls = raising_write(monkeypatch, fail_on=2)

    with pytest.raises(OSError, match=DISK_FULL):
        store.flush()

    assert len(calls) == 2, "the first group was never attempted; this is not a partial"
    assert store.buffered == 2, "a partial flush kept some bars out of the buffer"
    assert store.rows_written == 0, "rows_written counted a group that is not on disk"
    assert list(tmp_path.rglob("*.parquet")) == [], "the rolled-back file is still there"
    assert list(tmp_path.rglob("*.flushing")) == [], "a staging file was left behind"


def test_a_failed_flush_does_not_burn_a_file_ordinal(
    tmp_path: Path, monkeypatch
) -> None:
    """`self.flushes` is in the legacy file name, and a directory listing is meant to tell
    a reader nothing is missing. A failed flush that wrote nothing must not leave a gap in
    the ordinals for a reader to misread as a lost file."""
    store = BarStore(tmp_path)
    store.add([bar()])
    raising_write(monkeypatch, fail_on=1)

    with pytest.raises(OSError, match=DISK_FULL):
        store.flush()
    assert store.flushes == 0, "a flush that wrote nothing consumed a file ordinal"

    monkeypatch.undo()
    assert store.flush() == 1

    names = sorted(path.name for path in tmp_path.rglob("*.parquet"))
    assert names == ["20260904T090000Z-000001.parquet"]


def test_the_flush_after_a_failure_writes_every_bar_exactly_once(
    tmp_path: Path, monkeypatch
) -> None:
    """The point of keeping the buffer: the next interval's flush actually recovers the
    bars, and recovers each of them once. A duplicated bar is as wrong as a lost one."""
    store = BarStore(tmp_path)
    store.add([bar(), eth_bar()])
    raising_write(monkeypatch, fail_on=2)

    with pytest.raises(OSError, match=DISK_FULL):
        store.flush()
    monkeypatch.undo()

    assert store.flush() == 2
    assert store.rows_written == 2
    assert store.scan().collect().height == 2, "a bar was lost or written twice"
    assert len(list(tmp_path.rglob("*.parquet"))) == 2, "one file per partition"


@pytest.mark.parametrize("generation", [None, 7])
def test_both_flush_paths_answer_a_raising_write_the_same_way(
    tmp_path: Path, monkeypatch, generation
) -> None:
    """`flush()` is the monolith's path and `flush(generation=...)` the split's. #101
    exists because the two drifted; this asserts they cannot drift again without a test
    going red."""
    store = BarStore(tmp_path)
    store.add([bar()])
    raising_write(monkeypatch, fail_on=1)

    with pytest.raises(OSError, match=DISK_FULL):
        store.flush(generation=generation)

    assert store.buffered == 1
    assert store.flushes == 0
    assert store.rows_written == 0
    assert list(tmp_path.rglob("*.parquet")) == []


def test_a_rollback_that_cannot_delete_still_raises_the_write_error(
    tmp_path: Path, monkeypatch
) -> None:
    """On Windows a reader or a virus scanner holding a handle makes `unlink` raise, and
    that is one of the very conditions that makes the write fail in the first place. The
    caller must still be told what actually went wrong, and the buffer must still survive:
    a rollback that raises its own error hides the diagnosis and the flush loop learns
    nothing."""
    store = BarStore(tmp_path)
    store.add([bar(), eth_bar()])
    raising_write(monkeypatch, fail_on=2)
    monkeypatch.setattr(
        Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(
            PermissionError("the file is in use by another process")
        )
    )

    with pytest.raises(OSError, match=DISK_FULL):
        store.flush()

    assert store.buffered == 2
    assert store.rows_written == 0


# --- What the writer says about it -------------------------------------------------
#
# The tests above are about the bars. These are about the record: #107 found that a
# flush failing in the monolith incremented `flush_errors` and told nobody -- no log at
# any layer and no `alert` -- while the split logged at error and published
# `store.flush_failed` for the same event. They are parametrised over the composition on
# purpose, because two copies agreeing by inspection is exactly how the two drifted.


#: A fixed clock reading. The commit path stamps a checkpoint and an alert from it, and
#: a test that read the wall clock for either would behave differently in November.
CLOCK = datetime(2026, 9, 12, 10, 2, tzinfo=timezone.utc).timestamp()


def failing_writer(
    root: Path, monkeypatch, *, split: bool
) -> tuple[BarWriter, list[Any]]:
    """A writer holding one sealed bar, whose next flush raises on the first write.

    `split` selects the composition the way production does: a `checkpoint_root` is what
    `store_main.py` passes and `main.py` deliberately does not. Both are given a
    `publish`, so these tests are about the code path rather than about how either
    process happens to be wired -- see `test_store.py` for the wiring.

    The bar is handed to the store directly. Going through `_hand_to_store` would also
    publish an `OptionBar` per sealed bar and the alert would have to be picked out of
    them; this way anything in `published` is the flush's own doing.
    """
    published: list[Any] = []
    writer = BarWriter(
        BarStore(root),
        clock=lambda: CLOCK,
        checkpoint_root=root if split else None,
        publish=published.append,
    )
    writer.store.add([bar()])
    raising_write(monkeypatch, fail_on=1)
    return writer, published


def flush_failures(caplog) -> list[logging.LogRecord]:
    """Every error-level engine record. One failed flush must leave exactly one."""
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and getattr(record, "event", None) == log_events.ENGINE_ERROR
    ]


def alerts(published: list[Any]) -> list[Any]:
    return [
        event
        for event in published
        if getattr(event, "code", None) == "store.flush_failed"
    ]


@pytest.mark.parametrize("split", [False, True], ids=["monolith", "split"])
def test_both_compositions_count_log_and_alert_one_failed_flush(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture, split: bool
) -> None:
    """The whole of #107 in one assertion set, driven through the flush loop itself.

    Against the code before #107 the monolith parametrisation fails on the log record --
    `_maybe_flush` incremented the counter and did nothing else -- and the split
    parametrisation passes, which is the asymmetry the ticket is about.

    `flush_errors == 1` is the other half. The obvious wrong fix is to move the counting
    and the logging up into `_maybe_flush` for both paths, which double-counts the split,
    where `_commit` has always counted its own failure before re-raising.
    """
    caplog.set_level(logging.INFO, logger="deltapayoff.store")
    writer, published = failing_writer(tmp_path, monkeypatch, split=split)

    asyncio.run(writer._maybe_flush())

    assert writer.flush_errors == 1, "a failed flush was not counted exactly once"
    records = flush_failures(caplog)
    assert len(records) == 1, [record.getMessage() for record in caplog.records]
    assert records[0].exc_info is not None, "the failure was logged without exc_info"
    assert str(records[0].exc_info[1]) == DISK_FULL, "a different error was logged"
    failures = alerts(published)
    assert len(failures) == 1, published
    assert failures[0].severity == "error"
    assert failures[0].source == "store"
    assert DISK_FULL in failures[0].detail, "the alert does not name what went wrong"
    assert writer.buffered_rows == 1, "the bar was not kept for the next interval"


def test_a_failed_monolith_flush_names_the_table_it_could_not_write(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The monolith flushes four tables independently, so which one failed is the one
    thing an operator cannot work out from anything else. The split names a generation
    instead, because a commit is one transaction over all four."""
    caplog.set_level(logging.INFO, logger="deltapayoff.store")
    writer, _published = failing_writer(tmp_path, monkeypatch, split=False)

    asyncio.run(writer._maybe_flush())

    record = flush_failures(caplog)[0]
    assert record.table == DATASET
    assert DATASET in record.getMessage()


def test_the_record_belongs_to_the_flush_and_not_to_the_interval_loop(
    tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`set_recording(False)` and `aclose` flush through `_flush_all` without going near
    `_maybe_flush`, and a failure there is exactly as worth knowing about. Counting in
    the loop meant those two said nothing at all; counting in the flush means every
    caller is told once, and `_maybe_flush` can go back to its own job of not dying."""
    caplog.set_level(logging.INFO, logger="deltapayoff.store")
    writer, published = failing_writer(tmp_path, monkeypatch, split=False)

    with pytest.raises(OSError, match=DISK_FULL):
        writer._flush_all()

    assert writer.flush_errors == 1
    assert len(flush_failures(caplog)) == 1
    assert len(alerts(published)) == 1
