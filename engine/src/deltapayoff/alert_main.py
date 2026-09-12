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
from fastapi import FastAPI, Response

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
async def health(response: Response) -> dict[str, Any]:
    """**The status code is the contract, not the body** (#103, and now #115).

    This route used to answer `{"discord": "configured", "consumer": "alive"}` and it
    answered exactly that at 16:44Z on 2026-09-12, thirty-one minutes after the only
    log record this container had ever written was a failed post that lost
    `reconnect_budget_spent`. Both fields were true throughout. Neither was about
    delivery, which is this service's entire job: `configured` answers "is a webhook
    URL set" and `alive` answers "is the task object not done". Both stay -- they are
    true and useful -- and a third field now answers the question the service exists
    for.

    The judgement is `last_post`, not `failed`. A cumulative count only ever goes up,
    so a consumer that failed once at breakfast and has delivered every alert since
    would read as broken forever; the question worth asking is whether the *most
    recent* attempt reached Discord. A `429` is deliberately not a problem: Discord
    received that request and named its own backoff, and the poster is honouring it.

    Compose's health check is `urllib.request.urlopen(...)`, which fails on the status
    and never reads a body, so a 200 carrying a sad body would be invisible to it --
    which is why the problem states answer **503** rather than describing themselves
    inside a 200.
    """
    process = getattr(app.state, "process", None)
    configured = process is not None and process.webhook_url is not None
    task = None if process is None else process.task
    alive = task is not None and not task.done()
    consumer = None if process is None else process.consumer
    poster = None if consumer is None else consumer.poster

    problems: list[str] = []
    if process is not None and not alive:
        problems.append("the alert consumer task is not running")
    last_outcome = None if poster is None else poster.last_outcome
    if last_outcome is discord_alerts.PostOutcome.FAILED:
        problems.append("the last Discord post did not reach Discord")

    if problems:
        response.status_code = 503
    return {
        "status": "ok" if not problems else "error",
        "problems": problems,
        "discord": "configured" if configured else "unconfigured",
        "consumer": "alive" if alive else "dead",
        "delivered": 0 if poster is None else poster.delivered_count,
        "failed": 0 if poster is None else poster.failed_count,
        "blocked": 0 if poster is None else poster.blocked_count,
        "last_post": "none" if last_outcome is None else last_outcome.value,
    }


__all__ = ["WEBHOOK_ENV", "AlertProcess", "app", "health", "lifespan"]
