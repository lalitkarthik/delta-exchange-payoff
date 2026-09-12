"""FastAPI entrypoint for the standalone Parquet store process.

The module is importable without opening Redis.  The lifespan owns the store's one
lossless market-data reader, its control reader and the writer that commits the four
tables under one checkpoint generation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from . import log_events
from .bars import COMPUTED_SPLIT_GRACE_SECONDS, ComputedAggregator
from .events import Alert, ControlCommand, StoreState
from .logging_setup import configure_logging, log_event
from .main import live_underlyings
from .redis_bus import Position, RedisBus
from .store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    DATASET,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
    Checkpoint,
    default_root,
    read_checkpoint,
    recover_intent,
    write_checkpoint,
)

logger = logging.getLogger(__name__)
configure_logging()

STORE_EVENT_TYPES = (
    "md.option_quote",
    "md.option_reference",
    "md.index_quote",
    "computed.chain",
)
STORE_STATE_INTERVAL_SECONDS = 10.0
STORE_QUEUE_SIZE = 100_000
STORE_CONTROL_QUEUE_SIZE = 100


@dataclass
class StoreProcess:
    """The components owned by the standalone store process."""

    root: Path
    writer: BarWriter
    bus: RedisBus
    subscription: Any
    control_subscription: Any
    clock: Callable[[], float] = time.time
    tasks: list[asyncio.Task] = field(default_factory=list)
    state_publish_errors: int = 0
    state_last_signature: tuple[bool, int, int] | None = None
    state_last_published_at: float | None = None
    state_changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def config(self) -> Any:
        return self.bus.config


def _utc_from_clock(clock: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(clock(), tz=timezone.utc)


def _zero_sealed() -> dict[str, int]:
    return {
        DATASET: 0,
        REFERENCE_DATASET: 0,
        SPOT_DATASET: 0,
        COMPUTED_DATASET: 0,
    }


def _first_checkpoint(process: StoreProcess) -> Checkpoint:
    return Checkpoint(
        generation=0,
        written_at=_utc_from_clock(process.clock),
        group=process.writer.group,
        recording=True,
        streams=dict(process.subscription.positions),
        sealed_through_us=_zero_sealed(),
    )


def _set_replay_base(process: StoreProcess, stream: str, position: Position) -> None:
    """Make a trimmed suffix start at its true entries-added ordinal."""
    process.subscription.start_ids[stream] = position
    process.subscription.positions[stream] = position
    process.writer.prev_positions[stream] = position


def _gap_detail(gap: Any) -> str:
    return (
        f"stream {gap.stream} lost {gap.lost!r} entries before saved position "
        f"{gap.saved_id}; first retained id is {gap.first_retained_id!r}"
    )


async def _prepare_process(
    *,
    root: Path | None = None,
    bus: RedisBus | None = None,
    clock: Callable[[], float] = time.time,
) -> StoreProcess:
    """Read metadata, position Redis, then construct the store composition."""
    root = default_root() if root is None else Path(root)
    checkpoint = read_checkpoint(root)
    committed_generation = 0 if checkpoint is None else checkpoint.generation
    recover_intent(root, committed_generation)
    from .store import clear_stray_tmp

    clear_stray_tmp(root)

    if bus is None:
        from .redis_bus import BusConfig

        bus = RedisBus(BusConfig.from_env(live_underlyings()))
    group = "store" if checkpoint is None else checkpoint.group
    writer = BarWriter(
        BarStore(root),
        reference_store=BarStore(
            root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA
        ),
        spot_store=BarStore(root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
        computed_store=BarStore(
            root, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA
        ),
        chains=None,
        computed=ComputedAggregator(grace_seconds=COMPUTED_SPLIT_GRACE_SECONDS),
        group=group,
        checkpoint_root=root,
        publish=bus.publish,
        clock=clock,
    )
    if checkpoint is not None:
        writer.restore_checkpoint(checkpoint)

    saved = {} if checkpoint is None else checkpoint.streams
    pauses = () if checkpoint is None else checkpoint.pauses
    subscription = bus.subscribe(
        group,
        maxsize=STORE_QUEUE_SIZE,
        lossless=True,
        start_ids=saved,
        group_start="$",
        skip=pauses,
        event_types=STORE_EVENT_TYPES,
    )
    control_subscription = bus.subscribe(
        "store-control",
        maxsize=STORE_CONTROL_QUEUE_SIZE,
        event_types=("control.command",),
    )
    process = StoreProcess(
        root=root,
        writer=writer,
        bus=bus,
        subscription=subscription,
        control_subscription=control_subscription,
        clock=clock,
    )
    loop = asyncio.get_running_loop()
    writer.on_state_change = lambda: loop.call_soon_threadsafe(
        process.state_changed.set
    )
    writer._subscription = subscription
    writer.prev_positions = dict(subscription.positions)

    await bus.start(start_readers=False)
    await bus.ensure_groups(subscription)
    if checkpoint is not None:
        gaps = await bus.replay_gaps(subscription, saved)
        for gap in gaps.values():
            if gap.lost is None or gap.lost > 0:
                detail = _gap_detail(gap)
                process.writer.replay_gap_entries += gap.lost or 0
                try:
                    bus.publish(
                        Alert(
                            source="store",
                            ts_received=_utc_from_clock(clock),
                            severity="error",
                            code="store.replay_gap",
                            detail=detail,
                        )
                    )
                except Exception:
                    log_event(
                        logger,
                        logging.ERROR,
                        log_events.ENGINE_ERROR,
                        "the store replay-gap alert could not be published",
                        exc_info=True,
                    )
                log_event(
                    logger,
                    logging.ERROR,
                    log_events.STORE_REPLAY_GAP,
                    detail,
                    stream=gap.stream,
                    saved_id=gap.saved_id,
                    first_retained_id=gap.first_retained_id,
                    lost=gap.lost,
                )
                if gap.lost is not None and gap.lost > 0:
                    _set_replay_base(
                        process,
                        gap.stream,
                        Position(gap.first_retained_id or gap.saved_id, gap.trimmed),
                    )
    await bus.start_readers()

    if checkpoint is None:
        checkpoint = _first_checkpoint(process)
        write_checkpoint(root, checkpoint)
        log_event(
            logger,
            logging.INFO,
            log_events.STORE_CHECKPOINT,
            "store checkpoint generation %d initialized",
            checkpoint.generation,
            generation=checkpoint.generation,
            streams=len(checkpoint.streams),
            quote_sealed_through_us=checkpoint.sealed_through_us[DATASET],
            reference_sealed_through_us=checkpoint.sealed_through_us[REFERENCE_DATASET],
            spot_sealed_through_us=checkpoint.sealed_through_us[SPOT_DATASET],
            computed_sealed_through_us=checkpoint.sealed_through_us[COMPUTED_DATASET],
        )
    return process


def _state_signature(process: StoreProcess) -> tuple[bool, int, int]:
    writer = process.writer
    return writer.recording, writer.generation, writer.replay_gap_entries


def _already_flushed(process: StoreProcess) -> int:
    return sum(
        aggregator.already_flushed
        for aggregator in (
            process.writer.aggregator,
            process.writer.reference,
            process.writer.spot,
            process.writer.computed,
        )
    )


def state_event(process: StoreProcess) -> StoreState:
    writer = process.writer
    return StoreState(
        source="store",
        ts_received=_utc_from_clock(process.clock),
        recording=writer.recording,
        buffered_rows=writer.buffered_rows,
        rows_written=writer.rows_written,
        replay_gap_entries=writer.replay_gap_entries,
        already_flushed=_already_flushed(process),
        flush_errors=writer.flush_errors,
        generation=writer.generation,
    )


def publish_state(process: StoreProcess) -> bool:
    """Publish one state snapshot, keeping publisher failures inside the loop."""
    try:
        process.bus.publish(state_event(process))
    except Exception:
        process.state_publish_errors += 1
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "the store state could not be published",
            exc_info=True,
        )
        return False
    process.state_last_signature = _state_signature(process)
    process.state_last_published_at = process.clock()
    return True


async def publish_state_forever(
    process: StoreProcess,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Publish on the ten-second cadence and on the three state changes."""
    while True:
        process.state_changed.clear()
        now = process.clock()
        signature = _state_signature(process)
        due = (
            process.state_last_published_at is None
            or now - process.state_last_published_at >= STORE_STATE_INTERVAL_SECONDS
            or signature != process.state_last_signature
        )
        if due:
            publish_state(process)
        changed = asyncio.create_task(
            process.state_changed.wait(), name="store-state-change"
        )
        timer = asyncio.create_task(
            sleep(STORE_STATE_INTERVAL_SECONDS), name="store-state-timer"
        )
        done, pending = await asyncio.wait(
            (changed, timer), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if changed in done:
            process.state_changed.set()


async def consume_control(process: StoreProcess) -> None:
    """Queue commands so the writer applies them at its next drained point."""
    while True:
        event = await process.control_subscription.queue.get()
        if isinstance(event, ControlCommand):
            process.writer.enqueue_command(event)


def _health_payload(process: StoreProcess) -> dict[str, Any]:
    writer = process.writer
    return {
        "status": "ok",
        "recording": writer.recording,
        "generation": writer.generation,
        "buffered_rows": writer.buffered_rows,
        "rows_written": writer.rows_written,
        "replay_gap_entries": writer.replay_gap_entries,
        "already_flushed": _already_flushed(process),
    }


async def _close_process(process: StoreProcess) -> None:
    for task in process.tasks:
        task.cancel()
    if process.tasks:
        await asyncio.gather(*process.tasks, return_exceptions=True)
    process.tasks = []
    await process.writer.aclose()
    await process.bus.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the checkpointed store composition."""
    process = await _prepare_process()
    process.tasks = [
        asyncio.create_task(process.writer.run(), name="store-writer"),
        asyncio.create_task(consume_control(process), name="store-control"),
        asyncio.create_task(publish_state_forever(process), name="store-state"),
    ]
    publish_state(process)
    app.state.process = process
    app.state.bus = process.bus
    app.state.writer = process.writer
    app.state.store = process.writer
    app.state.control_task = process.tasks[1]
    app.state.state_task = process.tasks[2]
    try:
        yield
    finally:
        try:
            await _close_process(process)
        finally:
            for name in (
                "process",
                "bus",
                "writer",
                "store",
                "control_task",
                "state_task",
            ):
                setattr(app.state, name, None)


app = FastAPI(
    title="delta-exchange-payoff store",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    process = getattr(app.state, "process", None)
    if process is None:
        return {"status": "ok"}
    return _health_payload(process)


__all__ = [
    "STORE_EVENT_TYPES",
    "STORE_STATE_INTERVAL_SECONDS",
    "StoreProcess",
    "app",
    "consume_control",
    "health",
    "lifespan",
    "publish_state",
    "publish_state_forever",
    "state_event",
]
