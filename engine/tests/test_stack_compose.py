"""Daemon-free checks for the production Compose topology."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yml"
ENV_FILE = ROOT / "stack.env"
SERVICE_NAMES = {"redis", "feed", "store", "api", "web", "proxy"}
ENV_KEYS = {
    "DELTA_BUS",
    "DELTA_REDIS_URL",
    "DELTA_LIVE_UNDERLYINGS",
    "DELTA_STORE_ROOT",
    "DELTA_BUS_BATCH_MS",
    "DELTA_BUS_RETENTION_SECONDS",
    "DELTA_BUS_INSTANCE",
    "DELTA_LIVE_FEED",
    "PROXY_ORIGIN",
    "DISCORD_WEBHOOK_URL",
}


def _service_blocks(text: str) -> dict[str, str]:
    lines = text.splitlines()
    services_start = lines.index("services:")
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[services_start + 1 :]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = [line]
        elif current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_compose_names_the_project_and_only_publishes_the_proxy_port() -> None:
    text = COMPOSE.read_text(encoding="utf-8")

    assert "name: dxp" in text
    assert len(re.findall(r'^\s*-\s*"?(3000|8000):', text, re.MULTILINE)) == 0
    assert text.count('- "8080:80"') == 1
    assert 'NEXT_PUBLIC_ENGINE_URL: "http://localhost:8080/api"' in text


def test_compose_prefixes_every_container_and_gives_redis_the_bounded_command() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    blocks = _service_blocks(text)
    names = re.findall(r"^\s+container_name:\s*([^\s#]+)", text, re.MULTILINE)

    assert set(blocks) == SERVICE_NAMES
    assert set(names) == {
        "dxp-redis",
        "dxp-feed",
        "dxp-store",
        "dxp-api",
        "dxp-web",
        "dxp-proxy",
    }
    assert names and all(name.startswith("dxp-") for name in names)
    redis = blocks["redis"]
    assert "--maxmemory" in redis
    assert "1gb" in redis
    assert "--maxmemory-policy" in redis
    assert "noeviction" in redis
    assert "--appendonly" in redis
    assert "--save" in redis
    assert '""' in redis


def test_store_is_the_only_stack_writer_to_the_host_store_root() -> None:
    blocks = _service_blocks(COMPOSE.read_text(encoding="utf-8"))

    assert re.search(
        r"^\s+- ['\"]?\./\.stack-data:/data['\"]?\s*$",
        blocks["store"],
        re.MULTILINE,
    )
    assert re.search(
        r"^\s+- ['\"]?\./\.stack-data:/data:ro['\"]?\s*$",
        blocks["api"],
        re.MULTILINE,
    )
    assert ":ro" not in blocks["store"]


def test_every_compose_service_defines_a_healthcheck() -> None:
    blocks = _service_blocks(COMPOSE.read_text(encoding="utf-8"))

    assert set(blocks) == SERVICE_NAMES
    for service, block in blocks.items():
        assert re.search(r"^    healthcheck:\s*$", block, re.MULTILINE), service


def test_stack_env_has_non_secret_defaults_and_an_empty_webhook_slot() -> None:
    text = ENV_FILE.read_text(encoding="utf-8")
    keys = {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.lstrip().startswith("#") and "=" in line
    }

    assert ENV_KEYS <= keys
    assert re.search(r"^DISCORD_WEBHOOK_URL=$", text, re.MULTILINE)
    assert ".stack-data/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
