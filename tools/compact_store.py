"""T5.4: the nightly job. A day's hourly fragments into one file per table per partition.

Hourly flushing bounds crash loss at sixty minutes, which is why #5 chose it, and it pays
for that in files: 24 per table per partition per day, roughly 26,000 a year across the
four tables. Parquet is bad at that - every file carries its own header, footer,
dictionary pages and row-group statistics, and a reader has to open every one of them
before it can decide it wants none. This takes a closed day to one file per table per
partition, and the year to about a thousand.

The safety is all in `store.BarStore.compact_partition`, which verifies the output by
reading it back off the disk before a single input is deleted, and recovers by manifest
if it is interrupted. This file is the entry point and nothing else: a scheduler is the
operator's choice, and a plain command is the thing cron, a test and a person at a prompt
can all call.

    python tools/compact_store.py                 # every table, every closed day
    python tools/compact_store.py --before 2026-09-04
    python tools/compact_store.py --root /some/other/data
    python tools/compact_store.py --dry-run       # say what it would do

**Today's partition is skipped by default** and that is not tidiness: `flush` writes
straight to its final name, so the open day can be a half-written file at any instant.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.store import all_stores, default_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="store root (default: <repo>/data)")
    parser.add_argument(
        "--before",
        default=None,
        help="compact partitions strictly before this YYYY-MM-DD (default: today, UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the partitions that would be compacted and touch nothing",
    )
    args = parser.parse_args()

    root = Path(args.root) if args.root else default_root()
    # The same cutoff `BarStore.compact` applies by default, computed here so `--dry-run`
    # reports exactly what a real run would do. Without it this loop would reach past the
    # cutoff and compact the partition the writer is still flushing into.
    cutoff = args.before or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"store root {root}, compacting partitions before {cutoff}")

    total_before = total_after = total_rows = 0
    files_before = files_after = 0
    for store in all_stores(root):
        for day, underlying in store.partitions():
            if not day < cutoff:
                continue
            if args.dry_run:
                directory = store.path / f"date={day}" / f"underlying={underlying}"
                count = len(list(directory.glob("*.parquet")))
                verb = "would compact" if count > 1 else "already one file"
                print(
                    f"  {store.dataset:16} {day} {underlying:4} {count:4} files  {verb}"
                )
                continue
            result = store.compact_partition(day, underlying)
            if not result.compacted:
                print(
                    f"  {store.dataset:16} {day} {underlying:4} "
                    f"{result.files_before:4} files  no-op"
                )
                continue
            total_before += result.bytes_before
            total_after += result.bytes_after
            total_rows += result.rows
            files_before += result.files_before
            files_after += result.files_after
            print(
                f"  {store.dataset:16} {day} {underlying:4} "
                f"{result.files_before:4} -> {result.files_after} files  "
                f"{result.rows:9,} rows  "
                f"{result.bytes_before / 1024:10.1f} -> "
                f"{result.bytes_after / 1024:9.1f} KiB"
            )

    if not args.dry_run and files_before:
        saved = total_before - total_after
        print(
            f"\n{files_before} files -> {files_after}, {total_rows:,} rows, "
            f"{total_before / 1024 / 1024:.2f} -> {total_after / 1024 / 1024:.2f} MiB "
            f"({saved / total_before * 100:.1f}% smaller)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
