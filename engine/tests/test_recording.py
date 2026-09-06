"""`GET /recording` and `POST /recording` — the switch on the store.

Driven over HTTP through `TestClient`, the seam the rest of the endpoint suite uses. The
application under it is the real one: its lifespan runs, its `BarWriter` is the real
writer on the real bus, and the only two things replaced are the socket to Delta, which
no test may open, and the writer's **clock**, which every test here assigns to directly.

**Nothing in this file derives a time from the wall clock.** Two tests in this suite have
already detonated on a calendar date with nobody touching the code, by reading
`datetime.now()` against a hardcoded expiry. The minutes below are literals; `time.sleep`
appears only to let the writer's task turn its loop, never as a quantity anything is
asserted against.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltapayoff.feed import Quote
from deltapayoff.models import ChainResponse, ChainRow, ComputedLeg, Leg
from deltapayoff.store import COMPUTED_DATASET, COMPUTED_SCHEMA, BarStore

MINUTE_US = int(datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1e6)
MINUTE = 60_000_000

#: One contract, all through this file. The store keys on it, so it is spelled once.
SYMBOL = "C-BTC-77600-040926"
#: A second contract, used only for traffic that arrives while recording is off. A
#: different symbol so its absence from the store is a fact about the pause and not
#: about a minute boundary.
PAUSED_SYMBOL = "P-BTC-77600-040926"

#: Long enough for the writer's task — waking every 10 ms — to take several passes, and
#: short enough that the file stays fast. It is a *yield*, not a measurement: nothing
#: below asserts anything about how much of it elapsed.
LET_THE_LOOP_TURN = 0.08


class _Clock:
    """The writer's clock, as a variable this file assigns to."""

    def __init__(self, at: float) -> None:
        self.now = at
        #: The recompute loop's cache, as a list this file appends to. Table C is
        #: sampled from it; see the `clock` fixture.
        self.chains: list[ChainResponse] = []

    def __call__(self) -> float:
        return self.now

    def set(self, at: float) -> None:
        self.now = at


class _StubDeltaClient:
    """Stands in for `DeltaClient` inside the lifespan. Opens nothing."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def tickers(self, underlying: str, expiry=None):
        return [{"symbol": "C-BTC-77600-040926"}]

    async def aclose(self) -> None:
        return None


class _StubFeed:
    """Stands in for `DeltaFeed`. Registers subscriptions and never dials out."""

    def __init__(self, fanout) -> None:
        self.fanout = fanout
        self.registry: dict[str, list[str]] = {}

    def subscribe(self, channel: str, symbols) -> None:
        self.registry.setdefault(channel, []).extend(symbols)

    async def run(self) -> None:
        await asyncio.Event().wait()


@pytest.fixture
def clock(monkeypatch, tmp_path: Path) -> _Clock:
    """The real app, with Delta stubbed out and the writer on a clock this test drives.

    Returns the clock. The store lands in `tmp_path`; nothing here reads `data/`.
    """
    from deltapayoff import main
    from deltapayoff.store import BarWriter as RealBarWriter

    driven = _Clock(MINUTE_US / 1e6 + 5.0)

    monkeypatch.setenv("DELTA_LIVE_FEED", "1")
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    monkeypatch.setattr(main, "DeltaFeed", _StubFeed)
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    def writer(*args, **kwargs):
        # A flush interval no test reaches, so the only writes are the ones a test
        # asks for: the buffer stays a buffer until something empties it deliberately.
        # `chains` replaces the recompute loop's cache with a list this file appends to,
        # exactly as `tests/test_store.py` does — table C is sampled from that cache
        # rather than folded off the bus, so it needs its own handle.
        kwargs.update(
            clock=driven,
            flush_seconds=3600.0,
            tick_seconds=0.01,
            chains=lambda: list(driven.chains),
        )
        return RealBarWriter(*args, **kwargs)

    monkeypatch.setattr(main, "BarWriter", writer)
    return driven


@pytest.fixture
def stub_delta(chain_tickers):
    """Delta's REST answers, from the committed fixture. Nothing dials out."""
    from deltapayoff import main

    class _Stub:
        async def tickers(self, underlying: str, expiry=None):
            return chain_tickers

    main.app.dependency_overrides[main.get_delta_client] = _Stub
    yield
    main.app.dependency_overrides.clear()


@pytest.fixture
def client(clock: _Clock):
    """The running application. The lifespan starts the writer task."""
    from deltapayoff import main

    with TestClient(main.app) as running:
        yield running


def quote(symbol: str, at_us: int, bid: float = 70.0) -> Quote:
    """One book frame, stamped at an instant this file chose."""
    return Quote(
        symbol=symbol,
        channel="ob_l2",
        bid=bid,
        ask=bid + 2.0,
        received_at=at_us / 1e6,
        frame={"sy": symbol, "ts": at_us, "lts": at_us - 300_000},
    )


# --- the state at start-up -------------------------------------------------------


def test_recording_is_on_when_the_process_starts(client) -> None:
    """A process that starts without recording silently captures nothing, and forgetting
    to switch it on is a worse failure than forgetting to switch it off."""
    response = client.get("/recording")

    assert response.status_code == 200, response.text
    assert response.json()["recording"] is True


# --- what a reader is told, against what the writer is doing ---------------------


def test_the_state_a_reader_is_told_matches_the_writer_immediately(client) -> None:
    """The POST answers with the state *after* the change, and the next reader agrees.

    Two tabs must not be able to disagree about whether the store is writing, so the
    switched state has to be true of the writer itself before the response leaves — not
    queued for a loop to notice.
    """
    from deltapayoff import main

    posted = client.post("/recording", json={"recording": False})
    assert posted.status_code == 200, posted.text
    assert posted.json()["recording"] is False
    assert main.app.state.writer.recording is False, "the writer is still recording"
    assert client.get("/recording").json()["recording"] is False

    posted = client.post("/recording", json={"recording": True})
    assert posted.json()["recording"] is True
    assert main.app.state.writer.recording is True
    assert client.get("/recording").json()["recording"] is True


# --- switching off: the buffer reaches the store before it stops -----------------


def test_switching_off_writes_the_buffered_minutes_before_it_stops(
    client, clock: _Clock, tmp_path: Path
) -> None:
    """Not "the call returned" — the minutes are read back off the disk.

    The buffer holds up to a five-minute flush interval of **sealed** bars. Discarding
    them on a pause would throw away data the engine already has, which is the exact
    loss #16 shortened the interval to reduce. Pressing stop must never cost a minute
    the engine had already captured.
    """
    from deltapayoff import main

    for offset, bid in ((0, 70.0), (1, 80.0)):
        main.app.state.fanout.publish(
            quote(SYMBOL, MINUTE_US + offset * MINUTE + 1_000, bid=bid)
        )
    # 09:03:20 by the writer's clock: both minutes are past their eight-second grace.
    clock.set((MINUTE_US + 3 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)

    before = client.get("/recording").json()
    assert before["buffered_rows"] == 2, "two sealed minutes should be waiting in memory"
    assert before["rows_written"] == 0, "nothing has reached the disk for this test yet"

    after = client.post("/recording", json={"recording": False}).json()

    assert after["recording"] is False
    assert after["buffered_rows"] == 0, "the pause left sealed bars in memory"
    assert after["rows_written"] == 2

    frame = BarStore(tmp_path).scan().collect().sort("minute")
    assert frame["minute"].to_list() == [
        datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc),
    ], "the buffered minutes never reached the store"
    assert frame["bid_close"].to_list() == [70.0, 80.0]


# --- switching off: nothing is captured while it is off --------------------------


def test_switching_off_stops_rows_being_written_for_subsequent_minutes(
    client, clock: _Clock, tmp_path: Path
) -> None:
    """A quote that arrived while recording was off is nowhere in the store — not on
    disk, not in the buffer, and not resurrected by switching recording back on.

    The store's own rule has not moved: a minute with no arrivals produces no row, not
    nulls and never the previous close. A minute that was never aggregated is such a
    minute.
    """
    from deltapayoff import main

    fanout = main.app.state.fanout
    fanout.publish(quote(SYMBOL, MINUTE_US + 1_000, bid=70.0))
    clock.set((MINUTE_US + 2 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)

    stopped = client.post("/recording", json={"recording": False}).json()
    assert stopped["rows_written"] == 1, "the recorded minute should be on disk"

    # Off. Four minutes of a second contract arrive on the bus and must vanish.
    for offset in (5, 6, 7, 8):
        fanout.publish(quote(PAUSED_SYMBOL, MINUTE_US + offset * MINUTE + 1_000))
    clock.set((MINUTE_US + 10 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)

    while_off = client.get("/recording").json()
    assert while_off["buffered_rows"] == 0, "a paused writer aggregated a bar"
    assert while_off["rows_written"] == 1, "a paused writer wrote a row"

    # Back on, then off again so anything the resume produced is flushed and visible.
    client.post("/recording", json={"recording": True})
    clock.set((MINUTE_US + 12 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)
    client.post("/recording", json={"recording": False})

    frame = BarStore(tmp_path).scan().collect()
    assert frame["symbol"].to_list() == [SYMBOL], (
        "a minute that arrived while recording was off reached the store"
    )


# --- the guard that matters most -------------------------------------------------


def test_a_paused_writer_still_drains_its_subscription(client, clock: _Clock) -> None:
    """**Pausing the store must not stall the feed.**

    The writer holds a *lossless* subscription: its queue has no ceiling, so a writer
    that stopped taking messages off it would let it grow without bound and back the
    socket reader up behind it. The failure mode is not a missing row — it is a stalled
    feed and, eventually, Delta closing a connection we simply failed to drain. Nothing
    else in this suite would catch it.

    The second half of the same fault is visible one test above: a queue held through a
    pause is folded into bars the moment recording resumes, back-filling exactly the
    minutes the pause was meant to leave empty.
    """
    from deltapayoff import main

    client.post("/recording", json={"recording": False})
    # Let the loop reach its paused steady state *before* anything is published. Without
    # this the writer may still be parked in the `queue.get()` of a pass that began
    # while recording was on, and that one pass would drain the whole burst however the
    # pause is implemented — which makes the assertion below pass for the wrong reason.
    time.sleep(LET_THE_LOOP_TURN)

    for offset in range(200):
        main.app.state.fanout.publish(
            quote(PAUSED_SYMBOL, MINUTE_US + 5 * MINUTE + offset * 1_000)
        )
    time.sleep(LET_THE_LOOP_TURN)

    stats = main.app.state.writer.stats()
    assert stats["queued"] == 0, "a paused writer stopped draining its subscription"
    assert stats["discarded"] == 200, "the drained records were not accounted for"
    assert stats["ticks"] == 0, "a paused writer folded a record into a bar"


# --- switching back on -----------------------------------------------------------


def test_switching_recording_back_on_resumes_writing(
    client, clock: _Clock, tmp_path: Path
) -> None:
    """Pausing is not a decision anyone should have to be sure about.

    A minute that arrives after the resume is stored exactly as one before the pause is,
    and the minutes that elapsed while it was off stay absent — the resume recovers
    nothing and invents nothing.
    """
    from deltapayoff import main

    fanout = main.app.state.fanout
    client.post("/recording", json={"recording": False})
    time.sleep(LET_THE_LOOP_TURN)

    fanout.publish(quote(PAUSED_SYMBOL, MINUTE_US + 5 * MINUTE + 1_000))
    clock.set((MINUTE_US + 7 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)

    resumed = client.post("/recording", json={"recording": True}).json()
    assert resumed["recording"] is True
    time.sleep(LET_THE_LOOP_TURN)

    fanout.publish(quote(SYMBOL, MINUTE_US + 10 * MINUTE + 1_000, bid=91.0))
    clock.set((MINUTE_US + 12 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)

    # Off again purely to get the resumed minute onto the disk this test reads.
    client.post("/recording", json={"recording": False})

    frame = BarStore(tmp_path).scan().collect()
    assert frame["symbol"].to_list() == [SYMBOL], "recording did not resume"
    assert frame["minute"].to_list() == [
        datetime(2026, 9, 4, 9, 10, tzinfo=timezone.utc)
    ]
    assert frame["bid_close"].to_list() == [91.0]


# --- the live screens are upstream of the writer and are unaffected --------------


def test_the_live_routes_keep_answering_while_recording_is_off(
    client, stub_delta
) -> None:
    """Stopping the recording is not the same as stopping the terminal.

    Everything the live screens do — the ladder over REST, the ladder over the socket,
    the stored smile — happens upstream of the writer or beside it, and a pause must not
    reach any of it.
    """
    stopped = client.post("/recording", json={"recording": False}).json()
    assert stopped["recording"] is False

    assert client.get("/health").json() == {"status": "ok"}

    expiries = client.get("/expiries", params={"underlying": "BTC"})
    assert expiries.status_code == 200, expiries.text
    assert "04-09-2026" in expiries.json()["expiries"]

    chain = client.get("/chain", params={"underlying": "BTC", "expiry": "04-09-2026"})
    assert chain.status_code == 200, chain.text
    assert len(chain.json()["rows"]) == 65

    smile = client.get("/smile", params={"underlying": "BTC", "expiry": "04-09-2026"})
    assert smile.status_code == 200, smile.text

    with client.websocket_connect(
        "/ws/chain?underlying=BTC&expiry=04-09-2026&interval=0.02"
    ) as socket:
        message = socket.receive_json()
    assert message["type"] in {"chain", "waiting"}, message


# --- the error table -------------------------------------------------------------


@pytest.mark.parametrize("body", [{}, {"recording": "off"}, {"recording": None}])
def test_a_body_that_does_not_say_what_to_do_is_422(client, body) -> None:
    """A default here would let a malformed body silently stop the day's capture."""
    assert client.post("/recording", json=body).status_code == 422


def test_a_process_with_no_writer_is_503_rather_than_a_default(clock) -> None:
    """`TestClient` not entered as a context manager: the lifespan never runs, so there
    is no writer. Answering `false` would tell a reader that recording is off and can be
    switched on, when neither is true."""
    from deltapayoff import main

    # `app.state` is module level and outlives this test, so whatever was there is put
    # back — a later test finding no writer would fail for a reason nobody would look
    # for here.
    previous = getattr(main.app.state, "writer", None)
    if previous is not None:
        del main.app.state.writer
    try:
        client = TestClient(main.app)
        assert client.get("/recording").status_code == 503
        assert client.post("/recording", json={"recording": True}).status_code == 503
    finally:
        if previous is not None:
            main.app.state.writer = previous


def sampled_chain(minute: int, second: int, iv: float = 0.43) -> ChainResponse:
    """A two-leg enriched chain stamped inside `minute`, as the recompute loop would
    have left it in the cache the writer samples."""
    stamp = datetime.fromtimestamp(
        (MINUTE_US + minute * MINUTE + second * 1_000_000) / 1e6, tz=timezone.utc
    )
    block = ComputedLeg(
        iv=iv, iv_leg="call", delta=0.5, gamma=0.0001, vega=31.4, theta=-8.2, rho=1.9
    )
    return ChainResponse(
        underlying="BTC",
        expiry="04-09-2026",
        spot=77568.2,
        atm_strike=77600.0,
        fetched_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        rows=[
            ChainRow(
                strike=77600.0,
                call=Leg(symbol=SYMBOL, computed=block),
                put=Leg(symbol=PAUSED_SYMBOL, computed=block),
            )
        ],
        forward=77590.4,
        discount=0.99997892,
        years_to_expiry=0.00114155,
        forward_method="F1+assumed-rate",
    )


def test_the_chain_cache_is_not_sampled_while_recording_is_off(
    client, clock: _Clock
) -> None:
    """Table C is **sampled** from the recompute loop's cache rather than folded off the
    bus, so refusing bus records is not enough to stop it — the sampler needs the same
    guard, on the shutdown path as well as in the loop.

    Without it, a paused engine would keep writing our implied volatility and Greeks for
    every minute of the pause: exactly the table the volatility screen reads, filled
    while the operator believed nothing was being captured.
    """
    from deltapayoff import main

    clock.chains[:] = [sampled_chain(0, 20)]
    clock.set(MINUTE_US / 1e6 + 25.0)
    time.sleep(LET_THE_LOOP_TURN)

    while_on = main.app.state.writer.stats()["computed"]["ticks"]
    assert while_on > 0, "the cache was not being sampled even while recording"

    client.post("/recording", json={"recording": False})
    time.sleep(LET_THE_LOOP_TURN)

    clock.chains[:] = [sampled_chain(1, 20)]
    clock.set((MINUTE_US + MINUTE) / 1e6 + 25.0)
    time.sleep(LET_THE_LOOP_TURN)

    assert main.app.state.writer.stats()["computed"]["ticks"] == while_on, (
        "a paused writer went on sampling the chain cache"
    )


@pytest.mark.parametrize("recording", [True, False])
def test_the_shutdown_sample_of_the_chain_cache_obeys_the_switch(
    clock: _Clock, tmp_path: Path, recording: bool
) -> None:
    """Stopping the process while paused must not write one last computed minute.

    `aclose` takes a **forced** sample of the chain cache on the way out — the open
    minute's computed state is a real observation and the ticket that built it requires
    it be kept. Forced means it ignores the ten-second timer, so it is the one sampling
    path the loop's own pause check never reaches, and it needs the switch of its own.

    Both arms are here because only the pair says anything: without the `True` arm this
    would pass on a writer that had stopped sampling altogether.
    """
    from deltapayoff import main

    with TestClient(main.app) as client:
        if not recording:
            client.post("/recording", json={"recording": False})
            time.sleep(LET_THE_LOOP_TURN)
        clock.chains[:] = [sampled_chain(0, 20)]
        clock.set(MINUTE_US / 1e6 + 25.0)
        time.sleep(LET_THE_LOOP_TURN)

    rows = (
        BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
        .scan()
        .collect()
        .height
    )
    if recording:
        assert rows == 2, "the open minute's computed state was lost at shutdown"
    else:
        assert rows == 0, "a paused writer wrote a computed row on the way out"


def test_the_open_minute_is_held_through_the_pause_rather_than_split_across_it(
    client, clock: _Clock, tmp_path: Path
) -> None:
    """A minute still open when the switch is thrown is not written twice.

    The contract flushes what is **sealed** and deliberately leaves the partial bar in
    the aggregator. Sealing it at the pause and again on resume would put two rows in the
    store for one `(symbol, minute)` — a duplicate every reader downstream would have to
    know about — so the pause stops the sealing as well as the aggregating, and the bar
    lands once, on the resume, with its true tick counts.
    """
    from deltapayoff import main

    main.app.state.fanout.publish(quote(SYMBOL, MINUTE_US + 1_000, bid=64.0))
    # Half a minute in: minute 09:00 is still open, eight seconds of grace away from
    # sealing even once it closes.
    clock.set(MINUTE_US / 1e6 + 30.0)
    time.sleep(LET_THE_LOOP_TURN)

    paused = client.post("/recording", json={"recording": False}).json()
    assert paused["buffered_rows"] == 0 and paused["rows_written"] == 0

    # Two minutes pass with recording off. The bar is now long past its grace.
    clock.set((MINUTE_US + 2 * MINUTE) / 1e6 + 20.0)
    time.sleep(LET_THE_LOOP_TURN)

    while_off = client.get("/recording").json()
    assert while_off["buffered_rows"] == 0, "a paused writer sealed the open minute"
    assert while_off["rows_written"] == 0, "a paused writer wrote the open minute"

    client.post("/recording", json={"recording": True})
    time.sleep(LET_THE_LOOP_TURN)
    assert client.get("/recording").json()["buffered_rows"] == 1, (
        "the held minute was not sealed once recording resumed"
    )

    client.post("/recording", json={"recording": False})
    frame = BarStore(tmp_path).scan().collect()
    assert frame.height == 1, "one minute, one row"
    assert frame["bid_close"].to_list() == [64.0]
    assert frame["bid_ticks"].to_list() == [1], "the bar lost its true tick count"


def test_the_cors_preflight_allows_a_post_from_the_dev_server(client) -> None:
    """The trap this route walks into, pinned.

    Every other route here is a `GET` or a websocket, and `allow_methods` said `["GET"]`.
    A `POST` from the page against that allowance is refused at the **preflight**, before
    the request is ever made — which reaches the browser as a network error
    indistinguishable from the engine being down. The one thing it does not look like is
    a CORS rule, which is what makes it worth a test rather than a comment.
    """
    response = client.options(
        "/recording",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "POST" in response.headers["access-control-allow-methods"]
