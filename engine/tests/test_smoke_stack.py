"""Daemon-free checks for the committed Compose smoke entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "smoke_stack.py"


def test_smoke_entrypoint_skips_loudly_without_docker() -> None:
    environment = dict(os.environ)
    environment["PATH"] = ""

    result = subprocess.run(
        [sys.executable, "tools/smoke_stack.py"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 3
    assert "SKIPPED LOUDLY" in result.stderr
    assert "no docker on PATH" in result.stderr


def test_smoke_script_has_a_real_python_entrypoint() -> None:
    assert 'if __name__ == "__main__":' in SCRIPT.read_text(encoding="utf-8")


def test_help_exits_zero_and_starts_nothing() -> None:
    """`--help` must be answerable without touching Docker.

    Before this test existed, `main` did `del argv`, so every invocation - `--help`
    included - built the images and started six containers. The whole suite was green
    while that was true; only probing the tool with `--help` found it.

    PATH is emptied, so `docker` cannot be found. If `--help` fell through to the body
    the exit code would be 3, the loud skip. Exit 0 is what proves it short-circuited.
    """
    environment = dict(os.environ)
    environment["PATH"] = ""

    result = subprocess.run(
        [sys.executable, "tools/smoke_stack.py", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "SKIPPED LOUDLY" not in result.stderr
    assert "usage: smoke_stack.py" in result.stdout
    for flag in ("--project-name", "--health-timeout", "--keep-up"):
        assert flag in result.stdout, flag


def test_the_flags_reach_the_parser_rather_than_being_discarded() -> None:
    """A flag on the command line must change what the run does."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import smoke_stack
    finally:
        sys.path.pop(0)

    options = smoke_stack._parser().parse_args(
        ["--project-name", "dxp-probe", "--health-timeout", "7.5", "--keep-up"]
    )

    assert options.project_name == "dxp-probe"
    assert options.health_timeout == 7.5
    assert options.keep_up is True

    defaults = smoke_stack._parser().parse_args([])
    assert defaults.project_name == smoke_stack.PROJECT
    assert defaults.keep_up is False


# --- Isolation: what `--project-name` was supposed to buy -----------------------------
#
# Both #65 and #66 deferred the end-to-end smoke run with the same sentence: a second
# Compose project cannot bind host port 8080 while the live stack holds it. The port was
# only half the reason. `compose.yml` also pinned `container_name: dxp-redis` and its six
# siblings, and `container_name` overrides Compose's own project-service-index naming
# outright -- so a second project collided on `/dxp-redis` and could not start at all.
# The flag existed, was tested, and isolated nothing.


def _module():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import smoke_stack
    finally:
        sys.path.pop(0)
    return smoke_stack


def test_the_default_project_is_not_the_live_stacks_project() -> None:
    """The default must not name the project a live stack uses.

    The tool's `finally` branch runs `docker compose down --remove-orphans` on whatever
    project it was given. With `PROJECT = "dxp"` a bare `smoke_stack.py` tore down the
    live stack, which is the one thing this repository's `AGENTS.md` says must not
    happen. A destructive default is not "CI-shape", whatever else the script does.
    """
    smoke_stack = _module()

    assert smoke_stack.PROJECT != smoke_stack.LIVE_PROJECT
    assert smoke_stack.LIVE_PROJECT == "dxp"


def test_the_proxy_port_flag_moves_both_health_urls() -> None:
    """A second stack answers on a second port, so the probes must follow it."""
    smoke_stack = _module()

    api_health, self_health = smoke_stack._health_urls(8099)

    assert api_health == "http://127.0.0.1:8099/api/health"
    assert self_health == "http://127.0.0.1:8099/healthz"
    # The defaults are still the live stack's, so nothing about 8080 has moved.
    assert smoke_stack.PROXY_HEALTH == "http://127.0.0.1:8080/api/health"
    assert smoke_stack.PROXY_SELF_HEALTH == "http://127.0.0.1:8080/healthz"


def test_the_compose_environment_isolates_container_names_and_the_port() -> None:
    """The project name becomes the container prefix Compose interpolates."""
    smoke_stack = _module()

    environment = smoke_stack._isolating_env("smoke65", 8099)

    assert environment["DXP_CONTAINER_PREFIX"] == "smoke65"
    assert environment["DXP_PROXY_PORT"] == "8099"


def test_compose_passes_the_isolating_environment_to_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment must reach the subprocess, not merely be computable.

    `_isolating_env` returning the right mapping proves nothing on its own; #65 already
    shipped a parser whose values were discarded by `del argv`, and a green suite did
    not notice. This asserts the value at the boundary where Docker reads it.
    """
    smoke_stack = _module()
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(smoke_stack.subprocess, "run", fake_run)
    smoke_stack._compose(["ps"], project="smoke65", proxy_port=8099)

    assert "--project-name" in seen["command"]
    assert seen["command"][seen["command"].index("--project-name") + 1] == "smoke65"
    environment = seen["env"]
    assert environment is not None
    assert environment["DXP_CONTAINER_PREFIX"] == "smoke65"
    assert environment["DXP_PROXY_PORT"] == "8099"


def test_help_lists_the_proxy_port_flag() -> None:
    environment = dict(os.environ)
    environment["PATH"] = ""

    result = subprocess.run(
        [sys.executable, "tools/smoke_stack.py", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "--proxy-port" in result.stdout
