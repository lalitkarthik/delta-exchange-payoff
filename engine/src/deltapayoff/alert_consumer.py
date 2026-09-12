"""The Redis/FanOut consumer for the alert stream."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from . import discord_alerts, log_events
from .events import Alert
from .fanout import FanOut, Subscription
from .logging_setup import log_event
from .redis_bus import RedisBus

logger = logging.getLogger(__name__)

GROUP_NAME: str = "discord-alerts"
"""The service-owned group name has no venue or environment suffix."""

SUBSCRIBE_MAXSIZE: int = 100
"""A generous lossless watermark for the two rare alert codes."""


class AlertConsumer:
    """Subscribe to alerts without changing any event producer."""

    def __init__(
        self,
        bus: RedisBus | FanOut,
        group_name: str = GROUP_NAME,
        maxsize: int = SUBSCRIBE_MAXSIZE,
        *,
        poster: discord_alerts.DiscordPoster | None = None,
        gate: discord_alerts.AlertGate | None = None,
        webhook_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bus = bus
        self.group_name = group_name
        self.maxsize = maxsize
        self.subscription: Subscription | Any | None = None
        self.poster = poster
        self.gate = gate if gate is not None else discord_alerts.AlertGate()
        self.webhook_url = webhook_url
        self.clock = clock

    def subscribe(self) -> Subscription | Any:
        """Register the lossless alert subscription once."""
        if self.subscription is not None:
            return self.subscription
        if isinstance(self.bus, RedisBus):
            self.subscription = self.bus.subscribe(
                self.group_name,
                self.maxsize,
                lossless=True,
                group_start="$",
                event_types=("alert",),
            )
        else:
            self.subscription = self.bus.subscribe(
                self.group_name, self.maxsize, lossless=True
            )
        return self.subscription

    async def run(self) -> None:
        """Drain delivered events forever; cancellation is the shutdown signal."""
        if self.subscription is None:
            self.subscribe()
        while True:
            await self._run_once()

    async def _run_once(self) -> None:
        """Dispatch one queue item, keeping the loop alive after a bad alert."""
        event = await self.subscription.queue.get()
        if not isinstance(event, Alert):
            return
        decision: discord_alerts.GateDecision | None = None
        try:
            now = self.clock()
            decision = self.gate.decide(
                now=now,
                code=event.code,
                adapter=event.adapter,
                severity=event.severity,
            )
            if decision.post:
                if self.poster is None:
                    raise RuntimeError("the alert consumer has no Discord poster")
                attempted = await self.poster.post_alert(
                    self.webhook_url,
                    event,
                    decision.collapsed_count,
                    now=now,
                )
                if attempted:
                    commit_post = getattr(self.gate, "commit_post", None)
                    if commit_post is not None:
                        commit_post()
                else:
                    rollback_post = getattr(self.gate, "rollback_post", None)
                    if rollback_post is not None:
                        rollback_post()
        except Exception as exc:
            if decision is not None and decision.post:
                rollback_post = getattr(self.gate, "rollback_post", None)
                if rollback_post is not None:
                    rollback_post()
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "the alert consumer could not dispatch an alert: %s",
                type(exc).__name__,
            )
