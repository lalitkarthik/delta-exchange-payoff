"""Daemon-free checks for the Next.js image definition."""

from __future__ import annotations

import json
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_web_dockerfile_bakes_the_absolute_api_prefixed_engine_url_before_build() -> None:
    dockerfile = WEB / "Dockerfile"
    lines = dockerfile.read_text(encoding="utf-8").splitlines()

    assert dockerfile.is_file()
    arg_index = lines.index("ARG NEXT_PUBLIC_ENGINE_URL")
    env_index = lines.index("ENV NEXT_PUBLIC_ENGINE_URL=$NEXT_PUBLIC_ENGINE_URL")
    build_index = lines.index("RUN bun run build")
    comment = lines[arg_index - 1]

    assert env_index < build_index
    assert "/api" in comment
    assert "rebuild" in comment
    assert "restart" in comment


def test_web_dockerfile_uses_bun_lockfile_install_and_a_node_runtime_command() -> None:
    text = (WEB / "Dockerfile").read_text(encoding="utf-8")
    cmd_line = next(line for line in text.splitlines() if line.startswith("CMD ["))

    command = json.loads(cmd_line[len("CMD ") :])
    assert command[0] == "node"
    assert "--frozen-lockfile" in text
    assert "npm install" not in text
    assert "pnpm" not in text


def test_web_dockerignore_excludes_dependencies_and_tests() -> None:
    dockerignore = (WEB / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "node_modules/" in dockerignore
    assert "tests/" in dockerignore
