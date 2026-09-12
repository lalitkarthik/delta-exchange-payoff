"""Compare two store roots over the same UTC minute window.

The command is deliberately read-only and requires both roots explicitly:

    python tools/compare_store_runs.py --left data/monolith --right data/split \
        --start 2026-09-12T10:00:00Z --end 2026-09-12T11:00:00Z
    python tools/compare_store_runs.py --left run-a --right run-b \
        --start 2026-09-12T10:00:00Z --end 2026-09-12T11:00:00Z \
        --underlying BTC --table computed-bars

Tables A, B and D fold the same events in both processes, so VALUE_ATOL and
VALUE_RTOL are zero: a float published as JSON round-trips exactly and the store
performs no arithmetic of its own. Table C should also have identical values because
both processes use the same second-resolution fetched_at. Its split-mode grace is
2.0 seconds while the monolith's is 0.0, though, so a sample crossing a minute edge
may be present in only one run. That is a coverage difference, not a value difference.
MINUTE_COVERAGE is the assumed allowed number of differing minute keys; the live
side-by-side run must replace it with its measured value and date/window.

The end of the requested window is exclusive. A non-zero exit status means a coverage
or value difference is outside the module tolerances.
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

from deltapayoff.store import (  # noqa: E402
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    DATASET,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
)

VALUE_ATOL = 0.0
VALUE_RTOL = 0.0
#: assumed; replace with the measured value from the orchestrator's side-by-side run,
#: and record its date and comparison window.
MINUTE_COVERAGE = 0.0

TABLES: tuple[tuple[str, dict[str, Any]], ...] = (
    (DATASET, SCHEMA),
    (REFERENCE_DATASET, REFERENCE_SCHEMA),
    (SPOT_DATASET, SPOT_SCHEMA),
    (COMPUTED_DATASET, COMPUTED_SCHEMA),
)


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
    if left is None or right is None:
        return left != right, None, None
    if _numeric(left) and _numeric(right):
        absolute = abs(float(left) - float(right))
        scale = max(abs(float(left)), abs(float(right)))
        relative = 0.0 if scale == 0.0 else absolute / scale
        differs = absolute > VALUE_ATOL + VALUE_RTOL * scale
        return differs, absolute, relative
    return left != right, None, None


def compare_table(
    left: Path,
    right: Path,
    dataset: str,
    schema: dict[str, Any],
    start: datetime,
    end: datetime,
    underlying: str | None,
) -> bool:
    left_rows = _rows(left, dataset, schema, start, end, underlying)
    right_rows = _rows(right, dataset, schema, start, end, underlying)
    left_keys = set(left_rows)
    right_keys = set(right_rows)
    common = left_keys & right_keys
    left_only = left_keys - right_keys
    right_only = right_keys - left_keys
    coverage = len(left_only) + len(right_only)
    coverage_outside = coverage > MINUTE_COVERAGE

    status = "outside" if coverage_outside else "inside"
    print(
        f"{dataset}: common={len(common)} left_only={len(left_only)} "
        f"right_only={len(right_only)} coverage={coverage} ({status})"
    )
    if coverage:
        print(f"  coverage difference: {coverage} minute keys")

    key_columns = {"minute"} if dataset == SPOT_DATASET else {"symbol", "minute"}
    columns = [column for column in schema if column not in key_columns]
    values_outside = False
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
                values_outside = True
            if absolute is not None:
                max_absolute = (
                    absolute
                    if max_absolute is None
                    else max(max_absolute, absolute)
                )
            if relative is not None:
                max_relative = (
                    relative
                    if max_relative is None
                    else max(max_relative, relative)
                )
        absolute_text = "n/a" if max_absolute is None else str(max_absolute)
        relative_text = "n/a" if max_relative is None else str(max_relative)
        print(
            f"  {column}: differing={differing} "
            f"max_abs={absolute_text} max_rel={relative_text}"
        )

    if values_outside:
        print("  value difference: outside VALUE_ATOL/RTOL")
    return not coverage_outside and not values_outside


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", required=True, type=Path)
    parser.add_argument("--right", required=True, type=Path)
    parser.add_argument("--start", required=True, type=_parse_minute)
    parser.add_argument("--end", required=True, type=_parse_minute)
    parser.add_argument("--underlying")
    parser.add_argument("--table", choices=[name for name, _schema in TABLES])
    args = parser.parse_args()
    if args.end <= args.start:
        parser.error("--end must be after --start")

    selected = (
        TABLES
        if args.table is None
        else tuple(item for item in TABLES if item[0] == args.table)
    )
    inside = True
    for dataset, schema in selected:
        inside = (
            compare_table(
                args.left,
                args.right,
                dataset,
                schema,
                args.start,
                args.end,
                args.underlying,
            )
            and inside
        )
    return 0 if inside else 1


if __name__ == "__main__":
    raise SystemExit(main())
