"""#23: how many minutes have quotes but no volatility of ours. The two-line query.

`computed-bars` is the only table that is **sampled** rather than folded from arrivals,
so it is the only one that can go missing while the feed is fine. Comparing its minutes
against `quote-bars`' minutes for one expiry is what exposed the loss #23 fixes -
`measured` on the live store on 2026-09-04 for expiry 25-09-2026, 217 of 904 minutes had
quote bars and no computed bar, 24%, and **every gap was exactly one minute long**:
`run lengths: [(1, 217)]`. Nothing else on the screen or in the logs said so.

Three kinds of absence, and the point of the probe is to keep them apart:

**no quote bar at all** - the engine was down. Not a sampling loss, and it is the block
of 62 minutes that the run above found alongside the scatter.

**quote bar, no computed bar** - the sampling loss. Single-minute runs mean the boundary
race; long runs would mean the recompute loop itself stalled, which is a different bug.

**neither, outside the observed span** - not counted at all. The grid is the first to the
last minute this expiry was actually seen in, so a day that started late is not reported
as loss.

Read-only. It opens the running engine's own `data/` by default and never writes there.
Re-run it after #23's sampling change has had a day: the fix narrows the window from one
instant to ten seconds, and what is left is a number to measure, not to predict.

    python tools/measure_computed_gaps.py
    python tools/measure_computed_gaps.py --expiry 25-09-2026 --date 2026-09-04
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.store import (  # noqa: E402
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    DATASET,
    SCHEMA,
    BarStore,
    default_root,
)

MINUTE = timedelta(minutes=1)


#: Only the two tables this compares, with the schema each empty scan should report.
TABLES = {DATASET: SCHEMA, COMPUTED_DATASET: COMPUTED_SCHEMA}


def minutes(root: Path, dataset: str, expiry: str, day: str | None) -> set:
    """The distinct minutes one table holds for one expiry. Empty if the table is."""
    frame = BarStore(root, dataset=dataset, schema=TABLES[dataset]).scan()
    frame = frame.filter(pl.col("expiry") == expiry)
    if day is not None:
        frame = frame.filter(pl.col("date").cast(pl.Utf8) == day)
    collected = frame.select("minute").unique().collect()
    return set(collected["minute"].to_list()) if collected.height else set()


def runs(missing: set) -> list[tuple[int, int]]:
    """Gap lengths and how many of each, longest-first. One-minute runs are the boundary
    race; anything longer is the recompute loop having stopped, which is another bug."""
    lengths: Counter[int] = Counter()
    length = 0
    for stamp in sorted(missing):
        if stamp - MINUTE in missing:
            length += 1
        else:
            if length:
                lengths[length] += 1
            length = 1
    if length:
        lengths[length] += 1
    return sorted(lengths.items(), reverse=True)


def report(root: Path, expiry: str, day: str | None) -> None:
    quoted = minutes(root, DATASET, expiry, day)
    computed = minutes(root, COMPUTED_DATASET, expiry, day)
    observed = quoted | computed
    if not observed:
        print(f"{expiry}: no bars in {root}")
        return

    grid = set()
    stamp, last = min(observed), max(observed)
    while stamp <= last:
        grid.add(stamp)
        stamp += MINUTE

    absent = sorted(grid - quoted)
    lost = (grid & quoted) - computed
    rate = 100.0 * len(lost) / len(quoted) if quoted else 0.0

    print(f"\n{expiry}  {min(observed):%H:%M} - {max(observed):%H:%M}  (measured)")
    print(f"  minutes in the observed span      {len(grid):6d}")
    print(f"  no quote bar at all               {len(absent):6d}   engine down")
    print(f"  quote bar, no computed bar        {len(lost):6d}   {rate:5.1f}%")
    print(f"  gap run lengths (length, count)   {runs(lost)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument(
        "--expiry", help="DD-MM-YYYY, as the store spells it. All if omitted."
    )
    parser.add_argument("--date", help="YYYY-MM-DD partition. All if omitted.")
    args = parser.parse_args()

    expiries = [args.expiry]
    if args.expiry is None:
        store = BarStore(args.root, dataset=DATASET, schema=SCHEMA)
        found = store.scan().select("expiry").unique()
        expiries = sorted(found.collect()["expiry"].to_list())
    for expiry in expiries:
        report(args.root, expiry, args.date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
