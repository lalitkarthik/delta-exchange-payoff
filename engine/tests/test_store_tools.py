"""Store maintenance tools against a temporary underlying-first tree."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from deltapayoff.store import all_stores

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from compact_store import main as compact_store_main  # noqa: E402
from measure_computed_gaps import minutes  # noqa: E402
from measure_open_partition import census, file_counts, flush_files  # noqa: E402
from measure_store import fan_out_dates, synthesise_day  # noqa: E402
from measure_store_cloud import report_compaction_cost, report_partitions  # noqa: E402
from probe_index_history import _spot_bars  # noqa: E402

DAY = "2026-09-03"
UNDERLYING = "BTC"
MINUTE = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)


def _row(schema: dict[str, Any], underlying: str, minute: datetime) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, dtype in schema.items():
        dtype_name = str(dtype)
        if dtype_name == "Categorical":
            values[name] = {
                "symbol": f"C-{underlying}-77600-040926",
                "expiry": "04-09-2026",
                "option_type": "C",
                "iv_leg": "call",
                "iv_reason": "reason",
                "forward_method": "F1",
                "model_version": "model",
            }.get(name, "value")
        elif dtype_name.startswith("Datetime"):
            values[name] = minute
        elif dtype_name == "UInt32":
            values[name] = 1
        elif dtype_name == "Boolean":
            values[name] = True
        else:
            values[name] = 1.25
    return values


def _write_fixture(root: Path) -> None:
    for store in all_stores(root):
        directory = store.path / f"underlying={UNDERLYING}" / f"date={DAY}"
        directory.mkdir(parents=True, exist_ok=True)
        for index, offset in enumerate((timedelta(hours=10), timedelta(hours=9)), 1):
            frame = pl.DataFrame(
                [_row(store.schema, UNDERLYING, MINUTE + offset)], schema=store.schema
            )
            frame.write_parquet(
                directory / f"{(MINUTE + offset):%Y%m%dT%H%M%SZ}-{index:06d}.parquet"
            )


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def test_compact_store_dry_run_reports_each_new_layout_partition_without_modifying_it(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _write_fixture(tmp_path)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compact_store.py",
            "--root",
            str(tmp_path),
            "--before",
            "2026-09-04",
            "--dry-run",
        ],
    )

    assert compact_store_main() == 0

    output = capsys.readouterr().out
    for store in all_stores(tmp_path):
        assert f"{store.dataset:16} {DAY} BTC" in output
    assert output.count("2 files") == 4
    assert _snapshot(tmp_path) == before


def test_open_partition_tools_list_new_layout_and_parse_filenames_chronologically(
    tmp_path: Path, capsys
) -> None:
    _write_fixture(tmp_path)

    files = flush_files(tmp_path, "quote-bars", DAY, UNDERLYING)
    assert [stamp for stamp, _ in files] == [
        datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 3, 19, 0, tzinfo=timezone.utc),
    ]
    assert all(
        path.parent == tmp_path / "quote-bars" / "underlying=BTC" / f"date={DAY}"
        for _, path in files
    )

    found = census(tmp_path)
    output = capsys.readouterr().out
    assert set(found) == {(DAY, UNDERLYING)}
    assert all(
        counts[store.dataset] == 2
        for counts in found.values()
        for store in all_stores(tmp_path)
    )
    assert file_counts(tmp_path, DAY, UNDERLYING) == "2/2/2/2"
    assert output.count(DAY) >= 1


def test_measure_store_scratch_layout_and_fan_out_use_underlying_first_paths(
    tmp_path: Path
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    _write_fixture(source)

    synthesise_day(source, work, hours=2)
    fan_out_dates(work, 3)

    expected = {
        ("2026-09-03", "BTC"),
        ("2026-09-03", "ETH"),
        ("2026-09-02", "BTC"),
        ("2026-09-02", "ETH"),
        ("2026-09-01", "BTC"),
        ("2026-09-01", "ETH"),
    }
    for store in all_stores(work):
        assert set(store.partitions()) == expected
        assert all(
            (store.path / f"underlying={underlying}" / f"date={day}").is_dir()
            for day, underlying in expected
        )
        assert not list(store.path.glob("date=*"))


def test_cloud_reports_count_files_from_new_layout(tmp_path: Path, capsys) -> None:
    _write_fixture(tmp_path)

    report_partitions(tmp_path, DAY)
    partitions_output = capsys.readouterr().out
    report_compaction_cost(tmp_path, DAY)
    compaction_output = capsys.readouterr().out

    assert partitions_output.count(DAY) == 4
    assert partitions_output.count("       2") >= 4
    assert compaction_output.count("       2") >= 4
    assert "8" in compaction_output


def test_recursive_hive_readers_find_the_same_new_layout_fixture(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    assert minutes(tmp_path, "quote-bars", "04-09-2026", DAY) == {
        MINUTE + timedelta(hours=9),
        MINUTE + timedelta(hours=10),
    }
    assert minutes(tmp_path, "computed-bars", "04-09-2026", DAY) == {
        MINUTE + timedelta(hours=9),
        MINUTE + timedelta(hours=10),
    }

    spot = _spot_bars(tmp_path)
    assert spot is not None
    assert spot.height == 2
    assert set(spot["underlying"].to_list()) == {UNDERLYING}
    assert set(spot["date"].to_list()) == {datetime(2026, 9, 3).date()}


def _compare_command(
    left: Path, right: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    tool = TOOLS / "compare_store_runs.py"
    return subprocess.run(
        [
            sys.executable,
            str(tool),
            "--left",
            str(left),
            "--right",
            str(right),
            "--start",
            "2026-09-03T18:00:00Z",
            "--end",
            "2026-09-03T20:00:00Z",
            # Fixture dates are years behind the real clock, but pin --now anyway so
            # this suite's result never depends on when it happens to run.
            "--now",
            "2026-09-12T00:00:00+00:00",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _quote_tick_files(root: Path) -> list[Path]:
    return sorted((root / "quote-bars").rglob("*.parquet"))


def _bump_uint32_column(path: Path, column: str, delta: int) -> None:
    frame = pl.read_parquet(path).with_columns(
        (pl.col(column).cast(pl.Int64) + delta).cast(pl.UInt32).alias(column)
    )
    frame.write_parquet(path)


def test_compare_store_runs_identical_trees_are_inside_tolerance(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)

    result = _compare_command(left, right)

    assert result.returncode == 0
    assert "quote-bars" in result.stdout
    assert "common=2" in result.stdout


def test_compare_store_runs_separates_coverage_from_value_differences(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "quote-bars").rglob("*.parquet"))
    path.unlink()

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 1
    assert "left_only=1" in result.stdout
    assert "coverage difference" in result.stdout
    assert "value difference:" not in result.stdout


def test_compare_store_runs_treats_null_against_number_as_a_difference(
    tmp_path: Path,
) -> None:
    """Table B (`reference-bars`) is held exact, so a null-vs-number gap on one of its
    columns is exactly the kind of thing that must still fail -- unlike table A's prices,
    which #82 stopped gating because two live sockets never agree on them by construction.
    """
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((left / "reference-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("mark_open")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "reference-bars")

    assert result.returncode == 1
    assert "mark_open" in result.stdout
    assert "differing=1" in result.stdout


def test_compare_store_runs_table_b_reference_bars_value_difference_fails(
    tmp_path: Path,
) -> None:
    """#82 acceptance: tables B and D stay exact -- any value difference is a failure."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "reference-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        (pl.col("mark_open") + 1.0).alias("mark_open")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "reference-bars")

    assert result.returncode == 1
    assert "mark_open" in result.stdout
    assert "DIFFERS" in result.stdout


def test_compare_store_runs_table_d_spot_bars_value_difference_fails(
    tmp_path: Path,
) -> None:
    """#82 acceptance: tables B and D stay exact -- any value difference is a failure."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "spot-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        (pl.col("spot_open") + 1.0).alias("spot_open")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "spot-bars")

    assert result.returncode == 1
    assert "spot_open" in result.stdout
    assert "DIFFERS" in result.stdout


def test_compare_store_runs_table_a_identity_column_difference_fails(
    tmp_path: Path,
) -> None:
    """#82 acceptance: table A's identity/provenance columns stay exact."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = _quote_tick_files(right)[0]
    frame = pl.read_parquet(path).with_columns((~pl.col("from_book")).alias("from_book"))
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 1
    assert "from_book" in result.stdout
    assert "DIFFERS" in result.stdout


def test_compare_store_runs_table_a_price_jitter_is_informational_not_gated(
    tmp_path: Path,
) -> None:
    """The false premise #82 removes: two independent sockets never agree on a price bar
    by construction, so a difference there must be visible but must not fail the run."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "quote-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        (pl.col("bid_open") + 1.5).alias("bid_open")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 0, result.stdout
    assert "bid_open" in result.stdout
    assert "differing=1" in result.stdout
    assert "informational" in result.stdout


def test_compare_store_runs_table_a_dropped_ticks_break_the_row_bound(
    tmp_path: Path,
) -> None:
    """A bar that lost ticks: one row's count falls more than the measured ±1 socket
    jitter allows, and that must fail even though nothing else about the row moved.

    Bumps the *left* side up rather than the right side down by the same amount --
    the fixture's tick counts start at 1, and the tool must judge this by the
    difference between the two sides, not by which one happens to have the smaller
    absolute count (`UInt32` cannot hold a negative tick count either way)."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    _bump_uint32_column(_quote_tick_files(left)[0], "bid_ticks", 2)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 1
    assert "bid_ticks" in result.stdout
    assert "rows_outside_bound=1" in result.stdout


def test_compare_store_runs_table_a_duplicated_ticks_break_the_row_bound(
    tmp_path: Path,
) -> None:
    """A bar that double-counted ticks: caught by the same per-row bound, from the
    other direction."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    _bump_uint32_column(_quote_tick_files(right)[0], "bid_ticks", 2)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 1
    assert "bid_ticks" in result.stdout
    assert "rows_outside_bound=1" in result.stdout


def test_compare_store_runs_table_a_tick_sum_does_not_balance(tmp_path: Path) -> None:
    """A systemic one-tick bias every row would never trip the per-row bound -- each row
    is individually within jitter -- but the window's totals would not conserve. Only the
    net-sum check catches this, which is why it exists alongside the row bound."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    _bump_uint32_column(_quote_tick_files(right)[0], "bid_ticks", 1)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 1
    assert "rows_outside_bound=0" in result.stdout, (
        "this scenario must not be caught by the row bound"
    )
    assert "tick sum does not balance" in result.stdout


def test_compare_store_runs_table_a_balanced_jitter_is_inside_tolerance(
    tmp_path: Path,
) -> None:
    """The realistic case #82 exists for: one row favours each side by exactly the
    measured jitter bound, the window's totals still conserve, and the run is clean."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    files = _quote_tick_files(right)
    _bump_uint32_column(files[0], "bid_ticks", 1)
    _bump_uint32_column(files[1], "bid_ticks", -1)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 0, result.stdout
    assert "bid_ticks" in result.stdout
    assert "conserved" in result.stdout


def test_compare_store_runs_table_c_identity_column_difference_fails(
    tmp_path: Path,
) -> None:
    """#82 acceptance: table C's identity/provenance/model columns stay exact."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "computed-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        pl.lit("a-different-model", dtype=pl.Categorical).alias("model_version")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "computed-bars")

    assert result.returncode == 1
    assert "model_version" in result.stdout
    assert "DIFFERS" in result.stdout


def test_compare_store_runs_table_c_theta_small_magnitude_relative_blowup_is_ok(
    tmp_path: Path,
) -> None:
    """#82 evidence: near-zero theta inflates relative difference to something
    alarming (a `max_rel` of 1.85 was measured) while the absolute gap stays small
    and unremarkable. Relative difference must not gate a row this small."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "computed-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        pl.lit(-0.05, dtype=pl.Float64).alias("theta")
    )
    frame.write_parquet(path)
    # left's theta is the fixture default, 1.25; |theta| here never exceeds 1.25, and
    # the absolute gap (1.30) still exceeds ABS_TOLERANCE, so use a closer pair instead.

    path_left = next((left / "computed-bars").rglob("*.parquet"))
    frame_left = pl.read_parquet(path_left).with_columns(
        pl.lit(0.30, dtype=pl.Float64).alias("theta")
    )
    frame_left.write_parquet(path_left)

    result = _compare_command(left, right, "--table", "computed-bars")

    assert result.returncode == 0, result.stdout
    assert "theta" in result.stdout
    assert "inside envelope" in result.stdout


def test_compare_store_runs_table_c_theta_large_absolute_difference_fails(
    tmp_path: Path,
) -> None:
    """#82 evidence: the measured envelope's ceiling was an absolute gap of 1.000; a
    bigger absolute gap at a magnitude the relative gate actually applies to must fail."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "computed-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        pl.lit(3.25, dtype=pl.Float64).alias("theta")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "computed-bars")

    assert result.returncode == 1
    assert "theta" in result.stdout
    assert "OUTSIDE ENVELOPE" in result.stdout


def test_compare_store_runs_table_c_other_numeric_columns_are_informational_only(
    tmp_path: Path,
) -> None:
    """Only theta's envelope has been measured (#82); the rest of table C's Greeks are
    reported for visibility but must not fail the run until their own envelope exists."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((right / "computed-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        (pl.col("delta") + 50.0).alias("delta")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "computed-bars")

    assert result.returncode == 0, result.stdout
    assert "delta" in result.stdout
    assert "informational" in result.stdout


def test_compare_store_runs_refuses_a_window_too_recent_to_be_flushed(
    tmp_path: Path,
) -> None:
    """#82 acceptance: refuse rather than report unflushed minutes as missing coverage.

    Reproduces the exact shape of #82's read-time artefact -- minute 06:11 sealed at
    06:12:08Z and a comparison run made 2 m 51 s after the window's 06:12:00Z end, short
    of the measured 308 s (`FLUSH_SECONDS` 300 + `QUOTE_GRACE_SECONDS` 8) it needs.
    """
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    tool = TOOLS / "compare_store_runs.py"

    result = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--left",
            str(left),
            "--right",
            str(right),
            "--start",
            "2026-09-12T05:52:00Z",
            "--end",
            "2026-09-12T06:12:00Z",
            "--now",
            "2026-09-12T06:14:51Z",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing" in result.stderr
    assert "flushed to disk" in result.stderr


def test_compare_store_runs_proceeds_once_the_window_is_old_enough(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    tool = TOOLS / "compare_store_runs.py"

    result = subprocess.run(
        [
            sys.executable,
            str(tool),
            "--left",
            str(left),
            "--right",
            str(right),
            "--start",
            "2026-09-12T05:52:00Z",
            "--end",
            "2026-09-12T06:12:00Z",
            # exactly the measured threshold, 308 s, plus one second of slack
            "--now",
            "2026-09-12T06:17:09Z",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
