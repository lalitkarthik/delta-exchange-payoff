from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from deltapayoff.discord_alerts import (
    ALERT_COLLAPSE_WINDOW_SECONDS,
    ALERT_MIN_POST_INTERVAL_SECONDS,
    AlertGate,
    DiscordPoster,
    GateDecision,
)


def _alert(*, adapter: str | None = "delta") -> Any:
    return SimpleNamespace(
        severity="error",
        code="connection_silent",
        adapter=adapter,
        detail="the feed stopped",
    )


def _fake_post(response: Any = None, error: Exception | None = None):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def post(url: str, payload: dict[str, Any]) -> Any:
        calls.append((url, payload))
        if error is not None:
            raise error
        return response

    return calls, post


def test_a_single_alert_is_posted() -> None:
    decision = AlertGate().decide(
        now=10.0,
        code="connection_silent",
        adapter="delta",
        severity="error",
    )

    assert decision == GateDecision(post=True, collapsed_count=0, rate_limited=False)


def test_identical_alerts_inside_the_collapse_window_are_counted_and_dropped() -> None:
    gate = AlertGate()

    first = gate.decide(
        now=10.0, code="connection_silent", adapter="delta", severity="error"
    )
    second = gate.decide(
        now=10.0 + ALERT_COLLAPSE_WINDOW_SECONDS - 1.0,
        code="connection_silent",
        adapter="delta",
        severity="error",
    )

    assert first.post is True
    assert second == GateDecision(post=False, collapsed_count=0, rate_limited=False)


def test_alert_after_the_collapse_window_posts_the_folded_count() -> None:
    gate = AlertGate()

    gate.decide(
        now=10.0, code="connection_silent", adapter="delta", severity="error"
    )
    gate.decide(
        now=11.0, code="connection_silent", adapter="delta", severity="error"
    )
    decision = gate.decide(
        now=10.0 + ALERT_COLLAPSE_WINDOW_SECONDS,
        code="connection_silent",
        adapter="delta",
        severity="error",
    )

    assert decision == GateDecision(post=True, collapsed_count=1, rate_limited=False)


def test_different_signatures_can_be_rate_limited_independently_of_collapse() -> None:
    gate = AlertGate()

    first = gate.decide(
        now=10.0, code="connection_silent", adapter="delta", severity="error"
    )
    second = gate.decide(
        now=10.0 + ALERT_MIN_POST_INTERVAL_SECONDS - 0.1,
        code="poll_failing",
        adapter="delta",
        severity="error",
    )

    assert first.post is True
    assert second == GateDecision(post=False, collapsed_count=0, rate_limited=True)
    assert gate.rate_limited_count == 1


def test_a_rate_limited_signature_does_not_reset_another_signatures_collapse_window(
) -> None:
    gate = AlertGate()

    gate.decide(now=10.0, code="a", adapter="delta", severity="error")
    rate_limited = gate.decide(
        now=10.1, code="b", adapter="delta", severity="error"
    )
    collapsed = gate.decide(
        now=10.2, code="a", adapter="delta", severity="error"
    )

    assert rate_limited.rate_limited is True
    assert collapsed == GateDecision(post=False, collapsed_count=0, rate_limited=False)


def test_an_unconfigured_webhook_is_not_called() -> None:
    calls, post = _fake_post(SimpleNamespace(status=200))

    asyncio.run(DiscordPoster(post).post_alert(None, _alert(), 0, now=10.0))

    assert calls == []


def test_a_successful_post_contains_the_alert_fields() -> None:
    calls, post = _fake_post(SimpleNamespace(status=200))

    asyncio.run(
        DiscordPoster(post).post_alert(
            "https://discord.test/webhook", _alert(), 0, now=10.0
        )
    )

    assert calls == [
        (
            "https://discord.test/webhook",
            {
                "content": (
                    "[error] connection_silent -- delta: the feed stopped"
                )
            },
        )
    ]


def test_a_missing_adapter_is_rendered_as_engine() -> None:
    calls, post = _fake_post(SimpleNamespace(status=200))

    asyncio.run(
        DiscordPoster(post).post_alert(
            "https://discord.test/webhook", _alert(adapter=None), 0, now=10.0
        )
    )

    assert calls[0][1]["content"] == (
        "[error] connection_silent -- engine: the feed stopped"
    )


def test_discord_429_blocks_until_retry_after_has_elapsed() -> None:
    calls, post = _fake_post(
        SimpleNamespace(status=429, json_body={"retry_after": 3.0})
    )
    poster = DiscordPoster(post)

    asyncio.run(
        poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
    )

    assert poster.ready(12.9) is False
    assert poster.ready(13.0) is True
    assert len(calls) == 1


def test_discord_429_without_retry_after_uses_the_fixed_fallback() -> None:
    calls, post = _fake_post(SimpleNamespace(status=429, json_body={}))
    poster = DiscordPoster(post)

    asyncio.run(
        poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
    )

    assert poster.ready(10.9) is False
    assert poster.ready(11.0) is True
    assert len(calls) == 1


def test_discord_5xx_does_not_change_the_backoff_window() -> None:
    calls, post = _fake_post(SimpleNamespace(status=503))
    poster = DiscordPoster(post)

    asyncio.run(
        poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
    )

    assert poster.ready(10.0) is True
    assert len(calls) == 1


def test_a_local_backoff_skips_and_counts_the_alert_without_calling_discord() -> None:
    calls, post = _fake_post(
        SimpleNamespace(status=429, json_body={"retry_after": 5.0})
    )
    poster = DiscordPoster(post)

    async def scenario() -> None:
        await poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
        await poster.post_alert("https://discord.test/webhook", _alert(), 0, now=11.0)

    asyncio.run(scenario())

    assert len(calls) == 1
    assert poster.blocked_count == 1


def test_a_collapsed_count_is_appended_to_the_discord_message() -> None:
    calls, post = _fake_post(SimpleNamespace(status=200))

    asyncio.run(
        DiscordPoster(post).post_alert(
            "https://discord.test/webhook", _alert(), 2, now=10.0
        )
    )

    assert calls[0][1]["content"].endswith(" (collapsed 2 times since last post)")


def test_a_post_exception_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls, post = _fake_post(error=ConnectionError("offline"))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert(
                "https://discord.test/webhook", _alert(), 0, now=10.0
            )
        )

    assert len(calls) == 1
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert "https://discord.test/webhook" not in caplog.text
