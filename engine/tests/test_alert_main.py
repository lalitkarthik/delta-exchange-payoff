from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from deltapayoff import alert_main
from deltapayoff.alert_main import WEBHOOK_ENV


def test_real_alert_entrypoint_reports_unconfigured_health_without_logging_a_url(
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    engine_root = repo_root / "engine"
    stack_env = (repo_root / "stack.env").read_text(encoding="utf-8")
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    compose = (repo_root / "compose.yml").read_text(encoding="utf-8")

    assert any(line.strip() == "stack.local.env" for line in gitignore.splitlines())
    assert "stack.local.env" in stack_env
    assert "\nDISCORD_WEBHOOK_URL=\n" in stack_env
    assert "discord-alerts:" in compose
    assert (
        "      - stack.env\n      - path: stack.local.env\n        required: false"
        in compose
    )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    child_env = os.environ.copy()
    for variable in ("DELTA_BUS", "DELTA_DISCORD_WEBHOOK_URL", WEBHOOK_ENV):
        child_env.pop(variable, None)
    child_env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "deltapayoff.alert_main:app",
            "--app-dir",
            str(engine_root / "src"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=engine_root,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    response: tuple[int, dict[str, str]] | None = None
    deadline = time.monotonic() + 10.0
    try:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=0.2
                ) as answer:
                    response = (answer.status, json.load(answer))
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.05)

        assert response == (
            200,
            {"discord": "unconfigured", "consumer": "alive"},
        )
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        stdout, _ = process.communicate()

    assert stdout.count(WEBHOOK_ENV) == 1
    assert "alerts will be consumed and acked but not posted" in stdout
    assert "value" not in stdout.lower()
    assert "http" not in stdout.lower()
    # Windows' proc.terminate() is an explicit TerminateProcess(..., 1); that code is
    # expected here and is distinct from a startup traceback or another crash outcome.
    assert process.returncode in (0, 1)


def test_health_marks_a_finished_consumer_dead(monkeypatch) -> None:
    process = SimpleNamespace(
        webhook_url="https://discord.test/webhook",
        task=SimpleNamespace(done=lambda: True),
    )
    monkeypatch.setattr(alert_main.app.state, "process", process, raising=False)

    assert asyncio.run(alert_main.health()) == {
        "discord": "configured",
        "consumer": "dead",
    }


def test_configured_health_reports_configured_without_posting(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DELTA_BUS", raising=False)
    monkeypatch.setenv(WEBHOOK_ENV, "https://discord.test/webhook")

    with TestClient(alert_main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"discord": "configured", "consumer": "alive"}
