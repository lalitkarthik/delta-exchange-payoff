"""What a split `store` guarantees across a discontinuity: a restart, and a pause.

#110 is the restart and #109 is the pause.

Both tickets are one question asked twice — *which writer wrote these bars, and what did
the watermark say afterwards?* — so the reproductions live together. The seam is the same
in both: `BarWriter` with a `checkpoint_root`, driven over a real `RedisBus` on fakeredis,
with `_seal` and `_commit` called by hand so the count of each is exact rather than
load-dependent (the reason `test_store_process.py` gives for `flush_seconds=10_000.0`).

**Nothing here reads a wall clock.** `Clock` is a fixture the test moves, and every
wait is `wait_helpers.wait_until` on a condition.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from deltapayoff import store_main
from deltapayoff.bars import (
    COMPUTED_SPLIT_GRACE_SECONDS,
    TICKER_GRACE_SECONDS,
)
from deltapayoff.events import (
    ChainLeg,
    ChainStrike,
    ComputedChain,
    IndexQuote,
    Instrument,
    OptionQuote,
    OptionReference,
    Right,
)
from deltapayoff.redis_bus import BusConfig, RedisBus
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    DATASET,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    read_checkpoint,
)
from wait_helpers import wait_until

BASE = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)

#: The minute that is **open** when the store stops, as 15:56 was on 2026-09-12.
OPEN_MINUTE = 2
OPEN_MINUTE_END = (BASE + timedelta(minutes=OPEN_MINUTE + 1)).timestamp()

INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 9, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270926",
)

TABLES = (DATASET, REFERENCE_DATASET, SPOT_DATASET, COMPUTED_DATASET)
_SCHEMAS = {
    DATASET: SCHEMA,
    REFERENCE_DATASET: REFERENCE_SCHEMA,
    SPOT_DATASET: SPOT_SCHEMA,
    COMPUTED_DATASET: COMPUTED_SCHEMA,
}

#: A flush file written inside a checkpoint root. Record 0010 R3: the generation exists
#: "so two processes cannot collide on one", which is only true if every file has one.
GENERATION_NAME = re.compile(r"^\d{8}T\d{6}Z-g\d{8}\.parquet$")


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _factory(server):
    import fakeredis.aioredis

    def factory(_config: BusConfig):
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)

    return factory


def _stamp(minute: int) -> datetime:
    return BASE + timedelta(minutes=minute, seconds=5)


def _quote_event(minute: int, bid: float = 100.0) -> OptionQuote:
    return OptionQuote(
        source="feed",
        ts_venue=_stamp(minute),
        ts_received=_stamp(minute),
        instrument=INSTRUMENT,
        bid=bid,
        ask=bid + 1.0,
    )


def _reference_event(minute: int, mark: float = 100.5) -> OptionReference:
    return OptionReference(
        source="feed",
        ts_venue=_stamp(minute),
        ts_received=_stamp(minute),
        instrument=INSTRUMENT,
        mark=mark,
    )


def _spot_event(minute: int, spot: float = 60_000.0) -> IndexQuote:
    return IndexQuote(
        source="feed",
        ts_venue=_stamp(minute),
        ts_received=_stamp(minute),
        underlying="BTC",
        spot=spot,
    )


def _computed_event(minute: int, iv: float = 0.4) -> ComputedChain:
    return ComputedChain(
        source="chain-cache",
        ts_received=_stamp(minute),
        underlying="BTC",
        expiry=date(2026, 9, 27),
        fetched_at=_stamp(minute),
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
                    iv=iv,
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


def _publish_minute(bus: RedisBus, minute: int) -> None:
    """One arrival on every one of the four streams, inside `minute`."""
    bus.publish(_quote_event(minute))
    bus.publish(_reference_event(minute))
    bus.publish(_spot_event(minute))
    bus.publish(_computed_event(minute))


async def _make_process(root: Path, server, clock: Clock) -> store_main.StoreProcess:
    bus = RedisBus(
        BusConfig(
            venue="DELTA",
            underlyings=("BTC",),
            batch_ms=60_000,
            read_block_ms=0,
            read_count=10,
            idle_sleep_seconds=0.001,
            # Age trimming off, for `test_store_process.py`'s reason: the fixture clock
            # and Redis's own entry-id clock are two clocks, so a real floor would trim
            # whatever the hour of the run happened to make it trim.
            retention_seconds=1_000_000_000.0,
        ),
        client_factory=_factory(server),
    )
    return await store_main._prepare_process(root=root, bus=bus, clock=clock)


async def _kill(process: store_main.StoreProcess) -> None:
    for task in process.tasks:
        task.cancel()
    await asyncio.gather(*process.tasks, return_exceptions=True)
    process.tasks = []
    await process.bus.aclose()


def _aggregators(writer):
    return {
        DATASET: writer.aggregator,
        REFERENCE_DATASET: writer.reference,
        SPOT_DATASET: writer.spot,
        COMPUTED_DATASET: writer.computed,
    }


def _folded(writer) -> int:
    return sum(aggregator.ticks for aggregator in _aggregators(writer).values())


def _seen(writer) -> int:
    """Ticks the four aggregators have **decided about** — folded, late or already
    flushed. A replayed tick that is refused still moves one of these, so this is what a
    test waits on when the whole point is that a tick may be refused."""
    return sum(
        aggregator.ticks + aggregator.late + aggregator.already_flushed
        for aggregator in _aggregators(writer).values()
    )


def _minute_counts(root: Path) -> dict[str, Counter[str]]:
    """How many **rows** each table holds per minute, read back off Parquet.

    A set of minutes answers #110 (was it lost?); a count answers #109 (was it written
    twice?). #63's *"what to notice"* says a bar sealed twice is as wrong as a bar lost,
    so both questions are asked of the same files.
    """
    found: dict[str, Counter[str]] = {}
    for dataset, schema in _SCHEMAS.items():
        store = BarStore(root, dataset=dataset, schema=schema)
        if not any(Path(root, dataset).rglob("*.parquet")):
            found[dataset] = Counter()
            continue
        frame = store.scan().collect()
        found[dataset] = Counter(
            stamp.strftime("%H:%M") for stamp in frame["minute"].to_list()
        )
    return found


def _flush_files(root: Path) -> list[Path]:
    return sorted(
        path
        for dataset in TABLES
        for path in Path(root, dataset).rglob("*.parquet")
    )


def _minutes_on_disk(root: Path) -> dict[str, set[str]]:
    """The distinct `minute` values each table holds, read back off Parquet.

    The **minute set**, never the row count: a row count cannot tell a minute that is
    missing from one that was folded somewhere else (`test_compaction.py`, #63).
    """
    found: dict[str, set[str]] = {}
    for dataset, schema in _SCHEMAS.items():
        store = BarStore(root, dataset=dataset, schema=schema)
        if not any(Path(root, dataset).rglob("*.parquet")):
            found[dataset] = set()
            continue
        frame = store.scan().collect()
        found[dataset] = {
            stamp.strftime("%H:%M") for stamp in frame["minute"].to_list()
        }
    return found


# --------------------------------------------------------------------------- #110


def test_the_four_graces_put_a_six_second_window_on_the_checkpoint(
    tmp_path: Path,
) -> None:
    """#110's `measured` 6,000,000 us, reproduced from the code rather than quoted.

    The four aggregators seal at **one** clock reading and four graces, and
    `_Watermarked.seal` writes `(now - grace) - 60 s` into the watermark whether or not
    anything was open. So the checkpoint's four `sealed_through_us` differ by exactly the
    differences between the graces, always — and `computed-bars` sits
    `TICKER_GRACE_SECONDS - COMPUTED_SPLIT_GRACE_SECONDS` ahead of the other three.

    That is the whole of the correlation #110 reports. It is a property of the graces and
    it says nothing yet about the loss; the test below is what says something about that.
    """

    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=OPEN_MINUTE, seconds=56.8)).timestamp())
        process = await _make_process(tmp_path, server, clock)
        try:
            _publish_minute(process.bus, OPEN_MINUTE)
            await process.bus.flush()
            await wait_until(
                lambda: process.subscription.offered == 4,
                message="the four events did not reach the subscription",
            )
            while not process.subscription.queue.empty():
                process.writer.ingest(process.subscription.queue.get_nowait())
            assert _folded(process.writer) == 4

            # The shutdown flush, exactly as `aclose` performs it in split mode.
            await process.writer.aclose()
        finally:
            await _kill(process)

        checkpoint = read_checkpoint(tmp_path)
        assert checkpoint is not None
        sealed = checkpoint.sealed_through_us
        gap = int((TICKER_GRACE_SECONDS - COMPUTED_SPLIT_GRACE_SECONDS) * 1e6)
        assert gap == 6_000_000
        assert sealed[DATASET] == sealed[REFERENCE_DATASET] == sealed[SPOT_DATASET]
        assert sealed[COMPUTED_DATASET] - sealed[DATASET] == gap
        # And the minute that was open is **below** every one of them, so a restart is
        # entitled to re-fold it into all four tables.
        open_minute_us = int((BASE + timedelta(minutes=OPEN_MINUTE)).timestamp() * 1e6)
        for dataset in TABLES:
            assert sealed[dataset] < open_minute_us

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("offset", "expected_losers"),
    [
        (0.0, set()),
        (COMPUTED_SPLIT_GRACE_SECONDS - 0.1, set()),
        (COMPUTED_SPLIT_GRACE_SECONDS, {COMPUTED_DATASET}),
        (COMPUTED_SPLIT_GRACE_SECONDS + 3.0, {COMPUTED_DATASET}),
        (TICKER_GRACE_SECONDS - 0.1, {COMPUTED_DATASET}),
        (TICKER_GRACE_SECONDS, set(TABLES)),
    ],
    ids=[
        "at-the-edge",
        "just-inside",
        "grace-2",
        "mid-window",
        "just-under-8",
        "grace-8",
    ],
)
def test_a_seal_pass_during_the_replay_loses_the_open_minute_grace_by_grace(
    tmp_path: Path, offset: float, expected_losers: set[str]
) -> None:
    """**#110's mechanism.** The restart is not what loses the minute. *A seal pass that
    runs before the replay has re-folded it* is.

    `BarWriter.run` seals on **every** pass of its drain loop, including the passes where
    the queue is empty because the readers are still replaying — `_seal` is below the
    `if not self.recording: continue` guard and above nothing else. The seal boundary is
    `seal_clock() - grace - 60 s`, so a pass at `minute_end + g` refuses every later
    arrival for that minute in a table whose grace is `g` or less. Nothing else has to go
    wrong.

    That makes the loss a **window**, and the window's width is the difference between
    the graces:

    * before `minute_end + COMPUTED_SPLIT_GRACE_SECONDS` all four keep it,
    * from there to `minute_end + TICKER_GRACE_SECONDS` **`computed-bars` alone loses
      it** — which is exactly the shape `.stack-data` holds for 15:56Z,
    * at `minute_end + TICKER_GRACE_SECONDS` all four lose it together.

    So the grace asymmetry **is** the cause of *which* table lost 15:56, and it is not
    the cause of the loss: equalising the graces moves the single boundary, it does not
    remove it. Both halves of that sentence are parametrised above.

    The seal pass here is one explicit call rather than a drain loop left to race the
    readers, because the ordering **is** the finding: a loop started against an empty
    queue does this on its first pass and a test that waits for a scheduler to arrange it
    would be a duration bet on the mechanism it is trying to name.
    """

    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=OPEN_MINUTE, seconds=56.8)).timestamp())
        first = await _make_process(tmp_path, server, clock)
        try:
            _publish_minute(first.bus, OPEN_MINUTE)
            await first.bus.flush()
            await wait_until(
                lambda: first.subscription.offered == 4,
                message="the four events did not reach the first subscription",
            )
            while not first.subscription.queue.empty():
                first.writer.ingest(first.subscription.queue.get_nowait())
            assert _folded(first.writer) == 4
            # Shutdown with the minute open in all four aggregators. Nothing seals: the
            # clock is still inside the minute.
            await first.writer.aclose()
            assert first.writer.generation == 1
        finally:
            await _kill(first)

        assert _minutes_on_disk(tmp_path) == {dataset: set() for dataset in TABLES}

        second = await _make_process(tmp_path, server, clock)
        try:
            # One pass of the drain loop, on the queue as the readers found it — which
            # at this instant is whatever the replay has managed, and the clock has
            # moved on by `offset` past the end of the open minute.
            clock.value = OPEN_MINUTE_END + offset
            second.writer._seal(clock())

            # Now the replay lands. `_seen` rather than `_folded`: a refused tick is the
            # outcome under test, so waiting on `ticks` would hang on exactly the runs
            # that matter.
            await wait_until(
                lambda: second.subscription.offered == 4,
                message="the replay did not re-deliver the four events",
            )
            while not second.subscription.queue.empty():
                second.writer.ingest(second.subscription.queue.get_nowait())
            assert _seen(second.writer) == 4

            # Seal well past every grace, and commit, so nothing is left open.
            clock.value = OPEN_MINUTE_END + 600.0
            second.writer._seal(clock())
            second.writer._commit()
        finally:
            await _kill(second)

        minute = _stamp(OPEN_MINUTE).strftime("%H:%M")
        on_disk = _minutes_on_disk(tmp_path)
        losers = {dataset for dataset in TABLES if minute not in on_disk[dataset]}
        assert losers == expected_losers, (
            f"at minute_end+{offset}s the tables missing {minute} were "
            f"{sorted(losers)}, expected {sorted(expected_losers)}; on disk: "
            f"{ {k: sorted(v) for k, v in on_disk.items()} }"
        )

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #109


async def _start_writer(process: store_main.StoreProcess) -> None:
    """The drain loop at this file's tick, with the periodic commit out of the way.

    `flush_seconds` is 10_000.0 for `test_store_process.py`'s reason: every test here
    calls `_seal` and `_commit` by hand for an exact count, and the periodic timer would
    add a load-dependent number of commits nobody counted.
    """
    process.writer.tick_seconds = 0.01
    process.writer.flush_seconds = 10_000.0
    process.tasks = [
        asyncio.create_task(process.writer.run(), name="seam-store-writer"),
    ]


async def _fold_and_seal(
    process: store_main.StoreProcess, minutes: tuple[int, ...]
) -> None:
    """Publish one arrival per stream in each minute and fold every one of them."""
    for minute in minutes:
        _publish_minute(process.bus, minute)
    await process.bus.flush()
    expected = 4 * len(minutes)
    await wait_until(
        lambda: process.subscription.offered == expected,
        message=f"fewer than {expected} events reached the subscription",
    )
    while not process.subscription.queue.empty():
        process.writer.ingest(process.subscription.queue.get_nowait())
    assert _folded(process.writer) == expected


def test_a_pause_in_split_mode_writes_through_the_generation_transaction(
    tmp_path: Path,
) -> None:
    """**#109's answer, and it is the duplicate.**

    `set_recording(False)` used to call `_flush_all` with no `checkpoint_root` branch,
    where `aclose` branched to `_commit`. On a split `store` that wrote Parquet with no
    generation in the name, no flush intent and **no checkpoint** - so the watermark
    stayed behind bars that were already on disk, and the next start-up replayed the
    entries those bars came from and folded them again.

    #63's *"what to notice"* asked which way round the risk was, and answered that the
    tests have always looked for the duplicate while the losses are what keep happening.
    **Here the duplicate is what happens**, and this is the one seam where it does:
    every other restart resumes from a checkpoint that moved, and this one resumes from
    a checkpoint that did not.

    The shipped split `store` pauses over the bus - `store_main.consume_control` into
    `_apply_pending_commands` - which always did branch. `set_recording` is reachable
    only from `main.py`'s `POST /recording`, and only where the writer is the
    **monolith's**, which has no checkpoint root. So this was a real defect on a seam
    the shipped compositions do not take today, and a trap for the first caller that
    did. Both halves are asserted: the pause commits a generation, and the restart
    afterwards writes each minute exactly once.
    """

    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        # Past minute 1's grace so both minutes seal, and inside minute 2 so nothing of
        # minute 2 is written and the restart has nothing else to do.
        clock = Clock((BASE + timedelta(minutes=2, seconds=30)).timestamp())
        first = await _make_process(tmp_path, server, clock)
        try:
            await _fold_and_seal(first, (0, 1))
            assert first.writer.generation == 0
            assert await first.writer.set_recording(False) is False
            paused_generation = first.writer.generation
        finally:
            await _kill(first)

        # Criterion 2: the pause went through the same transaction as every other split
        # flush - a generation in every file name, and a checkpoint after it.
        assert paused_generation == 1, (
            "a pause on a split store committed no generation; it flushed outside the "
            "checkpoint protocol"
        )
        checkpoint = read_checkpoint(tmp_path)
        assert checkpoint is not None
        assert checkpoint.generation == 1
        assert checkpoint.recording is False
        # Criterion 5: the single assertion that would have caught this on its own.
        offenders = [
            path.name
            for path in _flush_files(tmp_path)
            if not GENERATION_NAME.match(path.name)
        ]
        assert offenders == [], (
            f"flush files without a generation in a checkpoint root: {offenders}"
        )

        before = _minute_counts(tmp_path)
        assert {minute for table in before.values() for minute in table} == {
            "10:00",
            "10:01",
        }

        second = await _make_process(tmp_path, server, clock)
        try:
            # The checkpoint carries the pause, so the restart comes back paused. Under
            # the defect there was no checkpoint at all and it came back **recording**,
            # which is how it got to re-fold the minutes it had already written.
            assert second.writer.recording is False
            second.writer.recording = True

            # A sentinel past everything already written. The stream is ordered, so a
            # reader that has reached the sentinel has delivered every entry the seam
            # could have doubled -- no sleeping and no patching of the code under test
            # to know when to look. (`test_store_process.py` uses the same device.)
            _publish_minute(second.bus, 5)
            await second.bus.flush()
            heads = {}
            for key in second.subscription.streams:
                info = await second.bus.client.xinfo_stream(key)
                value = info.get("last-generated-id", info.get(b"last-generated-id"))
                heads[key] = (
                    value.decode()
                    if isinstance(value, (bytes, bytearray))
                    else str(value)
                )
            await wait_until(
                lambda: all(
                    second.subscription.positions[key].id == head
                    for key, head in heads.items()
                ),
                message="the readers did not reach the sentinel on every stream",
            )
            while not second.subscription.queue.empty():
                second.writer.ingest(second.subscription.queue.get_nowait())
            clock.value = (BASE + timedelta(minutes=10)).timestamp()
            second.writer._seal(clock())
            second.writer._commit()
        finally:
            await _kill(second)

        after = _minute_counts(tmp_path)
        for dataset in TABLES:
            for minute in ("10:00", "10:01"):
                assert after[dataset][minute] == 1, (
                    f"{dataset} holds {after[dataset][minute]} rows for {minute} after "
                    f"a pause and a restart; the pause left the watermark behind the "
                    f"bars it had already written"
                )
        # Minute 5 is the sentinel, folded once. Nothing was invented for the
        # minutes that elapsed while the store was paused or down.
        assert set(after[DATASET]) == {"10:00", "10:01", "10:05"}

    asyncio.run(scenario())


def test_every_flush_file_in_a_checkpoint_root_carries_a_generation(
    tmp_path: Path,
) -> None:
    """Record 0010 R3's invariant, asserted over the root rather than over one writer.

    **One assertion, every path.** #109 existed because a criterion about the control
    path was met by a test of the control path, while the storage protocol underneath it
    was never looked at. This looks at the files, so it holds for a caller nobody has
    written yet: the periodic flush, a pause, a resume and a shutdown all land here.
    """

    async def scenario() -> None:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        clock = Clock((BASE + timedelta(minutes=2, seconds=30)).timestamp())
        process = await _make_process(tmp_path, server, clock)
        try:
            await _fold_and_seal(process, (0, 1))
            await process.writer.set_recording(False)  # pause
            await process.writer.set_recording(True)  # resume
            _publish_minute(process.bus, 2)
            await process.bus.flush()
            await wait_until(
                lambda: process.subscription.offered == 12,
                message="minute 2 did not reach the subscription",
            )
            while not process.subscription.queue.empty():
                process.writer.ingest(process.subscription.queue.get_nowait())
            clock.value = (BASE + timedelta(minutes=10)).timestamp()
            process.writer._seal(clock())
            process.writer._commit()  # periodic
            await process.writer.aclose()  # shutdown
        finally:
            await _kill(process)

        files = _flush_files(tmp_path)
        assert files, "the scenario wrote no flush files at all"
        offenders = [
            path.name for path in files if not GENERATION_NAME.match(path.name)
        ]
        assert offenders == [], (
            f"flush files without a generation in a checkpoint root: {offenders}"
        )
