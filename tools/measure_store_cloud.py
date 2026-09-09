"""R3: the store measured as a *cloud bill* rather than as a footprint.

`tools/measure_store.py` answers "how big is the store and how fast does it compress" —
bytes, rows and bytes per row. Every one of R3's (#67) five criteria turns on numbers it
does not report, because they are properties of the **object layout**, not of the data:

**How many objects, and how big is each one.** S3 charges per `PUT` and per `GET`, and two
of its cost lines have a **128 KB cliff** — Intelligent-Tiering does not monitor or tier
an object below it, and Standard-IA bills a 128 KB object minimum. A store of 288 files a
table a day sits on both sides of that line depending on the table, so the size
*distribution* decides the storage class, not the total.

**What one day of one partition costs to read.** The compaction job, a backtest and the
dashboard's historical routes are three different reads of the same tree, and on object
storage each one is a different number of `GET`s. This times all three against the live
`data/`, uncompacted, at the five-minute cadence the engine actually writes.

**What compaction rewrites.** `BarStore.compact_partition` reads every input, writes a
tmp, **reads the tmp back in full** to verify, writes a manifest, deletes the inputs and
publishes. On a local disk that is one extra pass over a warm file. On S3 it is a `GET`
per input, a `GET` for the verify, and `PUT`s for the tmp, the manifest and the publish —
a request bill that this reports rather than estimates.

Read-only. It never writes into the store root and never deletes anything; the compaction
figures are counted from a listing, not produced by running a compaction.

    python tools/measure_store_cloud.py
    python tools/measure_store_cloud.py --date 2026-09-08 --underlying BTC
    python tools/measure_store_cloud.py --root /some/data --runs 3
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.store import all_stores, default_root

#: The size below which S3 Intelligent-Tiering does not monitor an object and
#: Standard-IA bills a minimum. Both thresholds are 128 KB and both are decisive for a
#: store whose flush files straddle them.
CLIFF_BYTES = 128 * 1024

#: One month, for turning a per-day request count into a bill.
DAYS_PER_MONTH = 30.0


def _sizes(store) -> list[int]:
    return [path.stat().st_size for path in store.path.rglob("*.parquet")]


def report_objects(root: Path) -> dict[str, dict[str, float]]:
    """Per table: objects, bytes, and how the sizes sit against the 128 KB cliff."""
    print(f"\nOBJECTS  {root}   (measured)")
    print(
        f"  {'table':16} {'objects':>8} {'bytes':>13} {'min':>9} {'median':>9} "
        f"{'mean':>9} {'max':>10} {'<128KB':>7}"
    )
    facts: dict[str, dict[str, float]] = {}
    for store in all_stores(root):
        sizes = sorted(_sizes(store))
        if not sizes:
            continue
        small = sum(1 for size in sizes if size < CLIFF_BYTES)
        facts[store.dataset] = {
            "objects": len(sizes),
            "bytes": sum(sizes),
            "median": statistics.median(sizes),
            "mean": sum(sizes) / len(sizes),
            "under_cliff": small,
        }
        print(
            f"  {store.dataset:16} {len(sizes):8,} {sum(sizes):13,} {sizes[0]:9,} "
            f"{statistics.median(sizes):9,.0f} {sum(sizes) / len(sizes):9,.0f} "
            f"{sizes[-1]:10,} {small / len(sizes) * 100:6.1f}%"
        )
    return facts


def report_partitions(root: Path, day: str | None) -> None:
    """Objects per partition per table — the `PUT` count a day of flushing produces."""
    print("\nOBJECTS PER PARTITION  (measured; one object per flush per table per "
          "underlying)")
    print(f"  {'table':16} {'date':12} {'und':5} {'objects':>8} {'bytes':>13} "
          f"{'B/object':>10}")
    for store in all_stores(root):
        for date, underlying in store.partitions():
            if day is not None and date != day:
                continue
            directory = store.path / f"date={date}" / f"underlying={underlying}"
            files = list(directory.glob("*.parquet"))
            if not files:
                continue
            total = sum(path.stat().st_size for path in files)
            print(
                f"  {store.dataset:16} {date:12} {underlying:5} {len(files):8,} "
                f"{total:13,} {total / len(files):10,.0f}"
            )


def report_compaction_cost(root: Path, day: str) -> None:
    """What compacting one day would move, counted from the listing. Nothing is run.

    The request arithmetic follows `BarStore.compact_partition` exactly: one `LIST` per
    partition, one `GET` per input file, one more `GET` for the full read-back that
    verifies the output before any input is deleted, and three Tier-1 writes — the tmp,
    the manifest, and the `os.replace` that publishes (a `COPY` on object storage).
    Deletes are free on S3 and are counted anyway, because they are the only irreversible
    step in the job.
    """
    print(f"\nCOMPACTION, IF RUN ON {day}  (derived from a listing; nothing was run)")
    print(f"  {'table':16} {'und':5} {'inputs':>7} {'bytes read':>13} "
          f"{'GET':>6} {'PUT':>5} {'DELETE':>7}")
    inputs = gets = puts = deletes = 0
    read_bytes = 0
    for store in all_stores(root):
        for date, underlying in store.partitions():
            if date != day:
                continue
            directory = store.path / f"date={date}" / f"underlying={underlying}"
            files = sorted(directory.glob("*.parquet"))
            if len(files) <= 1:
                continue
            size = sum(path.stat().st_size for path in files)
            # inputs read once, then the written output read back in full to verify.
            partition_gets = len(files) + 1
            partition_puts = 3  # tmp, manifest, publish (COPY)
            inputs += len(files)
            read_bytes += size
            gets += partition_gets
            puts += partition_puts
            deletes += len(files)
            print(
                f"  {store.dataset:16} {underlying:5} {len(files):7,} {size:13,} "
                f"{partition_gets:6,} {partition_puts:5} {len(files):7,}"
            )
    print(
        f"  {'ALL FOUR':16} {'':5} {inputs:7,} {read_bytes:13,} "
        f"{gets:6,} {puts:5} {deletes:7,}"
    )


def timed(call, runs: int) -> tuple[float, float]:
    """Minimum and median wall clock over `runs`, in milliseconds.

    The **minimum** is reported beside the median because this machine is running the
    live engine on :8000 while the measurement takes place; the minimum is the closest
    thing to the cost with no other process in the way.
    """
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return min(samples), statistics.median(samples)


def report_reads(root: Path, day: str, underlying: str, runs: int) -> None:
    """The three reads that decide criterion 5, against the live uncompacted layout."""
    stores = {store.dataset: store for store in all_stores(root)}
    target = pl.date(*(int(part) for part in day.split("-")))
    clause = (pl.col("date") == target) & (pl.col("underlying") == underlying)

    print(f"\nREAD TIME  {day} {underlying}, uncompacted, warm cache, "
          f"min/median of {runs} (measured)")
    print(f"  {'read':46} {'rows':>10} {'min ms':>9} {'median ms':>10}")

    def whole_day_one_table(dataset: str) -> int:
        return stores[dataset].scan().filter(clause).collect().height

    def whole_day_four_tables() -> int:
        frames = [stores[name].scan().filter(clause) for name in stores]
        return sum(frame.height for frame in pl.collect_all(frames))

    for dataset in stores:
        rows, (low, mid) = None, (0.0, 0.0)
        rows = whole_day_one_table(dataset)
        low, mid = timed(lambda dataset=dataset: whole_day_one_table(dataset), runs)
        print(f"  {'whole day, ' + dataset:46} {rows:10,} {low:9.1f} {mid:10.1f}")

    rows = whole_day_four_tables()
    low, mid = timed(whole_day_four_tables, runs)
    print(f"  {'whole day, all four (the backtest / compaction read)':46} "
          f"{rows:10,} {low:9.1f} {mid:10.1f}")

    # The dashboard's historical ladder: one minute across four tables.
    minute = (
        stores["quote-bars"].scan().filter(clause).select("minute").collect()
    )
    if minute.height:
        one = minute.get_column("minute").sort()[minute.height // 2]

        def ladder() -> int:
            frames = [
                stores[name].scan().filter(clause & (pl.col("minute") == one))
                for name in stores
            ]
            return sum(frame.height for frame in pl.collect_all(frames))

        rows = ladder()
        low, mid = timed(ladder, runs)
        print(f"  {'one minute, all four (/chain/at)':46} {rows:10,} "
              f"{low:9.1f} {mid:10.1f}")

    # One contract's whole day across two tables: the `/bars` candle chart.
    symbols = (
        stores["quote-bars"]
        .scan()
        .filter(clause)
        .select("symbol")
        .unique()
        .collect()
        .get_column("symbol")
    )
    if len(symbols):
        symbol = sorted(str(value) for value in symbols)[len(symbols) // 2]

        def contract_day() -> int:
            frames = [
                stores[name].scan().filter(clause & (pl.col("symbol") == symbol))
                for name in ("quote-bars", "reference-bars")
            ]
            return sum(frame.height for frame in pl.collect_all(frames))

        rows = contract_day()
        low, mid = timed(contract_day, runs)
        print(f"  {'one contract, whole day, two tables (/bars)':46} {rows:10,} "
              f"{low:9.1f} {mid:10.1f}")


def report_requests(facts: dict[str, dict[str, float]], flushes: int) -> None:
    """Writes per day, per month and per year, from the flush cadence rather than a
    listing — because a listing of a partly recorded store understates a full day."""
    tables = len(facts)
    underlyings = 2
    per_day = flushes * tables * underlyings
    print(f"\nWRITE REQUESTS  (derived: {flushes} flushes x {tables} tables x "
          f"{underlyings} underlyings)")
    print(f"  per day    {per_day:12,}")
    print(f"  per month  {per_day * DAYS_PER_MONTH:12,.0f}")
    print(f"  per year   {per_day * 365:12,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--date", default=None, help="partition date, YYYY-MM-DD")
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--flushes", type=int, default=288)
    args = parser.parse_args()

    root = Path(args.root) if args.root else default_root()
    facts = report_objects(root)

    day = args.date
    if day is None:
        # The most complete closed day: the last partition strictly before today, which
        # is the one a nightly compaction would take and the only one that is a whole day.
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        dates = sorted(
            {date for store in all_stores(root) for date, _ in store.partitions()}
        )
        closed = [date for date in dates if date < today]
        day = yesterday if yesterday in closed else (closed[-1] if closed else today)
    print(f"\n(closed day chosen: {day})")

    report_partitions(root, day)
    report_compaction_cost(root, day)
    report_reads(root, day, args.underlying, args.runs)
    report_requests(facts, args.flushes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
