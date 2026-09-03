"""T5.4: measure the store, because #5's footprint and compression claims are arithmetic.

#5 asserts **50-100 MB/day compressed** against ~52 GB/day of raw JSON, roughly a
**500-1000x** reduction. Neither number was ever weighed. This project's `measured` /
`derived` / `assumed` convention exists because estimates like those have been wrong four
times already - the original storage ticket's own row estimate was 7x too small - so this
tool replaces them with figures that came off a disk and a socket.

Four phases, and each says plainly what its data was:

**raw** - the denominator. Opens its own socket, subscribes **both** channels over every
listed BTC option (exactly what `main.py` subscribes) and measures bytes and messages off
the wire for a fixed window. `measured`. The denominator has to be taken this way rather
than borrowed: `tools/measure_feed.py`'s 636.5 KB/s figure subscribes `ob_l2` over one
chain only, which is not what the engine stores.

**store** - the numerator. Walks a real store root and reports, per table, files, bytes,
rows, bytes per row, and the minutes actually covered. `measured`. Points at the running
engine's own `data/` by default, and never writes there.

**day** - a full day's file layout. A day of live data takes a day, so the measured hour
is time-shifted into 24 hourly fragments in a scratch directory and compacted. The
**values, the widths and the cardinalities are real**; the **row counts per hour are one
real hour repeated**. Labelled `synthetic-layout` throughout and never reported as live.

**read** - read time with and without partition pruning, over a multi-date store built
the same way. A negative result here is a good result provided it is measured.

    python tools/measure_store.py --raw-seconds 60
    python tools/measure_store.py --skip-raw --root /some/data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import tempfile
import time
import urllib.request
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.fanout import FanOut  # noqa: E402
from deltapayoff.feed import DeltaFeed  # noqa: E402
from deltapayoff.store import BarStore, BarWriter, all_stores, default_root  # noqa: E402
from deltapayoff.stream import ChainStream, recompute_forever  # noqa: E402

REST = "https://api.india.delta.exchange"

#: #5's ceiling: every listed contract quoting in every minute of the day.
CEILING_PER_CONTRACT_TABLE = 846_720
CEILING_SPOT = 1_440
CEILING_TOTAL = 3 * CEILING_PER_CONTRACT_TABLE + CEILING_SPOT

MINUTES_PER_DAY = 1_440
SECONDS_PER_DAY = 86_400


def listed_symbols() -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


async def measure_raw(seconds: float) -> dict[str, float]:
    """Bytes and messages off the wire, both channels, every listed BTC option.

    This is the compression denominator and it is deliberately taken against the engine's
    *own* subscription rather than borrowed from an older run over a narrower one.
    """
    symbols = listed_symbols()
    bus = FanOut()
    bus.subscribe("measure", maxsize=1_000_000, lossless=True)
    feed = DeltaFeed(bus)
    feed.subscribe("ticker", symbols)
    feed.subscribe("ob_l2", symbols)

    task = asyncio.create_task(feed.run())
    await asyncio.sleep(3.0)  # connect and subscribe before the clock starts
    start_messages, start_bytes = feed.messages, feed.bytes_read
    started = time.perf_counter()
    await asyncio.sleep(seconds)
    elapsed = time.perf_counter() - started
    messages = feed.messages - start_messages
    read = feed.bytes_read - start_bytes

    feed.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    return {
        "symbols": float(len(symbols)),
        "seconds": elapsed,
        "messages": float(messages),
        "bytes": float(read),
        "msg_per_second": messages / elapsed,
        "bytes_per_second": read / elapsed,
        "bytes_per_day": read / elapsed * SECONDS_PER_DAY,
    }


async def capture(root: Path, seconds: float, flush_seconds: float) -> dict[str, float]:
    """Run the engine's whole pipeline into a scratch root, live, for `seconds`.

    **Why this exists beside the running engine.** The engine flushes hourly, so its
    first file lands an hour after it starts; this stands up the same four aggregators,
    the same lossless subscription and the same recompute loop with a short flush
    interval, so a measurable store exists in minutes. It is the same code path - the
    only thing changed is `flush_seconds`, which decides how often the buffer is written
    and nothing about what is in it.

    Raw bytes are counted over the same window, so this run's own compression ratio has
    a numerator and a denominator taken from **one** socket over **one** interval rather
    than from two runs at two times of day.
    """
    if root.exists():
        shutil.rmtree(root)
    symbols = listed_symbols()

    bus = FanOut()
    stream = ChainStream()
    stream.attach(bus)
    writer = BarWriter(
        BarStore(root),
        chains=stream.computed_chains,
        flush_seconds=flush_seconds,
    )
    writer.attach(bus)
    feed = DeltaFeed(bus)
    feed.subscribe("ticker", symbols)
    feed.subscribe("ob_l2", symbols)

    tasks = [
        asyncio.create_task(feed.run(), name="feed"),
        asyncio.create_task(stream.run(), name="stream"),
        asyncio.create_task(recompute_forever(stream), name="recompute"),
        asyncio.create_task(writer.run(), name="writer"),
    ]
    await asyncio.sleep(3.0)  # connect and subscribe before the clock starts
    start_messages, start_bytes = feed.messages, feed.bytes_read
    started = time.perf_counter()
    await asyncio.sleep(seconds)
    elapsed = time.perf_counter() - started
    messages = feed.messages - start_messages
    read = feed.bytes_read - start_bytes

    feed.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # The open minute is a real observation; the lifespan writes it the same way.
    await writer.aclose()

    return {
        "seconds": elapsed,
        "symbols": float(len(symbols)),
        "messages": float(messages),
        "bytes": float(read),
        "msg_per_second": messages / elapsed,
        "bytes_per_second": read / elapsed,
        "bytes_per_day": read / elapsed * SECONDS_PER_DAY,
        "skipped": float(writer.skipped),
        "flush_errors": float(writer.flush_errors),
    }


def table_facts(store) -> dict[str, object] | None:
    """One table's files, bytes, rows and minutes. Reads the files, not a manifest."""
    files = sorted(store.path.rglob("*.parquet"))
    if not files:
        return None
    frame = store.scan().collect()
    if frame.height == 0:
        return None
    minutes = frame.get_column("minute")
    symbols = (
        frame.get_column("symbol").n_unique() if "symbol" in frame.columns else 1
    )
    return {
        "dataset": store.dataset,
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "rows": frame.height,
        "minutes": minutes.n_unique(),
        "symbols": symbols,
        "first": minutes.min(),
        "last": minutes.max(),
        "partitions": len(store.partitions()),
    }


def report_store(root: Path) -> list[dict[str, object]]:
    print(f"\nSTORE  {root}   (measured, live)")
    print(
        f"  {'table':16} {'files':>6} {'rows':>10} {'bytes':>12} "
        f"{'B/row':>8} {'minutes':>8}  window"
    )
    facts = []
    for store in all_stores(root):
        row = table_facts(store)
        if row is None:
            print(f"  {store.dataset:16} {'-':>6} {'empty':>10}")
            continue
        facts.append(row)
        print(
            f"  {row['dataset']:16} {row['files']:6} {row['rows']:10,} "
            f"{row['bytes']:12,} {row['bytes'] / row['rows']:8.2f} "
            f"{row['minutes']:8}  {row['first']} .. {row['last']}"
        )
    if facts:
        total_rows = sum(int(row["rows"]) for row in facts)
        total_bytes = sum(int(row["bytes"]) for row in facts)
        print(
            f"  {'ALL FOUR':16} {sum(int(r['files']) for r in facts):6} "
            f"{total_rows:10,} {total_bytes:12,} {total_bytes / total_rows:8.2f}"
        )
    return facts


def synthesise_day(source: Path, work: Path, *, hours: int = 24) -> Path:
    """One measured window, time-shifted into `hours` hourly fragments.

    **Synthetic layout, real values.** Every number in these files came off the wire; what
    is invented is only *how many hours of them there are*, and the file layout that
    implies. It is labelled `synthetic-layout` everywhere it is reported, because a figure
    presented as live when it was not is the exact failure the tagging convention exists
    to prevent.
    """
    if work.exists():
        shutil.rmtree(work)
    for store in all_stores(source):
        target = work / store.dataset
        files = sorted(store.path.rglob("*.parquet"))
        if not files:
            continue
        for day_dir in sorted(store.path.glob("date=*")):
            for underlying_dir in sorted(day_dir.glob("underlying=*")):
                sources = sorted(underlying_dir.glob("*.parquet"))
                if not sources:
                    continue
                frame = pl.read_parquet(sources)
                out = target / day_dir.name / underlying_dir.name
                out.mkdir(parents=True, exist_ok=True)
                for hour in range(hours):
                    shifted = frame.with_columns(
                        pl.col("minute") + timedelta(hours=hour)
                    )
                    shifted.write_parquet(out / f"hour-{hour:02d}.parquet")
    return work


def report_compaction(work: Path) -> None:
    print("\nCOMPACTION  (synthetic-layout: one measured window shifted into 24 hours)")
    print(
        f"  {'table':16} {'files':>12} {'rows':>10} "
        f"{'before KiB':>12} {'after KiB':>11} {'saved':>7}"
    )
    grand_before = grand_after = grand_rows = 0
    for store in all_stores(work):
        for day, underlying in store.partitions():
            started = time.perf_counter()
            result = store.compact_partition(day, underlying)
            elapsed = time.perf_counter() - started
            if not result.compacted:
                continue
            grand_before += result.bytes_before
            grand_after += result.bytes_after
            grand_rows += result.rows
            print(
                f"  {store.dataset:16} {result.files_before:5} -> "
                f"{result.files_after:<3} {result.rows:10,} "
                f"{result.bytes_before / 1024:12,.1f} {result.bytes_after / 1024:11,.1f} "
                f"{(1 - result.bytes_after / result.bytes_before) * 100:6.1f}%  "
                f"{elapsed:.2f}s"
            )
    if grand_before:
        print(
            f"  {'ALL FOUR':16} {'':5}    {grand_rows:10,} "
            f"{grand_before / 1024:12,.1f} {grand_after / 1024:11,.1f} "
            f"{(1 - grand_after / grand_before) * 100:6.1f}%"
        )
        print(
            f"  a synthetic day compacted:  "
            f"{grand_after / 1024 / 1024:.2f} MiB, {grand_rows:,} rows, "
            f"{grand_after / grand_rows:.2f} B/row"
        )


def fan_out_dates(
    work: Path, dates: int, underlyings: tuple[str, ...] = ("ETH",)
) -> None:
    """Copy the day back across `dates` dates, so pruning has something to prune.

    A second underlying too: a partition key with one value in it prunes nothing, and
    #5's key anticipates ETH even though ETH is not subscribed yet. The copies are the
    same rows under a different directory name - which is all a pruning measurement
    needs, since pruning is a decision taken on the *path*.
    """
    for store in all_stores(work):
        existing = store.partitions()
        if not existing:
            continue
        day, underlying = existing[0]
        source = store.path / f"date={day}" / f"underlying={underlying}"
        anchor = date_type(*(int(part) for part in day.split("-")))
        for offset in range(1, dates):
            new_day = (anchor - timedelta(days=offset)).isoformat()
            for name in (underlying, *underlyings):
                target = store.path / f"date={new_day}" / f"underlying={name}"
                target.mkdir(parents=True, exist_ok=True)
                for path in source.glob("*.parquet"):
                    shutil.copy2(path, target / path.name)
        for name in underlyings:
            target = store.path / f"date={day}" / f"underlying={name}"
            target.mkdir(parents=True, exist_ok=True)
            for path in source.glob("*.parquet"):
                shutil.copy2(path, target / path.name)


def timed(call, runs: int = 5) -> float:
    """Median wall clock over `runs`, in milliseconds. A median, not a mean: a single
    page-cache miss would otherwise decide the answer."""
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples)


def report_reads(work: Path, day: str, underlying: str) -> None:
    print("\nREAD TIME  (synthetic-layout store, median of 5, ms)")
    print(
        f"  {'table':16} {'parts':>5} {'full scan':>10} {'pruned':>10} "
        f"{'scan+filter':>12} {'speedup':>8}"
    )
    for store in all_stores(work):
        partitions = store.partitions()
        if len(partitions) < 2:
            continue
        target = pl.date(*(int(part) for part in day.split("-")))

        # Bound as defaults rather than closed over: these are built inside a loop and
        # a late-binding closure would time the last table five times.
        def full(store=store) -> int:
            return store.scan().collect().height

        def pruned(store=store, target=target) -> int:
            return (
                store.scan()
                .filter(pl.col("date") == target, pl.col("underlying") == underlying)
                .collect()
                .height
            )

        def eager_filter(store=store, target=target) -> int:
            return (
                store.scan()
                .collect()
                .filter(pl.col("date") == target, pl.col("underlying") == underlying)
                .height
            )

        rows_full, rows_pruned = full(), pruned()
        full_ms, pruned_ms, eager_ms = timed(full), timed(pruned), timed(eager_filter)
        print(
            f"  {store.dataset:16} {len(partitions):5} {full_ms:10.2f} "
            f"{pruned_ms:10.2f} {eager_ms:12.2f} {full_ms / pruned_ms:7.2f}x"
            f"   ({rows_full:,} -> {rows_pruned:,} rows)"
        )
    # Proof that the filter is answered by the path rather than by a scan of every row.
    store = all_stores(work)[0]
    plan = (
        store.scan()
        .filter(pl.col("underlying") == underlying)
        .explain(optimized=True)
    )
    print("\n  the pruned plan's file count, from Polars' own optimised plan:")
    for line in plan.splitlines():
        if "SCAN" in line or "FILE" in line or "hive" in line.lower():
            print(f"    {line.strip()}")


def report_projection(root: Path, raw: dict[str, float] | None) -> None:
    """Rows and bytes per day, against #5's ceiling and against the raw stream."""
    facts = [row for row in (table_facts(store) for store in all_stores(root)) if row]
    if not facts:
        return
    covered = max(int(row["minutes"]) for row in facts)
    print(f"\nPROJECTION  (derived from {covered} measured minutes)")
    print(
        f"  {'table':16} {'rows/min':>9} {'rows/day':>12} {'#5 ceiling':>11} "
        f"{'of #5':>7} {'live ceiling':>13} {'of live':>8} {'MB/day':>9}"
    )
    total_rows_day = total_bytes_day = 0.0
    total_ceiling = total_live_ceiling = 0
    for row in facts:
        per_minute = int(row["rows"]) / int(row["minutes"])
        per_day = per_minute * MINUTES_PER_DAY
        bytes_day = per_day * (int(row["bytes"]) / int(row["rows"]))
        spot_table = "spot" in str(row["dataset"])
        ceiling = CEILING_SPOT if spot_table else CEILING_PER_CONTRACT_TABLE
        # #5's ceiling was written at 588 listed contracts. The listing moves, so the
        # ceiling is also reported at the count actually seen in this window.
        seen = int(row["symbols"]) * MINUTES_PER_DAY
        live_ceiling = CEILING_SPOT if spot_table else seen
        total_rows_day += per_day
        total_bytes_day += bytes_day
        total_ceiling += ceiling
        total_live_ceiling += live_ceiling
        print(
            f"  {row['dataset']:16} {per_minute:9.1f} {per_day:12,.0f} "
            f"{ceiling:11,} {per_day / ceiling * 100:6.1f}% "
            f"{live_ceiling:13,} {per_day / live_ceiling * 100:7.1f}% "
            f"{bytes_day / 1e6:9.2f}"
        )
    print(
        f"  {'ALL FOUR':16} {'':9} {total_rows_day:12,.0f} {total_ceiling:11,} "
        f"{total_rows_day / total_ceiling * 100:6.1f}% {total_live_ceiling:13,} "
        f"{total_rows_day / total_live_ceiling * 100:7.1f}% {total_bytes_day / 1e6:9.2f}"
    )
    print(f"  (#5's stated total ceiling: {CEILING_TOTAL:,} rows/day)")
    if raw is not None:
        print(
            f"\n  raw JSON  {raw['bytes_per_day'] / 1e9:.2f} GB/day "
            f"({raw['bytes_per_second'] / 1024:.1f} KB/s, "
            f"{raw['msg_per_second']:.1f} msg/s, measured)"
        )
        print(
            f"  store     {total_bytes_day / 1e6:.2f} MB/day  ->  "
            f"{raw['bytes_per_day'] / total_bytes_day:.0f}x reduction (derived)"
        )
        print("  #5 said   50-100 MB/day, 500-1000x")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="store root (default: <repo>/data)")
    parser.add_argument("--raw-seconds", type=float, default=60.0)
    parser.add_argument("--skip-raw", action="store_true")
    parser.add_argument(
        "--capture",
        type=float,
        default=0.0,
        help="run the full live pipeline into a scratch root for N seconds first",
    )
    parser.add_argument("--capture-flush", type=float, default=60.0)
    parser.add_argument("--skip-day", action="store_true")
    parser.add_argument("--dates", type=int, default=8)
    parser.add_argument(
        "--work",
        default=None,
        help="scratch directory for the synthetic day (never the live root)",
    )
    args = parser.parse_args()

    root = Path(args.root) if args.root else default_root()
    # Scratch goes to the system temp directory, never into the repository and never
    # under the store root - a working copy inside `data/` would be picked up by the
    # store's own `rglob` and counted as part of the store it is measuring.
    work = (
        Path(args.work)
        if args.work
        else Path(tempfile.gettempdir()) / "deltapayoff-measure"
    )

    raw = None
    if args.capture:
        root = Path(args.root) if args.root else work.with_name("deltapayoff-capture")
        raw = await capture(root, args.capture, args.capture_flush)
        print(
            f"CAPTURE  (measured, live, {raw['seconds']:.1f} s, both channels, "
            f"{int(raw['symbols'])} listed BTC options, "
            f"flush every {args.capture_flush:.0f} s)"
        )
        print(
            f"  {raw['msg_per_second']:8.1f} msg/s   "
            f"{raw['bytes_per_second'] / 1024:8.1f} KB/s   "
            f"{raw['bytes_per_day'] / 1e9:6.2f} GB/day (derived)   "
            f"skipped {int(raw['skipped'])}   flush errors {int(raw['flush_errors'])}"
        )
    elif not args.skip_raw:
        raw = await measure_raw(args.raw_seconds)
        print(
            f"RAW  (measured, live, {raw['seconds']:.1f} s, both channels, "
            f"{int(raw['symbols'])} listed BTC options)"
        )
        print(
            f"  {raw['msg_per_second']:8.1f} msg/s   "
            f"{raw['bytes_per_second'] / 1024:8.1f} KB/s   "
            f"{raw['bytes_per_day'] / 1e9:6.2f} GB/day (derived)"
        )

    report_store(root)
    report_projection(root, raw)

    if not args.skip_day:
        synthesise_day(root, work)
        report_compaction(work)
        fan_out_dates(work, args.dates)
        store = all_stores(work)[0]
        if store.partitions():
            day, underlying = store.partitions()[-1]
            report_reads(work, day, underlying)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
