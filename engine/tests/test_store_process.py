"""The standalone store composition, driven with fake Redis and temporary roots."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from deltapayoff import log_events, store_main
from deltapayoff.bars import ComputedAggregator
from deltapayoff.events import (
    ChainLeg,
    ChainStrike,
    ComputedChain,
    ControlCommand,
    IndexQuote,
    Instrument,
    OptionQuote,
    OptionReference,
    Right,
)
from deltapayoff.events.redis_wire import stream_name
from deltapayoff.redis_bus import BusConfig, RedisBus
from deltapayoff.store import BarStore, BarWriter, read_checkpoint

ENGINE = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 9, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270926",
)


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _factory(server):
    import fakeredis.aioredis

    def factory(_config: BusConfig):
        return fakeredis.aioredis.FakeRedis(
            server=server, decode_responses=False
        )

    return factory


def _info(info: dict, field: str) -> str:
    """One `XINFO STREAM` field as text, whichever way the client decoded its keys."""
    value = info.get(field, info.get(field.encode()))
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _quote_event(minute: int, bid: float = 100.0) -> OptionQuote:
    stamp = BASE + timedelta(minutes=minute, seconds=5)
    return OptionQuote(
        source="feed",
        ts_venue=stamp,
        ts_received=stamp,
        instrument=INSTRUMENT,
        bid=bid,
        ask=bid + 1.0,
    )


def _reference_event(minute: int, mark: float = 100.5) -> OptionReference:
    stamp = BASE + timedelta(minutes=minute, seconds=5)
    return OptionReference(
        source="feed",
        ts_venue=stamp,
        ts_received=stamp,
        instrument=INSTRUMENT,
        mark=mark,
    )


def _spot_event(minute: int, spot: float = 60_000.0) -> IndexQuote:
    stamp = BASE + timedelta(minutes=minute, seconds=5)
    return IndexQuote(
        source="feed",
        ts_venue=stamp,
        ts_received=stamp,
        underlying="BTC",
        spot=spot,
    )


def _command(command: str, *, target: str = "store") -> ControlCommand:
    return ControlCommand(
        source="operator",
        ts_received=BASE,
        adapter="DELTA",
        command=command,  # type: ignore[arg-type]
        target=target,  # type: ignore[arg-type]
    )


async def _wait_until(
    predicate: Callable[[], bool], *, timeout: float = 5.0
) -> None:
    """Poll `predicate` until it is true, or fail loudly past `timeout`.

    #93: this used to poll with `await asyncio.sleep(0)`, which yields control but not
    time — a busy-wait, not a poll. Rescheduled immediately, it spins as fast as the
    event loop allows and competes for the loop with the very task whose work the
    predicate is waiting on, which is worse than a fixed sleep under load: a fixed
    sleep at least gets out of the way. `0.005` gives the writer task real time to run,
    matching the poll interval every other `_wait_until`/`until` helper in this suite
    already uses (`test_process_split.py`, `test_bus_contract.py`, `test_commands.py`).

    The bound also moves from 3.0s to 5.0s. #93's own measurement (three concurrent
    runs of this file, repeated) still timed out occasionally at 3.0s even with the
    poll fixed — a fixed-duration bet fixed for its interval but still too tight for
    its bound is the same defect half-fixed. 5.0s matches this suite's least generous
    sibling (`test_commands.py`'s `until`); the two others allow 10.0s.
    """

    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


async def _start_writer(
    process: store_main.StoreProcess, *, flush_seconds: float = 0.0
) -> None:
    process.writer.tick_seconds = 0.01
    process.writer.flush_seconds = flush_seconds
    process.tasks = [
        asyncio.create_task(process.writer.run(), name="test-store-writer"),
        asyncio.create_task(
            store_main.consume_control(process), name="test-store-control"
        ),
    ]


async def _kill_process(process: store_main.StoreProcess) -> None:
    for task in process.tasks:
        task.cancel()
    await asyncio.gather(*process.tasks, return_exceptions=True)
    process.tasks = []
    await process.bus.aclose()


async def _make_process(
    root: Path,
    server,
    clock: Clock,
    *,
    read_count: int = 10,
) -> store_main.StoreProcess:
    bus = RedisBus(
        BusConfig(
            venue="DELTA",
            underlyings=("BTC",),
            batch_ms=60_000,
            read_block_ms=0,
            read_count=read_count,
            idle_sleep_seconds=0.001,
            # **Age trimming off.** Every flush issues `XTRIM MINID` at
            # `fixture clock - retention`, while Redis stamps entries with its own real
            # clock: what gets trimmed then depends on the hour the suite is run, and the
            # trim tests below seed their own. A floor of zero trims nothing.
            retention_seconds=1_000_000_000.0,
        ),
        client_factory=_factory(server),
    )
    return await store_main._prepare_process(root=root, bus=bus, clock=clock)


def _computed_event() -> ComputedChain:
    return ComputedChain(
        source="chain-cache",
        ts_received=BASE,
        underlying="BTC",
        expiry=date(2026, 9, 27),
        fetched_at=BASE + timedelta(seconds=5),
        forward=60_000.0,
        discount=1.0,
        years_to_expiry=0.01,
        forward_method="fitted",
        model_version="api-model",
        solver="S1-newton",
        strikes=(
            ChainStrike(
                strike=Decimal("60000"),
                call=ChainLeg(
                    symbol=INSTRUMENT.venue_symbol,
                    iv=0.4,
                    iv_leg="call",
                    delta=0.5,
                    gamma=0.001,
                    vega=10.0,
                    theta=-2.0,
                    rho=1.0,
                ),
            ),
        ),
    )


def test_store_process_entrypoint_imports_without_starting_a_server() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import deltapayoff.store_main as m; print(m.app.routes)",
        ],
        cwd=ENGINE,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ENGINE / "src")},
    )

    assert result.returncode == 0, result.stderr
    assert "/health" in result.stdout


def test_store_process_first_start_positions_readers_and_writes_generation_zero(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    import fakeredis.aioredis

    caplog.set_level(logging.INFO, logger="deltapayoff.store_main")

    async def scenario() -> None:
        server = fakeredis.aioredis.FakeServer()

        def factory(_config: BusConfig):
            return fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )

        bus = RedisBus(
            BusConfig(
                venue="DELTA",
                underlyings=("BTC",),
                read_block_ms=0,
                idle_sleep_seconds=0.001,
            ),
            client_factory=factory,
        )
        await store_main._prepare_process(
            root=tmp_path,
            bus=bus,
            clock=lambda: datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp(),
        )
        checkpoint = read_checkpoint(tmp_path)
        assert checkpoint is not None
        assert checkpoint.generation == 0
        assert set(checkpoint.streams) == {
            "computed.chain:DELTA:BTC",
            "md.index_quote:DELTA:BTC",
            "md.option_quote:DELTA:BTC",
            "md.option_reference:DELTA:BTC",
        }
        records = [
            record
            for record in caplog.records
            if record.event == log_events.STORE_CHECKPOINT
        ]
        assert len(records) == 1
        assert records[0].generation == 0
        assert records[0].streams == 4
        await bus.aclose()

    asyncio.run(scenario())


def test_store_restart_replays_only_after_the_saved_positions(tmp_path: Path) -> None:
    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2, seconds=10)).timestamp())
        first = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(first)
            for minute in (0, 1):
                first.bus.publish(_quote_event(minute, bid=100.0 + minute))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.aggregator.ticks == 2)
            await _wait_until(lambda: first.writer.generation >= 1)
            checkpoint = read_checkpoint(tmp_path)
            assert checkpoint is not None
            saved = dict(checkpoint.streams)
            await _kill_process(first)

            second = await _make_process(tmp_path, server, clock)
            try:
                assert second.subscription.start_ids == saved
                await _start_writer(second)
                second.bus.publish(_quote_event(2, bid=102.0))
                await second.bus.flush()
                await _wait_until(lambda: second.writer.aggregator.ticks == 1)
                clock.value = (BASE + timedelta(minutes=3, seconds=10)).timestamp()
                await _wait_until(lambda: second.writer.rows_written == 1)
            finally:
                await _kill_process(second)

            rows = BarStore(tmp_path).scan().collect()
            assert rows.height == 3
            assert rows.select("minute").unique().height == 3
            assert rows["bid_open"].to_list() == [100.0, 101.0, 102.0]
        except Exception:
            if first.bus.client is not None:
                await first.bus.aclose()
            raise

    asyncio.run(scenario())


def test_kill_and_restart_replays_an_uncommitted_minute_once(tmp_path: Path) -> None:
    """The seam #84 named: a store restarts while the feed keeps publishing.

    The gap publish is the point. Without it the replay has nothing to over-read and the
    test is green whatever `_replay` does — which is how #63 shipped believing its "none
    duplicated" criterion held. One entry arrives while nothing is reading, and the
    restart must fold it once, not once per half of the seam.
    """

    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=3, seconds=10)).timestamp())
        first = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(first, flush_seconds=10_000.0)
            for minute in (0, 1, 2):
                first.bus.publish(_quote_event(minute, bid=100.0 + minute))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.aggregator.ticks == 3)
            first.writer._seal(clock())
            assert first.writer._commit() == 3
            checkpoint = read_checkpoint(tmp_path)
            assert checkpoint is not None
            assert checkpoint.generation == 1

            # Read and acked, never flushed: what the replay is for.
            first.bus.publish(_quote_event(3, bid=103.0))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.aggregator.ticks == 4)
            await _kill_process(first)

            # **And one published while the store is down.** It lands after the group's
            # last-delivered id and inside the batch the replay reads, which is exactly
            # the entry the replay used to hand over and the group's `>` then handed over
            # again.
            publisher = RedisBus(
                first.bus.config, client_factory=_factory(server), clock=clock
            )
            try:
                await publisher.start(start_readers=False)
                publisher.publish(_quote_event(3, bid=104.0))
                await publisher.flush()
            finally:
                await publisher.aclose()

            second = await _make_process(tmp_path, server, clock)
            try:
                await _start_writer(second, flush_seconds=10_000.0)
                # A sentinel in the next minute, published after the restart. The stream
                # is ordered, so a reader that has reached the sentinel has already
                # delivered everything the seam could have doubled — no sleeping, and no
                # patching of the code under test, to know when to look.
                key = stream_name(_quote_event(4))
                second.bus.publish(_quote_event(4, bid=105.0))
                await second.bus.flush()
                info = await second.bus.client.xinfo_stream(key)
                head = _info(info, "last-generated-id")
                await _wait_until(
                    lambda: second.subscription.last_ids.get(key) == head
                )
                await _wait_until(
                    lambda: second.writer.aggregator.ticks
                    == second.subscription.offered
                )

                # 103 from the replay, 104 from the group, 105 the sentinel. Four was the
                # #84 number: 104 arrived from both halves of the seam.
                assert second.writer.aggregator.ticks == 3
                # The index the next checkpoint saves, and what `replay_gaps` subtracts a
                # trim from. One past the end of the stream under-reports the next loss.
                assert second.subscription.positions[key].index == int(
                    _info(info, "entries-added")
                )

                clock.value = (BASE + timedelta(minutes=6, seconds=10)).timestamp()
                second.writer._seal(clock())
                assert second.writer._commit() == 2
            finally:
                await _kill_process(second)

            rows = BarStore(tmp_path).scan().collect()
            keys = list(zip(rows["symbol"], rows["minute"], strict=True))
            assert len(keys) == len(set(keys)) == 5
            base = BASE.replace(second=0, microsecond=0)
            assert sorted(row["minute"] for row in rows.iter_rows(named=True)) == [
                base + timedelta(minutes=n) for n in range(5)
            ]
            # **The tick count, not only key uniqueness.** A doubled tick folds into the
            # same bar and leaves the key set untouched.
            ticks = dict(zip(rows["minute"], rows["bid_ticks"], strict=True))
            assert ticks[base + timedelta(minutes=3)] == 2
        except Exception:
            if first.bus.client is not None:
                await first.bus.aclose()
            raise

    asyncio.run(scenario())


def test_a_trimmed_replay_folds_the_first_retained_entry(tmp_path: Path) -> None:
    """#85: the entry that survived the trim at the boundary is folded, not skipped.

    Redis trims past the saved position while the store is down. The retained suffix is
    replayed whole — its first entry included, which a cursor set to `first_retained_id`
    reads past, because a stream read from an id is exclusive of that id.
    """

    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2, seconds=10)).timestamp())
        first = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(first, flush_seconds=10_000.0)
            for minute in (0, 1):
                first.bus.publish(_quote_event(minute, bid=100.0 + minute))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.aggregator.ticks == 2)
            first.writer._seal(clock())
            assert first.writer._commit() == 2
            checkpoint = read_checkpoint(tmp_path)
            assert checkpoint is not None
            key = stream_name(_quote_event(0))
            assert checkpoint.streams[key].index == 2

            # Three more, acked and folded but never committed.
            for minute in (2, 3, 4):
                first.bus.publish(_quote_event(minute, bid=100.0 + minute))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.aggregator.ticks == 5)
            await _kill_process(first)

            # Redis keeps the newest two while the store is down: minute 2 is gone for
            # good and minute 3 is the entry that survived at the boundary.
            trimmer = fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )
            try:
                await trimmer.xtrim(key, maxlen=2, approximate=False)
                info = await trimmer.xinfo_stream(key)
            finally:
                await trimmer.aclose()
            assert int(_info(info, "entries-added")) == 5
            assert int(_info(info, "length")) == 2

            clock.value = (BASE + timedelta(minutes=6, seconds=10)).timestamp()
            second = await _make_process(tmp_path, server, clock)
            try:
                await _start_writer(second, flush_seconds=10_000.0)
                # Read through to the head of the stream, then let the writer catch up:
                # what is folded by then is everything the replay is going to give.
                head = _info(info, "last-generated-id")
                await _wait_until(
                    lambda: second.subscription.last_ids.get(key) == head
                )
                await _wait_until(
                    lambda: second.writer.aggregator.ticks
                    == second.subscription.offered
                )
                # Minute 3 is the entry that survived the trim at the boundary. A cursor
                # set on it rather than before it reads past it, and one arrives (#85).
                assert second.writer.aggregator.ticks == 2
                # One entry not delivered — minute 2 — and `lost` says one.
                assert second.writer.replay_gap_entries == 1
                # Rebased onto the trim, so the index is the stream's own ordinal again.
                assert second.subscription.positions[key].index == 5
                second.writer._seal(clock())
                assert second.writer._commit() == 2
            finally:
                await _kill_process(second)

            rows = BarStore(tmp_path).scan().collect()
            minutes = sorted(row["minute"] for row in rows.iter_rows(named=True))
            base = BASE.replace(second=0, microsecond=0)
            # Minutes 0 and 1 committed before the kill; 3 and 4 are the retained suffix,
            # 3 being the first retained entry. Minute 2 is the genuine loss.
            assert minutes == [base + timedelta(minutes=n) for n in (0, 1, 3, 4)]
        except Exception:
            if first.bus.client is not None:
                await first.bus.aclose()
            raise

    asyncio.run(scenario())


def test_store_pause_resume_commits_a_span_and_leaves_paused_minutes_empty(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2, seconds=10)).timestamp())
        process = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(process, flush_seconds=10_000.0)
            process.bus.publish(_quote_event(0, bid=100.0))
            await process.bus.flush()
            await _wait_until(lambda: process.writer.aggregator.ticks == 1)

            process.bus.publish(_command("pause"))
            await process.bus.flush()
            await _wait_until(lambda: process.writer.recording is False)
            await _wait_until(lambda: process.writer.generation >= 1)
            checkpoint = read_checkpoint(tmp_path)
            assert checkpoint is not None
            assert checkpoint.recording is False
            assert len(checkpoint.pauses) == 1
            assert checkpoint.pauses[0].to is None

            process.bus.publish(_quote_event(1, bid=999.0))
            await process.bus.flush()
            await _wait_until(lambda: process.writer.discarded == 1)

            process.bus.publish(_command("resume"))
            await process.bus.flush()
            await _wait_until(lambda: process.writer.recording is True)
            await _wait_until(lambda: process.writer.generation >= 2)
            final_checkpoint = read_checkpoint(tmp_path)
            assert final_checkpoint is not None
            assert final_checkpoint.recording is True
            assert final_checkpoint.pauses[0].to is not None

            rows = BarStore(tmp_path).scan().collect()
            assert rows.height == 1
            assert rows["minute"].to_list() == [
                BASE.replace(second=0, microsecond=0)
            ]
            assert rows["bid_open"].to_list() == [100.0]
        finally:
            if process.bus.client is not None:
                await store_main._close_process(process)

    asyncio.run(scenario())


def test_store_restart_drops_events_inside_an_open_pause_span(tmp_path: Path) -> None:
    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2, seconds=10)).timestamp())
        first = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(first, flush_seconds=10_000.0)
            first.bus.publish(_command("pause"))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.recording is False)
            await _wait_until(lambda: first.writer.generation >= 1)
            checkpoint = read_checkpoint(tmp_path)
            assert checkpoint is not None
            assert checkpoint.pauses[0].to is None

            first.bus.publish(_quote_event(1, bid=999.0))
            await first.bus.flush()
            await _wait_until(lambda: first.writer.discarded == 1)
            await _kill_process(first)

            second = await _make_process(tmp_path, server, clock)
            try:
                assert second.subscription.skip == checkpoint.pauses
                await _start_writer(second, flush_seconds=10_000.0)
                await _wait_until(lambda: second.subscription.span_dropped == 1)
                assert second.writer.aggregator.ticks == 0
                assert BarStore(tmp_path).scan().collect().height == 0
            finally:
                if second.bus.client is not None:
                    await _kill_process(second)
        finally:
            if first.bus.client is not None:
                await first.bus.aclose()

    asyncio.run(scenario())


def test_store_ignores_feed_targeted_commands_and_counts_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2)).timestamp())
        process = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(process, flush_seconds=10_000.0)
            process.bus.publish(_command("pause", target="feed"))
            await process.bus.flush()
            await _wait_until(lambda: process.writer.control_ignored == 1)
            assert process.writer.recording is True
        finally:
            if process.bus.client is not None:
                await _kill_process(process)

    asyncio.run(scenario())


def test_store_folds_computed_chain_events_from_its_lossless_queue(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2, seconds=10)).timestamp())
        process = await _make_process(tmp_path, server, clock)
        try:
            await _start_writer(process, flush_seconds=10_000.0)
            process.bus.publish(_computed_event())
            await process.bus.flush()
            await _wait_until(lambda: process.writer.computed.ticks == 1)
            process.writer._seal(clock())
            assert process.writer._commit() == 1
        finally:
            if process.bus.client is not None:
                await _kill_process(process)

        rows = BarStore(
            tmp_path,
            dataset="computed-bars",
            schema=process.writer.computed_store.schema,
        ).scan().collect()
        assert rows.height == 1
        assert rows["symbol"].to_list() == [INSTRUMENT.venue_symbol]
        assert rows["model_version"].to_list() == ["api-model"]

    asyncio.run(scenario())


def test_a_writer_with_a_chain_cache_counts_computed_chain_as_skipped() -> None:
    writer = BarWriter(chains=lambda: [])

    writer.ingest(_computed_event())

    assert writer.skipped == 1
    assert writer.computed.ticks == 0


def test_store_state_reports_writer_counters() -> None:
    writer = BarWriter()
    writer.recording = False
    writer.committed_generation = 7
    writer.replay_gap_entries = 4
    writer.flush_errors = 2
    writer.aggregator.already_flushed = 1
    writer.reference.already_flushed = 2
    writer.spot.already_flushed = 3
    writer.computed.already_flushed = 4
    process = store_main.StoreProcess(
        root=Path("."),
        writer=writer,
        bus=None,
        subscription=None,
        control_subscription=None,
        clock=Clock(BASE.timestamp()),
    )

    event = store_main.state_event(process)

    assert event.recording is False
    assert event.generation == 7
    assert event.replay_gap_entries == 4
    assert event.already_flushed == 10
    assert event.flush_errors == 2


def test_store_state_publishes_immediately_when_a_state_change_is_signalled(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        published = []
        writer = BarWriter(BarStore(tmp_path))
        process = store_main.StoreProcess(
            root=tmp_path,
            writer=writer,
            bus=type("Bus", (), {"publish": published.append})(),
            subscription=None,
            control_subscription=None,
            clock=Clock(BASE.timestamp()),
        )
        gate = asyncio.Event()

        async def sleep(_seconds: float) -> None:
            await gate.wait()

        task = asyncio.create_task(
            store_main.publish_state_forever(process, sleep=sleep)
        )
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert len(published) == 1
            writer.recording = False
            process.state_changed.set()
            for _ in range(10):
                await asyncio.sleep(0)
            assert len(published) == 2
            assert published[-1].recording is False
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_store_state_publish_failure_is_counted_without_raising(tmp_path: Path) -> None:
    writer = BarWriter(BarStore(tmp_path))

    class BrokenBus:
        def publish(self, _event) -> None:
            raise RuntimeError("injected state publish failure")

    process = store_main.StoreProcess(
        root=tmp_path,
        writer=writer,
        bus=BrokenBus(),
        subscription=None,
        control_subscription=None,
        clock=Clock(BASE.timestamp()),
    )

    assert store_main.publish_state(process) is False
    assert process.state_publish_errors == 1


def test_a_split_writer_cannot_also_sample_a_chain_cache(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        BarWriter(
            BarStore(tmp_path),
            chains=lambda: [],
            computed=ComputedAggregator(),
            checkpoint_root=tmp_path,
        )
