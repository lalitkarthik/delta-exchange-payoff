"""FastAPI entrypoint for the standalone Delta ingestion process."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from fastapi import FastAPI

from .adapters import DeltaAdapter, DeltaFeed
from .delta_client import DeltaClient, DeltaUnavailable
from .events import ConnectionState, ControlCommand
from .feed_runtime import relist_forever, relist_instruments
from .main import live_underlyings
from .models import HealthReport
from .redis_bus import BusConfig, RedisBus
from .supervisor import FeedSupervisor

ADAPTER_ENV = "DELTA_FEED_ADAPTER"


@dataclass
class FeedProcess:
    """The components owned by the standalone feed process."""

    bus: RedisBus
    client: DeltaClient
    adapter: Any
    supervisor: FeedSupervisor
    listed: dict[str, set[str]] = field(default_factory=dict)
    relist_task: asyncio.Task | None = None
    control_subscription: Any = None
    control_task: asyncio.Task | None = None


def build_adapter(client: DeltaClient, underlyings: tuple[str, ...]) -> Any:
    """Build the venue adapter, with one smoke-only seam.

    The default is the venue and stays the venue: an unset or empty variable uses the
    venue adapter. The only reason this seam exists is that a smoke run must drive this
    process from a script with no socket, while production images must not carry test
    code.
    """
    value = os.environ.get(ADAPTER_ENV, "")
    if not value.strip():
        return DeltaAdapter(
            client=client,
            underlyings=underlyings,
            feed_factory=DeltaFeed,
        )

    try:
        module_name, attribute = value.split(":", 1)
    except ValueError as exc:
        raise ValueError(f"{ADAPTER_ENV}={value!r} is not module:attribute") from exc
    if not module_name.strip() or not attribute.strip():
        raise ValueError(f"{ADAPTER_ENV}={value!r} is not module:attribute")
    return getattr(import_module(module_name), attribute)(underlyings)


def _set_state(app: FastAPI, process: FeedProcess) -> None:
    app.state.bus = process.bus
    app.state.delta = process.client
    app.state.adapter = process.adapter
    app.state.supervisor = process.supervisor
    app.state.relist_task = process.relist_task
    app.state.control_task = process.control_task


def _clear_state(app: FastAPI) -> None:
    for name in ("bus", "delta", "adapter", "supervisor", "relist_task", "control_task"):
        setattr(app.state, name, None)


async def _consume_control(process: FeedProcess) -> None:
    """Apply commands already published to the feed's inbound stream."""
    while True:
        event = await process.control_subscription.queue.get()
        if isinstance(event, ControlCommand):
            process.supervisor.dispatch_command(event)


async def _close_process(process: FeedProcess) -> None:
    """Stop relisting, supervisor, Redis, then the Delta client."""
    if process.relist_task is not None:
        process.relist_task.cancel()
        await asyncio.gather(process.relist_task, return_exceptions=True)
        process.relist_task = None
    if process.control_task is not None:
        process.control_task.cancel()
        await asyncio.gather(process.control_task, return_exceptions=True)
        process.control_task = None
    await process.supervisor.aclose()
    await process.bus.aclose()
    await process.client.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the Redis-backed ingestion composition in its required order."""
    underlyings = live_underlyings()
    bus = RedisBus(BusConfig.from_env(underlyings))
    try:
        await bus.start()
    except Exception:
        await bus.aclose()
        raise

    client = DeltaClient()
    process: FeedProcess | None = None
    try:
        await client.__aenter__()
        adapter = build_adapter(client, underlyings)
        supervisor = FeedSupervisor([adapter], bus.publish)
        process = FeedProcess(
            bus=bus,
            client=client,
            adapter=adapter,
            supervisor=supervisor,
        )
        process.control_subscription = bus.subscribe(
            "feed-control", maxsize=100, event_types=("control.command",)
        )
        process.control_task = asyncio.create_task(
            _consume_control(process), name="feed-control"
        )
        _set_state(app, process)
        try:
            await relist_instruments(process)
        except DeltaUnavailable:
            # Keep the HTTP app alive with the constructed, unstarted supervisor.
            yield
        else:
            supervisor.start()
            process.relist_task = asyncio.create_task(
                relist_forever(process), name="instrument-relist"
            )
            app.state.relist_task = process.relist_task
            yield
    finally:
        if process is not None:
            await _close_process(process)
        else:
            await bus.aclose()
            await client.aclose()
        _clear_state(app)


app = FastAPI(
    title="delta-exchange-payoff feed",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health", response_model=HealthReport)
async def health() -> HealthReport:
    supervisor = getattr(app.state, "supervisor", None)
    if supervisor is None:
        return HealthReport(feed=ConnectionState.STOPPED)
    return supervisor.report()
