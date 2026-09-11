"""R8 (#76): what a reader pays against an open partition, and whether file count is why.

Parquet cannot be appended to, so "compact on the go" means "fold more often". The store
flushes every five minutes, so by 15:00 UTC today's partition holds ~180 files per table
and by the close 288; the nightly job folds a closed day to one. This probe asks what the
dashboard's historical routes pay for that, and **where** they pay it:

* **The routes' own functions, called as the engine calls them.**
  `historical.read_ladder_at` (`/chain/at`), `historical.list_minutes` (`/chain/minutes`),
  `contract_bars.read_contract_bars` (`/bars`) and `smile.read_smile` (`/smile`), each
  over `BarStore`s rooted at a variant tree. Nothing is re-implemented, so the glob, the
  hive pruning and the four separate collects are exactly the engine's.
* **The same rows at several file counts.** The real day's flush files, then the same rows
  folded one file per hour, the two-tier layout at its worst instant (sealed hours folded,
  the last hour still raw), and one file per day.
* **Opening versus reading.** `footers` opens every file and reads its footer and no page
  (`select(pl.len())`, the path `BarStore._rows` uses); `all rows` decompresses every
  page of the partition. If a route's time tracks `footers` it is paying for files; if it
  tracks `all rows` it is paying for rows, and no compaction cadence moves it.

**Read-only against `data/`.** Every variant is built by reading the real files and
writing into a `tempfile` directory outside the repository, deleted at exit. The one read
against `data/` itself is the first, taken before anything else touches those files'
contents, to get the nearest thing to a cold read this machine allows without purging
its cache.

    python tools/measure_open_partition.py
    python tools/measure_open_partition.py --date 2026-09-08 --underlying BTC --cut 15:00
    python tools/measure_open_partition.py --runs 10 --keep
"""

from __future__ import annotations

import argparse
import math
import platform
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.contract_bars import read_contract_bars  # noqa: E402
from deltapayoff.events.instrument import Instrument  # noqa: E402
from deltapayoff.historical import list_minutes, read_ladder_at  # noqa: E402
from deltapayoff.smile import read_smile  # noqa: E402
from deltapayoff.store import (  # noqa: E402
    COMPACT_PREFIX,
    all_stores,
    default_root,
)

STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
STAMP_LEN = len("20260908T000000Z")
FLUSHES_PER_DAY = 288
FLUSHES_PER_HOUR = 12
#: Option 2's rule: fold an hour's sealed files once the newest is ten minutes old, so an
#: hour h is folded at h+1:10.
FOLD_LAG = timedelta(minutes=70)

#: 0004a's rates, ap-south-1, read 2026-09-09. DELETE has no SKU in the offer file.
TIER1_PER_1000 = 0.005
TIER2_PER_1000 = 0.0004
DAYS_PER_MONTH = 30


# ---------------------------------------------------------------- the store as it is


def census(root: Path) -> dict[tuple[str, str], dict[str, int]]:
    """Files per table per partition, and whether any is already a compacted output."""
    print(f"\nCENSUS  {root}   (measured, a listing)")
    print(
        f"  {'date':12} {'und':4} "
        + " ".join(f"{s.dataset:>15}" for s in all_stores(root))
        + "  compacted?"
    )
    found: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    compacted: set[tuple[str, str]] = set()
    for store in all_stores(root):
        for day, und in store.partitions():
            directory = store.path / f"underlying={und}" / f"date={day}"
            files = list(directory.glob("*.parquet"))
            found[(day, und)][store.dataset] = len(files)
            if any(f.name.startswith(COMPACT_PREFIX) for f in files):
                compacted.add((day, und))
    for key in sorted(found):
        counts = found[key]
        print(
            f"  {key[0]:12} {key[1]:4} "
            + " ".join(f"{counts.get(s.dataset, 0):>15}" for s in all_stores(root))
            + f"  {'yes' if key in compacted else 'no'}"
        )
    return {k: v for k, v in found.items() if k not in compacted}


def flush_files(
    root: Path, dataset: str, day: str, und: str
) -> list[tuple[datetime, Path]]:
    directory = root / dataset / f"underlying={und}" / f"date={day}"
    out = []
    for path in sorted(directory.glob("*.parquet")):
        stamp = datetime.strptime(path.name[:STAMP_LEN], STAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        out.append((stamp, path))
    return out


# ---------------------------------------------------------------- the variants


def plan(
    files: list[tuple[datetime, Path]], cut: datetime
) -> dict[str, list[list[Path]]]:
    """Each variant as groups of input files; a group of one is copied, more are folded.

    A flush file is placed in the hour of the earliest minute it holds - its name - which
    is how a fold over *files* would see it.
    """

    def hours(items):
        grouped: dict[datetime, list[Path]] = defaultdict(list)
        for stamp, path in items:
            grouped[stamp.replace(minute=0, second=0)].append(path)
        return grouped

    before = [(s, p) for s, p in files if s < cut]
    two_tier: list[list[Path]] = []
    for hour, paths in sorted(hours(before).items()):
        if hour + FOLD_LAG <= cut:
            two_tier.append(paths)  # sealed and folded
        else:
            two_tier.extend([p] for p in paths)  # still raw at this instant
    return {
        "open-raw": [[p] for _, p in before],
        "open-two-tier": two_tier,
        "open-hourly": [paths for _, paths in sorted(hours(before).items())],
        "open-single": [[p for _, p in before]],
        "day-raw": [[p] for _, p in files],
        "day-hourly": [paths for _, paths in sorted(hours(files).items())],
        "day-single": [[p for _, p in files]],
    }


def fold(inputs: list[Path], output: Path) -> tuple[int, float, float]:
    """Exactly `compact_partition`'s write - read, sort on minute, write - with no verify.
    Returns rows, wall seconds and CPU seconds."""
    w0, c0 = time.perf_counter(), time.process_time()
    frame = pl.read_parquet(inputs, hive_partitioning=False)
    frame.sort("minute", maintain_order=True).write_parquet(output)
    return frame.height, time.perf_counter() - w0, time.process_time() - c0


def materialise(
    name: str,
    groups_by_table: dict[str, list[list[Path]]],
    work: Path,
    day: str,
    und: str,
    fold_log: dict,
) -> Path:
    root = work / name
    for dataset, groups in groups_by_table.items():
        directory = root / dataset / f"underlying={und}" / f"date={day}"
        directory.mkdir(parents=True)
        single = name.endswith("-single")
        for index, group in enumerate(groups):
            if len(group) == 1 and not single:
                shutil.copyfile(group[0], directory / group[0].name)
                continue
            output = directory / f"fold-{index:03d}-{group[0].name[:STAMP_LEN]}.parquet"
            rows, wall, cpu = fold(group, output)
            fold_log[(name, dataset)].append((len(group), rows, wall, cpu))
    return root


# ---------------------------------------------------------------- the reads


def timed(call, runs: int) -> dict[str, float]:
    """One untimed warm-up, then `runs` timed calls. Wall median/p95/min; CPU is the
    process total across Polars' threads, divided by `runs` (Windows' process clock ticks
    at ~15.6 ms, so a per-call median would be quantised)."""
    call()
    walls = []
    c0 = time.process_time()
    for _ in range(runs):
        w0 = time.perf_counter()
        call()
        walls.append((time.perf_counter() - w0) * 1000)
    cpu = (time.process_time() - c0) * 1000 / runs
    walls.sort()
    return {
        "median": statistics.median(walls),
        "p95": walls[max(0, math.ceil(0.95 * runs) - 1)],
        "min": walls[0],
        "cpu": cpu,
    }


def reads(
    root: Path,
    und: str,
    expiry: str,
    minute: datetime,
    instrument: Instrument | None,
    day: Date,
    *,
    with_smile: bool = True,
) -> dict:
    """The routes' own functions over four stores at `root`, plus the two bounds."""
    quote, reference, spot, computed = all_stores(root)
    stores = (quote, reference, spot, computed)
    clause = (pl.col("date") == day) & (pl.col("underlying") == und)

    def ladder():
        chain = read_ladder_at(quote, reference, computed, spot, und, expiry, minute)
        return (
            0
            if chain is None
            else sum((r.call is not None) + (r.put is not None) for r in chain.rows)
        )

    def minutes():
        return len(list_minutes(quote, und, expiry, day))

    def bars():
        return len(read_contract_bars(quote, reference, instrument, day))

    def smile():
        return sum(len(m.points) for m in read_smile(computed, und, expiry).minutes)

    def footers():
        return sum(
            s.scan().filter(clause).select(pl.len()).collect().item() for s in stores
        )

    def all_rows():
        return sum(
            f.height for f in pl.collect_all([s.scan().filter(clause) for s in stores])
        )

    calls = {"/chain/at": ladder, "/chain/minutes": minutes}
    if instrument is not None:
        calls["/bars"] = bars
    if with_smile:
        calls["/smile"] = smile
    calls["footers"] = footers
    calls["all rows"] = all_rows
    return calls


def file_counts(root: Path, day: str, und: str) -> str:
    counts = []
    for store in all_stores(root):
        directory = store.path / f"underlying={und}" / f"date={day}"
        counts.append(len(list(directory.glob("*.parquet"))))
    return "/".join(str(c) for c in counts)


# ---------------------------------------------------------------- derived S3 requests


def compaction_requests(n: int) -> tuple[int, int, int]:
    """`compact_partition` over n inputs, mapped call by call onto S3 (`assumed` mapping;
    no S3 implementation exists). Tier 1: three globs as LIST (`_recover`'s `*.tmp`, the
    inputs, `_next_output_name`), PUT tmp, PUT manifest staging, COPY manifest into place,
    COPY publish. Tier 2: HEAD manifest, one footer GET per input (`_rows`), footer+data
    per input for the read (`assumed` 2), footer+data for the verify. DELETE: every input,
    the manifest staging, the tmp after publish, the manifest."""
    return 3 + 2 + 2, 1 + n + 2 * n + 2, n + 3


def s3_report(day_bytes: dict[str, int], reads_per_day: int) -> None:
    print("\nDERIVED  S3 requests per day, per table, per partition (one underlying)")
    t1, t2, dl = compaction_requests(FLUSHES_PER_DAY)
    nightly = (FLUSHES_PER_DAY + t1, t2, dl)
    h1, h2, hd = compaction_requests(FLUSHES_PER_HOUR)
    n1, n2, nd = compaction_requests(23 + FLUSHES_PER_HOUR)  # 23 hour files + hour 23 raw
    two_tier = (FLUSHES_PER_DAY + 23 * h1 + n1, 23 * h2 + n2, 23 * hd + nd)
    print(
        f"  {'option':34} {'Tier-1 (PUT/COPY/LIST)':>22} {'Tier-2 (GET/HEAD)':>18} "
        f"{'DELETE':>7}"
    )
    print(f"  {'1 nightly only':34} {nightly[0]:22,} {nightly[1]:18,} {nightly[2]:7,}")
    print(
        f"  {'2 atomic flush + hourly + nightly':34} {two_tier[0]:22,} {two_tier[1]:18,} "
        f"{two_tier[2]:7,}"
    )

    # Objects a today-read touches, averaged over the day in five-minute steps.
    def objects(option: int, minutes: int) -> int:
        flushes = minutes // 5
        if option == 1:
            return flushes
        folded = max(0, (minutes - 70) // 60 + 1) if minutes >= 70 else 0
        return folded + (flushes - folded * FLUSHES_PER_HOUR)

    steps = range(5, 1441, 5)
    avg1 = statistics.mean(objects(1, m) for m in steps)
    avg2 = statistics.mean(objects(2, m) for m in steps)
    print(
        f"  objects in today's partition, day average: option 1 {avg1:.1f}, "
        f"option 2 {avg2:.1f}"
    )

    def monthly(
        write: tuple[int, int, int], avg: float, scale: int
    ) -> tuple[float, float]:
        tables = 4 * scale
        w = (write[0] * TIER1_PER_1000 + write[1] * TIER2_PER_1000) / 1000 * tables
        # a /chain/at: four tables, one LIST each, `assumed` 2 GETs per object touched
        r = (
            reads_per_day
            * scale
            * 4
            * (1 * TIER1_PER_1000 + 2 * avg * TIER2_PER_1000)
            / 1000
        )
        return w * DAYS_PER_MONTH, r * DAYS_PER_MONTH

    print(
        f"\n  $/month, ap-south-1, `assumed` {reads_per_day:,} today-reads a day at 1x "
        f"(each a /chain/at over four tables); DELETE priced at $0"
    )
    for scale in (1, 10):
        w1, r1 = monthly(nightly, avg1, scale)
        w2, r2 = monthly(two_tier, avg2, scale)
        print(
            f"  {scale:>2}x  option 1: writes+compaction ${w1:7.2f}  "
            f"today-reads ${r1:7.2f}  total ${w1 + r1:7.2f}"
        )
        print(
            f"  {scale:>2}x  option 2: writes+compaction ${w2:7.2f}  "
            f"today-reads ${r2:7.2f}  total ${w2 + r2:7.2f}"
        )
    extra = (
        (
            (two_tier[0] - nightly[0]) * TIER1_PER_1000
            + (two_tier[1] - nightly[1]) * TIER2_PER_1000
        )
        / 1000
        * 4
    )
    saved = 4 * 2 * (avg1 - avg2) * TIER2_PER_1000 / 1000
    print(
        f"  break-even: {extra / saved:.1f} today-reads a day (option 2's extra fold "
        f"requests ${extra:.5f}/day against ${saved:.6f} saved per read)"
    )
    delete_extra = (two_tier[2] - nightly[2]) * 4 * TIER1_PER_1000 / 1000 * DAYS_PER_MONTH
    print(
        f"  if DELETE were billed at Tier-1: option 2 adds "
        f"${delete_extra:.3f}/month at 1x"
    )

    print("\nDERIVED  option 3, rewrite the day file on every flush")
    total = sum(day_bytes.values())
    for dataset, size in day_bytes.items():
        rewritten = size * (FLUSHES_PER_DAY + 1) / 2
        print(
            f"  {dataset:16} day file {size / 1e6:8.2f} MB  rewritten "
            f"{rewritten / 1e9:7.2f} GB/day"
        )
    print(
        f"  {'all four':16} day file {total / 1e6:8.2f} MB  rewritten "
        f"{total * (FLUSHES_PER_DAY + 1) / 2 / 1e9:7.2f} GB/day  "
        f"(option 1 writes each byte once to flush and once to fold)"
    )


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="store root (default: <repo>/data)")
    parser.add_argument(
        "--date",
        default=None,
        help="closed day to stand in for the open one (default: the "
        "uncompacted closed day with the most quote-bars files)",
    )
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument(
        "--cut", default="15:00", help="HH:MM UTC the open partition is cut at"
    )
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument(
        "--reads-per-day",
        type=int,
        default=1000,
        help="`assumed` today-reads a day, for the derived bill",
    )
    parser.add_argument("--keep", action="store_true", help="leave the temp tree behind")
    args = parser.parse_args()

    root = Path(args.root) if args.root else default_root()
    started = datetime.now(timezone.utc)
    print(
        f"measure_open_partition  {started:%Y-%m-%dT%H:%M:%SZ}  "
        f"python {platform.python_version()}  polars {pl.__version__}"
    )
    print("command: python tools/measure_open_partition.py " + " ".join(sys.argv[1:]))

    today = started.strftime("%Y-%m-%d")
    candidates = census(root)
    if args.date:
        day = args.date
    else:
        closed = [
            (v.get("quote-bars", 0), k)
            for k, v in candidates.items()
            if k[0] < today and k[1] == args.underlying
        ]
        day = max(closed)[1][0]
    und = args.underlying
    hh, mm = (int(x) for x in args.cut.split(":"))
    day_date = Date.fromisoformat(day)
    cut = datetime(
        day_date.year, day_date.month, day_date.day, hh, mm, tzinfo=timezone.utc
    )
    print(f"\nstand-in: {day} {und}, cut at {cut:%H:%M}Z")

    # What to ask for, chosen from ONE quote file so the cold read below stays cold.
    quote_files = flush_files(root, "quote-bars", day, und)
    target = cut - timedelta(minutes=30)
    probe_file = [p for s, p in quote_files if s <= target][-1]
    sample = pl.read_parquet(probe_file, hive_partitioning=False)
    expiry = str(
        sample.group_by("expiry")
        .len()
        .sort(["len", "expiry"], descending=True)["expiry"][0]
    )
    rows = sample.filter(pl.col("expiry") == expiry)
    minute = min(rows["minute"].unique().to_list(), key=lambda m: abs(m - target))
    calls = rows.filter(pl.col("option_type") == "C").sort("strike")
    symbol = str(calls["symbol"][calls.height // 2])
    kind, sym_und, strike, ddmmyy = symbol.split("-")
    canonical = (
        f"DELTA-{sym_und}-20{ddmmyy[4:]}{ddmmyy[2:4]}{ddmmyy[:2]}-{strike}-{kind}-USD"
    )
    try:
        instrument = Instrument.from_canonical(canonical)
    except Exception as error:  # a symbol shape this probe does not know: skip /bars
        print(f"  /bars skipped: {canonical!r} did not parse ({error})")
        instrument = None
    print(
        f"expiry {expiry}, minute {minute:%H:%M}Z, contract {canonical}  "
        f"(chosen from {probe_file.name})"
    )

    # The nearest thing to a cold read: the first read of these files by this process
    # since the 18:39Z reboot. No cache purge is possible here without admin rights, and
    # another process may have read them, so it is labelled "first read", not "cold".
    real = reads(root, und, expiry, minute, instrument, day_date, with_smile=False)
    print(f"\nFIRST READ  {root}, /chain/at at {day} {minute:%H:%M}Z, n=1 (measured)")
    w0, c0 = time.perf_counter(), time.process_time()
    real["/chain/at"]()
    print(
        f"  wall {(time.perf_counter() - w0) * 1000:.1f} ms   "
        f"cpu {(time.process_time() - c0) * 1000:.1f} ms"
    )

    work = Path(tempfile.mkdtemp(prefix="measure_open_partition-"))
    print(f"\nvariants built under {work}  (outside the repository; removed at exit)")
    fold_log: dict = defaultdict(list)
    results: dict[str, dict[str, dict[str, float]]] = {}
    counts: dict[str, str] = {}
    day_bytes: dict[str, int] = {}
    try:
        per_table = {
            s.dataset: plan(flush_files(root, s.dataset, day, und), cut)
            for s in all_stores(root)
        }
        names = list(next(iter(per_table.values())))
        roots = {}
        for name in names:
            roots[name] = materialise(
                name, {t: per_table[t][name] for t in per_table}, work, day, und, fold_log
            )
            counts[name] = file_counts(roots[name], day, und)
        for store in all_stores(roots["day-single"]):
            directory = store.path / f"underlying={und}" / f"date={day}"
            day_bytes[store.dataset] = sum(
                p.stat().st_size for p in directory.glob("*.parquet")
            )

        print("\nFOLD COMPUTE  (measured while building; read+sort+write, no verify)")
        for name in ("day-hourly", "day-single"):
            for dataset in per_table:
                log = fold_log[(name, dataset)]
                print(
                    f"  {name:12} {dataset:16} {len(log):3} folds  "
                    f"{sum(e[0] for e in log):4} inputs  "
                    f"{sum(e[1] for e in log):9,} rows  "
                    f"wall {sum(e[2] for e in log):6.2f} s  "
                    f"cpu {sum(e[3] for e in log):6.2f} s"
                )

        results[f"data/ ({day} at full file count, whole store globbed)"] = {
            k: timed(v, args.runs) for k, v in real.items()
        }
        counts[f"data/ ({day} at full file count, whole store globbed)"] = file_counts(
            root, day, und
        )
        for name in names:
            calls = reads(roots[name], und, expiry, minute, instrument, day_date)
            results[name] = {k: timed(v, args.runs) for k, v in calls.items()}
            answers = {k: v() for k, v in calls.items()}
            print(
                f"  {name:14} files {counts[name]:>16} (quote/reference/spot/computed)  "
                + "  ".join(f"{k}={v:,}" for k, v in answers.items())
            )
    finally:
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    print(
        f"\nREADS  warm cache (variants were just written), {args.runs} timed runs after "
        f"one warm-up, ms (measured)"
    )
    print(f"  {'variant':52} {'read':15} {'median':>8} {'p95':>8} {'min':>8} {'cpu':>8}")
    for name, by_read in results.items():
        for read, t in by_read.items():
            print(
                f"  {name:52} {read:15} {t['median']:8.1f} {t['p95']:8.1f} "
                f"{t['min']:8.1f} {t['cpu']:8.1f}"
            )

    s3_report(day_bytes, args.reads_per_day)
    print(f"\nfinished {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
