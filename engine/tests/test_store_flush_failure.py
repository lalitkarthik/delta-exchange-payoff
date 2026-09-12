"""What a store flush does when `write_parquet` raises.

Every other test in `test_store.py` drives the happy path of `write_parquet`, which is why
#101's defect survived: `_flush_legacy` emptied its buffer before it wrote anything, so a
raising write destroyed up to one flush interval of sealed bars. The aggregator has
already sealed those minutes and will not seal them again, so nothing could put them back.

The seam is `polars.DataFrame.write_parquet` itself -- the exact call the issue names, and
the one a full disk, a held file handle or a permission change actually raises from.
Writes before the chosen one land for real, so a partial flush is a partial flush and not
a simulation of one.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from deltapayoff.store import BarStore
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
