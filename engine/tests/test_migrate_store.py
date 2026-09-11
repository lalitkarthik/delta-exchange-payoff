"""One-shot migration from the old date-first store tree to underlying/date."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from deltapayoff.store import all_stores

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _row(schema: dict[str, Any], underlying: str, day: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, dtype in schema.items():
        dtype_name = str(dtype)
        if dtype_name == "Categorical":
            values[name] = {
                "symbol": f"C-{underlying}-77600-030926",
                "expiry": "03-09-2026",
                "option_type": "C",
                "iv_leg": "call",
                "iv_reason": "reason",
                "forward_method": "F1",
                "model_version": "model",
            }.get(name, "value")
        elif dtype_name.startswith("Datetime"):
            values[name] = datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        elif dtype_name == "UInt32":
            values[name] = 1
        elif dtype_name == "Boolean":
            values[name] = True
        else:
            values[name] = 1.25
    return values


def _write_old_file(
    root: Path,
    dataset: str,
    schema: dict[str, Any],
    day: str,
    underlying: str,
    name: str,
    *,
    rows: int = 1,
) -> Path:
    path = root / dataset / f"date={day}" / f"underlying={underlying}" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        [_row(schema, underlying, day) for _ in range(rows)], schema=schema
    )
    frame.write_parquet(path)
    return path


def _old_tree(root: Path) -> list[tuple[str, str, str, Path]]:
    files = []
    for store in all_stores(root):
        for day in ("2026-09-03", "2026-09-04"):
            for underlying in ("BTC", "ETH"):
                files.append(
                    (
                        store.dataset,
                        day,
                        underlying,
                        _write_old_file(
                            root,
                            store.dataset,
                            store.schema,
                            day,
                            underlying,
                            f"{day.replace('-', '')}-{underlying.lower()}-001.parquet",
                            rows=2 if day == "2026-09-03" and underlying == "BTC" else 1,
                        ),
                    )
                )
        files.append(
            (
                store.dataset,
                "2026-09-03",
                "BTC",
                _write_old_file(
                    root,
                    store.dataset,
                    store.schema,
                    "2026-09-03",
                    "BTC",
                    "20260903-btc-002.parquet",
                ),
            )
        )
    return files


def _snapshot(root: Path) -> dict[str, bytes | None]:
    snapshot = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = None if path.is_dir() else path.read_bytes()
    return snapshot


def _destination(record: tuple[str, str, str, Path]) -> Path:
    dataset, day, underlying, source = record
    return (
        source.parents[3]
        / dataset
        / f"underlying={underlying}"
        / f"date={day}"
        / source.name
    )


def _run(argv: list[str]) -> int:
    from migrate_store import main

    return main(argv)


def _run_script(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(TOOLS / "migrate_store.py"),
            "--root",
            str(root),
            *arguments,
        ],
        cwd=TOOLS.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_entrypoint_migrates_files_and_is_a_resumable_no_op(
    tmp_path: Path,
) -> None:
    records = [
        (
            store.dataset,
            "2026-09-04",
            underlying,
            _write_old_file(
                tmp_path,
                store.dataset,
                store.schema,
                "2026-09-04",
                underlying,
                f"20260904-{underlying.lower()}-001.parquet",
            ),
        )
        for store, underlying in zip(
            all_stores(tmp_path), ("BTC", "ETH", "BTC", "ETH"), strict=True
        )
    ]
    original_bytes = {source: source.read_bytes() for *_, source in records}

    dry_run = _run_script(tmp_path, "--dry-run")

    assert dry_run.returncode == 0, dry_run.stderr
    dry_output = dry_run.stdout.replace("\\", "/")
    for record in records:
        destination = _destination(record)
        assert (
            f"{record[3].relative_to(tmp_path).as_posix()} -> "
            f"{destination.relative_to(tmp_path).as_posix()}"
        ) in dry_output
        assert record[3].exists()
        assert not destination.exists()

    migrated = _run_script(tmp_path)

    assert migrated.returncode == 0, migrated.stderr
    for record in records:
        destination = _destination(record)
        assert not record[3].exists()
        assert destination.read_bytes() == original_bytes[record[3]]

    second_run = _run_script(tmp_path)

    assert second_run.returncode == 0, second_run.stderr
    assert "nothing to migrate" in second_run.stdout
    missing_root = tmp_path / "missing-root"
    missing = _run_script(missing_root, "--dry-run")
    assert missing.returncode == 2
    assert missing.stderr.strip() == (
        f"error: store root does not exist: {missing_root}"
    )


def test_missing_store_root_returns_error_before_discovery(
    tmp_path: Path, capsys
) -> None:
    missing_root = tmp_path / "missing-root"

    assert _run(["--root", str(missing_root), "--dry-run"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        f"error: store root does not exist: {missing_root}"
    )


def test_dry_run_lists_every_old_to_new_mapping_without_changing_files(
    tmp_path: Path, capsys
) -> None:
    records = _old_tree(tmp_path)
    before = _snapshot(tmp_path)

    assert _run(["--root", str(tmp_path), "--dry-run"]) == 0

    output = capsys.readouterr().out.replace("\\", "/")
    for dataset, day, underlying, source in records:
        destination = _destination((dataset, day, underlying, source))
        assert (
            f"{source.relative_to(tmp_path).as_posix()} -> "
            f"{destination.relative_to(tmp_path).as_posix()}"
        ) in output
    assert "files: 20" in output
    assert "partitions: 16" in output
    assert "total bytes:" in output
    assert "already copied: 0 files" in output
    assert "phase discovery+preflight:" in output
    assert "phase copy:" not in output
    assert _snapshot(tmp_path) == before


def test_successful_migration_preserves_files_bytes_rows_and_removes_old_tree(
    tmp_path: Path, capsys
) -> None:
    records = _old_tree(tmp_path)
    before = {source: source.read_bytes() for _, _, _, source in records}
    expected_rows: dict[tuple[str, str, str], int] = {}
    for dataset, day, underlying, source in records:
        key = (dataset, day, underlying)
        expected_rows[key] = expected_rows.get(key, 0) + pl.read_parquet(
            source, hive_partitioning=False
        ).height

    assert _run(["--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "files: 20" in output
    assert "partitions: 16" in output
    assert "already copied: 0 files" in output
    assert "phase discovery+preflight:" in output
    assert "phase copy:" in output
    assert "phase verify:" in output
    assert "phase delete:" in output
    for dataset, day, underlying, source in records:
        destination = _destination((dataset, day, underlying, source))
        assert not source.exists()
        assert destination.name == source.name
        assert destination.read_bytes() == before[source]
    for store in all_stores(tmp_path):
        assert not list(store.path.glob("date=*"))
        assert not any(child.name.startswith("date=") for child in store.path.iterdir())
    for key, rows in expected_rows.items():
        dataset, day, underlying = key
        destination = (
            tmp_path
            / dataset
            / f"underlying={underlying}"
            / f"date={day}"
        )
        assert pl.read_parquet(
            list(destination.glob("*.parquet")), hive_partitioning=False
        ).height == rows


def test_completed_migration_is_a_no_op(tmp_path: Path, capsys) -> None:
    _old_tree(tmp_path)
    assert _run(["--root", str(tmp_path)]) == 0
    after_first = _snapshot(tmp_path)
    capsys.readouterr()

    assert _run(["--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "nothing to migrate" in output
    assert _snapshot(tmp_path) == after_first


def test_identical_destination_is_resumed_and_only_remaining_files_are_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    records = _old_tree(tmp_path)
    already_copied = records[:3]
    for record in already_copied:
        destination = _destination(record)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(record[3].read_bytes())

    import migrate_store

    copied: list[tuple[Path, Path]] = []
    copyfile = migrate_store.shutil.copyfile

    def record_copy(source: str | os.PathLike[str], destination: str | os.PathLike[str]):
        copied.append((Path(source), Path(destination)))
        return copyfile(source, destination)

    monkeypatch.setattr(migrate_store.shutil, "copyfile", record_copy)

    assert _run(["--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "already copied: 3 files" in output
    assert len(copied) == len(records) - len(already_copied)
    assert {source for source, _ in copied} == {
        record[3] for record in records[len(already_copied) :]
    }
    assert all(not source.exists() for _, _, _, source in records)


def test_different_destination_aborts_preflight_without_touching_sources(
    tmp_path: Path, capsys
) -> None:
    records = _old_tree(tmp_path)
    first = records[0]
    destination = _destination(first)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(first[3].read_bytes() + b"different")
    before = _snapshot(tmp_path)

    assert _run(["--root", str(tmp_path)]) == 2

    assert "error:" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    assert first[3].exists()


@pytest.mark.parametrize("kind", ["bad-date", "non-parquet", "nested"])
def test_malformed_old_layout_aborts_before_copying(
    tmp_path: Path, capsys, kind: str
) -> None:
    store = all_stores(tmp_path)[0]
    if kind == "bad-date":
        path = store.path / "date=not-a-date" / "underlying=BTC" / "bad.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(schema=store.schema).write_parquet(path)
    elif kind == "non-parquet":
        path = store.path / "date=2026-09-03" / "underlying=BTC" / "notes.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not parquet", encoding="utf-8")
    else:
        path = (
            store.path
            / "date=2026-09-03"
            / "underlying=BTC"
            / "nested"
            / "file.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(schema=store.schema).write_parquet(path)
    before = _snapshot(tmp_path)

    assert _run(["--root", str(tmp_path)]) == 2

    assert "error:" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    assert path.exists()


def test_verification_failure_removes_only_this_run_destinations_and_keeps_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    records = _old_tree(tmp_path)
    import migrate_store

    def fail(*args, **kwargs):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(migrate_store, "_verify_partition", fail)

    assert _run(["--root", str(tmp_path)]) == 2

    assert "error:" in capsys.readouterr().err
    assert all(source.exists() for _, _, _, source in records)
    assert all(not _destination(record).exists() for record in records)
    assert all(
        not list(store.path.glob("underlying=*/date=*"))
        for store in all_stores(tmp_path)
    )


def test_verification_failure_keeps_directory_with_already_copied_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    records = _old_tree(tmp_path)
    already_copied = records[0]
    existing_destination = _destination(already_copied)
    existing_destination.parent.mkdir(parents=True, exist_ok=True)
    existing_destination.write_bytes(already_copied[3].read_bytes())
    import migrate_store

    def fail(*args, **kwargs):
        raise RuntimeError("injected verification failure")

    monkeypatch.setattr(migrate_store, "_verify_partition", fail)

    assert _run(["--root", str(tmp_path)]) == 2

    assert "error:" in capsys.readouterr().err
    assert existing_destination.exists()
    assert existing_destination.parent.is_dir()
    assert all(
        not _destination(record).exists()
        for record in records
        if record is not already_copied
    )
    new_partition_dirs = [
        path
        for store in all_stores(tmp_path)
        for path in store.path.glob("underlying=*/date=*")
    ]
    assert new_partition_dirs == [existing_destination.parent]


def test_delete_failure_leaves_a_resumable_already_copied_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = all_stores(tmp_path)[0]
    records = [
        (
            store.dataset,
            "2026-09-04",
            "BTC",
            _write_old_file(
                tmp_path,
                store.dataset,
                store.schema,
                "2026-09-04",
                "BTC",
                name,
            ),
        )
        for name in ("20260904-btc-001.parquet", "20260904-btc-002.parquet")
    ]
    original_bytes = {source: source.read_bytes() for *_, source in records}
    source_paths = {source for *_, source in records}
    original_unlink = Path.unlink
    source_unlinks = 0

    def fail_second_source_unlink(path: Path, *args, **kwargs):
        nonlocal source_unlinks
        if path in source_paths:
            source_unlinks += 1
            if source_unlinks == 2:
                raise PermissionError("injected file lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second_source_unlink)

    assert _run(["--root", str(tmp_path)]) == 2

    first_error = capsys.readouterr().err
    assert "injected file lock" in first_error
    assert not records[0][3].exists()
    assert records[1][3].exists()
    assert all(
        _destination(record).read_bytes() == original_bytes[record[3]]
        for record in records
    )

    assert _run(["--root", str(tmp_path)]) == 0

    second_output = capsys.readouterr().out
    assert "already copied: 1 files" in second_output
    assert all(not source.exists() for *_, source in records)
    assert all(
        _destination(record).read_bytes() == original_bytes[record[3]]
        for record in records
    )
    assert not list(store.path.glob("date=*"))
    assert list(store.path.glob("underlying=*/date=*")) == [
        _destination(records[0]).parent
    ]
