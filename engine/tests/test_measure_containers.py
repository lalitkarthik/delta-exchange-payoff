from __future__ import annotations

import json
import os
import subprocess
import sys
import types
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
    """Yield each moment in turn, then hold the last one.

    Since #100 the clock is read once per docker read rather than once per sweep,
    so a fixture that ran out after one call would only be testing StopIteration.
    """
    values = list(moments)
    state = {"index": 0}

    def tick() -> datetime:
        moment = values[min(state["index"], len(values) - 1)]
        state["index"] += 1
        return moment

    return tick


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
        "sweep",
        "sampled_at",
        "read_seconds",
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
        "sweep",
        "sampled_at",
        "read_seconds",
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
    assert all(record["type"] in {"sample", "health", "sweep"} for record in records)
    assert len([record for record in records if record["type"] == "sweep"]) == 3
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


# ---------------------------------------------------------------------------
# #100: cadence, per-row timestamps, and the batched `docker stats` call.
#
# These drive the real `collect` entry point in-process. Two seams are faked:
#
#   * the clock -- `measure_containers.time` and `measure_containers.datetime` are
#     replaced, so no test here waits on a real duration; and
#   * docker -- `measure_containers._run_docker` is replaced, so every subprocess
#     the tool would spawn becomes a recorded call that charges the fake clock.
#
# Charging the clock inside the docker seam is what makes a sweep take time, which
# is the whole subject of the ticket.
# ---------------------------------------------------------------------------

EPOCH = datetime(2026, 9, 12, tzinfo=UTC)
FAKE_IDS = ["feed-id", "store-id", "api-id", "web-id", "redis-id", "proxy-id"]
FAKE_NAMES = {
    "feed-id": "dxp-feed",
    "store-id": "dxp-store",
    "api-id": "dxp-api",
    "web-id": "dxp-web",
    "redis-id": "dxp-redis",
    "proxy-id": "dxp-proxy",
}


class FakeClock:
    """One clock behind `time.monotonic`, `time.sleep` and `datetime.now`."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def utc(self) -> datetime:
        return EPOCH + timedelta(seconds=self.now)


class FakeDocker:
    """A docker seam that charges the fake clock for every call it serves."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        container_ids: list[str] | None = None,
        stats_seconds: float = 3.0,
        ps_seconds: float = 0.0,
        inspect_seconds: float = 0.0,
        accepts_many_ids: bool = True,
    ) -> None:
        self.clock = clock
        self.container_ids = list(container_ids or FAKE_IDS)
        self.stats_seconds = stats_seconds
        self.ps_seconds = ps_seconds
        self.inspect_seconds = inspect_seconds
        self.accepts_many_ids = accepts_many_ids
        self.calls: list[list[str]] = []
        self.stats_calls_at: list[float] = []

    def _done(self, stdout: str, returncode: int = 0):
        return subprocess.CompletedProcess(["docker"], returncode, stdout, "")

    def __call__(self, docker_cmd, *arguments):
        argv = list(arguments)
        self.calls.append(argv)
        command = argv[0]

        if command == "ps":
            self.clock.advance(self.ps_seconds)
            lines = "\n".join(
                f"{cid}\t{FAKE_NAMES[cid]}" for cid in self.container_ids
            )
            return self._done(lines + "\n")

        if command == "stats":
            ids = [value for value in argv if value in FAKE_NAMES]
            if len(ids) > 1 and not self.accepts_many_ids:
                # A docker that refuses several ids fails fast and costs nothing,
                # so the fallback's own timings stay readable in the assertions.
                return self._done("", returncode=1)
            self.stats_calls_at.append(self.clock.now)
            self.clock.advance(self.stats_seconds)
            payloads = [
                json.dumps(
                    {
                        "Container": cid,
                        "ID": cid,
                        "Name": FAKE_NAMES[cid],
                        "CPUPerc": "12.50%",
                        "MemUsage": "100MiB / 8GiB",
                        "MemPerc": "1.22%",
                        "NetIO": "1.0kB / 2.0kB",
                        "BlockIO": "3.0kB / 4.0kB",
                        "PIDs": "1",
                    }
                )
                for cid in ids
            ]
            return self._done("\n".join(payloads) + "\n")

        if command == "inspect":
            self.clock.advance(self.inspect_seconds)
            format_value = argv[argv.index("--format") + 1]
            if format_value == "{{.State.StartedAt}}":
                return self._done("2026-09-12T00:00:00.000000000Z\n")
            if format_value == "{{json .State.Health}}":
                return self._done('{"Status":"healthy"}\n')
            return self._done("running\n")

        return self._done("", returncode=1)


def _install(monkeypatch: pytest.MonkeyPatch, clock: FakeClock, docker: FakeDocker):
    import measure_containers as module

    fake_time = types.SimpleNamespace(
        sleep=clock.sleep, monotonic=clock.monotonic, time=clock.monotonic
    )

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return clock.utc()

    monkeypatch.setattr(module, "time", fake_time)
    monkeypatch.setattr(module, "datetime", FakeDatetime)
    monkeypatch.setattr(module, "_run_docker", docker)
    return module


def _collect(module, tmp_path: Path, *extra: str) -> tuple[int, Path]:
    output = tmp_path / "collected.jsonl"
    code = module.main(
        [
            "collect",
            "--project",
            "dxp",
            "--out",
            str(output),
            "--docker-cmd",
            "docker",
            *extra,
        ]
    )
    return code, output


def test_collect_sleeps_the_remainder_of_the_period_not_the_whole_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 defect 1. The loop must pace on the period, not add the interval as a gap.

    The fake docker charges 3.0 s for the one batched `stats` call, so a 5 s interval
    leaves 2.0 s to sleep. The shipped loop slept the full 5.0 s and produced a
    cadence of 8.0 s.
    """
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=3.0)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "5", "--samples", "3")

    assert code == 0
    assert clock.sleeps == [2.0, 2.0], (
        f"expected the remainder of the period, got {clock.sleeps}"
    )
    starts = sorted(docker.stats_calls_at)
    gaps = [
        round(later - earlier, 6)
        for earlier, later in zip(starts, starts[1:], strict=False)
    ]
    assert gaps == [5.0, 5.0], f"cadence should be the interval, got {gaps}"
    assert clock.now == pytest.approx(3.0 + 2.0 + 3.0 + 2.0 + 3.0)


def test_collect_floors_the_sleep_at_zero_and_says_an_overrun_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#100 defect 1, second half. An overrun must be loud, not silently absorbed."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=9.0)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "5", "--samples", "2")
    captured = capsys.readouterr()

    assert code == 0
    assert clock.sleeps == [0.0], f"a sleep must never go negative, got {clock.sleeps}"
    assert "overrun" in captured.err.lower(), captured.err
    assert "9.0" in captured.err or "9.000" in captured.err, captured.err

    overruns = [
        record for record in _records(output) if record.get("type") == "overrun"
    ]
    assert len(overruns) == 2, "the file itself must disclose the overrun"
    assert overruns[0]["interval_seconds"] == 5.0
    assert overruns[0]["elapsed_seconds"] == pytest.approx(9.0)


def test_each_row_carries_the_timestamp_of_its_own_read_when_docker_is_read_serially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 defect 3. Six reads 1 s apart must not share one stamp.

    The fake docker here refuses several ids, forcing the per-container fallback, so
    the six reads genuinely happen at six different times.
    """
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=1.0, accepts_many_ids=False)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "30", "--samples", "1")

    assert code == 0
    samples = [record for record in _records(output) if record["type"] == "sample"]
    assert len(samples) == 6
    stamps = [record["sampled_at"] for record in samples]
    assert len(set(stamps)) == 6, f"six reads, six stamps; got {sorted(set(stamps))}"
    moments = sorted(
        datetime.fromisoformat(stamp.replace("Z", "+00:00")) for stamp in stamps
    )
    spread = (moments[-1] - moments[0]).total_seconds()
    assert spread == pytest.approx(5.0), f"expected a 5 s spread, got {spread}"


def test_every_sample_row_carries_the_sweep_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 criterion 2's recorded decision: rows keep a sweep id so they regroup."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=1.0)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "5", "--samples", "3")

    assert code == 0
    samples = [record for record in _records(output) if record["type"] == "sample"]
    assert len(samples) == 18
    assert sorted({record["sweep"] for record in samples}) == [0, 1, 2]
    for sweep in (0, 1, 2):
        assert len([r for r in samples if r["sweep"] == sweep]) == 6
    assert all(
        isinstance(record["read_seconds"], float) for record in samples
    ), "the residual uncertainty on a row's own stamp must travel with it"


def test_one_batched_stats_call_replaces_the_six_serial_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 criterion 3. Six containers, one `docker stats` invocation per sweep."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=2.0)
    module = _install(monkeypatch, clock, docker)

    code, _ = _collect(module, tmp_path, "--interval", "5", "--samples", "2")

    assert code == 0
    stats_calls = [argv for argv in docker.calls if argv[0] == "stats"]
    assert len(stats_calls) == 2, f"one call per sweep, got {len(stats_calls)}"
    for argv in stats_calls:
        assert argv[:3] == ["stats", "--no-stream", "--format"]
        assert sorted(value for value in argv if value in FAKE_NAMES) == sorted(FAKE_IDS)


def test_a_docker_that_refuses_several_ids_falls_back_to_one_call_per_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 criterion 3's safety net: the batched call is an optimisation, not a
    requirement."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=1.0, accepts_many_ids=False)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "30", "--samples", "1")

    assert code == 0
    samples = [record for record in _records(output) if record["type"] == "sample"]
    assert {record["container_name"] for record in samples} == set(FAKE_NAMES.values())
    stats_calls = [argv for argv in docker.calls if argv[0] == "stats"]
    assert len(stats_calls) == 7, "one refused batch, then six singles"


def test_duration_bounds_the_run_by_wall_clock_rather_than_by_sweep_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 criterion 6. `--samples 17280 --interval 5` meant 78.7 h, not 24 h."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=1.0)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "5", "--duration", "20")

    assert code == 0
    samples = [record for record in _records(output) if record["type"] == "sample"]
    sweeps = {record["sweep"] for record in samples}
    assert sweeps == {0, 1, 2, 3}, f"20 s at a 5 s period is four sweeps, got {sweeps}"
    assert clock.now <= 20.0


def test_samples_bound_now_spans_the_duration_its_arithmetic_promises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#100 criterion 6, the other half: n sweeps at a 5 s period spans (n-1)*5 s."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=2.0)
    module = _install(monkeypatch, clock, docker)

    code, _ = _collect(module, tmp_path, "--interval", "5", "--samples", "5")

    assert code == 0
    assert clock.now == pytest.approx(4 * 5.0 + 2.0)


def test_the_sweep_record_says_how_many_containers_were_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instrument must state what it saw, so a later reader can ask whether that
    was everything. #79 measured six services because `docker ps` returned six."""
    clock = FakeClock()
    docker = FakeDocker(clock, stats_seconds=1.0)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "5", "--samples", "2")

    assert code == 0
    sweeps = [record for record in _records(output) if record.get("type") == "sweep"]
    assert len(sweeps) == 2
    assert sweeps[0]["containers_seen"] == 6
    assert sorted(sweeps[0]["container_names"]) == sorted(FAKE_NAMES.values())


def test_a_short_batched_answer_is_topped_up_per_container_with_its_own_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batched call that answers for only some containers must not silently drop
    the rest. The ones it missed are re-read singly, and those reads really are at a
    different time, so they carry a different stamp."""
    clock = FakeClock()

    class ShortDocker(FakeDocker):
        def __call__(self, docker_cmd, *arguments):
            argv = list(arguments)
            if argv and argv[0] == "stats":
                ids = [value for value in argv if value in FAKE_NAMES]
                if len(ids) > 1:
                    # Answer for the first four only.
                    self.calls.append(argv)
                    self.clock.advance(self.stats_seconds)
                    payloads = [
                        json.dumps(
                            {
                                "ID": cid,
                                "Name": FAKE_NAMES[cid],
                                "CPUPerc": "12.50%",
                                "MemUsage": "100MiB / 8GiB",
                                "NetIO": "1.0kB / 2.0kB",
                                "BlockIO": "3.0kB / 4.0kB",
                            }
                        )
                        for cid in ids[:4]
                    ]
                    return self._done("\n".join(payloads) + "\n")
            return super().__call__(docker_cmd, *arguments)

    docker = ShortDocker(clock, stats_seconds=1.0)
    module = _install(monkeypatch, clock, docker)

    code, output = _collect(module, tmp_path, "--interval", "30", "--samples", "1")

    assert code == 0
    samples = [record for record in _records(output) if record["type"] == "sample"]
    assert {record["container_name"] for record in samples} == set(FAKE_NAMES.values())
    stamps = {record["container_name"]: record["sampled_at"] for record in samples}
    batched_stamp = stamps["dxp-feed"]
    assert [stamps[FAKE_NAMES[cid]] for cid in FAKE_IDS[:4]] == [batched_stamp] * 4
    assert stamps[FAKE_NAMES[FAKE_IDS[4]]] != batched_stamp
    assert stamps[FAKE_NAMES[FAKE_IDS[5]]] != stamps[FAKE_NAMES[FAKE_IDS[4]]]
    sweep_record = next(r for r in _records(output) if r.get("type") == "sweep")
    assert sweep_record["containers_seen"] == 6
    assert sweep_record["containers_read"] == 6
