"""T-step I13 (#79): measure the real Compose containers after I6.

The load profile in R6 was derived from the monolith because the split did not yet
exist. This tool observes each container under live load so that the day-long
per-service figures can replace those derived figures without touching the engine,
the venue, or the store.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


# `--interval` is the sampling PERIOD, sweep start to sweep start. Five seconds is
# `assumed`: short enough to catch a CPU spike lasting a few seconds, long enough that
# one sweep fits inside it.
#
# This tool's Docker-daemon calls are NOT below any noise floor, and the sentence that
# stood here claiming they were was wrong from the day it was written (#100). The
# `measured` figures, 2026-09-12, quiet host, six `dxp` containers:
#
#   one `docker stats --no-stream`, one container ......  1.88 s   (n=96)
#   one `docker stats --no-stream`, all six at once ....  1.84 s   (n=16, concurrent)
#   `docker ps` discovery ..............................  0.21 s   (n=32)
#
# Reading the six serially cost 11.79 s, so the real cadence was 16.84 s against this
# configured 5 s -- a 3.37x overrun, and **70.0% of every period spent in `docker`**.
# The same 3.32x shows in the I13 collection's own timestamps. Batching the six reads
# into one call cuts a sweep to 2.33 s, and a 5 s period is now actually achieved.
#
# The share is still material: `measured` 46-47% of each period is spent in `docker`
# subprocesses, and it is spent during the window whose CPU is being measured. It is
# smaller in absolute terms -- 2.33 s per period rather than 11.79 s -- but it has not
# become negligible, and a run that needs it smaller should raise `--interval` rather
# than assume this line away.
SAMPLE_INTERVAL_SECONDS = 5.0

# R6's reference figures, transcribed from research/0007-load-profile.md sections 4 and 5.
CPU_REFERENCE_RANGES = {
    "feed": {"1x": (0.52, 0.71), "10x": (5.2, 7.1)},
    "store": {"1x": (0.28, 0.51), "10x": (2.8, 5.1)},
    "api": {"1x": (0.25, 0.49), "10x": (2.5, 4.9)},
}
MEMORY_REFERENCE_MIB = {"web": 240.8, "redis": 1056.4}
MOVED_REASON = (
    "reason: not attributed automatically -- the operator fills this in against "
    "docs/design/decisions/0007-load-profile.md and "
    "docs/design/decisions/0008-topology.md"
)
PENDING = "pending"
THROUGHPUT_FIELDS = {
    "net_rx": "net_rx_bytes",
    "net_tx": "net_tx_bytes",
    "block_read": "block_read_bytes",
    "block_write": "block_write_bytes",
}

UTC = timezone.utc
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)?\s*$")
_UNIT_BYTES = {
    "B": 1,
    "kB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KiB": 1_024,
    "MiB": 1_048_576,
    "GiB": 1_073_741_824,
    "TiB": 1_099_511_627_776,
}


@dataclass
class ContainerState:
    started_at: str
    healthy: bool = False


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _run_docker(
    docker_cmd: Sequence[str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*docker_cmd, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def discover_containers(
    docker_cmd: Sequence[str], project: str
) -> list[tuple[str, str]]:
    """Return the current Compose project's container IDs and names."""
    result = _run_docker(
        docker_cmd,
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        r"{{.ID}}\t{{.Names}}",
    )
    if result.returncode != 0:
        return []

    containers = []
    for line in result.stdout.splitlines():
        if "\t" in line:
            container_id, container_name = line.split("\t", 1)
        elif r"\t" in line:
            container_id, container_name = line.split(r"\t", 1)
        else:
            continue
        container_id = container_id.strip()
        container_name = container_name.strip()
        if container_id and container_name:
            containers.append((container_id, container_name))
    return containers


def parse_size(value: str) -> int:
    """Parse one Docker stats size, preserving it as an integer byte count."""
    match = _SIZE_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid Docker size: {value!r}")
    number, unit = match.groups()
    unit = unit or "B"
    if unit not in _UNIT_BYTES:
        raise ValueError(f"unknown Docker size unit: {unit!r}")
    return int(round(float(number) * _UNIT_BYTES[unit]))


def _parse_percent(value: str) -> float:
    stripped = value.strip()
    if not stripped.endswith("%"):
        raise ValueError(f"invalid Docker percentage: {value!r}")
    return float(stripped[:-1])


def _split_counter_pair(value: str) -> tuple[str, str]:
    try:
        return tuple(value.split(" / ", 1))  # type: ignore[return-value]
    except ValueError as error:
        raise ValueError(f"invalid Docker counter pair: {value!r}") from error


def parse_stats(value: str) -> dict[str, int | float]:
    """Parse the exact string-shaped fields returned by Docker stats."""
    payload = json.loads(value)
    memory_used, memory_limit = _split_counter_pair(payload["MemUsage"])
    net_rx, net_tx = _split_counter_pair(payload["NetIO"])
    block_read, block_write = _split_counter_pair(payload["BlockIO"])
    return {
        "cpu_percent": _parse_percent(payload["CPUPerc"]),
        "mem_bytes": parse_size(memory_used),
        "mem_limit_bytes": parse_size(memory_limit),
        "net_rx_bytes": parse_size(net_rx),
        "net_tx_bytes": parse_size(net_tx),
        "block_read_bytes": parse_size(block_read),
        "block_write_bytes": parse_size(block_write),
    }


def _utc_now(clock: Callable[[], datetime] | None) -> datetime:
    current = clock() if clock is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    if not value or value == "pending":
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _started_at(docker_cmd: Sequence[str], container_id: str) -> str:
    result = _run_docker(
        docker_cmd,
        "inspect",
        "--format",
        "{{.State.StartedAt}}",
        container_id,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "pending"
    return result.stdout.strip()


def _health_signal(docker_cmd: Sequence[str], container_id: str) -> tuple[bool, str]:
    result = _run_docker(
        docker_cmd,
        "inspect",
        "--format",
        "{{json .State.Health}}",
        container_id,
    )
    if result.returncode == 0:
        try:
            health = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            health = None
        if isinstance(health, dict):
            return health.get("Status") == "healthy", "healthcheck"

    fallback = _run_docker(
        docker_cmd,
        "inspect",
        "--format",
        "{{.State.Status}}",
        container_id,
    )
    return fallback.stdout.strip() == "running", "running-fallback"


def _elapsed_seconds(started_at: str, healthy_at: datetime) -> float | str:
    started = _parse_timestamp(started_at)
    if started is None:
        return "pending"
    return round((healthy_at - started).total_seconds(), 3)


def _write_record(handle: IO[str], record: dict[str, Any]) -> None:
    handle.write(json.dumps(record) + "\n")
    handle.flush()


def _stats_rows(stdout: str) -> dict[str, dict[str, int | float]]:
    """Parse one `docker stats` invocation, which may carry one line per container."""
    rows: dict[str, dict[str, int | float]] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
            identifier = str(payload.get("ID") or payload.get("Container") or "")
            if not identifier:
                continue
            rows[identifier] = parse_stats(stripped)
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return rows


def _match_ids(
    requested: Sequence[str], rows: dict[str, dict[str, int | float]]
) -> dict[str, dict[str, int | float]]:
    """Key Docker's rows back onto the ids we asked for, tolerating id truncation."""
    matched: dict[str, dict[str, int | float]] = {}
    for container_id in requested:
        if container_id in rows:
            matched[container_id] = rows[container_id]
            continue
        for key, value in rows.items():
            if key.startswith(container_id) or container_id.startswith(key):
                matched[container_id] = value
                break
    return matched


def _read_stats(
    docker_cmd: Sequence[str],
    container_ids: Sequence[str],
    clock: Callable[[], datetime] | None,
) -> dict[str, tuple[dict[str, int | float], str, float]]:
    """Read every container's stats, and stamp each read with its own clock.

    `docker stats --no-stream` accepts several ids in one invocation and reads them
    concurrently, so one batched call costs about what one serial call costs. Every
    container's percentage is still computed from that container's own two consecutive
    daemon reads, so batching changes the cost and not the arithmetic -- see #100 for
    the paired run that checked it. A daemon that refuses the batched form, or answers
    it short, falls back to one call per container; those reads are genuinely at
    different times and each row then carries its own.
    """
    readings: dict[str, tuple[dict[str, int | float], str, float]] = {}
    arguments = ("stats", "--no-stream", "--format", r"{{json .}}")

    if len(container_ids) > 1:
        started = _isoformat(_utc_now(clock))
        begin = time.monotonic()
        result = _run_docker(docker_cmd, *arguments, *container_ids)
        read_seconds = round(time.monotonic() - begin, 6)
        if result.returncode == 0:
            for container_id, parsed in _match_ids(
                container_ids, _stats_rows(result.stdout)
            ).items():
                readings[container_id] = (parsed, started, read_seconds)

    for container_id in container_ids:
        if container_id in readings:
            continue
        started = _isoformat(_utc_now(clock))
        begin = time.monotonic()
        result = _run_docker(docker_cmd, *arguments, container_id)
        read_seconds = round(time.monotonic() - begin, 6)
        if result.returncode != 0:
            continue
        matched = _match_ids([container_id], _stats_rows(result.stdout))
        if container_id in matched:
            readings[container_id] = (matched[container_id], started, read_seconds)
    return readings


def sample_tick(
    docker_cmd: Sequence[str],
    project: str,
    output: str | Path | IO[str],
    states: dict[str, ContainerState],
    *,
    clock: Callable[[], datetime] | None = None,
    sweep: int = 0,
) -> None:
    """Collect and append one sample for every container visible on this sweep.

    Every sample row carries the timestamp of **its own** read, never the sweep's.
    Before #100 one `sampled_at` was computed ahead of the reads and stamped across
    all of them, so rows up to 6.9 s apart looked simultaneous (`measured` 2026-09-12).
    A `sweep` field keeps the rows groupable, and `read_seconds` carries the residual
    uncertainty on the row's own stamp rather than hiding it.
    """
    sweep_began = time.monotonic()
    sweep_started_text = _isoformat(_utc_now(clock))
    containers = discover_containers(docker_cmd, project)
    names = dict(containers)

    handle: IO[str]
    close_handle = False
    if isinstance(output, (str, Path)):
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handle = output_path.open("a", encoding="utf-8")
        close_handle = True
    else:
        handle = output

    try:
        for container_id, _ in containers:
            if container_id not in states:
                states[container_id] = ContainerState(
                    _started_at(docker_cmd, container_id)
                )

        readings = _read_stats(docker_cmd, [cid for cid, _ in containers], clock)

        for container_id, container_name in containers:
            reading = readings.get(container_id)
            if reading is None:
                continue
            parsed, read_at_text, read_seconds = reading

            _write_record(
                handle,
                {
                    "type": "sample",
                    "sweep": sweep,
                    "sampled_at": read_at_text,
                    "read_seconds": read_seconds,
                    "container_id": container_id,
                    "container_name": container_name,
                    **parsed,
                },
            )

            state = states[container_id]
            if state.healthy:
                continue
            healthy, method = _health_signal(docker_cmd, container_id)
            if not healthy:
                continue
            state.healthy = True
            read_at = _parse_timestamp(read_at_text) or _utc_now(clock)
            _write_record(
                handle,
                {
                    "type": "health",
                    "container_id": container_id,
                    "container_name": container_name,
                    "started_at": state.started_at,
                    "healthy_at": read_at_text,
                    "seconds": _elapsed_seconds(state.started_at, read_at),
                    "method": method,
                },
            )

        _write_record(
            handle,
            {
                "type": "sweep",
                "sweep": sweep,
                "started_at": sweep_started_text,
                "elapsed_seconds": round(time.monotonic() - sweep_began, 6),
                "containers_seen": len(containers),
                "container_names": [names[cid] for cid, _ in containers],
                "containers_read": len(readings),
            },
        )
    finally:
        if close_handle:
            handle.close()


def _new_output_path(started_at: datetime) -> Path:
    output_dir = Path(__file__).resolve().parent / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    candidate = output_dir / f"measure_containers-{stamp}.jsonl"
    suffix = 1
    while candidate.exists():
        candidate = output_dir / f"measure_containers-{stamp}-{suffix}.jsonl"
        suffix += 1
    return candidate


def _split_docker_command(value: str) -> list[str]:
    """Split a command string, keeping unquoted Windows paths with spaces intact."""
    parts = shlex.split(value, posix=False)
    if sys.platform != "win32" or len(parts) < 2:
        return parts

    executable_end = next(
        (
            index
            for index, part in enumerate(parts)
            if part.lower().endswith((".exe", ".cmd", ".bat", ".com"))
        ),
        0,
    )
    script_end = next(
        (
            index
            for index, part in enumerate(parts[executable_end + 1 :], executable_end + 1)
            if part.lower().endswith((".py", ".js", ".mjs", ".ps1"))
        ),
        None,
    )
    if script_end is None or script_end == executable_end + 1:
        return parts
    return parts[: executable_end + 1] + [
        " ".join(parts[executable_end + 1 : script_end + 1]),
        *parts[script_end + 1 :],
    ]


def _collect(args: argparse.Namespace) -> int:
    started_at = datetime.now(UTC)
    docker_cmd = _split_docker_command(args.docker_cmd)
    if not docker_cmd:
        print("error: --docker-cmd must not be empty", file=sys.stderr)
        return 2
    output = Path(args.out) if args.out else _new_output_path(started_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    states: dict[str, ContainerState] = {}
    limit = args.samples
    duration = args.duration
    interval = args.interval

    cadences: list[float] = []
    sweep_costs: list[float] = []
    overruns = 0

    with output.open("a", encoding="utf-8") as handle:
        run_began = time.monotonic()
        sweep = 0
        while True:
            if limit is not None and sweep >= limit:
                break
            if duration is not None and time.monotonic() - run_began >= duration:
                break

            sweep_began = time.monotonic()
            sample_tick(docker_cmd, args.project, handle, states, sweep=sweep)
            elapsed = time.monotonic() - sweep_began
            sweep_costs.append(elapsed)
            sweep += 1

            # The interval is the period, not the gap. Sleep only what is left of it,
            # and never a negative amount. #100: the loop used to sleep the whole
            # interval on top of the sweep, so a configured 5 s produced a `measured`
            # 16.4 s cadence and drifted in silence.
            remaining = interval - elapsed
            if remaining < 0:
                overruns += 1
                print(
                    f"measure_containers: OVERRUN on sweep {sweep - 1} -- the sweep "
                    f"took {elapsed:.3f} s against a {interval:.3f} s interval, over "
                    f"by {-remaining:.3f} s. This sweep's real cadence is "
                    f"{elapsed:.3f} s, not {interval:.3f} s. Every rate derived from "
                    "the configured interval is wrong by that ratio.",
                    file=sys.stderr,
                    flush=True,
                )
                _write_record(
                    handle,
                    {
                        "type": "overrun",
                        "sweep": sweep - 1,
                        "interval_seconds": interval,
                        "elapsed_seconds": round(elapsed, 6),
                        "over_by_seconds": round(-remaining, 6),
                    },
                )
                remaining = 0.0
            cadences.append(elapsed + remaining)

            if limit is not None and sweep >= limit:
                break
            if (
                duration is not None
                and (time.monotonic() - run_began) + remaining >= duration
            ):
                break
            time.sleep(remaining)

    if cadences:
        mean_cadence = statistics.fmean(cadences)
        mean_cost = statistics.fmean(sweep_costs)
        share = mean_cost / mean_cadence * 100 if mean_cadence else 0.0
        print(
            f"measure_containers: {len(cadences)} sweeps, cadence mean "
            f"{mean_cadence:.3f} s against a configured {interval:.3f} s, "
            f"max {max(cadences):.3f} s, {overruns} overrun(s). Sweeps spent "
            f"{mean_cost:.3f} s in docker, {share:.1f}% of the period.",
            file=sys.stderr,
            flush=True,
        )
    return 0


def _read_records(paths: Sequence[str | Path]) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    skipped = 0
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if isinstance(record, dict) and record.get("type") in {
                    "sample",
                    "health",
                }:
                    records.append(record)
    return records, skipped


def _mean_max(values: list[float | int]) -> dict[str, float | int | str]:
    if not values:
        return {"mean": PENDING, "max": PENDING}
    return {"mean": statistics.fmean(values), "max": max(values)}


def _sorted_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        samples,
        key=lambda record: (
            _parse_timestamp(str(record.get("sampled_at", ""))) is None,
            _parse_timestamp(str(record.get("sampled_at", "")))
            or datetime.min.replace(tzinfo=UTC),
        ),
    )


def _throughput(samples: list[dict[str, Any]]) -> dict[str, dict[str, float | str]]:
    rates = {name: [] for name in THROUGHPUT_FIELDS}
    ordered = _sorted_samples(samples)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        previous_at = _parse_timestamp(str(previous.get("sampled_at", "")))
        current_at = _parse_timestamp(str(current.get("sampled_at", "")))
        if previous_at is None or current_at is None:
            continue
        gap = (current_at - previous_at).total_seconds()
        if gap <= 0:
            continue
        for name, field in THROUGHPUT_FIELDS.items():
            previous_value = previous.get(field)
            current_value = current.get(field)
            if not isinstance(previous_value, (int, float)) or not isinstance(
                current_value, (int, float)
            ):
                continue
            rates[name].append((current_value - previous_value) / gap)
    return {name: _mean_max(values) for name, values in rates.items()}


def _metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    cpu_values = [
        float(record["cpu_percent"])
        for record in samples
        if isinstance(record.get("cpu_percent"), (int, float))
    ]
    memory_values = [
        int(record["mem_bytes"])
        for record in samples
        if isinstance(record.get("mem_bytes"), (int, float))
    ]
    cpu = _mean_max(cpu_values)
    memory = _mean_max(memory_values)
    cpu_mean = cpu["mean"]
    memory_mean = memory["mean"]
    return {
        "sample_count": len(samples),
        "cpu_percent": cpu,
        "cpu_cores": {
            "mean": cpu_mean / 100 if isinstance(cpu_mean, (int, float)) else PENDING
        },
        "mem_bytes": memory,
        "memory_mib": {
            "mean": memory_mean / 1_048_576
            if isinstance(memory_mean, (int, float))
            else PENDING
        },
        "throughput_bytes_per_second": _throughput(samples),
    }


def _health_summary(record: dict[str, Any] | None) -> dict[str, Any]:
    if record is None:
        return {
            "started_at": PENDING,
            "healthy_at": PENDING,
            "seconds": PENDING,
            "method": PENDING,
        }
    return {
        "started_at": record.get("started_at", PENDING),
        "healthy_at": record.get("healthy_at", PENDING),
        "seconds": record.get("seconds", PENDING),
        "method": record.get("method", PENDING),
    }


def _nearest_cpu_reference(
    service: str, measured: float
) -> tuple[str, float]:
    candidates = [
        (f"{service} {load} lower", bounds[0])
        for load, bounds in CPU_REFERENCE_RANGES[service].items()
    ]
    candidates.extend(
        (f"{service} {load} upper", bounds[1])
        for load, bounds in CPU_REFERENCE_RANGES[service].items()
    )
    return min(candidates, key=lambda candidate: abs(measured - candidate[1]))


def _comparisons(name: str, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons = []
    lowered = name.casefold()
    cpu_mean = metrics["cpu_cores"]["mean"]
    for service in CPU_REFERENCE_RANGES:
        if service not in lowered or not isinstance(cpu_mean, (int, float)):
            continue
        reference, reference_value = _nearest_cpu_reference(service, cpu_mean)
        moved = abs(cpu_mean - reference_value) / reference_value * 100
        if moved > 25:
            comparisons.append(
                {
                    "service": service,
                    "reference": reference,
                    "reference_value": reference_value,
                    "measured_value": cpu_mean,
                    "percent_moved": round(moved, 3),
                    "unit": "cores",
                    "reason": MOVED_REASON,
                }
            )

    memory_mean = metrics["memory_mib"]["mean"]
    for service, reference_value in MEMORY_REFERENCE_MIB.items():
        if service not in lowered or not isinstance(memory_mean, (int, float)):
            continue
        moved = abs(memory_mean - reference_value) / reference_value * 100
        if moved > 25:
            comparisons.append(
                {
                    "service": service,
                    "reference": service,
                    "reference_value": reference_value,
                    "measured_value": memory_mean,
                    "percent_moved": round(moved, 3),
                    "unit": "MiB",
                    "reason": MOVED_REASON,
                }
            )
    return comparisons


def summarize_paths(paths: Sequence[str | Path]) -> dict[str, Any]:
    """Digest one or more collector files into a JSON-serializable report."""
    records, skipped = _read_records(paths)
    samples_by_name: dict[str, list[dict[str, Any]]] = {}
    health_by_name: dict[str, dict[str, Any]] = {}
    for record in records:
        name = record.get("container_name")
        if not isinstance(name, str) or not name:
            continue
        if record["type"] == "sample":
            samples_by_name.setdefault(name, []).append(record)
        elif name not in health_by_name:
            health_by_name[name] = record

    names = sorted(set(samples_by_name) | set(health_by_name), key=str.casefold)
    containers: dict[str, Any] = {}
    for name in names:
        facts = _metrics(samples_by_name.get(name, []))
        facts["health"] = _health_summary(health_by_name.get(name))
        facts["comparisons"] = _comparisons(name, facts)
        containers[name] = facts
    return {"skipped_lines": skipped, "containers": containers}


def _display(value: Any) -> str:
    return str(value)


def render_summary(report: dict[str, Any]) -> str:
    """Render the digest as a readable plain-text report."""
    lines = ["CONTAINER MEASUREMENT SUMMARY"]
    for name, facts in report["containers"].items():
        lines.extend(
            [
                f"Container: {name}",
                f"  samples: {facts['sample_count']}",
                "  CPU percent mean/max: "
                f"{_display(facts['cpu_percent']['mean'])} / "
                f"{_display(facts['cpu_percent']['max'])}",
                f"  CPU cores mean: {_display(facts['cpu_cores']['mean'])}",
                "  memory bytes mean/max: "
                f"{_display(facts['mem_bytes']['mean'])} / "
                f"{_display(facts['mem_bytes']['max'])}",
                f"  memory MiB mean: {_display(facts['memory_mib']['mean'])}",
            ]
        )
        for name_key in THROUGHPUT_FIELDS:
            rate = facts["throughput_bytes_per_second"][name_key]
            lines.append(
                f"  {name_key} B/s mean/max: {_display(rate['mean'])} / "
                f"{_display(rate['max'])}"
            )
        health = facts["health"]
        lines.append(
            "  health: "
            f"started_at={_display(health['started_at'])} "
            f"healthy_at={_display(health['healthy_at'])} "
            f"seconds={_display(health['seconds'])} "
            f"method={_display(health['method'])}"
        )
        for comparison in facts["comparisons"]:
            lines.append(
                f"  MOVED >25%: {name} | reference: "
                f"{comparison['reference']}={comparison['reference_value']} "
                f"{comparison['unit']} | measured: "
                f"{comparison['measured_value']} {comparison['unit']} "
                f"({comparison['percent_moved']}% moved) | {comparison['reason']}"
            )
    lines.append(f"skipped lines: {report['skipped_lines']}")
    return "\n".join(lines) + "\n"


def _summarize(args: argparse.Namespace) -> int:
    report = summarize_paths(args.inputs)
    rendered = render_summary(report)
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(rendered, end="")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="append live container samples")
    collect.add_argument("--project", default="dxp")
    collect.add_argument(
        "--interval",
        type=_non_negative_float,
        default=SAMPLE_INTERVAL_SECONDS,
        help="the sampling PERIOD in seconds, measured sweep start to sweep start",
    )
    collect.add_argument(
        "--samples",
        type=_non_negative_int,
        default=None,
        help=(
            "stop after this many sweeps. At the real cadence that is "
            "(samples - 1) * interval seconds, so 17280 sweeps at 5 s is 24 hours. "
            "Prefer --duration when what you mean is a length of time"
        ),
    )
    collect.add_argument(
        "--duration",
        type=_non_negative_float,
        default=None,
        help=(
            "stop after this many seconds of wall clock. A hard bound that holds "
            "even when sweeps overrun the interval"
        ),
    )
    collect.add_argument("--out", default=None)
    collect.add_argument("--docker-cmd", default="docker")

    summarize = commands.add_parser("summarize", help="summarize collected JSONL")
    summarize.add_argument("--in", dest="inputs", nargs="+", required=True)
    summarize.add_argument("--out", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "collect":
        return _collect(args)
    return _summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
