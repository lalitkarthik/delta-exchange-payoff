"""Compare two store roots over the same UTC minute window.

The command is deliberately read-only and requires both roots explicitly:

    python tools/compare_store_runs.py --left data/monolith --right data/split \
        --start 2026-09-12T10:00:00Z --end 2026-09-12T11:00:00Z
    python tools/compare_store_runs.py --left run-a --right run-b \
        --start 2026-09-12T10:00:00Z --end 2026-09-12T11:00:00Z \
        --underlying BTC --table computed-bars

Two recorders holding **separate websockets** to the venue do not see the same ticks at
the same instant, so byte-equality between them is the wrong test: a correct pair of
runs will still differ row by row. What is actually invariant between two independent
recorders is conservation -- nothing the venue sent is lost or duplicated -- and identity
-- a contract's strike, expiry and option type do not depend on which socket recorded it.
This tool checks those two things per table, states plainly which columns it holds exact,
which it checks by invariant, and which it only reports, and refuses to run at all over a
window too recent to have been safely flushed to disk.

Table-by-table:

- **Tables B (`reference-bars`) and D (`spot-bars`)** fold the same broadcast/index
  channels in both processes and perform no arithmetic of their own, so every column is
  held to byte-equality. A difference here is a real defect.
- **Table A (`quote-bars`)** folds the book, and a tick can land in a different minute
  on each side of a socket-boundary race. Identity and provenance (`strike`, `expiry`,
  `option_type`, `from_book`) stay exact; the three tick counts are judged by
  conservation (see `QUOTE_TICK_ROW_TOLERANCE` below) rather than equality; the price
  and `last_lts` columns are reported for visibility but do not gate the exit code.
- **Table C (`computed-bars`)** is a point sample of a 100 ms recompute cache, not a
  fold of the book, so two independently-sampled caches disagree at a high rate whether
  or not the underlying quote row agrees. Identity, provenance and model bookkeeping
  (`strike`, `expiry`, `option_type`, `iv_leg`, `iv_reason`, `model_version`) stay exact;
  `theta` is judged against a measured envelope; the remaining Greeks, `iv`, `forward`,
  `discount`, `years_to_expiry` and `forward_method` are reported for visibility but do
  not gate the exit code, because #82 measured only `theta`'s envelope -- widening the
  gate to the rest needs its own side-by-side run first.

The end of the requested window is exclusive. Exit 0 means every gated invariant held;
exit 1 means one did not; exit 2 means the window itself was refused (see
`MIN_WINDOW_AGE_SECONDS`) before any table was read.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.bars import QUOTE_GRACE_SECONDS  # noqa: E402
from deltapayoff.store import (  # noqa: E402
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    DATASET,
    FLUSH_SECONDS,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
)

# ---------------------------------------------------------------------------------
# Tolerances. Every one carries the run it was measured or derived from -- see #82's
# acceptance criteria and docs/design/lld/store-numbers.md, where the same numbers are
# recorded for anyone who does not read this file.
# ---------------------------------------------------------------------------------

#: Tables B and D fold the same channels in both processes and do no arithmetic of
#: their own. `measured`, 2026-09-12T05:52-06:12Z, BTC (#82): bit-identical over 10,564
#: and 19 common keys, 24/24 and 5/5 columns -- `mark_ticks`, a count, included.
EXACT_TABLES: tuple[str, ...] = (REFERENCE_DATASET, SPOT_DATASET)

#: Table A's three tick counts. `measured`, 2026-09-12T05:52-06:12Z, BTC (#82): every one
#: of 11,120 rows had `abs(right - left) <= 1`; the window's total tick count matched
#: exactly (1,333,097) on all three columns; rows favouring the right side (1,215) were
#: matched by rows favouring the left (1,215), so the net signed difference was 0. That
#: last fact is checked as an exact-zero window total, not "close to zero": a systemic
#: one-tick bias every row would never trip the per-row bound below but would still be a
#: real defect, and only a net-zero total rules it out.
QUOTE_TICK_COLUMNS: tuple[str, ...] = ("bid_ticks", "ask_ticks", "mid_ticks")
QUOTE_TICK_ROW_TOLERANCE = 1

#: Never differs between two recorders of the same feed regardless of socket timing;
#: any difference here is a real defect, not sampling jitter.
QUOTE_EXACT_COLUMNS: tuple[str, ...] = ("strike", "expiry", "option_type", "from_book")

#: Never differs between two recorders' solves of the same model version.
COMPUTED_EXACT_COLUMNS: tuple[str, ...] = (
    "strike",
    "expiry",
    "option_type",
    "iv_leg",
    "iv_reason",
    "model_version",
)

#: `theta`. `measured`, 2026-09-12T05:52-06:12Z, BTC (#82): the largest absolute
#: difference over every differing row was 1.000 (the ATM 0DTE straddle, -114.5 vs
#: -115.5, 0.87% relative, from a 0.82% iv difference). The 21 rows whose *relative*
#: difference exceeded 1 all had `|theta| <= 0.34` -- a near-zero denominator inflating
#: an unremarkable absolute gap -- so relative difference only judges rows big enough
#: for it to mean something; below that magnitude only the absolute bound applies.
COMPUTED_THETA_COLUMN = "theta"
COMPUTED_THETA_ABS_TOLERANCE = 1.0
COMPUTED_THETA_REL_TOLERANCE = 0.01
COMPUTED_THETA_REL_MAGNITUDE_FLOOR = 1.0

#: A minute is not safely on disk until one flush interval plus the seal grace has
#: passed behind it: a minute can seal just after a scheduled flush and then wait a
#: full interval for the next one. `derived` from `FLUSH_SECONDS` (`deltapayoff.store`,
#: five minutes) and `QUOTE_GRACE_SECONDS` (`deltapayoff.bars`, the 8 s seal grace this
#: table shares with tables B and D) -- exactly what #82 observed on 2026-09-12: minute
#: 06:11 sealed at 06:12:08Z, missed the 06:12:03Z flush, and was not on disk until
#: 06:17:03Z. A comparison run made 2 m 51 s (171 s) after the window's 06:12:00Z end --
#: 137 s short of this 308 s threshold -- read that minute as 556 missing rows on three
#: tables; a later run, past the threshold, read 0.
MIN_WINDOW_AGE_SECONDS = FLUSH_SECONDS + QUOTE_GRACE_SECONDS


def _parse_minute(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO 8601 timestamp"
        ) from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise argparse.ArgumentTypeError(f"{value!r} must carry a UTC offset")
    if stamp.second or stamp.microsecond:
        raise argparse.ArgumentTypeError(f"{value!r} is not a minute boundary")
    return stamp.astimezone(timezone.utc)


def _parse_instant(value: str) -> datetime:
    """Like `_parse_minute` but without the minute-boundary requirement.

    Only used by the hidden `--now` flag, which lets a test hold the "current time" the
    window-age refusal below compares against fixed, instead of racing the real clock.
    """
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO 8601 timestamp"
        ) from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise argparse.ArgumentTypeError(f"{value!r} must carry a UTC offset")
    return stamp.astimezone(timezone.utc)


def _rows(
    root: Path,
    dataset: str,
    schema: dict[str, Any],
    start: datetime,
    end: datetime,
    underlying: str | None,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    frame = BarStore(root, dataset=dataset, schema=schema).scan()
    frame = frame.filter(
        (pl.col("minute") >= start) & (pl.col("minute") < end)
    )
    if underlying is not None:
        frame = frame.filter(pl.col("underlying") == underlying.upper())
    collected = frame.collect()
    key_columns = ("minute",) if dataset == SPOT_DATASET else ("symbol", "minute")
    return {
        tuple(row[column] for column in key_columns): row
        for row in collected.to_dicts()
    }


def _numeric(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _value_difference(left: Any, right: Any) -> tuple[bool, float | None, float | None]:
    """Whether two values differ at all, plus absolute/relative gaps when numeric.

    Used both where a difference gates the exit code (exact columns) and where it is
    only reported (informational columns) -- the boolean means "not identical", not
    "outside tolerance"; tolerance, where one applies, is judged by the caller.
    """
    if left is None or right is None:
        return left != right, None, None
    if _numeric(left) and _numeric(right):
        absolute = abs(float(left) - float(right))
        scale = max(abs(float(left)), abs(float(right)))
        relative = 0.0 if scale == 0.0 else absolute / scale
        return absolute != 0.0, absolute, relative
    return left != right, None, None


def _refuse_if_window_not_flushed(end: datetime, *, now: datetime) -> str | None:
    """A refusal message if `end` is too recent to trust coverage against, else None."""
    age = (now - end).total_seconds()
    if age >= MIN_WINDOW_AGE_SECONDS:
        return None
    return (
        f"refusing: --end {end.isoformat()} is only {age:.1f}s behind --now "
        f"{now.isoformat()}, and a minute needs {MIN_WINDOW_AGE_SECONDS:.1f}s "
        f"(flush interval {FLUSH_SECONDS:.0f}s + seal grace {QUOTE_GRACE_SECONDS:.0f}s) "
        "to be safely flushed to disk. Comparing now would read an unflushed minute as "
        "missing coverage rather than as not-yet-written. Re-run once the window is "
        "that old, or pick an --end further in the past."
    )


def _print_coverage(
    dataset: str,
    left_keys: set[tuple[Any, ...]],
    right_keys: set[tuple[Any, ...]],
    common: set[tuple[Any, ...]],
) -> bool:
    left_only = left_keys - common
    right_only = right_keys - common
    coverage = len(left_only) + len(right_only)
    print(
        f"{dataset}: common={len(common)} left_only={len(left_only)} "
        f"right_only={len(right_only)} coverage={coverage}"
    )
    if coverage:
        print(f"  coverage difference: {coverage} minute keys")
    return coverage == 0


def _print_exact_columns(
    columns: tuple[str, ...],
    common: set[tuple[Any, ...]],
    left_rows: dict[tuple[Any, ...], dict[str, Any]],
    right_rows: dict[tuple[Any, ...], dict[str, Any]],
) -> bool:
    ok = True
    for column in columns:
        differing = 0
        for key in common:
            differs, _absolute, _relative = _value_difference(
                left_rows[key][column], right_rows[key][column]
            )
            if differs:
                differing += 1
        status = "exact" if differing == 0 else "DIFFERS"
        print(f"  {column}: differing={differing} ({status})")
        if differing:
            ok = False
    return ok


def _print_informational_columns(
    columns: list[str],
    common: set[tuple[Any, ...]],
    left_rows: dict[tuple[Any, ...], dict[str, Any]],
    right_rows: dict[tuple[Any, ...], dict[str, Any]],
) -> None:
    for column in columns:
        differing = 0
        max_absolute: float | None = None
        max_relative: float | None = None
        for key in common:
            differs, absolute, relative = _value_difference(
                left_rows[key][column], right_rows[key][column]
            )
            if differs:
                differing += 1
            if absolute is not None:
                max_absolute = (
                    absolute if max_absolute is None else max(max_absolute, absolute)
                )
            if relative is not None:
                max_relative = (
                    relative if max_relative is None else max(max_relative, relative)
                )
        absolute_text = "n/a" if max_absolute is None else str(max_absolute)
        relative_text = "n/a" if max_relative is None else str(max_relative)
        print(
            f"  {column}: differing={differing} max_abs={absolute_text} "
            f"max_rel={relative_text} (informational, not gated)"
        )


def compare_exact_table(
    left: Path,
    right: Path,
    dataset: str,
    schema: dict[str, Any],
    start: datetime,
    end: datetime,
    underlying: str | None,
) -> bool:
    """Table B or D: every non-key column is held to byte-equality."""
    left_rows = _rows(left, dataset, schema, start, end, underlying)
    right_rows = _rows(right, dataset, schema, start, end, underlying)
    left_keys, right_keys = set(left_rows), set(right_rows)
    common = left_keys & right_keys
    coverage_ok = _print_coverage(dataset, left_keys, right_keys, common)

    key_columns = {"minute"} if dataset == SPOT_DATASET else {"symbol", "minute"}
    columns = tuple(column for column in schema if column not in key_columns)
    exact_ok = _print_exact_columns(columns, common, left_rows, right_rows)
    return coverage_ok and exact_ok


def compare_quote_table(
    left: Path,
    right: Path,
    start: datetime,
    end: datetime,
    underlying: str | None,
) -> bool:
    """Table A: identity exact, ticks by conservation, everything else informational."""
    dataset, schema = DATASET, SCHEMA
    left_rows = _rows(left, dataset, schema, start, end, underlying)
    right_rows = _rows(right, dataset, schema, start, end, underlying)
    left_keys, right_keys = set(left_rows), set(right_rows)
    common = left_keys & right_keys
    coverage_ok = _print_coverage(dataset, left_keys, right_keys, common)

    exact_ok = _print_exact_columns(QUOTE_EXACT_COLUMNS, common, left_rows, right_rows)

    tick_ok = True
    for column in QUOTE_TICK_COLUMNS:
        rows_outside_bound = 0
        max_row_diff = 0
        left_total = 0
        right_total = 0
        for key in common:
            left_value = int(left_rows[key][column])
            right_value = int(right_rows[key][column])
            left_total += left_value
            right_total += right_value
            diff = abs(right_value - left_value)
            max_row_diff = max(max_row_diff, diff)
            if diff > QUOTE_TICK_ROW_TOLERANCE:
                rows_outside_bound += 1
        net = right_total - left_total
        row_bound_ok = rows_outside_bound == 0
        balance_ok = net == 0
        status = "conserved" if row_bound_ok and balance_ok else "NOT CONSERVED"
        print(
            f"  {column}: rows={len(common)} max_row_diff={max_row_diff} "
            f"rows_outside_bound={rows_outside_bound} left_sum={left_total} "
            f"right_sum={right_total} net={net} ({status})"
        )
        if not row_bound_ok:
            print(
                f"  {column}: per-row tick difference exceeds "
                f"{QUOTE_TICK_ROW_TOLERANCE} on {rows_outside_bound} row(s)"
            )
        if not balance_ok:
            print(
                f"  {column}: tick sum does not balance across the window "
                f"({left_total} left vs {right_total} right, net {net})"
            )
        tick_ok = tick_ok and row_bound_ok and balance_ok

    informational = [
        column
        for column in schema
        if column not in {"symbol", "minute", *QUOTE_EXACT_COLUMNS, *QUOTE_TICK_COLUMNS}
    ]
    _print_informational_columns(informational, common, left_rows, right_rows)

    return coverage_ok and exact_ok and tick_ok


def _theta_differs(left: Any, right: Any) -> tuple[bool, float | None, float | None]:
    if left is None or right is None:
        return left != right, None, None
    absolute = abs(float(left) - float(right))
    magnitude = max(abs(float(left)), abs(float(right)))
    relative = 0.0 if magnitude == 0.0 else absolute / magnitude
    if absolute <= COMPUTED_THETA_ABS_TOLERANCE:
        return False, absolute, relative
    if (
        magnitude > COMPUTED_THETA_REL_MAGNITUDE_FLOOR
        and relative <= COMPUTED_THETA_REL_TOLERANCE
    ):
        return False, absolute, relative
    return True, absolute, relative


def compare_computed_table(
    left: Path,
    right: Path,
    start: datetime,
    end: datetime,
    underlying: str | None,
) -> bool:
    """Table C: identity exact, theta by envelope, everything else informational."""
    dataset, schema = COMPUTED_DATASET, COMPUTED_SCHEMA
    left_rows = _rows(left, dataset, schema, start, end, underlying)
    right_rows = _rows(right, dataset, schema, start, end, underlying)
    left_keys, right_keys = set(left_rows), set(right_rows)
    common = left_keys & right_keys
    coverage_ok = _print_coverage(dataset, left_keys, right_keys, common)

    exact_ok = _print_exact_columns(
        COMPUTED_EXACT_COLUMNS, common, left_rows, right_rows
    )

    outside_envelope = 0
    max_absolute: float | None = None
    max_relative: float | None = None
    for key in common:
        differs, absolute, relative = _theta_differs(
            left_rows[key][COMPUTED_THETA_COLUMN], right_rows[key][COMPUTED_THETA_COLUMN]
        )
        if differs:
            outside_envelope += 1
        if absolute is not None:
            max_absolute = (
                absolute if max_absolute is None else max(max_absolute, absolute)
            )
        if relative is not None:
            max_relative = (
                relative if max_relative is None else max(max_relative, relative)
            )
    absolute_text = "n/a" if max_absolute is None else str(max_absolute)
    relative_text = "n/a" if max_relative is None else str(max_relative)
    theta_status = "inside envelope" if outside_envelope == 0 else "OUTSIDE ENVELOPE"
    print(
        f"  theta: outside_envelope={outside_envelope} max_abs={absolute_text} "
        f"max_rel={relative_text} ({theta_status}; abs<={COMPUTED_THETA_ABS_TOLERANCE} "
        f"or rel<={COMPUTED_THETA_REL_TOLERANCE} when |theta|>"
        f"{COMPUTED_THETA_REL_MAGNITUDE_FLOOR})"
    )
    theta_ok = outside_envelope == 0

    informational = [
        column
        for column in schema
        if column
        not in {"symbol", "minute", *COMPUTED_EXACT_COLUMNS, COMPUTED_THETA_COLUMN}
    ]
    _print_informational_columns(informational, common, left_rows, right_rows)

    return coverage_ok and exact_ok and theta_ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--left", required=True, type=Path)
    parser.add_argument("--right", required=True, type=Path)
    parser.add_argument("--start", required=True, type=_parse_minute)
    parser.add_argument("--end", required=True, type=_parse_minute)
    parser.add_argument("--underlying")
    parser.add_argument(
        "--table",
        choices=(DATASET, REFERENCE_DATASET, SPOT_DATASET, COMPUTED_DATASET),
    )
    parser.add_argument("--now", type=_parse_instant, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.end <= args.start:
        parser.error("--end must be after --start")

    now = args.now if args.now is not None else datetime.now(timezone.utc)
    refusal = _refuse_if_window_not_flushed(args.end, now=now)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2

    selected = (
        (DATASET, REFERENCE_DATASET, SPOT_DATASET, COMPUTED_DATASET)
        if args.table is None
        else (args.table,)
    )
    inside = True
    for dataset in selected:
        if dataset == DATASET:
            ok = compare_quote_table(
                args.left, args.right, args.start, args.end, args.underlying
            )
        elif dataset == COMPUTED_DATASET:
            ok = compare_computed_table(
                args.left, args.right, args.start, args.end, args.underlying
            )
        else:
            schema = REFERENCE_SCHEMA if dataset == REFERENCE_DATASET else SPOT_SCHEMA
            ok = compare_exact_table(
                args.left,
                args.right,
                dataset,
                schema,
                args.start,
                args.end,
                args.underlying,
            )
        inside = ok and inside
    return 0 if inside else 1


if __name__ == "__main__":
    raise SystemExit(main())
