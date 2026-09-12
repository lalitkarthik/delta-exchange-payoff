"""The Redis/FanOut consumer for the alert stream."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
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
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.bus = bus
        self.group_name = group_name
        self.maxsize = maxsize
        self.subscription: Subscription | Any | None = None
        self.poster = poster
        self.gate = gate if gate is not None else discord_alerts.AlertGate()
        self.webhook_url = webhook_url
        self.clock = clock
        self.wall_clock = wall_clock
        #: When this instance subscribed. `None` until `subscribe()` runs, then fixed:
        #: an alert timestamped before it is dropped in `_run_once`, see the comment
        #: there for why that is decision #66's "no replay" rather than a second policy.
        self.started_at: datetime | None = None

    def subscribe(self) -> Subscription | Any:
        """Register the lossless alert subscription once."""
        if self.subscription is not None:
            return self.subscription
        self.started_at = self.wall_clock()
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
        if self.started_at is not None and event.ts_received < self.started_at:
            # Decision #66: "no replay". `group_start='$'` only keeps a *newly
            # created* group off the backlog; rejoining an existing group (this
            # process restarting) reads with `>` from wherever that group's
            # last-delivered id sits, which is everything published while nothing
            # read it -- up to the bus's retention window. Rather than a second
            # Redis client issuing `XGROUP SETID` (considered and rejected, see
            # docs/design/lld/discord-alerts.md), this consumer records its own
            # start time and refuses to post anything timestamped before it, here,
            # ahead of the gate -- so a stale alert cannot spend a real one's
            # collapse or rate-limit slot.
            log_event(
                logger,
                logging.WARNING,
                log_events.ALERT,
                "the alert consumer dropped an alert published before it started: %s",
                event.code,
            )
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
                # Called directly, not through `getattr(..., None)`. The gate is typed
                # `AlertGate` and `decide()` above is already called directly, so a gate
                # without these methods is a programming error and should say so. Behind a
                # silent fallback it would instead revert to the exact defect the review
                # caught -- the occurrence spent although Discord never saw the alert --
                # and the guarantee this pair exists to install would quietly not hold.
                if attempted:
                    self.gate.commit_post()
                else:
                    self.gate.rollback_post()
        except Exception as exc:
            if decision is not None and decision.post:
                self.gate.rollback_post()
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "the alert consumer could not dispatch an alert: %s",
                type(exc).__name__,
            )
