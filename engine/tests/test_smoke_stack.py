"""Daemon-free checks for the committed Compose smoke entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
