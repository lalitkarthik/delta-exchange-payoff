"""Pure Discord alert policy and the injected HTTP posting seam."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
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


class PostOutcome(Enum):
    """What became of one attempted Discord post.

    `post_alert` used to return one boolean answering "was the HTTP seam called", and
    `alert_consumer` read it as "did Discord get this". On 2026-09-12 at 16:13:45.415Z
    those two questions diverged for the first time in production: a `ConnectTimeout`
    was *attempted*, so the consumer committed the occurrence, and
    `reconnect_budget_spent` -- the alert saying the venue connection would not come
    back without a resume -- was both undelivered and suppressed for the following
    300 seconds. The feed then sat dead for 10.9 minutes (#108, #115).

    Three facts have to be separable, so they are three members plus `DELIVERED`:

    - `NOT_ATTEMPTED` -- no webhook configured, or the local `429` backoff skipped the
      call. Discord's endpoint was never touched, so nothing at all was spent.
    - `DELIVERED` -- a `2xx`. A person can see the message.
    - `RATE_LIMITED` -- a `429`. Discord *received* this request and answered it; the
      message is dropped, and this is the one response that sets `_blocked_until`.
    - `FAILED` -- a transport exception, a `5xx`, or any other non-`2xx`. The request
      was made and the message did not arrive.
    """

    NOT_ATTEMPTED = "not_attempted"
    DELIVERED = "delivered"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"

    @property
    def reached_discord(self) -> bool:
        """Whether Discord's endpoint was actually called for this occurrence."""
        return self is not PostOutcome.NOT_ATTEMPTED

    @property
    def spends_collapse_window(self) -> bool:
        """Whether this occurrence may keep the signature's collapse window.

        Only an outcome Discord itself produced may spend it. A `2xx` is the message
        landing; a `429` is Discord seeing the request and refusing it, and holding
        the window open there would let the next repeat re-attempt into the very
        backoff the `429` installed. A `FAILED` post is neither: nothing arrived and
        nothing on Discord's side knows the alert exists, so the next occurrence of
        that signature must still be allowed through.
        """
        return self in (PostOutcome.DELIVERED, PostOutcome.RATE_LIMITED)


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

    def rollback_collapse_window(self) -> None:
        """Un-spend the signature's collapse window after a post that did not deliver.

        This is deliberately *not* `rollback_post`. The HTTP request was made, so two
        of the three things `decide()` spent must stay spent:

        - `last_post_at`, the global `ALERT_MIN_POST_INTERVAL_SECONDS` floor, is kept.
          Refunding it would remove the only rate floor on this consumer at exactly
          the moment Discord is unreachable, and a burst of same-signature alerts
          during an outage would hammer the webhook as fast as the bus delivered them.
        - the folded `collapsed_count` already popped by `decide()` stays gone. It was
          rendered into a message that never arrived and `DiscordPoster._lost` logged
          it as lost; restoring it here would make that record false. This is §3a
          consequence 2 and #115 does not change it.

        What is restored is the signature's own post timestamp, so the *next*
        occurrence of `(code, adapter, severity)` is not folded into a post that never
        happened. That is the whole of #115's first criterion.
        """
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
        #: Posts Discord answered with a `2xx`, and posts that reached Discord's
        #: endpoint and did not arrive. A service whose only job is delivery has to be
        #: able to say how much it has delivered and how much it has not; before #115
        #: `blocked_count` was the only number here and it counts neither.
        self.delivered_count = 0
        self.failed_count = 0
        #: The most recent attempt's outcome, or `None` before the first one. `/health`
        #: reads this rather than `failed_count`: a cumulative failure count can never
        #: go back down, so it cannot answer "is this consumer getting alerts out
        #: *now*", which is the question #103 taught this repository to ask.
        self.last_outcome: PostOutcome | None = None

    def ready(self, now: float) -> bool:
        """Return whether the local backoff permits another Discord call."""
        return now >= self._blocked_until

    def _record(self, outcome: PostOutcome) -> PostOutcome:
        """Count one attempt and remember it as the latest."""
        if outcome is PostOutcome.DELIVERED:
            self.delivered_count += 1
        elif outcome is PostOutcome.RATE_LIMITED or outcome is PostOutcome.FAILED:
            self.failed_count += 1
        self.last_outcome = outcome
        return outcome

    async def post_alert(
        self,
        webhook_url: str | None,
        alert: Any,
        collapsed_count: int,
        *,
        now: float,
    ) -> PostOutcome:
        """Attempt one rendered alert and report what became of it.

        The return value is the *delivery* outcome, not "was the seam called". See
        `PostOutcome` for why those had to stop being one boolean (#115).
        """
        if not webhook_url:
            return PostOutcome.NOT_ATTEMPTED
        if not self.ready(now):
            self.blocked_count += 1
            return PostOutcome.NOT_ATTEMPTED

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
            return self._record(PostOutcome.FAILED)

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
            return self._record(PostOutcome.DELIVERED)
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
            return self._record(PostOutcome.RATE_LIMITED)
        if response.status >= 500:
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "Discord returned HTTP %d; %s",
                response.status,
                lost,
            )
            return self._record(PostOutcome.FAILED)
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "Discord returned unexpected HTTP %d; %s",
            response.status,
            lost,
        )
        return self._record(PostOutcome.FAILED)

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
