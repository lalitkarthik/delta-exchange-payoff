"""FastAPI entrypoint for the standalone Discord alert consumer."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI

from . import alert_consumer, discord_alerts, log_events
from .fanout import FanOut
from .logging_setup import configure_logging, log_event
from .redis_bus import REDIS_BUS, BusConfig, RedisBus, selected_bus

logger = logging.getLogger(__name__)
configure_logging(is_terminal=lambda: True)

#: The committed stack environment reserves this name; the real secret belongs in the
#: optional, git-ignored `stack.local.env` overlay.
WEBHOOK_ENV: str = "DISCORD_WEBHOOK_URL"
"""The webhook setting is deliberately shared by the committed and local env files."""


@dataclass
class AlertProcess:
    """The components owned by the standalone alert process."""

    bus: RedisBus | FanOut
    consumer: alert_consumer.AlertConsumer
    webhook_url: str | None
    task: asyncio.Task | None = None


async def _post_discord(
    url: str, payload: dict[str, Any]
) -> discord_alerts.DiscordResponse:
    """Make one Discord request and return only the response facts the poster needs."""
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
    try:
        body = response.json()
    except ValueError:
        body = None
    return discord_alerts.DiscordResponse(
        status=response.status_code,
        json_body=body if isinstance(body, Mapping) else None,
    )


def _set_state(app: FastAPI, process: AlertProcess) -> None:
    app.state.process = process
    app.state.bus = process.bus
    app.state.consumer = process.consumer
    app.state.alert_task = process.task
    app.state.webhook_url = process.webhook_url


def _clear_state(app: FastAPI) -> None:
    for name in ("process", "bus", "consumer", "alert_task", "webhook_url"):
        setattr(app.state, name, None)


async def _close_process(process: AlertProcess) -> None:
    """Cancel the consumer before closing its Redis connection."""
    if process.task is not None:
        process.task.cancel()
        await asyncio.gather(process.task, return_exceptions=True)
        process.task = None
    if isinstance(process.bus, RedisBus):
        await process.bus.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the alert consumer composition."""
    config = BusConfig.from_env(underlyings=())
    bus: RedisBus | FanOut = (
        RedisBus(config) if selected_bus() == REDIS_BUS else FanOut()
    )
    webhook_url = os.environ.get(WEBHOOK_ENV) or None
    if webhook_url is None:
        log_event(
            logger,
            logging.INFO,
            log_events.ENGINE_ERROR,
            "%s is not configured; alerts will be consumed and acked but not posted",
            WEBHOOK_ENV,
        )

    consumer = alert_consumer.AlertConsumer(
        bus,
        poster=discord_alerts.DiscordPoster(_post_discord),
        gate=discord_alerts.AlertGate(),
        webhook_url=webhook_url,
    )
    consumer.subscribe()
    process = AlertProcess(bus=bus, consumer=consumer, webhook_url=webhook_url)
    try:
        if isinstance(bus, RedisBus):
            await bus.start()
        process.task = asyncio.create_task(consumer.run(), name="alert-consumer")
        _set_state(app, process)
        yield
    finally:
        try:
            await _close_process(process)
        finally:
            _clear_state(app)


app = FastAPI(
    title="delta-exchange-payoff Discord alerts",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
async def health() -> dict[str, str]:
    """Report configuration and whether the background consumer is still alive."""
    process = getattr(app.state, "process", None)
    configured = process is not None and process.webhook_url is not None
    task = None if process is None else process.task
    alive = task is not None and not task.done()
    return {
        "discord": "configured" if configured else "unconfigured",
        "consumer": "alive" if alive else "dead",
    }


__all__ = ["WEBHOOK_ENV", "AlertProcess", "app", "health", "lifespan"]
