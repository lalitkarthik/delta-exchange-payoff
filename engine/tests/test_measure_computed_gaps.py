"""#104: the gap probe must not score a store that stopped better than one that ran.

`tools/measure_computed_gaps.py` grids from the first to the last minute it observed, so
a store that stops recording **shortens the grid instead of putting holes in it**. The
stall removes minutes from the numerator and the denominator together and the percentage
improves. These tests build stores in `tmp_path` — never the live ones — whose minutes
stop part-way through the day asked for, and pin that the run says so on its face.

No wall clock: every test hands `report()` the `now` it should measure against.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    DATASET,
    SCHEMA,
    BarStore,
)

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from measure_computed_gaps import report, source_last_minute  # noqa: E402

DAY = "2026-09-04"
EXPIRY = "25-09-2026"
OTHER_EXPIRY = "27-11-2026"
UNDERLYING = "BTC"

#: The wall clock the tests measure against: late enough that the whole of `DAY` is in
#: the past, so "the day asked for" is the whole 1,440 minutes for every test that does
#: not deliberately ask about a day still in progress.
AFTER_THE_DAY = datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)


def _row(schema: dict[str, Any], expiry: str, minute: datetime) -> dict[str, Any]:
    """One bar. Only `expiry` and `minute` are read by the probe; the rest is filler."""
    values: dict[str, Any] = {}
    for name, dtype in schema.items():
        dtype_name = str(dtype)
        if name == "expiry":
            values[name] = expiry
        elif dtype_name.startswith("Datetime"):
            values[name] = minute
        elif dtype_name == "Categorical":
            values[name] = "value"
        elif dtype_name == "UInt32":
            values[name] = 1
        elif dtype_name == "Boolean":
            values[name] = True
        else:
            values[name] = 1.25
    return values


def _write(root: Path, dataset: str, expiry: str, stamps: Iterable[datetime]) -> None:
    """Write one bar per minute for one expiry into the dataset's day partition."""
    stamps = sorted(stamps)
    if not stamps:
        return
    schema = SCHEMA if dataset == DATASET else COMPUTED_SCHEMA
    store = BarStore(root, dataset=dataset, schema=schema)
    directory = store.path / f"underlying={UNDERLYING}" / f"date={stamps[0]:%Y-%m-%d}"
    directory.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame([_row(schema, expiry, s) for s in stamps], schema=schema)
    frame.write_parquet(directory / f"{expiry}-{stamps[0]:%H%M}.parquet")


def _span(first: str, last: str) -> list[datetime]:
    """Every minute from `HH:MM` to `HH:MM` inclusive, on `DAY`."""
    start = datetime.strptime(f"{DAY} {first}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc
    )
    end = datetime.strptime(f"{DAY} {last}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc
    )
    out, stamp = [], start
    while stamp <= end:
        out.append(stamp)
        stamp += timedelta(minutes=1)
    return out


def _store_that_stopped(root: Path) -> None:
    """00:00 - 01:59 of a 1,440-minute day, with exactly one computed bar missing.

    This is #103's shape in miniature: the store stopped four minutes into the third
    hour of the day and every table stopped with it.
    """
    quoted = _span("00:00", "01:59")
    _write(root, DATASET, EXPIRY, quoted)
    _write(
        root,
        COMPUTED_DATASET,
        EXPIRY,
        [m for m in quoted if (m.hour, m.minute) != (0, 30)],
    )


def _store_that_ran(root: Path) -> None:
    """The whole day, with twenty-four computed bars missing. The healthy comparison.

    1 of 120 is 0.8%; 24 of 1,440 is 1.7%. The store that stopped scores twice as well,
    which is #103's eight-to-one in miniature and the reason this probe needed changing.
    """
    quoted = _span("00:00", "23:59")
    _write(root, DATASET, EXPIRY, quoted)
    _write(
        root,
        COMPUTED_DATASET,
        EXPIRY,
        [m for m in quoted if not (m.minute in (15, 30) and m.hour % 2 == 0)],
    )


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def _figure(lines: list[str], label: str) -> str:
    """The one line whose own label is `label`.

    Matched as a prefix of the stripped line rather than anywhere in it: "span examined"
    and "outside the span examined" are two different findings and a substring match
    would silently read the wrong one.
    """
    found = [line for line in lines if line.strip().lstrip("* ").startswith(label)]
    assert found, f"no line labelled {label!r} in:\n" + "\n".join(lines)
    assert len(found) == 1, f"{label!r} labels {len(found)} lines in:\n" + "\n".join(
        lines
    )
    return found[0]


def _count(line: str) -> int:
    """The first integer on a report line, which is always the figure it labels."""
    for word in line.split():
        if word.isdigit():
            return int(word)
    raise AssertionError(f"no count on: {line}")


def _percent(line: str) -> float:
    """The first percentage on a report line."""
    for word in line.split():
        if word.endswith("%"):
            return float(word[:-1])
    raise AssertionError(f"no percentage on: {line}")


def test_a_store_that_stops_part_way_through_the_day_reports_the_span_it_was_asked_for(
    tmp_path: Path, capsys
) -> None:
    """Criterion 1. `--date` asks for a day; 00:00 - 01:59 is a finding, not a heading."""
    _store_that_stopped(tmp_path)

    report(tmp_path, EXPIRY, DAY, now=AFTER_THE_DAY, source_last=None)

    lines = _lines(capsys)
    asked = _figure(lines, "span asked for")
    examined = _figure(lines, "span examined")
    assert "00:00 - 23:59" in asked and _count(asked) == 1440
    assert "00:00 - 01:59" in examined and _count(examined) == 120
    assert any("SHORT SPAN" in line for line in lines), "\n".join(lines)
    # The heading names the day asked for. The span examined is never a heading again.
    assert lines[1].startswith(f"{EXPIRY}  {DAY}"), lines[1]
    assert "01:59" not in lines[1], lines[1]


def test_a_day_that_started_late_is_not_reported_as_a_store_that_stopped(
    tmp_path: Path, capsys
) -> None:
    """Criterion 2. A late start and an early stop are different facts.

    The engine came up at 22:00 and ran to the end of the day. Nothing stopped.
    """
    quoted = _span("22:00", "23:59")
    _write(tmp_path, DATASET, EXPIRY, quoted)
    _write(tmp_path, COMPUTED_DATASET, EXPIRY, quoted)

    report(tmp_path, EXPIRY, DAY, now=AFTER_THE_DAY, source_last=None)

    lines = _lines(capsys)
    assert _count(_figure(lines, "started late")) == 1320
    stopped = _figure(lines, "stopped early")
    assert _count(stopped) == 0, stopped
    assert "the span reaches the end asked for" in stopped, stopped
    assert "***" not in stopped, (
        "a late start must not be flagged as a stop:\n" + "\n".join(lines)
    )
    # It is still a short span — 8.3% of the day — and the coverage line says so.
    assert _percent(_figure(lines, "span examined")) == 8.3


def test_a_store_that_stopped_is_flagged_even_though_its_loss_rate_is_the_better_one(
    tmp_path: Path, capsys
) -> None:
    """Criterion 4, and the whole ticket in one test.

    Two stores, the same day, the same probe. The one that stopped after two hours has
    the lower loss percentage, exactly as `.stack-data` beat `data/` 0.3% to 2.3% during
    #103. Coverage is what refuses to agree with it.
    """
    stopped, ran = tmp_path / "stopped", tmp_path / "ran"
    _store_that_stopped(stopped)
    _store_that_ran(ran)

    report(stopped, EXPIRY, DAY, now=AFTER_THE_DAY, source_last=None)
    stopped_lines = _lines(capsys)
    report(ran, EXPIRY, DAY, now=AFTER_THE_DAY, source_last=None)
    ran_lines = _lines(capsys)

    # The defect itself, still true: the store that stopped has the better loss rate.
    stopped_loss = _percent(_figure(stopped_lines, "quote bar, no computed bar"))
    ran_loss = _percent(_figure(ran_lines, "quote bar, no computed bar"))
    assert stopped_loss < ran_loss, (stopped_loss, ran_loss)

    # And now unreadable alone, because coverage refuses to agree with it.
    stopped_coverage = _percent(_figure(stopped_lines, "span examined"))
    ran_coverage = _percent(_figure(ran_lines, "span examined"))
    assert stopped_coverage < ran_coverage
    assert stopped_coverage == 8.3, stopped_coverage
    assert ran_coverage == 100.0, ran_coverage
    assert any("SHORT SPAN" in line for line in stopped_lines)
    assert not any("SHORT SPAN" in line for line in ran_lines), "\n".join(ran_lines)


def test_the_minutes_outside_the_span_examined_are_never_folded_into_the_percentage(
    tmp_path: Path, capsys
) -> None:
    """Criterion 3. A fourth kind of absence, counted on its own line.

    120 minutes examined, 1 of them with a quote and no computed bar, and 1,320 minutes
    of the day never examined at all. The percentage is 1/120, and the 1,320 appear
    nowhere in it.
    """
    _store_that_stopped(tmp_path)

    report(tmp_path, EXPIRY, DAY, now=AFTER_THE_DAY, source_last=None)

    lines = _lines(capsys)
    loss = _figure(lines, "quote bar, no computed bar")
    assert _count(loss) == 1, loss
    assert _percent(loss) == 0.8, loss  # 1/120, not 1/1440 and not 1/1439
    assert "of the 120 minutes with a quote bar" in loss, loss
    outside = _figure(lines, "outside the span examined")
    assert _count(outside) == 1320, outside
    assert "never in the percentage" in outside, outside
    # The first kind of absence keeps its meaning: the engine was not down.
    assert _count(_figure(lines, "no quote bar at all")) == 0


def test_an_expiry_that_stops_before_the_rest_of_the_store_names_the_store_s_own_last(
    tmp_path: Path, capsys
) -> None:
    """Criterion 2's second limb: the span ends before the last entry in the source.

    Seven expiries ran all day and this one stopped at 01:59. The wall clock alone
    cannot tell you that — the source can, and `source_last_minute` reads it.
    """
    _store_that_stopped(tmp_path)
    _write(tmp_path, DATASET, OTHER_EXPIRY, _span("00:00", "23:59"))
    _write(tmp_path, COMPUTED_DATASET, OTHER_EXPIRY, _span("00:00", "23:59"))

    last = source_last_minute(tmp_path, DAY)
    assert last == _span("23:59", "23:59")[0]

    report(tmp_path, EXPIRY, DAY, now=AFTER_THE_DAY, source_last=last)

    lines = _lines(capsys)
    stopped = _figure(lines, "stopped early")
    assert "23:59" in stopped, stopped
    assert "this expiry" in stopped, stopped


def test_a_tail_shorter_than_one_store_flush_is_not_called_a_stop(
    tmp_path: Path, capsys
) -> None:
    """`store.FLUSH_SECONDS` is 300, and `BarStore.scan()` reads Parquet only.

    So the newest few minutes of a day still being recorded are legitimately absent from
    disk, and calling that a stop would make the probe cry wolf on every live run.
    """
    _write(tmp_path, DATASET, EXPIRY, _span("00:00", "09:57"))
    _write(tmp_path, COMPUTED_DATASET, EXPIRY, _span("00:00", "09:57"))
    now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)

    report(tmp_path, EXPIRY, DAY, now=now, source_last=None)

    lines = _lines(capsys)
    assert not any("SHORT SPAN" in line for line in lines), "\n".join(lines)
    assert "agree to within one store flush" in "\n".join(lines)
    # The day asked for ends at the wall clock, not at 23:59, so coverage is 99.5% and
    # not the 41.5% that dividing by a whole 1,440-minute day would have printed.
    assert _percent(_figure(lines, "span examined")) == 99.5
    assert _count(_figure(lines, "outside the span examined")) == 3
