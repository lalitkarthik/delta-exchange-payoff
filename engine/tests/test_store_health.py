"""What the store says about itself when it has stopped consuming. **#103.**

Throughout a two-hour stall the store answered `/health` with `200 OK` and this body,
`measured` live at 13:12Z from `dxp-store`:

```
{"status":"ok","recording":true,"generation":88,"buffered_rows":0,
 "rows_written":523510,"replay_gap_entries":0,"already_flushed":0}
```

The consumer group was 2,229,086 entries behind across three streams and 97 minutes of
those entries had already been trimmed away unread. `generation` and `rows_written` were
both advancing and both true. **There is no field in that payload a person could have
looked at and known**, and `"status": "ok"` was a literal.

Every test here drives the real `store_main` composition over `fakeredis`, kills or
starves the reader the way the incident did, and asks the process what it thinks.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltapayoff import store_main
from deltapayoff.events import Alert, Instrument, OptionQuote, Right
from deltapayoff.events.redis_wire import stream_name
from deltapayoff.redis_bus import BusConfig, RedisBus, StreamLag

BASE = datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)
INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 9, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270926",
)
QUOTE_STREAM = stream_name(
    OptionQuote(source="delta", ts_received=BASE, instrument=INSTRUMENT, bid=1.0)
)
TIMEOUT_MESSAGE = "Timeout reading from redis:6379"


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def quote(minute: int = 0, bid: float = 100.0) -> OptionQuote:
    return OptionQuote(
        source="delta",
        ts_received=BASE + timedelta(minutes=minute),
        instrument=INSTRUMENT,
        bid=bid,
    )


class Disrupted:
    """`fakeredis`'s client with the incident's timeout under the test's control."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failing = 0
        self.failures = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any:
        import redis.exceptions

        if self.failing:
            if self.failing > 0:
                self.failing -= 1
            self.failures += 1
            raise redis.exceptions.TimeoutError(TIMEOUT_MESSAGE)
        return await self._inner.xreadgroup(*args, **kwargs)


async def until(predicate, timeout: float = 10.0, what: str = "") -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(poll(), timeout)
    except TimeoutError:  # pragma: no cover - only on a real failure
        raise AssertionError(f"never became true: {what}") from None


def _bus(server, clients: list, *, retries: int = 0) -> RedisBus:
    import fakeredis.aioredis

    def factory(_config: BusConfig) -> Any:
        client = Disrupted(
            fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)
        )
        clients.append(client)
        return client

    return RedisBus(
        BusConfig(
            venue="DELTA",
            underlyings=("BTC",),
            # **The flusher must not fire on its own here.** `_alerts` reads the
            # outbox, so a background batch every 5 ms would drain the very thing the
            # assertion inspects, and the test would pass or fail on scheduler timing.
            # Every publish this file depends on goes through an explicit `flush()`.
            batch_ms=60_000,
            read_block_ms=0,
            read_count=10,
            idle_sleep_seconds=0.001,
            retention_seconds=1_000_000_000.0,
            read_retries=retries,
            read_retry_seconds=0.01,
            read_retry_ceiling_seconds=0.01,
        ),
        client_factory=factory,
    )


async def _make(root: Path, clock: Clock, clients: list) -> Any:
    import fakeredis.aioredis

    server = fakeredis.aioredis.FakeServer()
    bus = _bus(server, clients)
    process = await store_main._prepare_process(root=root, bus=bus, clock=clock)
    return process, server


async def _close(process: Any) -> None:
    for task in process.tasks:
        task.cancel()
    if process.tasks:
        await asyncio.gather(*process.tasks, return_exceptions=True)
    process.tasks = []
    with contextlib.suppress(Exception):
        await process.bus.aclose()


def _seconds(entry_id: str) -> float:
    """A stream id's milliseconds as seconds -- `store.py`'s own `_stream_id_seconds`."""
    milliseconds, _, _sequence = entry_id.partition("-")
    return int(milliseconds) / 1000.0


def _alerts(process: Any) -> list[Alert]:
    """Alerts sitting in the publisher's outbox, decoded back off the wire.

    Read from the outbox rather than through a second subscription: the reader this
    ticket is about is the thing being broken in these tests, so a test that depended on
    one to observe the alert would be depending on the failure under test.
    """
    from deltapayoff.events.redis_wire import decode

    return [
        event
        for key, fields in process.bus._outbox
        if (event := decode(fields, stream=key)).type == "alert"
    ]


# ------------------------------------------------- a dead reader must fail the check


def test_a_store_whose_reader_has_died_does_not_report_ok(tmp_path: Path) -> None:
    """The criterion, in one sentence: a consumer that stops consuming fails its health
    check. `/health` returning 200 while a lossless subscription is 1.86 million entries
    behind is the defect that made this cost two hours rather than two minutes."""

    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        clients: list = []
        clock = Clock(BASE.timestamp())
        process, _server = await _make(tmp_path, clock, clients)
        try:
            healthy = store_main._health_payload(process)
            clients[0].failing = -1
            await until(
                lambda: not process.bus.readers()["store"]["alive"],
                what="the store's reader stopped",
            )
            return healthy, store_main._health_payload(process)
        finally:
            await _close(process)

    healthy, stalled = asyncio.run(scenario())

    assert healthy["status"] == "ok"
    assert stalled["status"] == "error"
    assert stalled["readers"]["store"]["alive"] is False
    assert any("store" in problem for problem in stalled["problems"])


def test_the_health_route_answers_503_when_the_store_has_stopped_consuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The status code is the whole point, not the body.**

    Compose's health check is `urllib.request.urlopen('.../health')`, which fails on a
    status and reads nothing. Every container in the stalled stack reported
    `Up 7 hours (healthy)` at 13:09Z for exactly that reason. A body saying `stalled`
    behind a 200 would have left `docker ps` green and changed nothing.
    """
    clients: list = []
    clock = Clock(BASE.timestamp())

    original = store_main._prepare_process

    async def prepared(**kwargs: Any) -> Any:
        import fakeredis.aioredis

        server = fakeredis.aioredis.FakeServer()
        return await original(root=tmp_path, bus=_bus(server, clients), clock=clock)

    monkeypatch.setattr(store_main, "_prepare_process", prepared)

    with TestClient(store_main.app) as client:
        first = client.get("/health")
        clients[0].failing = -1
        process = store_main.app.state.process
        deadline = 0
        while process.bus.readers()["store"]["alive"] and deadline < 2000:
            deadline += 1
        second = client.get("/health")

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.status_code == 503
    assert second.json()["status"] == "error"


# ------------------------------------------------------------- the lag threshold


def _lag(entries: int) -> dict[str, StreamLag]:
    return {
        QUOTE_STREAM: StreamLag(
            stream=QUOTE_STREAM,
            lag=entries,
            entries_read=1_000,
            entries_added=1_000 + entries,
            length=1_000 + entries,
            last_delivered_id="1789210958987-74",
            first_retained_id="1789210000000-0",
        )
    }


@pytest.mark.parametrize(
    ("entries", "status"),
    [
        (0, "ok"),
        (500, "ok"),
        (store_main.STORE_LAG_ALERT_ENTRIES, "ok"),
        (store_main.STORE_LAG_ALERT_ENTRIES + 1, "error"),
        (1_858_892, "error"),
    ],
)
def test_the_lag_threshold_separates_a_busy_tick_from_a_stalled_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: int, status: str
) -> None:
    """500 is one read batch and a busy tick; 1,858,892 is the incident.

    The threshold is `STORE_QUEUE_SIZE`, the lossless watermark this store already
    declares -- not a new number chosen beside it. `store_main.py` documents the
    derivation.
    """

    async def scenario() -> dict[str, Any]:
        clients: list = []
        clock = Clock(BASE.timestamp())
        process, _server = await _make(tmp_path, clock, clients)
        try:
            monkeypatch.setattr(
                process.bus, "consumer_lag", lambda _sub: _answer(_lag(entries))
            )
            await store_main.poll_bus(process)
            return store_main._health_payload(process)
        finally:
            await _close(process)

    payload = asyncio.run(scenario())

    assert payload["consumer_lag"] == {QUOTE_STREAM: entries}
    assert payload["status"] == status


async def _answer(value: Any) -> Any:
    return value


def test_a_lossless_consumer_past_the_threshold_raises_an_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted is not enough; the criterion asks for an `alert` event."""

    async def scenario() -> tuple[list[Alert], list[Alert]]:
        clients: list = []
        clock = Clock(BASE.timestamp())
        process, _server = await _make(tmp_path, clock, clients)
        try:
            monkeypatch.setattr(
                process.bus, "consumer_lag", lambda _sub: _answer(_lag(500))
            )
            await store_main.poll_bus(process)
            quiet = _alerts(process)
            monkeypatch.setattr(
                process.bus, "consumer_lag", lambda _sub: _answer(_lag(1_858_892))
            )
            await store_main.poll_bus(process)
            return quiet, _alerts(process)
        finally:
            await _close(process)

    quiet, loud = asyncio.run(scenario())

    assert [alert.code for alert in quiet] == []
    assert [alert.code for alert in loud] == ["store.consumer_lag"]
    assert "1858892" in loud[0].detail.replace(",", "")


# ------------------------------------------------ the replay gap, without a restart


def test_a_watermark_trimmed_past_is_counted_and_alerted_without_a_restart(
    tmp_path: Path,
) -> None:
    """**The exact test, and it needs no threshold.**

    `CONTEXT.md` calls a replay gap "a counted, alerted hole, when the watermark was
    trimmed before `store` came back". "Came back" was the whole problem: the check ran
    only inside `_prepare_process`, so **a store that never restarts never evaluates
    it**. This one did not crash and did not restart -- it simply stopped reading -- and
    `replay_gap_entries` reported `0` through the exact condition it was built to report.

    A replay gap is not an event that happens at startup. It is a condition that becomes
    true the moment retention passes the watermark, and it is true whether or not anyone
    restarts.
    """

    async def scenario() -> tuple[int, int, tuple[str, ...], list[Alert], dict[str, Any]]:
        import fakeredis.aioredis

        clients: list = []
        clock = Clock(BASE.timestamp())
        process, server = await _make(tmp_path, clock, clients)
        try:
            for minute in range(3):
                process.bus.publish(quote(minute, bid=100.0 + minute))
            await process.bus.flush()
            await until(
                lambda: process.subscription.offered == 3,
                what="the store read the three entries",
            )
            before = process.writer.replay_gap_entries
            await store_main.poll_bus(process)
            assert process.bus_gaps == ()

            # The reader stops, the publisher does not, and retention passes the
            # watermark -- the incident, in three commands.
            clients[0].failing = -1
            await until(
                lambda: not process.bus.readers()["store"]["alive"],
                what="the store's reader stopped",
            )
            for minute in range(3, 9):
                process.bus.publish(quote(minute, bid=100.0 + minute))
            await process.bus.flush()
            trimmer = fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )
            try:
                await trimmer.xtrim(QUOTE_STREAM, maxlen=2, approximate=False)
            finally:
                await trimmer.aclose()

            await store_main.poll_bus(process)
            return (
                before,
                process.writer.replay_gap_entries,
                process.bus_gaps,
                _alerts(process),
                store_main._health_payload(process),
            )
        finally:
            await _close(process)

    before, after, gaps, alerts, payload = asyncio.run(scenario())

    assert before == 0
    assert gaps == (QUOTE_STREAM,)
    # Nine entries added, two retained, three read: four were trimmed unread.
    assert after == 4
    assert "store.replay_gap" in [alert.code for alert in alerts]
    assert payload["status"] == "error"
    assert payload["replay_gap_entries"] == 4


def test_a_replay_gap_is_alerted_once_rather_than_every_poll(tmp_path: Path) -> None:
    """The condition stays true for as long as the store stays stopped -- two hours here,
    720 polls. The standing signal is `/health` and the counter; the alert marks the
    transition, because an alert repeated 720 times is an alert nobody reads."""

    async def scenario() -> tuple[int, int]:
        import fakeredis.aioredis

        clients: list = []
        clock = Clock(BASE.timestamp())
        process, server = await _make(tmp_path, clock, clients)
        try:
            for minute in range(3):
                process.bus.publish(quote(minute))
            await process.bus.flush()
            await until(lambda: process.subscription.offered == 3)
            clients[0].failing = -1
            await until(lambda: not process.bus.readers()["store"]["alive"])
            for minute in range(3, 9):
                process.bus.publish(quote(minute))
            await process.bus.flush()
            trimmer = fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )
            try:
                await trimmer.xtrim(QUOTE_STREAM, maxlen=2, approximate=False)
            finally:
                await trimmer.aclose()

            await store_main.poll_bus(process)
            once = len(
                [a for a in _alerts(process) if a.code == "store.replay_gap"]
            )
            for _ in range(5):
                await store_main.poll_bus(process)
            return once, len(
                [a for a in _alerts(process) if a.code == "store.replay_gap"]
            )
        finally:
            await _close(process)

    once, after_six = asyncio.run(scenario())

    assert once == 1
    assert after_six == 1


# ------------------------------------------------------------------ the seal clock


def test_a_dead_reader_hands_the_seal_clock_the_position_it_stopped_at(
    tmp_path: Path,
) -> None:
    """**The symptom that destroyed data, fixed as far as this ticket's territory goes.**

    `sealed_through_us` advanced by 300,304,025 microseconds between generations 88 and
    89 -- exact wall-clock cadence -- while the store had read nothing since 11:02:38Z.
    Two hours of minutes were sealed empty, and a seal closes a minute so nothing more
    can enter it.

    `store.py`'s rule is `min(wall clock, the time inside the last stream id of any
    stream still behind)`, and its two inputs are both this file's: **which streams are
    behind**, and **the position each one stopped at**.

    All three parts are asserted here. The third was impossible until `_stream_id_seconds`
    stopped raising on every stream id it was handed -- see the test below, and the
    docstring on `store._stream_id_seconds`. **Record 0010 R4 pins for the first time.**
    """

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...], str, float, float]:
        clients: list = []
        clock = Clock(BASE.timestamp())
        process, _server = await _make(tmp_path, clock, clients)
        try:
            process.bus.publish(quote(0))
            await process.bus.flush()
            await until(
                lambda: process.subscription.offered == 1,
                what="the store read the entry",
            )
            await until(
                lambda: process.subscription.behind_streams() == (),
                what="the reader reported itself caught up",
            )
            caught_up = process.subscription.behind_streams()
            # **Redis stamps entries with its own clock, so the fixture takes its time
            # from the entry rather than the other way round** -- the same reason
            # `test_bus_contract.py`'s trim tests move their clock forward from what
            # Redis produced. Two hours then pass, as they did in the incident.
            position = process.subscription.positions[QUOTE_STREAM].id
            clock.value = _seconds(position) + 7_200.0

            clients[0].failing = -1
            await until(
                lambda: not process.bus.readers()["store"]["alive"],
                what="the store's reader stopped",
            )
            return (
                caught_up,
                process.subscription.behind_streams(),
                process.subscription.positions[QUOTE_STREAM].id,
                clock.value,
                process.writer.seal_clock(),
            )
        finally:
            await _close(process)

    caught_up, behind, position, wall, sealed = asyncio.run(scenario())

    assert caught_up == (), "a reading store must not hold the seal clock back"
    assert behind == (QUOTE_STREAM,), "a dead reader reported itself caught up"
    # The position is unmoved since the reader stopped, and two hours older than the
    # wall clock the seal clock would otherwise use.
    assert wall - _seconds(position) == pytest.approx(7_200.0, abs=0.001)
    # And R4 actually pins: the clock follows the log, not the wall. This is the assertion
    # the incident needed and nothing could make -- `_stream_id_seconds` raised on every
    # id it was given, `seal_clock` swallowed it, and the clock fell through to `wall`.
    assert sealed == pytest.approx(_seconds(position), abs=0.001), (
        "the seal clock advanced past data the store has not read"
    )
    assert sealed < wall, "the seal clock did not pin behind the wall clock"


def test_store_stream_id_seconds_parses_well_formed_ids(
    tmp_path: Path,
) -> None:
    """**A direct unit pin of `_stream_id_seconds`, the input R4's clock is built on.**

    `store.py`'s `_stream_id_seconds` is::

        milliseconds, _, _sequence = value.partition("-")

    From #63 (`83120d6`) until #103 (`720036d`) this line read `value.split("-", 1)`
    instead, which yields **two** parts against three names unpacked, so it raised
    `ValueError: not enough values to unpack (expected 3, got 2)` for every stream id
    there is -- `0-0` included. `seal_clock` wraps the call in
    `except (TypeError, ValueError): continue`, so the list of times was always empty
    and the clock always fell through to `return wall`: record 0010 R4 never ran.
    #103's commit changed `split` to `partition`, matching `redis_bus._id_parts`, which
    this test now pins directly against the function itself.

    `test_a_dead_reader_hands_the_seal_clock_the_position_it_stopped_at` above pins the
    same fix through `seal_clock()`, end to end; this test isolates the one call that
    turns a stream id into a time, so a future regression here fails at the narrowest
    possible point.
    """
    from deltapayoff.store import _stream_id_seconds

    assert _stream_id_seconds("1789210956984-9") == 1789210956.984
    assert _stream_id_seconds("1789221274132-0") == 1789221274.132
    assert _stream_id_seconds("0-0") == 0.0
