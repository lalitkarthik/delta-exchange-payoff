"""A two-container Docker CLI fixture for the container measurement tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONTAINERS = {
    "feed-id": "fixture-feed",
    "api-id": "fixture-api",
}
LOG = Path(os.environ.get("FAKE_DOCKER_LOG", Path(__file__).with_name("fake_docker.log")))


def _invocations() -> list[list[str]]:
    if not LOG.exists():
        return []
    return [json.loads(line)["argv"] for line in LOG.read_text().splitlines()]


def _log(argv: list[str]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")


def _inspect_count(argv: list[str], container_id: str, marker: str) -> int:
    return sum(
        1
        for previous in _invocations()
        if previous[0] == "inspect"
        and container_id in previous
        and marker in previous
    )


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return 2
    _log(argv)
    command = argv[0]

    if command == "ps":
        project_filter = next(
            (value for value in argv if value.startswith("label=")), ""
        )
        if project_filter != "label=com.docker.compose.project=fixture-project":
            return 1
        for container_id, name in CONTAINERS.items():
            print(f"{container_id}\t{name}")
        return 0

    if command == "stats":
        container_id = argv[-1]
        values = {
            "feed-id": {
                "Container": "feed-id",
                "ID": "feed-id",
                "Name": "fixture-feed",
                "CPUPerc": "12.50%",
                "MemUsage": "1.891MiB / 7.601GiB",
                "MemPerc": "0.02%",
                "NetIO": "1.0kB / 2.0kB",
                "BlockIO": "3.0kB / 4.0kB",
                "PIDs": "1",
            },
            "api-id": {
                "Container": "api-id",
                "ID": "api-id",
                "Name": "fixture-api",
                "CPUPerc": "7.50%",
                "MemUsage": "2MiB / 8GiB",
                "MemPerc": "0.02%",
                "NetIO": "5B / 6B",
                "BlockIO": "7B / 8B",
                "PIDs": "1",
            },
        }
        print(json.dumps(values[container_id]))
        return 0

    if command == "inspect":
        container_id = argv[-1]
        format_value = argv[argv.index("--format") + 1]
        if format_value == "{{.State.StartedAt}}":
            print("2026-09-12T00:00:00.000000000Z")
            return 0
        if format_value == "{{json .State.Health}}":
            if container_id == "feed-id":
                print('{"Status":"healthy"}')
            else:
                print("null")
            return 0
        if format_value == "{{.State.Status}}":
            calls = _inspect_count(argv, container_id, "{{.State.Status}}")
            print("running" if calls >= 2 else "starting")
            return 0
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
