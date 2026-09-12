"""Daemon-free checks for the smoke-only Compose override."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_smoke_override_only_changes_feed_and_mounts_tests_read_only() -> None:
    override = (ROOT / "compose.smoke.yml").read_text(encoding="utf-8")
    services = re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", override, re.MULTILINE)

    assert services == ["feed"]
    assert "./engine/tests:/app/engine/tests:ro" in override
    assert "PYTHONPATH: /app/engine/tests" in override
    assert "DELTA_FEED_ADAPTER: fakes.smoke_feed:build_adapter" in override


def test_production_paths_do_not_select_or_mount_the_smoke_factory() -> None:
    production = "\n".join(
        [
            (ROOT / "compose.yml").read_text(encoding="utf-8"),
            *( (ROOT / "engine" / name).read_text(encoding="utf-8")
               for name in ("Dockerfile.api", "Dockerfile.feed", "Dockerfile.store") ),
        ]
    )

    assert "engine/tests" not in production
    assert "DELTA_FEED_ADAPTER" not in production
