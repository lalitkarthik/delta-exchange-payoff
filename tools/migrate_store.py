"""Move the four store tables from date/underlying to underlying/date once.

The migration is deliberately a copy, verify, delete operation. It does not compact or
rewrite Parquet files, so an interrupted copy or verification leaves the old tree usable.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.store import all_stores, default_root  # noqa: E402


@dataclass(frozen=True, slots=True)
class Migration:
    dataset: str
    date: str
    underlying: str
    source: Path
    destination: Path


class MigrationError(RuntimeError):
    """The old tree is not safe to migrate, or a migration check failed."""


def _old_partition_day(path: Path) -> str:
    value = path.name.removeprefix("date=")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise MigrationError(f"malformed old date partition: {path}") from error
    if parsed.strftime("%Y-%m-%d") != value:
        raise MigrationError(f"malformed old date partition: {path}")
    return value


def _old_partition_underlying(path: Path) -> str:
    prefix, separator, value = path.name.partition("=")
    if prefix != "underlying" or not separator or not value:
        raise MigrationError(f"malformed old underlying partition: {path}")
    return value


def _discover(root: Path) -> list[Migration]:
    discovered: list[Migration] = []
    for store in all_stores(root):
        if not store.path.is_dir():
            continue
        for date_dir in sorted(store.path.glob("date=*")):
            if not date_dir.is_dir():
                raise MigrationError(f"old date partition is not a directory: {date_dir}")
            day = _old_partition_day(date_dir)
            children = sorted(date_dir.iterdir())
            for underlying_dir in children:
                if not underlying_dir.is_dir():
                    raise MigrationError(
                        f"unexpected entry in old date partition: {underlying_dir}"
                    )
                underlying = _old_partition_underlying(underlying_dir)
                for entry in sorted(underlying_dir.iterdir()):
                    if entry.is_dir():
                        raise MigrationError(f"nested entry in old partition: {entry}")
                    if not entry.is_file() or entry.suffix != ".parquet":
                        raise MigrationError(
                            f"non-Parquet entry in old partition: {entry}"
                        )
                    destination = (
                        store.path
                        / f"underlying={underlying}"
                        / f"date={day}"
                        / entry.name
                    )
                    discovered.append(
                        Migration(
                            dataset=store.dataset,
                            date=day,
                            underlying=underlying,
                            source=entry,
                            destination=destination,
                        )
                    )
    return discovered


def _preflight(discovered: list[Migration]) -> set[Path]:
    destinations: set[str] = set()
    already_copied: set[Path] = set()
    for migration in discovered:
        key = os.path.normcase(os.path.abspath(migration.destination))
        if key in destinations:
            raise MigrationError(f"destination already exists: {migration.destination}")
        destinations.add(key)
        if not os.path.lexists(migration.destination):
            continue
        if migration.destination.is_file() and filecmp.cmp(
            migration.source, migration.destination, shallow=False
        ):
            already_copied.add(migration.destination)
            continue
        raise MigrationError(f"destination already exists: {migration.destination}")
    return already_copied


def _partitions(
    migrations: list[Migration],
) -> dict[tuple[str, str, str], list[Migration]]:
    grouped: dict[tuple[str, str, str], list[Migration]] = defaultdict(list)
    for migration in migrations:
        grouped[
            (migration.dataset, migration.date, migration.underlying)
        ].append(migration)
    return {
        key: sorted(value, key=lambda item: item.source.name)
        for key, value in grouped.items()
    }


def _verify_partition(migrations: list[Migration]) -> None:
    sources = [migration.source for migration in migrations]
    destinations = [migration.destination for migration in migrations]
    source_frame = pl.read_parquet(sources, hive_partitioning=False)
    destination_frame = pl.read_parquet(destinations, hive_partitioning=False)
    if source_frame.height != destination_frame.height:
        raise MigrationError(
            f"row count differs for {migrations[0].dataset}/date={migrations[0].date}/"
            f"underlying={migrations[0].underlying}: "
            f"{source_frame.height} != {destination_frame.height}"
        )


def _verify(migrations: list[Migration]) -> None:
    destination_count = sum(
        migration.destination.is_file() for migration in migrations
    )
    if len(migrations) != destination_count:
        raise MigrationError(
            f"source/destination file count differs: {len(migrations)} != "
            f"{destination_count}"
        )
    for migration in migrations:
        if not filecmp.cmp(migration.source, migration.destination, shallow=False):
            raise MigrationError(
                f"source and destination bytes differ: {migration.source} -> "
                f"{migration.destination}"
            )
    for partition in _partitions(migrations).values():
        _verify_partition(partition)


def _cleanup_destinations(
    migrations: list[Migration], directories: set[Path]
) -> None:
    for migration in migrations:
        migration.destination.unlink(missing_ok=True)
    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass


def _delete_sources(migrations: list[Migration]) -> None:
    for migration in migrations:
        migration.source.unlink()

    for partition in _partitions(migrations):
        dataset, day, underlying = partition
        root = migrations[0].source.parents[3]
        old_underlying = (
            root / dataset / f"date={day}" / f"underlying={underlying}"
        )
        if old_underlying.is_dir() and not any(old_underlying.iterdir()):
            old_underlying.rmdir()
        old_date = old_underlying.parent
        if old_date.is_dir() and not any(old_date.iterdir()):
            old_date.rmdir()


def _print_totals(migrations: list[Migration], already_copied: int = 0) -> None:
    print(f"files: {len(migrations)}")
    print(f"partitions: {len(_partitions(migrations))}")
    print(
        f"total bytes: {sum(migration.source.stat().st_size for migration in migrations)}"
    )
    print(f"already copied: {already_copied} files")


def _print_phase(name: str, started: float) -> None:
    print(f"phase {name}: {time.perf_counter() - started:.6f}s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="store root (default: configured store root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the migration without changing the filesystem",
    )
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else default_root()
    if not root.is_dir():
        print(f"error: store root does not exist: {root}", file=sys.stderr)
        return 2

    discovery_started = time.perf_counter()
    try:
        migrations = _discover(root)
        already_copied = _preflight(migrations)
    except Exception as error:
        _print_phase("discovery+preflight", discovery_started)
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print_phase("discovery+preflight", discovery_started)

    if not migrations:
        print("nothing to migrate")
        _print_totals(migrations, len(already_copied))
        return 0

    _print_totals(migrations, len(already_copied))
    if args.dry_run:
        for migration in migrations:
            print(
                f"  {migration.source.relative_to(root)} -> "
                f"{migration.destination.relative_to(root)}"
            )
        return 0

    created: list[Migration] = []
    created_directories: set[Path] = set()
    copy_started = time.perf_counter()
    try:
        for migration in migrations:
            parent = migration.destination.parent
            missing_directories: list[Path] = []
            while not parent.exists():
                missing_directories.append(parent)
                parent = parent.parent
            created_directories.update(missing_directories)
            migration.destination.parent.mkdir(parents=True, exist_ok=True)
            if migration.destination in already_copied:
                continue
            created.append(migration)
            shutil.copyfile(migration.source, migration.destination)
    except Exception as error:
        _print_phase("copy", copy_started)
        _cleanup_destinations(created, created_directories)
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print_phase("copy", copy_started)

    verify_started = time.perf_counter()
    try:
        _verify(migrations)
    except Exception as error:
        _print_phase("verify", verify_started)
        _cleanup_destinations(created, created_directories)
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print_phase("verify", verify_started)

    delete_started = time.perf_counter()
    try:
        _delete_sources(migrations)
    except Exception as error:
        _print_phase("delete", delete_started)
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print_phase("delete", delete_started)
    print(
        f"migrated {len(migrations)} files across "
        f"{len(_partitions(migrations))} partitions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
