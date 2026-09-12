"""Daemon-free checks for the three Python service images."""

from __future__ import annotations

import json
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
DOCKERFILES = {
    "Dockerfile.api": "deltapayoff.main:app",
    "Dockerfile.feed": "deltapayoff.feed_main:app",
    "Dockerfile.store": "deltapayoff.store_main:app",
}
BASE_HEADER = [
    "# syntax=docker/dockerfile:1",
    "FROM python:3.13-slim-bookworm AS base",
    "ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1",
    "WORKDIR /app/engine",
    "COPY requirements.txt ./",
    "RUN python -m pip install --no-cache-dir -r requirements.txt",
]


def _shared_stage(text: str) -> list[str]:
    lines = text.splitlines()
    end = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("RUN python -m pip install")
    )
    return lines[: end + 1]


def test_all_three_engine_dockerfiles_share_the_same_base_stage() -> None:
    texts = {
        name: (ENGINE / name).read_text(encoding="utf-8")
        for name in DOCKERFILES
    }

    assert all((ENGINE / name).is_file() for name in DOCKERFILES)
    assert all(text.splitlines()[:6] == BASE_HEADER for text in texts.values())
    assert len({tuple(_shared_stage(text)) for text in texts.values()}) == 1


def test_engine_dockerfiles_use_exec_form_commands_for_the_expected_modules() -> None:
    for filename, module in DOCKERFILES.items():
        text = (ENGINE / filename).read_text(encoding="utf-8")
        cmd_lines = [line for line in text.splitlines() if line.startswith("CMD [")]

        assert len(cmd_lines) == 1
        command = json.loads(cmd_lines[0][len("CMD ") :])
        assert command[:3] == ["python", "-m", "uvicorn"]
        assert f"{module}" in command
        assert "COPY tests" not in text
        assert "--reload" not in text


def test_engine_dockerignore_excludes_tests_and_the_local_environment() -> None:
    dockerignore = (ENGINE / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "tests/" in dockerignore
    assert ".venv/" in dockerignore
