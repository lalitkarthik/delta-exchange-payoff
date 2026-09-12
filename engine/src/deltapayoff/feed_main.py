"""FastAPI entrypoint for the standalone Delta ingestion process."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from fastapi import FastAPI, Response

from .adapters import DeltaAdapter, DeltaFeed
from .controller import REASON_PAUSED, RECONNECT_AFTER_SECONDS
from .delta_client import DeltaClient, DeltaUnavailable
from .events import ConnectionState, ControlCommand
from .feed_runtime import relist_forever, relist_instruments
from .main import live_underlyings
from .models import HealthReport
from .redis_bus import BusConfig, RedisBus
from .supervisor import FeedSupervisor

ADAPTER_ENV = "DELTA_FEED_ADAPTER"

#: **The staleness bound `/health` fails on**, and the one number in this module that is a
#: judgement rather than a fact. `derived`: three `reconnect_after` intervals, 3 x 45.0 s.
#:
#: **A quiet market is not a dead feed — but silence here is not a quiet market.** The
#: venue pushes a ticker refresh every `measured` 5001 ms whether or not anything trades,
#: which is the observation `degraded_after` (15 s, three refreshes) was derived from and
#: `reconnect_after` (45 s, three degraded intervals) after it. This bound is the same
#: rule applied once more, and it is the only clock available that a quiet market cannot
#: move: `controller.last_message_age()` is reset in `sink`, by market-data events off the
#: adapter and by nothing else. Heartbeats, transitions and `control.command` traffic all
#: publish without touching it -- `CONTEXT.md` section 5 makes that distinction and this
#: reuses it rather than inventing a second one.
#:
#: **Why three intervals and not one.** At 45 s of silence the controller cuts the socket
#: itself (C7) and redials; a clean recovery costs about 47 s end to end, two consecutive
#: ones about 93 s. A bound at 45 s would fail the health check for a recovery the
#: controller completes unaided. At 135 s the feed has failed to deliver across the whole
#: of its own recovery cycle twice over.
#:
#: **503 means "not delivering", not "give up on me".** Nothing in the controller reads
#: this number and no recovery is cancelled by crossing it; it decides one thing, which is
#: whether an operator and Docker are told. On 2026-09-12 that answer was ten minutes and
#: fifty-four seconds late (#108).
FEED_STALE_SECONDS = RECONNECT_AFTER_SECONDS * 3


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


def _adapter_problems(row: Any) -> list[str]:
    """Every reason one adapter is not feeding, in the words an operator would want.

    Each line is a fact the controller already holds and already published; none of them
    is new information. On 2026-09-12 all three of the first were true at once and the
    route answered `{"status": "ok"}` over the top of them.
    """
    problems: list[str] = []
    name = row.adapter
    if row.state is ConnectionState.STOPPED:
        if row.reason is None:
            problems.append(
                f"the {name!r} connection has never been started; this process is not "
                f"connected to the venue"
            )
        elif row.reason == REASON_PAUSED:
            problems.append(
                f"the {name!r} connection is stopped because an operator paused it; it "
                f"delivers nothing until a resume"
            )
        else:
            problems.append(
                f"the {name!r} connection is stopped (reason {row.reason!r}) and will "
                f"not dial again on its own; only restarting this process revives it"
            )
    if row.budget_remaining == 0:
        problems.append(
            f"the {name!r} connection has no reconnect budget left after "
            f"{row.reconnects} drops; the next drop stops it for good"
        )
    age = row.last_message_age_seconds
    if age is not None and age > FEED_STALE_SECONDS:
        problems.append(
            f"the {name!r} connection has delivered no market-data event for "
            f"{age:.0f}s, past the bound of {FEED_STALE_SECONDS:.0f}s; heartbeats and "
            f"control traffic do not reset this clock"
        )
    return problems


def _bus_problems(bus: Any) -> list[str]:
    """What the feed's own bus says about itself. **`feed` had no view of this at all.**

    The publisher is the half of this process nothing was watching: every event the
    adapter decodes goes into the outbox and the flusher is what puts it on Redis, so a
    flusher that has exited is a feed that reads the venue perfectly and publishes
    nothing. `redis_bus._flusher_exited` already logs it and records it under
    `bus-flush`; until now no route asked.

    **The bus is asked directly rather than through an `isinstance` guard.** This process
    builds a `RedisBus` and only a `RedisBus` -- `lifespan` has no other branch -- so a
    bus here that cannot answer `readers()` is a double, and a double that does not
    implement what its subject needs should say so loudly at the first call rather than
    be quietly skipped. `None` is the one real case: the window before `lifespan` sets
    the state and the window after it clears it, in both of which there is no supervisor
    either and the adapter clause has already failed the check.
    """
    if bus is None:
        return []
    problems: list[str] = []
    for name, reader in bus.readers().items():
        if reader["gave_up"]:
            problems.append(
                f"the {name!r} bus reader gave up after repeated failures and is no "
                f"longer consuming: {reader['failure']}"
            )
        elif not reader["alive"]:
            problems.append(f"the {name!r} bus reader is not running")
    for name, detail in bus.reader_exits().items():
        if name == "bus-flush":
            problems.append(
                f"the bus flusher exited and nothing this feed decodes is reaching "
                f"Redis: {detail}"
            )
        else:
            problems.append(f"the {name!r} task exited: {detail}")
    return problems


def _health_problems(
    report: HealthReport, bus: Any, control_task: asyncio.Task | None
) -> list[str]:
    """Every reason this feed is not fine. **The list is the status; 503 is the word.**

    `store_main._health_problems` is the model and this is deliberately its sibling
    rather than a call into it -- see `docs/design/lld/reconnect.md` section 8 for the
    argument. The one thing the two share is the rule: a non-empty list is a 503.

    The empty-adapter clause is the residue this route had in common with the one #103
    fixed. `supervisor.worst()` returns `stopped` for a supervisor with no controllers on
    purpose -- "a process with no feed at all, which is the strongest possible not
    ready" -- and a route that then reported `ok` over it would be the same literal in a
    new place. Where adapters do exist, each is answered for by its own row, so this
    clause is the no-adapter case and only that.
    """
    problems: list[str] = []
    if not report.adapters and report.feed is ConnectionState.STOPPED:
        problems.append(
            "this feed process has no adapter running; nothing is connected to the venue"
        )
    for row in report.adapters:
        problems.extend(_adapter_problems(row))
    problems.extend(_bus_problems(bus))
    if control_task is not None and control_task.done() and not control_task.cancelled():
        problems.append(
            f"the feed's control consumer exited and pause, resume and reconnect "
            f"commands are no longer applied: "
            f"{control_task.exception() or 'it returned without raising'}"
        )
    return problems


@app.get("/health")
async def health(response: Response) -> dict[str, Any]:
    """**The status code is the contract, not the body** (#103, and #108 after it).

    Compose's check is `urllib.request.urlopen(...)`, which raises on a status and never
    reads a body. On 2026-09-12 this route answered 200 with `"feed": "stopped"`,
    `"budget_remaining": 0` and a last message 583 seconds old in the same object, and
    `docker ps` read `Up (healthy)` throughout. Every field that said the feed was dead
    was already here; nothing reads a body, so nothing knew.

    `status` stays for what reads it and stops being a constant: `ok` while the list is
    empty, `error` when it is not, which is the shape `store` took in #103. The rest of
    `models.HealthReport` is unchanged and still here -- #40's badge, #41's commands and
    #44's watched set read the same keys they always did.
    """
    supervisor = getattr(app.state, "supervisor", None)
    report = (
        HealthReport(feed=ConnectionState.STOPPED)
        if supervisor is None
        else supervisor.report()
    )
    payload: dict[str, Any] = report.model_dump(mode="json")
    problems = _health_problems(
        report,
        getattr(app.state, "bus", None),
        getattr(app.state, "control_task", None),
    )
    payload["problems"] = problems
    if problems:
        payload["status"] = "error"
        response.status_code = 503
    return payload
