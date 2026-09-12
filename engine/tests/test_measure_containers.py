from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from measure_containers import render_summary, sample_tick, summarize_paths  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "fake_docker.py"
UTC = timezone.utc


def _docker_prefix() -> list[str]:
    return [sys.executable, str(FIXTURE)]


def _clock(*moments: datetime):
    values = iter(moments)
    return lambda: next(values)


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_single_tick_writes_typed_samples_and_health_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "fake-docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    output = tmp_path / "samples.jsonl"
    states: dict[str, object] = {}

    sample_tick(
        _docker_prefix(),
        "fixture-project",
        output,
        states,
        clock=_clock(datetime(2026, 9, 12, tzinfo=UTC)),
    )

    records = _records(output)
    samples = [record for record in records if record["type"] == "sample"]
    health = [record for record in records if record["type"] == "health"]
    assert {record["container_name"] for record in samples} == {
        "fixture-feed",
        "fixture-api",
    }
    required = {
        "type",
        "sampled_at",
        "container_id",
        "container_name",
        "cpu_percent",
        "mem_bytes",
        "mem_limit_bytes",
        "net_rx_bytes",
        "net_tx_bytes",
        "block_read_bytes",
        "block_write_bytes",
    }
    assert all(set(record) == required for record in samples)
    assert all(isinstance(record["sampled_at"], str) for record in samples)
    assert all(isinstance(record["cpu_percent"], float) for record in samples)
    integer_fields = required - {
        "type",
        "sampled_at",
        "container_id",
        "container_name",
        "cpu_percent",
    }
    assert all(
        isinstance(record[field], int) for record in samples for field in integer_fields
    )
    feed_sample = next(
        record for record in samples if record["container_name"] == "fixture-feed"
    )
    assert feed_sample["cpu_percent"] == 12.5
    assert feed_sample["mem_bytes"] == 1_982_857
    assert feed_sample["mem_limit_bytes"] == 8_161_511_604
    assert feed_sample["net_rx_bytes"] == 1_000
    assert feed_sample["net_tx_bytes"] == 2_000
    assert feed_sample["block_read_bytes"] == 3_000
    assert feed_sample["block_write_bytes"] == 4_000
    assert {record["method"] for record in health} == {"healthcheck"}
    assert all(isinstance(record["seconds"], float) for record in health)
    assert all(isinstance(record["started_at"], str) for record in health)


def test_single_tick_appends_valid_json_when_one_file_handle_spans_three_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "fake-docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    output = tmp_path / "samples.jsonl"
    states: dict[str, object] = {}
    start = datetime(2026, 9, 12, tzinfo=UTC)

    with output.open("a", encoding="utf-8") as handle:
        for offset in (0, 5, 10):
            sample_tick(
                _docker_prefix(),
                "fixture-project",
                handle,
                states,
                clock=lambda offset=offset: start + timedelta(seconds=offset),
            )

    records = _records(output)
    assert len([record for record in records if record["type"] == "sample"]) == 6
    assert all(record["type"] in {"sample", "health"} for record in records)
    assert len(records) == len(output.read_text(encoding="utf-8").splitlines())


def test_collect_subprocess_runs_the_docker_seam_and_writes_two_bounded_ticks(
    tmp_path: Path,
) -> None:
    log = tmp_path / "fake-docker.log"
    output = tmp_path / "out.jsonl"
    environment = os.environ.copy()
    environment["FAKE_DOCKER_LOG"] = str(log)
    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "measure_containers.py"),
            "collect",
            "--project",
            "fixture-project",
            "--docker-cmd",
            f"{sys.executable} {FIXTURE}",
            "--interval",
            "0.01",
            "--samples",
            "2",
            "--out",
            str(output),
        ],
        cwd=TOOLS.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    records = _records(output)
    samples = [record for record in records if record["type"] == "sample"]
    health = [record for record in records if record["type"] == "health"]
    assert len(samples) == 4
    assert {record["container_name"] for record in samples} == {
        "fixture-feed",
        "fixture-api",
    }
    assert {record["container_name"] for record in health} == {
        "fixture-feed",
        "fixture-api",
    }
    assert {
        record["container_name"]: record["method"] for record in health
    } == {
        "fixture-feed": "healthcheck",
        "fixture-api": "running-fallback",
    }

    invocations = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    commands = {argv[0] for argv in invocations}
    assert {"ps", "stats", "inspect"} <= commands
    assert any(argv[:2] == ["stats", "--no-stream"] for argv in invocations)
    assert all(argv[0] in {"ps", "stats", "inspect"} for argv in invocations)


def test_help_short_circuits_before_needing_docker_on_path(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["PATH"] = ""
    result = subprocess.run(
        [sys.executable, str(TOOLS / "measure_containers.py"), "collect", "--help"],
        cwd=TOOLS.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--project" in result.stdout


def _sample(
    name: str,
    second: int,
    cpu_percent: float,
    mem_bytes: int,
    *,
    net_rx: int,
    net_tx: int,
    block_read: int,
    block_write: int,
) -> dict[str, object]:
    return {
        "type": "sample",
        "sampled_at": f"2026-09-12T00:00:{second:02d}Z",
        "container_id": f"{name}-id",
        "container_name": name,
        "cpu_percent": cpu_percent,
        "mem_bytes": mem_bytes,
        "mem_limit_bytes": 1_000_000,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "block_read_bytes": block_read,
        "block_write_bytes": block_write,
    }


def _health(name: str) -> dict[str, object]:
    return {
        "type": "health",
        "container_id": f"{name}-id",
        "container_name": name,
        "started_at": "2026-09-12T00:00:00Z",
        "healthy_at": "2026-09-12T00:00:10Z",
        "seconds": 10.0,
        "method": "healthcheck",
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def test_summarize_reports_hand_computed_means_maxima_and_throughput(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "summary.jsonl"
    records = [
        _sample(
            "alpha",
            second,
            cpu,
            memory,
            net_rx=rx,
            net_tx=tx,
            block_read=read,
            block_write=write,
        )
        for second, cpu, memory, rx, tx, read, write in (
            (0, 10.0, 100, 0, 0, 0, 0),
            (10, 20.0, 200, 100, 200, 50, 80),
            (20, 30.0, 300, 300, 400, 150, 240),
        )
    ]
    records.extend(
        _sample(
            "beta",
            second,
            cpu,
            memory,
            net_rx=rx,
            net_tx=tx,
            block_read=read,
            block_write=write,
        )
        for second, cpu, memory, rx, tx, read, write in (
            (0, 40.0, 400, 0, 0, 0, 0),
            (10, 50.0, 500, 200, 400, 100, 160),
            (20, 60.0, 600, 500, 800, 300, 480),
        )
    )
    records.append(_health("alpha"))
    _write_jsonl(fixture, records)

    report = summarize_paths([fixture])

    assert report["skipped_lines"] == 0
    assert report["containers"]["alpha"] == {
        "sample_count": 3,
        "cpu_percent": {"mean": 20.0, "max": 30.0},
        "cpu_cores": {"mean": 0.2},
        "mem_bytes": {"mean": 200.0, "max": 300},
        "memory_mib": {"mean": 0.00019073486328125},
        "throughput_bytes_per_second": {
            "net_rx": {"mean": 15.0, "max": 20.0},
            "net_tx": {"mean": 20.0, "max": 20.0},
            "block_read": {"mean": 7.5, "max": 10.0},
            "block_write": {"mean": 12.0, "max": 16.0},
        },
        "health": {
            "started_at": "2026-09-12T00:00:00Z",
            "healthy_at": "2026-09-12T00:00:10Z",
            "seconds": 10.0,
            "method": "healthcheck",
        },
        "comparisons": [],
    }
    assert report["containers"]["beta"]["cpu_percent"] == {
        "mean": 50.0,
        "max": 60.0,
    }


def test_summarize_marks_all_zero_sample_figures_pending(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "empty.jsonl"
    _write_jsonl(fixture, [_health("empty-container")])

    empty = summarize_paths([fixture])["containers"]["empty-container"]

    assert empty["sample_count"] == 0
    assert empty["cpu_percent"] == {"mean": "pending", "max": "pending"}
    assert empty["cpu_cores"] == {"mean": "pending"}
    assert empty["mem_bytes"] == {"mean": "pending", "max": "pending"}
    assert empty["memory_mib"] == {"mean": "pending"}
    assert empty["throughput_bytes_per_second"] == {
        "net_rx": {"mean": "pending", "max": "pending"},
        "net_tx": {"mean": "pending", "max": "pending"},
        "block_read": {"mean": "pending", "max": "pending"},
        "block_write": {"mean": "pending", "max": "pending"},
    }
    assert empty["health"] == {
        "started_at": "2026-09-12T00:00:00Z",
        "healthy_at": "2026-09-12T00:00:10Z",
        "seconds": 10.0,
        "method": "healthcheck",
    }


def test_summarize_skips_one_malformed_line_and_keeps_later_records(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "partial.jsonl"
    first = _sample(
        "partial",
        0,
        10.0,
        100,
        net_rx=0,
        net_tx=0,
        block_read=0,
        block_write=0,
    )
    second = _sample(
        "partial",
        10,
        20.0,
        200,
        net_rx=100,
        net_tx=100,
        block_read=100,
        block_write=100,
    )
    fixture.write_text(
        json.dumps(first) + "\n{\"type\":\"sample\"\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )

    report = summarize_paths([fixture])

    assert report["skipped_lines"] == 1
    assert report["containers"]["partial"]["sample_count"] == 2
    assert report["containers"]["partial"]["cpu_percent"] == {
        "mean": 15.0,
        "max": 20.0,
    }


def test_summarize_prints_only_out_of_range_comparison_lines(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "comparisons.jsonl"
    records = [
        _sample(
            "fixture-feed-high",
            second,
            100.0,
            100,
            net_rx=second,
            net_tx=second,
            block_read=second,
            block_write=second,
        )
        for second in (0, 10, 20)
    ]
    records.extend(
        _sample(
            "fixture-feed-within",
            second,
            60.0,
            100,
            net_rx=second,
            net_tx=second,
            block_read=second,
            block_write=second,
        )
        for second in (0, 10, 20)
    )
    _write_jsonl(fixture, records)

    report = summarize_paths([fixture])
    rendered = render_summary(report)

    assert len(report["containers"]["fixture-feed-high"]["comparisons"]) == 1
    assert report["containers"]["fixture-feed-within"]["comparisons"] == []
    assert "MOVED >25%: fixture-feed-high" in rendered
    assert "MOVED >25%: fixture-feed-within" not in rendered
    comparison_line = next(
        line for line in rendered.splitlines() if "MOVED >25%" in line
    )
    assert comparison_line.endswith(
        "reason: not attributed automatically -- the operator fills this in against "
        "docs/design/decisions/0007-load-profile.md and "
        "docs/design/decisions/0008-topology.md"
    )


def test_summarize_subprocess_prints_container_names_and_figures(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "subprocess.jsonl"
    records = [
        _sample(
            "fixture-feed",
            second,
            60.0,
            100,
            net_rx=0,
            net_tx=0,
            block_read=0,
            block_write=0,
        )
        for second in (0, 10, 20)
    ]
    _write_jsonl(fixture, records)

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "measure_containers.py"),
            "summarize",
            "--in",
            str(fixture),
        ],
        cwd=TOOLS.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "fixture-feed" in result.stdout
    assert "60.0" in result.stdout
    assert "0.6" in result.stdout
    assert "skipped lines: 0" in result.stdout
