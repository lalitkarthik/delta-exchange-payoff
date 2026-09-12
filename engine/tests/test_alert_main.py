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
from typing import Any

from fastapi import Response
from fastapi.testclient import TestClient

from deltapayoff import alert_main, discord_alerts
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
            {
                "status": "ok",
                "problems": [],
                "discord": "unconfigured",
                "consumer": "alive",
                "delivered": 0,
                "failed": 0,
                "blocked": 0,
                "last_post": "none",
            },
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


def _fake_process(
    *,
    done: bool = False,
    delivered: int = 0,
    failed: int = 0,
    blocked: int = 0,
    last_outcome: Any = None,
) -> SimpleNamespace:
    """A stand-in for `AlertProcess` carrying only what `/health` reads."""
    poster = SimpleNamespace(
        delivered_count=delivered,
        failed_count=failed,
        blocked_count=blocked,
        last_outcome=last_outcome,
    )
    return SimpleNamespace(
        webhook_url="https://discord.test/webhook",
        task=SimpleNamespace(done=lambda: done),
        consumer=SimpleNamespace(poster=poster),
    )


def _health(monkeypatch, process: SimpleNamespace) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(alert_main.app.state, "process", process, raising=False)
    response = Response()
    payload = asyncio.run(alert_main.health(response))
    return response.status_code, payload


def test_health_marks_a_finished_consumer_dead(monkeypatch) -> None:
    status, payload = _health(monkeypatch, _fake_process(done=True))

    assert payload["consumer"] == "dead"
    assert payload["discord"] == "configured"
    assert payload["status"] == "error"
    assert payload["problems"] == ["the alert consumer task is not running"]
    # #103: the status code is the contract. Compose's check is `urlopen`, which never
    # reads a body, so a dead consumer behind a 200 is invisible to it.
    assert status == 503


def test_health_reports_a_failed_delivery_as_a_problem(monkeypatch) -> None:
    """The question this route could not answer on 2026-09-12.

    At 16:44Z, thirty-one minutes after the only log record in the container was a
    failed post, `/health` answered `200 {"discord":"configured","consumer":"alive"}`.
    Both fields were true. Neither was about delivery.
    """
    status, payload = _health(
        monkeypatch,
        _fake_process(
            delivered=2,
            failed=1,
            last_outcome=discord_alerts.PostOutcome.FAILED,
        ),
    )

    assert status == 503
    assert payload["status"] == "error"
    assert payload["problems"] == ["the last Discord post did not reach Discord"]
    assert payload["delivered"] == 2
    assert payload["failed"] == 1
    assert payload["last_post"] == "failed"
    # The two original fields stay, and stay true.
    assert payload["discord"] == "configured"
    assert payload["consumer"] == "alive"


def test_health_recovers_once_a_later_post_is_delivered(monkeypatch) -> None:
    """The judgement is the latest attempt, not the running total.

    `failed` never goes back down, so a consumer that failed once and has delivered
    everything since would read as broken forever if the count were the judgement.
    """
    status, payload = _health(
        monkeypatch,
        _fake_process(
            delivered=3,
            failed=1,
            last_outcome=discord_alerts.PostOutcome.DELIVERED,
        ),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["problems"] == []
    assert payload["failed"] == 1
    assert payload["last_post"] == "delivered"


def test_health_does_not_call_a_429_a_problem(monkeypatch) -> None:
    """Discord received that request and named its own backoff; nothing is broken."""
    status, payload = _health(
        monkeypatch,
        _fake_process(
            failed=1,
            blocked=2,
            last_outcome=discord_alerts.PostOutcome.RATE_LIMITED,
        ),
    )

    assert status == 200
    assert payload["status"] == "ok"
    assert payload["blocked"] == 2
    assert payload["last_post"] == "rate_limited"


def test_configured_health_reports_configured_without_posting(
    monkeypatch,
) -> None:
    monkeypatch.delenv("DELTA_BUS", raising=False)
    monkeypatch.setenv(WEBHOOK_ENV, "https://discord.test/webhook")

    with TestClient(alert_main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "problems": [],
        "discord": "configured",
        "consumer": "alive",
        "delivered": 0,
        "failed": 0,
        "blocked": 0,
        "last_post": "none",
    }
