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
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_compare_store_runs_identical_trees_are_inside_tolerance(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)

    result = _compare_command(left, right)

    assert result.returncode == 0
    assert "quote-bars" in result.stdout
    assert "common=2" in result.stdout


def test_compare_store_runs_reports_a_value_difference_and_exits_one(
    tmp_path: Path,
) -> None:
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

    assert result.returncode == 1
    assert "bid_open" in result.stdout
    assert "differing=1" in result.stdout
    assert "1.5" in result.stdout


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
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_fixture(left)
    _write_fixture(right)
    path = next((left / "quote-bars").rglob("*.parquet"))
    frame = pl.read_parquet(path).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("bid_open")
    )
    frame.write_parquet(path)

    result = _compare_command(left, right, "--table", "quote-bars")

    assert result.returncode == 1
    assert "bid_open" in result.stdout
    assert "differing=1" in result.stdout
