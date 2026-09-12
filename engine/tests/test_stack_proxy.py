"""Daemon-free checks for the one-origin nginx routing contract."""

from __future__ import annotations

import re
from pathlib import Path

from deltapayoff import main

ROOT = Path(__file__).resolve().parents[2]


def _locations(text: str) -> list[tuple[str, str]]:
    return [
        (path.strip(), body)
        for path, body in re.findall(
            r"(?ms)^[ \t]*location[ \t]+([^{]+)\{(.*?)\}",
            text,
        )
    ]


def test_the_api_routes_have_no_ambiguous_api_prefix() -> None:
    routes = [route.path for route in main.app.routes]

    assert all(
        not path.startswith("/api") for path in routes
    ), f"API-prefixed route found: {[path for path in routes if path.startswith('/api')]}"


def test_the_web_app_does_not_claim_the_api_prefix() -> None:
    app_dir = ROOT / "web" / "app"

    assert not (app_dir / "api").exists()
    assert not list(app_dir.rglob("route.ts"))
    assert not list(app_dir.rglob("route.tsx"))


def test_nginx_has_one_prefix_stripping_api_location_and_one_web_catch_all() -> None:
    locations = _locations((ROOT / "proxy" / "nginx.conf").read_text(encoding="utf-8"))
    api_locations = [(path, body) for path, body in locations if "dxp_api" in body]
    web_locations = [(path, body) for path, body in locations if "dxp_web" in body]

    assert len(api_locations) == 1
    api_path, api_body = api_locations[0]
    assert api_path == "^~ /api/"
    assert re.search(r"proxy_pass\s+http://dxp_api/;\s*$", api_body, re.MULTILINE)

    assert web_locations == [("/", " proxy_pass http://dxp_web; ")]


def test_nginx_forwards_websocket_upgrade_headers() -> None:
    text = (ROOT / "proxy" / "nginx.conf").read_text(encoding="utf-8")

    assert "proxy_set_header Upgrade $http_upgrade;" in text
    assert "proxy_set_header Connection $connection_upgrade;" in text
    assert "map $http_upgrade $connection_upgrade" in text


def test_the_proxy_answers_its_own_liveness_without_reaching_a_service() -> None:
    """The proxy's HEALTHCHECK must not depend on api or web being up.

    With the api under /api/, a bare /health falls through to the web catch-all, and the
    web app declares no such page - so a probe against /health would 404 and read as the
    whole stack being down. nginx answers /healthz itself instead.
    """
    locations = _locations((ROOT / "proxy" / "nginx.conf").read_text(encoding="utf-8"))
    healthz = [(path, body) for path, body in locations if path == "= /healthz"]

    assert len(healthz) == 1, "expected exactly one `location = /healthz`"
    body = healthz[0][1]
    assert "return 200" in body
    assert "proxy_pass" not in body, "the proxy's own liveness must not be proxied"

    assert not any(
        path in ("= /health", "/health") for path, _ in locations
    ), "/health belongs to the web catch-all; the proxy's own liveness is /healthz"


def test_the_smoke_probe_polls_the_api_through_the_prefix() -> None:
    """The health the smoke test waits on is the api's, reached through the proxy."""
    text = (ROOT / "tools" / "smoke_stack.py").read_text(encoding="utf-8")

    assert 'PROXY_HEALTH = "http://127.0.0.1:8080/api/health"' in text
    assert 'PROXY_SELF_HEALTH = "http://127.0.0.1:8080/healthz"' in text
