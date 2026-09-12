"""Bring up the dxp Compose stack and prove one scripted quote bar reaches disk."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import polars as pl

#: The project a live stack runs under. This tool must never be pointed at it by
#: default: its `finally` branch runs `docker compose down --remove-orphans`, so the
#: old default of "dxp" meant a bare `smoke_stack.py` tore down the running system.
LIVE_PROJECT = "dxp"
PROJECT = "dxp-smoke"
PROXY_PORT = 8080
COMPOSE_FILES = ("compose.yml", "compose.smoke.yml")
ENV_FILE = "stack.env"


def _health_urls(port: int) -> tuple[str, str]:
    """The two probes, for whichever host port this stack published.

    The api is reached THROUGH the proxy - which is the thing worth proving, and which
    /health no longer is: with the api under /api/, a bare /health falls to the web
    catch-all and 404s. The proxy's own liveness is /healthz, answered by nginx itself.
    """
    return (
        f"http://127.0.0.1:{port}/api/health",
        f"http://127.0.0.1:{port}/healthz",
    )


PROXY_HEALTH, PROXY_SELF_HEALTH = _health_urls(PROXY_PORT)


def _isolating_env(project: str, proxy_port: int) -> dict[str, str]:
    """The two variables `compose.yml` interpolates, so a second stack is its own.

    `container_name` overrides Compose's project-service-index naming, so without
    `DXP_CONTAINER_PREFIX` a second project collides on `/dxp-redis` and never starts;
    `DXP_PROXY_PORT` is the other half, because only one project can bind 8080. Naming
    the prefix after the project keeps `docker ps` readable: one prefix, one stack.
    """
    return {
        **os.environ,
        "DXP_CONTAINER_PREFIX": project,
        "DXP_PROXY_PORT": str(proxy_port),
    }


HEALTH_TIMEOUT_SECONDS = 60.0
SETTLE_SECONDS = 5.0
SYMBOL = "C-BTC-77600-040926"
DATASET = "quote-bars"
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 3
ROOT = Path(__file__).resolve().parents[1]


def _compose(
    arguments: list[str],
    project: str = PROJECT,
    proxy_port: int = PROXY_PORT,
) -> subprocess.CompletedProcess[str]:
    command = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        ENV_FILE,
        "-f",
        COMPOSE_FILES[0],
        "-f",
        COMPOSE_FILES[1],
        *arguments,
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=_isolating_env(project, proxy_port),
    )


def _captured(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


def _skip(reason: str) -> int:
    print(
        f"SKIPPED LOUDLY: {reason}",
        "Start Docker Desktop, then re-run: "
        "engine/.venv/Scripts/python.exe tools/smoke_stack.py",
        sep=chr(10),
        file=sys.stderr,
    )
    return EXIT_SKIPPED


def _wait_for_proxy(
    started: float,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
    url: str = PROXY_HEALTH,
) -> tuple[float | None, str]:
    last_error = "no response yet"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return time.monotonic() - started, ""
                last_error = f"HTTP {response.status}"
        except Exception as exc:  # pragma: no cover - only live Docker runs reach this
            last_error = str(exc)
        time.sleep(0.5)
    return None, last_error


def _parser() -> argparse.ArgumentParser:
    """The argument parser, built without touching Docker.

    It exists so that `--help` is answerable. Before this, `main` did `del argv` and every
    invocation - `--help` included - built the images and started six containers. The gate
    found that by probing the tool with `--help`, which a green suite could not.
    """
    parser = argparse.ArgumentParser(
        prog="smoke_stack.py",
        description=(
            "Bring up the dxp Compose stack with the smoke override, wait for the api's "
            "health through the proxy, drive one scripted quote frame, and prove a bar "
            "reaches disk. Exits 0 on success, 1 on failure, and "
            f"{EXIT_SKIPPED} with a loud message when Docker is unavailable."
        ),
    )
    parser.add_argument(
        "--project-name",
        default=PROJECT,
        help=(
            "Compose project name, and so the container name prefix "
            f"(default: {PROJECT})."
        ),
    )
    parser.add_argument(
        "--proxy-port",
        type=int,
        default=PROXY_PORT,
        metavar="PORT",
        help=(
            "host port the proxy publishes, and so the port both health probes use "
            f"(default: {PROXY_PORT}). Give a second stack its own, or it cannot bind."
        ),
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=HEALTH_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "how long to wait for the api to answer through the proxy "
            f"(default: {HEALTH_TIMEOUT_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--keep-up",
        action="store_true",
        help="leave the stack running afterwards, for debugging. Tear it down yourself.",
    )
    return parser


def main(argv: list[str]) -> int:
    """Run the smoke stack, or return a loud skip when Docker is unavailable."""
    options = _parser().parse_args(argv)
    project = options.project_name
    proxy_port = options.proxy_port
    api_health, _proxy_self_health = _health_urls(proxy_port)

    if shutil.which("docker") is None:
        return _skip("no docker on PATH.")

    version = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode != 0:
        detail = _captured(version).strip() or "no output"
        return _skip(f"docker is installed but its daemon is not answering ({detail})")

    stack_data = ROOT / ".stack-data"
    stack_data.mkdir(exist_ok=True)
    dataset_root = stack_data / DATASET
    baseline = set(dataset_root.glob("underlying=BTC/date=*/*.parquet"))
    started = time.monotonic()
    stack_started = False
    healthy_seconds: float | None = None

    try:
        stack_started = True
        up = _compose(
            project=project,
            proxy_port=proxy_port,
            arguments=["up", "-d", "--wait", "--wait-timeout", "120"],
        )
        if up.returncode != 0:
            print(_captured(up), file=sys.stderr)
            print(
                f"start-to-healthy: {time.monotonic() - started:.1f}s",
                file=sys.stderr,
            )
            return EXIT_FAIL

        healthy_seconds, health_error = _wait_for_proxy(
            started, options.health_timeout, api_health
        )
        if healthy_seconds is None:
            print(
                f"start-to-healthy: {time.monotonic() - started:.1f}s",
                file=sys.stderr,
            )
            print(f"proxy health timed out: {health_error}", file=sys.stderr)
            return EXIT_FAIL
        print(f"start-to-healthy: {healthy_seconds:.1f}s")

        store_health = _compose(
            project=project,
            proxy_port=proxy_port,
            arguments=[
                "exec",
                "-T",
                "store",
                "python",
                "-c",
                (
                    "import urllib.request; "
                    "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
                ),
            ]
        )
        if store_health.returncode != 0:
            print(_captured(store_health), file=sys.stderr)
            return EXIT_FAIL

        # The bus batches every DELTA_BUS_BATCH_MS (50 ms) and the writer wakes on its
        # own tick, so five seconds is a bounded readiness wait with a hundred-fold
        # margin, not a race dressed up as a delay.
        time.sleep(SETTLE_SECONDS)

        stop_store = _compose(
            project=project, proxy_port=proxy_port, arguments=["stop", "store"]
        )
        if stop_store.returncode != 0:
            print(_captured(stop_store), file=sys.stderr)
            return EXIT_FAIL

        new_files = sorted(
            set(dataset_root.glob("underlying=BTC/date=*/*.parquet")) - baseline
        )
        if not new_files:
            logs = _compose(
                project=project, proxy_port=proxy_port, arguments=["logs", "store"]
            )
            print(f"no new parquet file under {dataset_root}", file=sys.stderr)
            print(*_captured(logs).splitlines()[-50:], sep=chr(10), file=sys.stderr)
            return EXIT_FAIL

        matching: list[tuple[Path, int]] = []
        for parquet in new_files:
            try:
                frame = pl.read_parquet(parquet)
            except Exception as exc:
                print(f"could not read {parquet}: {exc}", file=sys.stderr)
                return EXIT_FAIL
            matches = frame.filter(pl.col("symbol") == SYMBOL).height
            print(f"parquet: {parquet} rows={frame.height}")
            if matches:
                matching.append((parquet, frame.height))

        if not matching:
            print(
                f"new parquet files contained no row for {SYMBOL}: {new_files}",
                file=sys.stderr,
            )
            return EXIT_FAIL

        parquet, row_count = matching[0]
        print(
            f"smoke passed: parquet={parquet} rows={row_count} "
            f"start-to-healthy={healthy_seconds:.1f}s"
        )
        return EXIT_OK
    finally:
        if stack_started and not options.keep_up:
            down = _compose(
                project=project,
                proxy_port=proxy_port,
                arguments=["down", "--remove-orphans"],
            )
            if down.returncode != 0:
                print(_captured(down), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
