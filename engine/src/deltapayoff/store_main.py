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

from fastapi import FastAPI, Response

from . import log_events
from .bars import COMPUTED_SPLIT_GRACE_SECONDS, ComputedAggregator
from .events import Alert, ControlCommand, StoreState
from .logging_setup import configure_logging, log_event
from .main import live_underlyings
from .redis_bus import Position, RedisBus, StreamLag
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
    read_checkpoint,
    recover_intent,
    resolve_root,
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

#: How often the store asks Redis where its own consumer group stands.
#:
#: Ten seconds, and it is not a fresh number: it is `STORE_STATE_INTERVAL_SECONDS`, the
#: cadence this process already publishes its state on. Two loops on one period are one
#: thing to reason about. The cost is two `XINFO` calls per stream per ten seconds --
#: `derived` 0.8 calls a second across four streams -- against a `measured` 1,849.8
#: entries a second flowing the other way.
STORE_BUS_MONITOR_INTERVAL_SECONDS = STORE_STATE_INTERVAL_SECONDS

#: How far behind a lossless consumer may fall before it is no longer merely busy.
#:
#: **`assumed`, and derived from a number this store already declares rather than chosen
#: beside it.** It is `STORE_QUEUE_SIZE`, the lossless watermark, and the ticket sets the
#: two bounds it has to sit between: "a store 1.86 million entries behind must not report
#: ok; a store 500 entries behind on a busy tick must not report failure."
#:
#: * 500 is `DEFAULT_READ_COUNT`, one read batch, and `measured` the exact pending count
#:   at the moment the reader died. One batch in flight is what working looks like.
#: * 100,000 is 200 batches, and `derived` 54.1 seconds of traffic at 1,849.8 entries a
#:   second -- 3.0% of the thirty-minute retention window.
#: * Data starts being destroyed at `derived` about 3.33 million entries, which is the
#:   whole window at that rate. The incident was 1.86 million on one stream and 2.23
#:   million across three.
#:
#: So the threshold is two hundred times a busy tick and one thirty-third of the point of
#: no return, which leaves `derived` about 29 minutes to act on it. It is also the depth
#: at which the bus's own lossless queue is full, so past it back-pressure is the story
#: whatever else is true.
#:
#: **What would change it:** a `measured` rate materially above 1,849.8 entries a second,
#: or a retention window shorter than thirty minutes. Both move the headroom, not the
#: reasoning.
STORE_LAG_ALERT_ENTRIES = STORE_QUEUE_SIZE

#: How many monitor intervals may pass before the monitor itself is presumed stopped.
#: The whole lesson of #103 is that a loop can die quietly, and this file has just been
#: given another loop.
STORE_BUS_MONITOR_STALE_INTERVALS = 3


@dataclass
class StoreProcess:
    """The components owned by the standalone store process."""

    root: Path | str
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
    #: The last answer `poll_bus` got, and when. **`/health` reads this and never Redis**:
    #: a health route that made a round trip would hang on exactly the Redis whose
    #: sickness it is meant to report, and Compose would read the timeout as the process
    #: being down rather than the bus being behind.
    bus_lag: dict[str, int | None] = field(default_factory=dict)
    bus_gaps: tuple[str, ...] = ()
    bus_checked_at: float | None = None
    bus_check_errors: int = 0
    #: Per stream, the replay-gap loss already added to `replay_gap_entries`, so a
    #: condition that stays true for two hours is counted once and then only as it grows.
    replay_gap_counted: dict[str, int] = field(default_factory=dict)
    replay_gap_alerted: set[str] = field(default_factory=set)
    lag_alerted: set[str] = field(default_factory=set)

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
    """Rebase a trimmed stream: the id stays a read cursor, the index moves.

    **The id must sit before the first retained entry, not on it.** A stream read from an
    id is exclusive of that id, so a cursor set to `first_retained_id` skips the one entry
    that survived the trim at the boundary (#85). The saved id is the right cursor even
    though Redis no longer holds it: a read from a trimmed id returns the whole retained
    suffix. Only the index is wrong, and it is rebased onto the trim count so the first
    entry delivered carries its true `entries-added` ordinal.
    """
    process.subscription.start_ids[stream] = position
    process.subscription.positions[stream] = position
    process.writer.prev_positions[stream] = position


def _gap_detail(gap: Any) -> str:
    """One sentence about a replay gap, for an `alert` and a `store.replay_gap` record.

    **An absent count is said in words, never rendered as a value.** `replay_gaps`
    answers `lost = None` when the stream is gone or its consumer group is, because what
    it held before the saved position is then genuinely unknowable; `first_retained_id`
    is `None` when nothing survives. Both are correct and neither may become `0` --
    `CONTEXT.md` section 7 refuses `0` for absent, and a fabricated `0` here is the exact
    class of lie #103 was about. What was wrong was `!r`, which put the `None` straight
    into the operator's sentence:

        stream computed.chain:DELTA:BTC lost None entries before saved position
        1789210919555-2; first retained id is None

    `measured` on the restarted stack, `docker logs dxp-store` at 2026-09-12T15:52:33Z,
    four times, once per stream. [0010](../../docs/design/decisions/0010-store-replay.md)
    R5 requires that case to read *absent or empty*.

    A missing group does not make the stream's own contents unknown, so a retained bound
    that exists is still reported beside the uncountable count. The countable case is
    untouched.
    """
    if gap.lost is None or gap.first_retained_id is None:
        retained = (
            "nothing is retained"
            if gap.first_retained_id is None
            else f"the first retained id is {gap.first_retained_id}"
        )
        return (
            f"stream {gap.stream} is absent or empty at saved position {gap.saved_id}; "
            f"what it held before that position cannot be counted ({retained})"
        )
    return (
        f"stream {gap.stream} lost {gap.lost!r} entries before saved position "
        f"{gap.saved_id}; first retained id is {gap.first_retained_id!r}"
    )


async def _prepare_process(
    *,
    root: Path | str | None = None,
    bus: RedisBus | None = None,
    clock: Callable[[], float] = time.time,
) -> StoreProcess:
    """Read metadata, position Redis, then construct the store composition.

    `resolve_root` rather than `Path(root)`: an explicit `s3://` root must stay a string
    (I8, #70) or `read_checkpoint` below mangles it into a local path silently, rather
    than raising `StoreHomeUnavailable` where the failure is actually legible.
    """
    root = resolve_root(root)
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
                    # The saved id, not `first_retained_id`: see `_set_replay_base`.
                    _set_replay_base(
                        process, gap.stream, Position(gap.saved_id, gap.trimmed)
                    )
    await bus.start_readers()
    # Seed the continuous check from the position the readers just took, without
    # re-announcing what the start-up gap check above has already alerted on. It also
    # means `/health` has a real answer from the first request rather than after the
    # monitor's first tick.
    await poll_bus(process, announce=False)

    if checkpoint is None:
        checkpoint = _first_checkpoint(process)
        write_checkpoint(root, checkpoint)
        _log_start_up(process, checkpoint, "initialized")
    else:
        _log_start_up(process, checkpoint, "restored")
    return process


def _replayed_from(checkpoint: Checkpoint) -> str:
    """Every stream and the position this start-up is reading forward from, in one field.

    One string rather than four, because the point of the record is that it is **one
    line an operator reads after a restart**: four fields whose names are stream keys
    would be four columns that appear and disappear with the configuration.
    """
    return " ".join(
        f"{stream}@{position.id}(index {position.index})"
        for stream, position in sorted(checkpoint.streams.items())
    )


def _log_start_up(
    process: StoreProcess, checkpoint: Checkpoint, disposition: str
) -> None:
    """**One record on every start-up, whether or not anything was wrong.** #110.

    Before this, `store` said nothing at all on a clean replay: the gap check logs when
    it finds a gap and is silent when it does not, and the generation-zero record fired
    only on a first start. So the restart at `measured` 2026-09-12T15:56:57Z left
    `.stack-logs/store/2026-09-12.log` empty between 15:56:57.1Z and 16:01:59.3Z, and
    the only evidence that a minute had been dropped from one of four tables was a
    **file name**. #110 had to establish its own mechanism from Parquet because of that
    silence, and this is the line that would have answered it: what was replayed, from
    where, and what each of the four tables had already sealed through.

    **It is the same `store.checkpoint` event, not a new one.** A start-up record is a
    statement about a checkpoint -- the one being adopted -- and a second event name for
    it would split every "what did this generation do" query in two. `disposition` is
    what distinguishes the two cases in the message: *initialized* is a root with no
    checkpoint in it, *restored* is every other start.

    Exactly one record per start-up, so
    `test_store_process_first_start_positions_readers_and_writes_generation_zero`'s count
    still means what it says.
    """
    sealed = checkpoint.sealed_through_us
    log_event(
        logger,
        logging.INFO,
        log_events.STORE_CHECKPOINT,
        "store checkpoint generation %d %s: replaying %d streams from %s; "
        "recording=%s, replay gap %d entries",
        checkpoint.generation,
        disposition,
        len(checkpoint.streams),
        _replayed_from(checkpoint) or "nothing saved",
        checkpoint.recording,
        process.writer.replay_gap_entries,
        generation=checkpoint.generation,
        streams=len(checkpoint.streams),
        replayed_from=_replayed_from(checkpoint),
        recording=checkpoint.recording,
        replay_gap_entries=process.writer.replay_gap_entries,
        quote_sealed_through_us=sealed[DATASET],
        reference_sealed_through_us=sealed[REFERENCE_DATASET],
        spot_sealed_through_us=sealed[SPOT_DATASET],
        computed_sealed_through_us=sealed[COMPUTED_DATASET],
    )


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


def _publish_alert(process: StoreProcess, code: str, detail: str) -> None:
    """One alert on the bus, with the publisher's failure kept inside this function."""
    try:
        process.bus.publish(
            Alert(
                source="store",
                ts_received=_utc_from_clock(process.clock),
                severity="error",
                code=code,
                detail=detail,
            )
        )
    except Exception:
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "the store could not publish a %s alert",
            code,
            exc_info=True,
        )


def _note_replay_gaps(
    process: StoreProcess, lags: dict[str, StreamLag], *, announce: bool
) -> None:
    """Count what was trimmed unread, and say so once.

    **Counted every poll, alerted once.** The condition stays true for as long as the
    store stays stopped -- two hours and 720 polls in the incident -- so the alert marks
    the transition into it and `/health` carries the standing signal. An alert repeated
    720 times is an alert nobody reads, and the Discord consumer takes all of them.

    The counter still moves on every poll, because the hole keeps growing while nothing
    reads: what is added is the increase over what this stream has already contributed.
    """
    for stream, lag in lags.items():
        if not lag.trimmed_past:
            # Recovered, or never gapped. A later gap on this stream is a new episode.
            process.replay_gap_alerted.discard(stream)
            continue
        lost = lag.lost
        if lost is not None:
            already = process.replay_gap_counted.get(stream, 0)
            if lost > already:
                process.writer.replay_gap_entries += lost - already
                process.replay_gap_counted[stream] = lost
        if not announce or stream in process.replay_gap_alerted:
            continue
        process.replay_gap_alerted.add(stream)
        detail = (
            f"stream {stream} is at {lag.last_delivered_id} and the oldest entry Redis "
            f"still holds is {lag.first_retained_id}; {lost!r} entries were trimmed "
            f"before the store read them and cannot be replayed"
        )
        _publish_alert(process, "store.replay_gap", detail)
        log_event(
            logger,
            logging.ERROR,
            log_events.STORE_REPLAY_GAP,
            detail,
            stream=stream,
            saved_id=lag.last_delivered_id,
            first_retained_id=lag.first_retained_id,
            lost=lost,
        )


def _note_lag(
    process: StoreProcess, lags: dict[str, StreamLag], *, announce: bool
) -> None:
    """Alert a lossless consumer past the threshold, once per excursion."""
    for stream, lag in lags.items():
        entries = lag.lag
        if entries is None or entries <= STORE_LAG_ALERT_ENTRIES:
            # Hysteresis: back under the threshold, and the next excursion is new.
            process.lag_alerted.discard(stream)
            continue
        if not announce or stream in process.lag_alerted:
            continue
        process.lag_alerted.add(stream)
        detail = (
            f"the store's lossless consumer group is {entries} entries behind on "
            f"{stream}, past the threshold of {STORE_LAG_ALERT_ENTRIES}"
        )
        _publish_alert(process, "store.consumer_lag", detail)
        log_event(
            logger,
            logging.ERROR,
            log_events.ALERT,
            detail,
            stream=stream,
            lag=entries,
            threshold=STORE_LAG_ALERT_ENTRIES,
        )


async def poll_bus(process: StoreProcess, *, announce: bool = True) -> None:
    """Ask Redis where the store's group stands, and act on the answer.

    **This is #103's fifth change and the one with no threshold in it.** The replay-gap
    check used to live inside `_prepare_process`, so it ran only at start-up -- and
    `CONTEXT.md`'s "when the watermark was trimmed before `store` came back" was the whole
    problem. A store that never restarts never came back, so the only code path that could
    raise a replay gap was never reached, and `replay_gap_entries` reported `0` for two
    hours through the exact condition it was built to report.

    A replay gap is not an event that happens at start-up. It is a condition that becomes
    true the moment retention passes the watermark, and it is true right now whether or
    not anyone restarts.

    `announce=False` is start-up's own call: `_prepare_process` has already alerted on
    the saved positions it checked, so this seeds the baselines without saying it twice.
    """
    try:
        lags = await process.bus.consumer_lag(process.subscription)
    except Exception:
        process.bus_check_errors += 1
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "the store could not read its own consumer group's position",
            exc_info=True,
        )
        return
    process.bus_lag = {stream: lag.lag for stream, lag in lags.items()}
    process.bus_gaps = tuple(
        sorted(stream for stream, lag in lags.items() if lag.trimmed_past)
    )
    _note_replay_gaps(process, lags, announce=announce)
    _note_lag(process, lags, announce=announce)
    process.bus_checked_at = process.clock()


async def monitor_bus_forever(
    process: StoreProcess,
    *,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Poll on the ten-second cadence. **It may not raise out of itself.**

    `poll_bus` swallows and counts its own failures, and this loop adds nothing that can
    throw, because a monitoring loop that dies is precisely the shape of the defect it
    was written to catch. `/health` does not take this loop's word for it either: it
    reports an error when the last poll is older than three intervals.
    """
    while True:
        await poll_bus(process)
        await sleep(STORE_BUS_MONITOR_INTERVAL_SECONDS)


def _health_problems(process: StoreProcess) -> list[str]:
    """Every reason this store is not fine, in the words an operator would want.

    **Nothing here is a judgement except the lag threshold.** A reader that is not
    running, a task that exited, a watermark trimmed past, a monitor that has stopped
    monitoring: each is a fact, and each one made itself invisible on 2026-09-12.
    """
    problems: list[str] = []
    for name, reader in process.bus.readers().items():
        if reader["gave_up"]:
            problems.append(
                f"the {name!r} bus reader gave up after repeated failures and is no "
                f"longer consuming: {reader['failure']}"
            )
        elif not reader["alive"]:
            problems.append(f"the {name!r} bus reader is not running")
    for name, detail in process.bus.reader_exits().items():
        problems.append(f"the {name!r} task exited: {detail}")
    for stream in process.bus_gaps:
        problems.append(
            f"the consumer group's position on {stream} is older than the oldest entry "
            f"the stream still holds; data has been trimmed before it was read"
        )
    if process.writer.replay_gap_entries:
        problems.append(
            f"{process.writer.replay_gap_entries} entries were trimmed before the store "
            f"read them"
        )
    for stream, entries in process.bus_lag.items():
        if entries is not None and entries > STORE_LAG_ALERT_ENTRIES:
            problems.append(
                f"the consumer group is {entries} entries behind on {stream}, past the "
                f"threshold of {STORE_LAG_ALERT_ENTRIES}"
            )
    checked = process.bus_checked_at
    stale_after = (
        STORE_BUS_MONITOR_INTERVAL_SECONDS * STORE_BUS_MONITOR_STALE_INTERVALS
    )
    if checked is None:
        problems.append("the store has not yet read its own consumer group's position")
    elif process.clock() - checked > stale_after:
        problems.append(
            f"the store has not read its own consumer group's position for "
            f"{process.clock() - checked:.0f}s"
        )
    return problems


def _health_payload(process: StoreProcess) -> dict[str, Any]:
    """What this process knows about itself, and **whether that is acceptable**.

    `"status": "ok"` was a literal here. It stayed `ok` through two hours in which the
    store read nothing, 97 minutes of market data were trimmed away unread and every
    minute in between was sealed empty. `generation` and `rows_written` were advancing
    and both true; `buffered_rows: 0` reads identically to a quiet market. There was no
    field in this payload a person could have looked at and known.
    """
    writer = process.writer
    problems = _health_problems(process)
    checked = process.bus_checked_at
    return {
        "status": "ok" if not problems else "error",
        "problems": problems,
        "recording": writer.recording,
        "generation": writer.generation,
        "buffered_rows": writer.buffered_rows,
        "rows_written": writer.rows_written,
        "replay_gap_entries": writer.replay_gap_entries,
        "already_flushed": _already_flushed(process),
        "flush_errors": writer.flush_errors,
        "readers": process.bus.readers(),
        "reader_exits": process.bus.reader_exits(),
        "consumer_lag": dict(process.bus_lag),
        "lag_threshold": STORE_LAG_ALERT_ENTRIES,
        "replay_gap_streams": list(process.bus_gaps),
        "bus_checked_seconds_ago": (
            None if checked is None else round(process.clock() - checked, 3)
        ),
        "bus_check_errors": process.bus_check_errors,
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
        asyncio.create_task(monitor_bus_forever(process), name="store-bus-monitor"),
    ]
    publish_state(process)
    app.state.process = process
    app.state.bus = process.bus
    app.state.writer = process.writer
    app.state.store = process.writer
    app.state.control_task = process.tasks[1]
    app.state.state_task = process.tasks[2]
    app.state.monitor_task = process.tasks[3]
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
                "monitor_task",
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
async def health(response: Response) -> dict[str, Any]:
    """**The status code is the contract, not the body** (#103).

    Compose's health check is `urllib.request.urlopen(...)`, which fails on a status and
    reads nothing at all; every container in the stalled stack reported
    `Up 7 hours (healthy)` for seven hours because this route answered 200 whatever had
    happened. A body that said `stalled` behind a 200 would have changed nothing.
    """
    process = getattr(app.state, "process", None)
    if process is None:
        return {"status": "ok"}
    payload = _health_payload(process)
    if payload["status"] != "ok":
        response.status_code = 503
    return payload


__all__ = [
    "STORE_EVENT_TYPES",
    "STORE_STATE_INTERVAL_SECONDS",
    "StoreProcess",
    "app",
    "consume_control",
    "health",
    "lifespan",
    "STORE_BUS_MONITOR_INTERVAL_SECONDS",
    "STORE_LAG_ALERT_ENTRIES",
    "monitor_bus_forever",
    "poll_bus",
    "publish_state",
    "publish_state_forever",
    "state_event",
]
