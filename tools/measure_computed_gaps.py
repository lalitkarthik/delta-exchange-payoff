"""#23: how many minutes have quotes but no volatility of ours. The two-line query.

`computed-bars` is the only table that is **sampled** rather than folded from arrivals,
so it is the only one that can go missing while the feed is fine. Comparing its minutes
against `quote-bars`' minutes for one expiry is what exposed the loss #23 fixes -
`measured` on the live store on 2026-09-04 for expiry 25-09-2026, 217 of 904 minutes had
quote bars and no computed bar, and **every gap was exactly one minute long**:
`run lengths: [(1, 217)]`. Nothing else on the screen or in the logs said so.

**That 904 was a truncated run, and #104 is the ticket that found out.** The same store,
the same day, the same expiry, re-read `measured` 2026-09-12 holds 00:00 - 19:40, 1,181
minutes - 277 more than the run above examined. The probe was run while the day was still
being recorded and reported the part it saw as though it were the day.

Four kinds of absence, and the point of the probe is to keep them apart:

**no quote bar at all** - the engine was down. Not a sampling loss, and it is the block
of 62 minutes that the run above found alongside the scatter.

**quote bar, no computed bar** - the sampling loss. Single-minute runs mean the boundary
race; long runs would mean the recompute loop itself stalled, which is a different bug.

**neither, inside the span examined** - impossible by construction: the span examined is
the first to the last minute either table holds, so nothing inside it is absent from both.

**outside the span examined** - #104's fourth kind. The span examined is shorter than the
span asked for, and those minutes were never looked at. **Counted on their own line and
never in the percentage**, because a percentage that counts them would answer a question
nobody asked and a percentage that hides them is how a stalled store scores better than a
working one.

**Why the fourth kind had to exist.** The grid is the first to the last minute the expiry
was seen in, so a store that stops recording does not put holes inside the grid - it
shortens the grid. #103 froze the split stack at 11:02:38Z and this probe scored it 0.3%
against the healthy monolith's 2.3%, eight times better, because the stall took minutes
out of the numerator and the denominator together (`measured` 2026-09-12T13:22Z, #104).
Keeping the span from the data is still right - a day that started late is not loss - so
the fix is not to change the grid but to **say what was asked for beside what was
examined**, and to report coverage beside loss so a shrinking denominator cannot read as
an improving system.

**A late start and an early stop are different facts, and only one is a defect.** A span
that begins after the start asked for is a day that started late: reported, not flagged.
A span that ends before the end asked for is a stop, and it is told apart two ways - the
span ends before the last entry the *source* holds for that day across every expiry, or
it ends more than one store flush before the wall clock. `store.FLUSH_SECONDS` is 300, and
`BarStore.scan()` reads Parquet only, so the newest few minutes of a live day are
legitimately not on disk yet; `FLUSH_TOLERANCE_MINUTES` is that grace and nothing more.

Read-only. It opens the running engine's own `data/` by default and never writes there.

    python tools/measure_computed_gaps.py
    python tools/measure_computed_gaps.py --expiry 25-09-2026 --date 2026-09-04
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
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

#: The right edge a run may legitimately be missing. `store.FLUSH_SECONDS` is 300 s and
#: `BarStore.scan()` reads Parquet only, so up to five minutes of a day still being
#: recorded are sealed in the buffer and not yet on disk. A tail shorter than this is
#: that, not a stop - and a probe that cried wolf on every live run would be ignored,
#: which is the failure mode this ticket is already about. `derived` from
#: `store.FLUSH_SECONDS`, #104.
FLUSH_TOLERANCE_MINUTES = 5


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


def source_last_minute(root: Path, day: str | None) -> datetime | None:
    """The last minute the **source** holds for this request, across every expiry.

    Criterion 2's second limb. One expiry ending at 11:02 while seven others ran to
    23:59 is that expiry having stopped, and the wall clock cannot tell you so - the
    store can. Read once per run and handed to every `report`, because eight expiries
    asking the same question of the same files is eight scans of one answer.
    """
    last: datetime | None = None
    for dataset, schema in TABLES.items():
        frame = BarStore(root, dataset=dataset, schema=schema).scan()
        if day is not None:
            frame = frame.filter(pl.col("date").cast(pl.Utf8) == day)
        collected = frame.select(pl.col("minute").max()).collect()
        if not collected.height:
            continue
        value = collected["minute"].to_list()[0]
        if value is not None and (last is None or value > last):
            last = value
    return last


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


@dataclass(frozen=True)
class Span:
    """The span asked for, the span examined, and what separates them.

    Nothing here is a judgement about the loss rate. It is only about *how much of the
    question the run actually answered*, which is the thing the old output could not say.
    """

    #: Inclusive, both ends, both spans. `observed_*` is None when nothing was found.
    requested_first: datetime
    requested_last: datetime
    observed_first: datetime | None
    observed_last: datetime | None
    #: The last minute any expiry has in this request, or None when not looked up.
    source_last: datetime | None

    @staticmethod
    def _count(first: datetime, last: datetime) -> int:
        return max(0, int((last - first) / MINUTE) + 1)

    @property
    def requested_minutes(self) -> int:
        return self._count(self.requested_first, self.requested_last)

    @property
    def observed_minutes(self) -> int:
        if self.observed_first is None or self.observed_last is None:
            return 0
        return self._count(self.observed_first, self.observed_last)

    @property
    def late_start_minutes(self) -> int:
        """Asked for and never reached, at the head. A day that started late."""
        if self.observed_first is None:
            return self.requested_minutes
        return max(0, self._count(self.requested_first, self.observed_first) - 1)

    @property
    def early_stop_minutes(self) -> int:
        """Asked for and never reached, at the tail. A day that stopped early."""
        if self.observed_last is None:
            return 0
        return max(0, self._count(self.observed_last, self.requested_last) - 1)

    @property
    def outside_minutes(self) -> int:
        """The fourth kind of absence: asked for, outside the span examined."""
        return self.late_start_minutes + self.early_stop_minutes

    @property
    def coverage(self) -> float:
        """How much of the span asked for was examined, as a percentage.

        **The denominator comes from the request**, which is the whole point: it is the
        one figure in this output a stall cannot improve.
        """
        if not self.requested_minutes:
            return 0.0
        return 100.0 * self.observed_minutes / self.requested_minutes

    @property
    def stopped_early(self) -> bool:
        """Whether the tail is a stop rather than the edge of an open partition."""
        return self.early_stop_minutes > FLUSH_TOLERANCE_MINUTES

    @property
    def stop_note(self) -> str:
        """What the tail is, in one clause, with the evidence a reader can check."""
        if self.observed_last is None:
            return "nothing at all was recorded for the span asked for"
        if not self.early_stop_minutes:
            return "the span reaches the end asked for"
        if not self.stopped_early:
            return "within one store flush of the end asked for; an open partition's edge"
        if (
            self.source_last is not None
            and self.source_last - self.observed_last > FLUSH_TOLERANCE_MINUTES * MINUTE
        ):
            return (
                "this expiry stopped: the store holds entries for this day until "
                f"{self.source_last:%H:%M}"
            )
        return (
            f"the source stopped: its own last entry is {self.observed_last:%H:%M}, "
            f"{self.early_stop_minutes} minutes before {self.requested_last:%H:%M}"
        )


def requested_window(
    day: str | None, observed: set, now: datetime
) -> tuple[datetime, datetime]:
    """The span the operator asked for, in minutes, inclusive at both ends.

    `--date` asks for a calendar day in UTC, so the span asked for is that day - cut at
    the wall clock when the day has not finished, because a run at 13:22 cannot be
    faulted for not holding 23:59. With no `--date` the request is every day the store
    holds for this expiry, whole days at both ends, which is the same rule applied to a
    wider question.
    """
    if day is not None:
        first = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        last_date = first.date()
    else:
        first = datetime.combine(min(observed).date(), time.min, timezone.utc)
        last_date = max(observed).date()
    last = datetime.combine(last_date, time(23, 59), timezone.utc)
    now = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return first, max(first, min(last, now))


def _edges(span: Span) -> str:
    """`HH:MM - HH:MM` for the span examined, or `none` when nothing was found."""
    if span.observed_first is None or span.observed_last is None:
        return "  none       "
    return f"{span.observed_first:%H:%M} - {span.observed_last:%H:%M}"


def render(
    expiry: str, day: str | None, span: Span, absent: int, lost: set, quoted: int
) -> list[str]:
    """The printed report, as lines. Separate from `report` so a test can read it.

    **The heading names the expiry and the day asked for, never the span examined.** The
    old heading was the span examined, and `05:49 - 11:02` under `--date 2026-09-12` read
    as a column title rather than as the finding it was (#104).
    """
    rate = 100.0 * len(lost) / quoted if quoted else 0.0
    lines = [
        "",
        f"{expiry}  {day or 'all dates'}  (measured)",
        f"  span asked for                {span.requested_first:%H:%M} - "
        f"{span.requested_last:%H:%M}   {span.requested_minutes:6d} minutes",
        f"  span examined                 {_edges(span)}   "
        f"{span.observed_minutes:6d} minutes   {span.coverage:5.1f}% coverage",
    ]
    if span.outside_minutes > FLUSH_TOLERANCE_MINUTES:
        lines += [
            f"  *** SHORT SPAN: {span.outside_minutes} of the "
            f"{span.requested_minutes} minutes asked for were never examined",
            f"        started late  {span.late_start_minutes:6d} minutes   "
            "a day may start late; that is not a defect",
            f"  {'***' if span.stopped_early else '   '}   stopped early "
            f"{span.early_stop_minutes:6d} minutes   {span.stop_note}",
        ]
    elif span.outside_minutes:
        lines.append(
            "  span asked for and span examined agree to within one store flush"
        )
    else:
        lines.append("  span asked for and span examined agree")
    lines += [
        f"  no quote bar at all           {absent:6d}   engine down",
        f"  quote bar, no computed bar    {len(lost):6d}   {rate:5.1f}%   "
        f"of the {quoted} minutes with a quote bar",
        f"  outside the span examined     {span.outside_minutes:6d}   "
        "the span itself is short; never in the percentage",
        f"  gap run lengths (length, count)   {runs(lost)}",
    ]
    return lines


def report(
    root: Path,
    expiry: str,
    day: str | None,
    *,
    now: datetime | None = None,
    source_last: datetime | None = None,
) -> None:
    """Print one expiry's report. `now` and `source_last` are injected so a test needs
    neither the wall clock nor a second scan of the store."""
    now = datetime.now(timezone.utc) if now is None else now

    quoted = minutes(root, DATASET, expiry, day)
    computed = minutes(root, COMPUTED_DATASET, expiry, day)
    observed = quoted | computed
    if not observed and day is None:
        # With no `--date` there is no request to measure the silence against, so there
        # is nothing to report but the silence.
        print(f"{expiry}: no bars in {root}")
        return

    requested_first, requested_last = requested_window(day, observed, now)
    span = Span(
        requested_first=requested_first,
        requested_last=requested_last,
        observed_first=min(observed) if observed else None,
        observed_last=max(observed) if observed else None,
        source_last=source_last,
    )

    grid = set()
    if observed:
        stamp, last = min(observed), max(observed)
        while stamp <= last:
            grid.add(stamp)
            stamp += MINUTE

    absent = grid - quoted
    lost = (grid & quoted) - computed
    print("\n".join(render(expiry, day, span, len(absent), lost, len(quoted))))


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
    source_last = source_last_minute(args.root, args.date)
    for expiry in expiries:
        report(args.root, expiry, args.date, source_last=source_last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
