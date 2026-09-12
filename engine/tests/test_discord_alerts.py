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
    PostOutcome,
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


# --- What a lost alert leaves behind -----------------------------------------------
#
# This consumer acks on receipt and never replays (#66's own decision). So an alert
# whose post does not reach Discord is gone: no retry, no pending entry, no second
# chance. That trade is defensible for an alert consumer, but only if the loss is
# recorded -- and until these tests it was not. `measured` 2026-09-12 on the live
# `dxp` stack: seven alerts were consumed and acked, one post failed, and the only
# record of it was
#
#     ERROR deltapayoff.discord_alerts engine.error: Discord webhook post failed:
#     ConnectTimeout
#
# which names the exception type and nothing else. The alert it lost was
# `reconnect_budget_spent` -- the one saying the venue connection would not come back
# without a resume -- and no reader of that line could have known that.


def test_a_failed_post_names_the_alert_it_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exception path must say which alert died, not only how it died."""
    _calls, post = _fake_post(error=ConnectionError("offline"))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
        )

    assert "connection_silent" in caplog.text
    assert "error" in caplog.text
    assert "delta" in caplog.text
    assert "ConnectionError" in caplog.text
    # The reason this line exists at all: it must say the alert is not coming back.
    assert "dropped" in caplog.text
    # The URL must still never reach a log record.
    assert "https://discord.test/webhook" not in caplog.text


def test_a_dropped_5xx_names_the_alert_it_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 5xx is logged and dropped without retry, so it loses an alert too."""
    _calls, post = _fake_post(SimpleNamespace(status=503))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
        )

    assert "503" in caplog.text
    assert "connection_silent" in caplog.text
    assert "dropped" in caplog.text


def test_a_429_drop_names_the_alert_it_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 429 is never retried either, so the alert it refuses is lost as well."""
    _calls, post = _fake_post(SimpleNamespace(status=429, json_body={"retry_after": 3.0}))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.WARNING, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
        )

    assert "connection_silent" in caplog.text
    assert "dropped" in caplog.text


def test_a_lost_post_records_the_collapsed_count_that_died_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The folded repeats go with it, so the line must say how many.

    The gate hands the accumulated count to the post that reveals it, and `decide()`
    has already popped it by then. #115 rolls the signature's *collapse window* back
    after a failed post so the next occurrence can still be posted, and deliberately
    does **not** restore that popped count -- it was rendered into a message that
    never arrived, and restoring it would make this very log line false. Four repeats
    can die in one failed post and the log is still the only place that says so.
    """
    _calls, post = _fake_post(error=ConnectionError("offline"))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert("https://discord.test/webhook", _alert(), 4, now=10.0)
        )

    assert "collapsed 4" in caplog.text


def test_a_lost_alert_with_no_adapter_is_named_engine_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log uses the same null-adapter spelling the Discord message does."""
    _calls, post = _fake_post(error=ConnectionError("offline"))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert(
                "https://discord.test/webhook", _alert(adapter=None), 0, now=10.0
            )
        )

    assert "engine" in caplog.text


def test_a_successful_post_is_logged_with_its_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Delivery must leave a record, or the absence of one proves nothing.

    Until now a 2xx returned silently, so the only evidence a post had succeeded was
    that no failure had been logged. That is why the live run of 2026-09-12 read as a
    contradiction: `XINFO GROUPS alert` said seven entries read with none pending, and
    the container's log held one line. Three posts were attempted and two delivered,
    but nothing recorded the two -- they had to be reconstructed from the gate's rules.
    An alert consumer whose successes are invisible cannot be audited after the fact.
    """
    _calls, post = _fake_post(SimpleNamespace(status=204))
    poster = DiscordPoster(post)

    with caplog.at_level(logging.INFO, logger="deltapayoff.discord_alerts"):
        asyncio.run(
            poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
        )

    assert "204" in caplog.text
    assert "connection_silent" in caplog.text
    assert any(record.levelno == logging.INFO for record in caplog.records)
    assert "https://discord.test/webhook" not in caplog.text


# --- One boolean for two questions, and the collapse window it spent (#115) ---------
#
# `post_alert` returned `True` for "the HTTP seam was called" and `alert_consumer`
# read it as "Discord has this". Those agreed until 2026-09-12T16:13:45.415Z, when a
# `ConnectTimeout` was attempted, committed, and suppressed `reconnect_budget_spent`
# for the next 300 seconds. The return value is now a `PostOutcome` and the gate has
# a rollback that un-spends the signature's window without refunding the global floor.


def test_a_transport_exception_reports_a_failed_delivery() -> None:
    """The seam was called; the message did not arrive. Those are now two answers."""
    _calls, post = _fake_post(error=ConnectionError("offline"))

    outcome = asyncio.run(
        DiscordPoster(post).post_alert(
            "https://discord.test/webhook", _alert(), 0, now=10.0
        )
    )

    assert outcome is PostOutcome.FAILED
    assert outcome.reached_discord is True
    assert outcome.spends_collapse_window is False


def test_every_post_outcome_says_whether_it_may_spend_the_collapse_window() -> None:
    """The whole policy of #115, in one table, so a new member cannot slip through.

    A `2xx` landed and a `429` was answered by Discord itself -- both spend the
    window. A transport exception, a `5xx` and an unexpected status did not arrive,
    and an unconfigured or locally blocked call never left this process.
    """
    spends = {
        outcome: outcome.spends_collapse_window for outcome in PostOutcome
    }

    assert spends == {
        PostOutcome.DELIVERED: True,
        PostOutcome.RATE_LIMITED: True,
        PostOutcome.FAILED: False,
        PostOutcome.NOT_ATTEMPTED: False,
    }
    assert PostOutcome.NOT_ATTEMPTED.reached_discord is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (204, PostOutcome.DELIVERED),
        (200, PostOutcome.DELIVERED),
        (429, PostOutcome.RATE_LIMITED),
        (503, PostOutcome.FAILED),
        (400, PostOutcome.FAILED),
    ],
)
def test_each_discord_status_maps_to_one_delivery_outcome(
    status: int, expected: PostOutcome
) -> None:
    _calls, post = _fake_post(SimpleNamespace(status=status, json_body=None))

    outcome = asyncio.run(
        DiscordPoster(post).post_alert(
            "https://discord.test/webhook", _alert(), 0, now=10.0
        )
    )

    assert outcome is expected


def test_an_unconfigured_or_blocked_post_never_reached_discord() -> None:
    _calls, post = _fake_post(SimpleNamespace(status=429, json_body={"retry_after": 5.0}))
    poster = DiscordPoster(post)

    async def scenario() -> tuple[PostOutcome, PostOutcome, PostOutcome]:
        unconfigured = await poster.post_alert(None, _alert(), 0, now=10.0)
        limited = await poster.post_alert(
            "https://discord.test/webhook", _alert(), 0, now=10.0
        )
        blocked = await poster.post_alert(
            "https://discord.test/webhook", _alert(), 0, now=11.0
        )
        return unconfigured, limited, blocked

    unconfigured, limited, blocked = asyncio.run(scenario())

    assert unconfigured is PostOutcome.NOT_ATTEMPTED
    assert limited is PostOutcome.RATE_LIMITED
    assert blocked is PostOutcome.NOT_ATTEMPTED
    assert poster.blocked_count == 1


def test_deliveries_and_failures_are_counted_separately() -> None:
    """`/health` has nothing to report if nothing counts.

    Before #115 the poster carried exactly one number, `blocked_count`, which counts
    calls the *local* backoff skipped. A post that was attempted and failed -- the one
    thing that happened on 2026-09-12 -- incremented nothing at all.
    """
    responses = iter(
        (
            SimpleNamespace(status=204),
            SimpleNamespace(status=503),
            SimpleNamespace(status=204),
        )
    )

    async def post(_url: str, _payload: dict[str, Any]) -> Any:
        response = next(responses)
        if response.status == 503:
            return response
        return response

    poster = DiscordPoster(post)

    async def scenario() -> None:
        for offset in (0.0, 1.0, 2.0):
            await poster.post_alert(
                "https://discord.test/webhook", _alert(), 0, now=10.0 + offset
            )

    asyncio.run(scenario())

    assert poster.delivered_count == 2
    assert poster.failed_count == 1
    # The judgement `/health` reads is the latest attempt, not the running total.
    assert poster.last_outcome is PostOutcome.DELIVERED


def test_a_failed_post_leaves_the_last_outcome_failed() -> None:
    _calls, post = _fake_post(error=ConnectionError("offline"))
    poster = DiscordPoster(post)

    assert poster.last_outcome is None

    asyncio.run(
        poster.post_alert("https://discord.test/webhook", _alert(), 0, now=10.0)
    )

    assert poster.last_outcome is PostOutcome.FAILED
    assert poster.failed_count == 1
    assert poster.delivered_count == 0


def test_rolling_back_a_failed_post_frees_the_signature_for_the_next_occurrence(
) -> None:
    """The gate half of #115, at the unit seam.

    A repeat one second after a *failed* post is still inside the 300 s window that
    post opened, and before this rollback it was folded into a post nobody saw.
    """
    gate = AlertGate()

    first = gate.decide(
        now=10.0, code="reconnect_budget_spent", adapter="DELTA", severity="error"
    )
    gate.rollback_collapse_window()
    second = gate.decide(
        now=10.0 + ALERT_MIN_POST_INTERVAL_SECONDS,
        code="reconnect_budget_spent",
        adapter="DELTA",
        severity="error",
    )

    assert first.post is True
    assert second.post is True


def test_rolling_back_a_failed_post_keeps_the_global_post_floor_spent() -> None:
    """The request really was made, so the 2 s floor must not be refunded.

    Otherwise the rollback would remove this consumer's only rate limit at exactly
    the moment Discord is unreachable, and a burst of alerts during an outage would
    be re-attempted as fast as the bus delivered them. `rollback_post` -- for the
    paths where no call happened at all -- does refund it, and that is the difference
    between the two methods.
    """
    gate = AlertGate()
    gate.decide(
        now=10.0, code="reconnect_budget_spent", adapter="DELTA", severity="error"
    )
    gate.rollback_collapse_window()

    too_soon = gate.decide(
        now=10.0 + ALERT_MIN_POST_INTERVAL_SECONDS - 0.1,
        code="a_different_code",
        adapter="DELTA",
        severity="error",
    )

    assert too_soon == GateDecision(post=False, collapsed_count=0, rate_limited=True)
    assert gate.last_post_at == 10.0


def test_rolling_back_a_failed_post_does_not_resurrect_the_folded_count() -> None:
    """Section 3a consequence 2 is unchanged by #115, and this pins that.

    The folded repeats were rendered into the message that failed, and
    `DiscordPoster._lost` logged them as lost. Restoring them here would make that
    log record false, so the rollback restores the signature's timestamp and nothing
    else.
    """
    gate = AlertGate()
    signature = ("reconnect_budget_spent", "DELTA", "error")

    gate.decide(now=10.0, code=signature[0], adapter=signature[1], severity=signature[2])
    gate.decide(now=20.0, code=signature[0], adapter=signature[1], severity=signature[2])
    revealing = gate.decide(
        now=10.0 + ALERT_COLLAPSE_WINDOW_SECONDS,
        code=signature[0],
        adapter=signature[1],
        severity=signature[2],
    )
    gate.rollback_collapse_window()

    assert revealing.collapsed_count == 1
    assert signature not in gate._suppressed_count_by_signature
