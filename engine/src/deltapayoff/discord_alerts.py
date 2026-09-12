"""Pure Discord alert policy and the injected HTTP posting seam."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import log_events
from .logging_setup import log_event

logger = logging.getLogger(__name__)


ALERT_MIN_POST_INTERVAL_SECONDS: float = 2.0
"""The floor between any two Discord posts, regardless of their signatures."""

ALERT_COLLAPSE_WINDOW_SECONDS: float = 300.0
"""The window in which repeats of one signature are folded into a later post."""

DISCORD_RETRY_AFTER_FALLBACK_SECONDS: float = 1.0
"""The safe delay used when Discord's 429 body has no usable retry_after."""


@dataclass(frozen=True)
class GateDecision:
    """The policy outcome for one alert occurrence."""

    post: bool
    collapsed_count: int
    rate_limited: bool


@dataclass(frozen=True)
class _PostSnapshot:
    """The gate state to restore when the poster cannot accept a post."""

    signature: tuple[str, str | None, str]
    had_signature_post: bool
    signature_post_at: float | None
    had_suppressed_count: bool
    suppressed_count: int
    last_post_at: float | None


@dataclass(frozen=True)
class DiscordResponse:
    """The response facts the poster needs from its injected HTTP seam."""

    status: int
    json_body: Mapping[str, Any] | None = None


class AlertGate:
    """Decide whether an alert may become a Discord post."""

    def __init__(self) -> None:
        self._last_post_at_by_signature: dict[tuple[str, str | None, str], float] = {}
        self._suppressed_count_by_signature: dict[tuple[str, str | None, str], int] = {}
        self.last_post_at: float | None = None
        self.rate_limited_count = 0
        self._pending_post: _PostSnapshot | None = None

    def decide(
        self,
        *,
        now: float,
        code: str,
        adapter: str | None,
        severity: str,
    ) -> GateDecision:
        """Apply collapse first, then the global local rate limit."""
        self._pending_post = None
        signature = (code, adapter, severity)
        last_signature_post = self._last_post_at_by_signature.get(signature)
        if (
            last_signature_post is not None
            and now - last_signature_post < ALERT_COLLAPSE_WINDOW_SECONDS
        ):
            self._suppressed_count_by_signature[signature] = (
                self._suppressed_count_by_signature.get(signature, 0) + 1
            )
            return GateDecision(post=False, collapsed_count=0, rate_limited=False)

        if (
            self.last_post_at is not None
            and now - self.last_post_at < ALERT_MIN_POST_INTERVAL_SECONDS
        ):
            self.rate_limited_count += 1
            return GateDecision(post=False, collapsed_count=0, rate_limited=True)

        self._pending_post = _PostSnapshot(
            signature=signature,
            had_signature_post=signature in self._last_post_at_by_signature,
            signature_post_at=self._last_post_at_by_signature.get(signature),
            had_suppressed_count=signature in self._suppressed_count_by_signature,
            suppressed_count=self._suppressed_count_by_signature.get(signature, 0),
            last_post_at=self.last_post_at,
        )
        collapsed_count = self._suppressed_count_by_signature.pop(signature, 0)
        self._last_post_at_by_signature[signature] = now
        self.last_post_at = now
        return GateDecision(
            post=True, collapsed_count=collapsed_count, rate_limited=False
        )

    def commit_post(self) -> None:
        """Forget the rollback point after the poster attempted this occurrence."""
        self._pending_post = None

    def rollback_post(self) -> None:
        """Restore the last decision when the poster refused to attempt it."""
        snapshot = self._pending_post
        if snapshot is None:
            return

        if snapshot.had_signature_post:
            signature_post_at = snapshot.signature_post_at
            if signature_post_at is None:  # pragma: no cover - impossible snapshot
                raise RuntimeError("a gate post snapshot lost its timestamp")
            self._last_post_at_by_signature[snapshot.signature] = signature_post_at
        else:
            self._last_post_at_by_signature.pop(snapshot.signature, None)
        if snapshot.had_suppressed_count:
            self._suppressed_count_by_signature[snapshot.signature] = (
                snapshot.suppressed_count
            )
        else:
            self._suppressed_count_by_signature.pop(snapshot.signature, None)
        self.last_post_at = snapshot.last_post_at
        self._pending_post = None


class DiscordPoster:
    """Render alerts and hand them to an injected Discord HTTP callable."""

    def __init__(
        self,
        post_fn: Callable[[str, dict[str, Any]], Awaitable[DiscordResponse]],
    ) -> None:
        self.post_fn = post_fn
        self._blocked_until = 0.0
        self.blocked_count = 0

    def ready(self, now: float) -> bool:
        """Return whether the local backoff permits another Discord call."""
        return now >= self._blocked_until

    async def post_alert(
        self,
        webhook_url: str | None,
        alert: Any,
        collapsed_count: int,
        *,
        now: float,
    ) -> bool:
        """Attempt one rendered alert and report whether the HTTP seam was called."""
        if not webhook_url:
            return False
        if not self.ready(now):
            self.blocked_count += 1
            return False

        adapter = "engine" if alert.adapter is None else alert.adapter
        text = f"[{alert.severity}] {alert.code} -- {adapter}: {alert.detail}"
        if collapsed_count > 0:
            text += f" (collapsed {collapsed_count} times since last post)"

        lost = self._lost(alert, collapsed_count)

        try:
            response = await self.post_fn(webhook_url, {"content": text})
        except Exception as exc:
            # The exception text can contain the request URL, so log only its type.
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "Discord webhook post failed: %s; %s",
                type(exc).__name__,
                lost,
            )
            return True

        if 200 <= response.status < 300:
            # Delivery is logged, not silent. A consumer that records only its failures
            # cannot be audited afterwards: on 2026-09-12 the live stack showed seven
            # alerts read, none pending, and one log line, and the two that were
            # actually delivered had to be reconstructed from the gate's rules rather
            # than read off anything. One line per delivered alert is affordable --
            # `ALERT_MIN_POST_INTERVAL_SECONDS` already floors the post rate.
            log_event(
                logger,
                logging.INFO,
                log_events.ALERT,
                "Discord accepted HTTP %d: [%s] %s -- %s",
                response.status,
                alert.severity,
                alert.code,
                adapter,
            )
            return True
        if response.status == 429:
            retry_after = self._retry_after(response)
            self._blocked_until = now + retry_after
            log_event(
                logger,
                logging.WARNING,
                log_events.ENGINE_ERROR,
                "Discord rate-limited this consumer for %.3f seconds; %s",
                retry_after,
                lost,
            )
            return True
        if response.status >= 500:
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "Discord returned HTTP %d; %s",
                response.status,
                lost,
            )
            return True
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "Discord returned unexpected HTTP %d; %s",
            response.status,
            lost,
        )
        return True

    @staticmethod
    def _lost(alert: Any, collapsed_count: int) -> str:
        """Name the alert this consumer is about to throw away.

        Every branch below the HTTP call drops its message: #66 decided this consumer
        acks on receipt and never replays, so there is no pending entry to come back
        to and no retry behind it. That is the right trade for an alert -- a stale
        Discord notification is worth less than a consumer blocked behind it -- but it
        is only defensible if the loss leaves a record, and for one live failure on
        2026-09-12 it did not: the line named `ConnectTimeout` and nothing else, while
        the alert it lost was `reconnect_budget_spent`.

        Only engine-generated fields go in. `detail` is left out to keep this to one
        line, and the webhook URL cannot reach it, which is the rule the exception
        branch above exists to honour.
        """
        adapter = "engine" if alert.adapter is None else alert.adapter
        lost = (
            f"alert dropped, not retried: [{alert.severity}] {alert.code} -- {adapter}"
        )
        if collapsed_count > 0:
            lost += f" (collapsed {collapsed_count} folded repeats lost with it)"
        return lost

    @staticmethod
    def _retry_after(response: Any) -> float:
        body = getattr(response, "json_body", None)
        if body is None:
            json_method = getattr(response, "json", None)
            if callable(json_method):
                try:
                    body = json_method()
                except Exception:
                    body = None
        retry_after = body.get("retry_after") if isinstance(body, Mapping) else None
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            return DISCORD_RETRY_AFTER_FALLBACK_SECONDS
        if delay < 0:
            return DISCORD_RETRY_AFTER_FALLBACK_SECONDS
        return delay
