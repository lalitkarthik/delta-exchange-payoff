"""Re-listing: the contracts a venue lists **after** the engine started. Issue #51.

`start_feed_stack` listed instruments once and nothing ever asked again. The reconnect
path replays the registry, which is deliberately never cleared, so it re-subscribed the
same set for the life of the process — and every strike Delta listed afterwards was never
subscribed, never stored, and absent from every historical screen. The live path went on
answering `/chain` from a fresh REST read, so nothing looked wrong until a stored chart
was drawn six hours later.

**What makes a test of this easy to write vacuously.** The registry is a set an assertion
can find a symbol in whether or not anything was ever subscribed on a socket, and every
existing app-level store test publishes decoded frames straight onto the bus, which
bypasses subscription altogether. Both would pass with the fix deleted. So the socket here
is a real one — `DeltaFeed` over a scripted connection — and it **delivers only what has
been subscribed on it**, which is what a venue does. The frames below name a contract that
appears only in the *second* listing, so with the re-listing removed the socket is never
told about it, nothing is delivered, and all four tables stay empty.

Seam 1 of #33's testing decisions, with seams 2, 3 and 5 underneath it: the FastAPI app
under `TestClient`, the real adapter and socket owner over a scripted connection, the
venue's own frames, and a `BarStore` on a `tmp_path`. No network — `conftest.py` refuses
the real client and `_ListingClient` stands in its place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import polars as pl
import pytest

from deltapayoff import log_events, main
from deltapayoff import stream as stream_module
from deltapayoff.adapters import DeltaAdapter, DeltaFeed, instrument_from_symbol
from deltapayoff.controller import ConnectionController
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
)
from wait_helpers import wait_until, wait_until_sync

#: Listed when the engine starts.
EARLY = "C-BTC-77600-040926"
#: **Listed later**, and the whole subject of this file. #51's own evidence is a strike
#: that appeared six and a half hours late in the store while sitting between two strikes
#: recorded from midnight — a venue does not list two and skip the one between them.
LATE = "C-BTC-77800-040926"


# --- the doubles ------------------------------------------------------------------


class _ListingClient:
    """`DeltaClient`, answering `/v2/tickers` with one more contract after the first ask.

    A venue that lists a new strike as spot moves, compressed into two answers.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.calls = 0

    async def __aenter__(self) -> _ListingClient:
        return self

    async def tickers(self, underlying: str, expiry=None) -> list[dict]:
        self.calls += 1
        symbols = [EARLY] if self.calls == 1 else [EARLY, LATE]
        return [{"symbol": symbol} for symbol in symbols]

    async def aclose(self) -> None:
        return None


class SubscribedOnlySocket:
    """A connection that delivers **only what has been subscribed on it**.

    The one thing that makes every assertion in this file able to fail. A fake that
    yielded its script regardless of the subscribe frames it was sent would prove that
    frames reach the four tables — which was never in doubt — rather than that the
    contract they name was ever subscribed, which is the entire bug.
    """

    def __init__(self, frames: list[dict]) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        #: Symbols this connection has been told about, from its own subscribe frames.
        self.subscribed: set[str] = set()
        self.delivered: list[str] = []
        self.dropped = False

    def drop(self) -> None:
        """Pull the cable: the next read raises, as a real dropped connection does."""
        self.dropped = True

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") != "subscribe":
            return
        for entry in message["payload"]["channels"]:
            self.subscribed.update(entry["symbols"])

    async def recv(self) -> str:
        while True:
            if self.dropped:
                raise ConnectionResetError("scripted drop")
            for index, frame in enumerate(self.frames):
                if frame["sy"] in self.subscribed:
                    del self.frames[index]
                    self.delivered.append(frame["sy"])
                    return json.dumps(frame)
            # #93 triage: fake cadence, not a bet -- already a condition poll (the
            # `while True` above), not a fixed-duration wait; 0.005s is only the
            # backoff between checks of `self.subscribed`.
            await asyncio.sleep(0.005)

    async def ping(self):
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    async def __aenter__(self) -> SubscribedOnlySocket:
        return self

    async def __aexit__(self, *_) -> bool:
        return False


def book_frame(symbol: str, exchange_us: int) -> dict:
    """An `ob_l2` payload shaped as the venue sends one. Becomes table A."""
    return {
        "type": "ob_l2",
        "sy": symbol,
        "ts": exchange_us,
        "lts": exchange_us - 300_000,
        "b": [["70.0", "10"]],
        "a": [["72.0", "10"]],
    }


def ticker_frame(symbol: str, exchange_us: int) -> dict:
    """A `ticker` payload shaped as the venue sends one.

    Becomes table B from its own fields, table D from `sp`, and — once the minute pass
    solves the ladder it lands in — table C. Written out in full for the reason
    `test_store.py` gives: the array layout is the thing under test.
    """
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": "77651.9",
        "ts": exchange_us,
        "d": [
            {
                "g": [
                    "-0.73938982",
                    "0.00024511",
                    "-1.70380880",
                    "-202.29182089",
                    "13.60933495",
                ],
                "i": 148290,
                "m": "1059.85780065",
                "m24hc": "-48.3655",
                "ohlc": [2051.0, 2243.0, 750.0, 1082.0],
                "oi": ["1997", "74608.2000"],
                "pb": ["0.1", "2514.52568587"],
                "q": ["1080", "5425", "1066", "8096", None],
                "qiv": ["0.32129313", "0.3110054", "0.31623765"],
                "s": symbol,
                "v": 74608.2,
            }
        ],
    }


def wire_the_app(monkeypatch, tmp_path: Path, socket, relist_interval: float = 0.05):
    """The whole engine, with a scripted connection where the network would be.

    Everything is looked up in `main`'s globals at call time, which is what
    `build_feed_stack` documents this seam for. Only three things are doubles: the REST
    client, the connection, and the store's root. The adapter, the socket owner, the
    controller, the supervisor, the bus, the chain cache, the writer and every one of the
    five background tasks are the code the process runs.
    """
    monkeypatch.setenv("DELTA_LIVE_FEED", "1")
    # BTC alone, so the counts below are one underlying's and the re-list's two REST
    # reads per cycle do not have to be told apart. The default set is BTC and ETH.
    monkeypatch.setenv("DELTA_LIVE_UNDERLYINGS", "BTC")
    monkeypatch.setattr(main, "DeltaClient", _ListingClient)
    monkeypatch.setattr(
        main,
        "DeltaFeed",
        lambda sink, **kwargs: DeltaFeed(sink, connect=lambda _url: socket, **kwargs),
    )
    monkeypatch.setattr(main, "BarStore", lambda *a, **k: BarStore(tmp_path))

    real_relist = main.relist_forever
    monkeypatch.setattr(
        main, "relist_forever", lambda stack: real_relist(stack, interval=relist_interval)
    )
    # The minute pass on a 0.2 s cadence rather than its real minute, so table C is
    # filled inside the test instead of a minute after it. `test_store.py` does the same.
    real_pass = stream_module.recompute_every_minute
    monkeypatch.setattr(
        main,
        "recompute_every_minute",
        lambda chain_stream, sink: real_pass(chain_stream, sink, interval=0.2, lead=0.0),
    )


def stored(tmp_path: Path) -> dict[str, pl.DataFrame]:
    """The four tables, read back off disk."""
    return {
        "quote": BarStore(tmp_path).scan().collect(),
        "reference": BarStore(
            tmp_path, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA
        )
        .scan()
        .collect(),
        "spot": BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA)
        .scan()
        .collect(),
        "computed": BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
        .scan()
        .collect(),
    }


# --- the acceptance test ----------------------------------------------------------


def test_a_contract_listed_after_start_up_reaches_all_four_tables(
    monkeypatch, tmp_path: Path
) -> None:
    """**#51's acceptance line, end to end.**

    The only frames on the wire name a contract that does not exist in the first listing.
    Nothing subscribes it at start-up, and the socket delivers nothing that has not been
    subscribed on it — so every row asserted below exists only because the re-listing ran,
    found it, and told a connection that was already open.

    Revert `relist_forever` out of `start_feed_stack` and all four assertions fail, which
    is the point: this suite has produced four separate batches of tests that asserted
    things guaranteed by construction, and "the registry contains X" is the easiest of
    them to write vacuously.
    """
    from fastapi.testclient import TestClient

    now_us = int(time.time() * 1e6)
    socket = SubscribedOnlySocket(
        [book_frame(LATE, now_us), ticker_frame(LATE, now_us + 1_000)]
    )
    wire_the_app(monkeypatch, tmp_path, socket, relist_interval=0.25)

    with TestClient(main.app) as client:
        # **First, before anything else in this block.** These two are pre-conditions and
        # the re-list is already ticking, so every millisecond spent here — an HTTP round
        # trip most of all — is a millisecond in which they can stop being true on a
        # loaded machine, and a failure of a pre-condition reads as a failure of the
        # thing under test.
        assert main.app.state.stack.listed == {"BTC": {EARLY}}
        assert LATE not in socket.subscribed
        assert client.get("/health").status_code == 200

        # #93 triage: bet -- this used to be a fixed time.sleep(2.0) guessing at
        # "several re-list ticks and a shortened minute pass or two". Wait on the
        # three things the assertions below and the table check after the `with`
        # block actually need: the late contract subscribed, a frame delivered for
        # it, and the recompute pass having folded it into a solved ladder (which is
        # what the shutdown-forced sample sees at the very end of this block).
        def late_contract_is_ready() -> bool:
            if LATE not in socket.subscribed or not socket.delivered:
                return False
            # `computed_chains()`, not `live_computed_chains()`: nothing here opens a
            # `/ws/chain` connection, so this expiry is never in the *watched* set the
            # narrower method filters to -- it is only ever solved by the whole-board
            # minute pass, which is what `computed_chains()` reads back.
            chains = main.app.state.stream.computed_chains()
            return any(
                leg is not None and leg.symbol == LATE
                for chain in chains
                for row in chain.rows
                for leg in (row.call, row.put)
            )

        wait_until_sync(
            late_contract_is_ready,
            timeout=10.0,
            message="the late listing was never subscribed, delivered, and computed",
        )

        assert LATE in socket.subscribed, "the late listing never reached the open socket"
        assert socket.delivered, "the venue delivered nothing for it"

    tables = stored(tmp_path)
    for name in ("quote", "reference", "computed"):
        assert tables[name].height >= 1, f"table {name} has no row for a late listing"
        assert set(tables[name]["symbol"].to_list()) == {LATE}
    # Table D is keyed by underlying rather than by contract, and the only ticker frame
    # that ever carried a spot was this contract's — so a spot row exists only because
    # the late listing was subscribed.
    assert tables["spot"].height >= 1, "table spot has no row for a late listing"
    assert set(tables["spot"]["underlying"].to_list()) == {"BTC"}


def test_the_late_contract_is_registered_and_survives_a_reconnect(
    monkeypatch, tmp_path: Path
) -> None:
    """The addition joins the replay, so a drop does not undo it.

    A live subscribe that reached the venue but not the registry would work until the
    first reconnect and then stop, silently — #51 again, one connection later. The
    registry is asserted here as *what a reconnect would replay*; that a reconnect
    actually replays it is `test_a_relisted_contract_is_replayed_on_the_redial` below.
    """
    from fastapi.testclient import TestClient

    now_us = int(time.time() * 1e6)
    socket = SubscribedOnlySocket([book_frame(LATE, now_us)])
    wire_the_app(monkeypatch, tmp_path, socket)

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        # #93 triage: bet -- this used to be a fixed time.sleep(0.5) guessing how many
        # of the default 0.05s re-list ticks would land inside it. Wait on the
        # registry itself, which is what every assertion below reads.
        wait_until_sync(
            lambda: main.app.state.stack.listed.get("BTC") == {EARLY, LATE},
            timeout=5.0,
            message="the late listing was never added to the registry",
        )
        registry = main.app.state.feed.registry

        assert registry["ticker"] == {EARLY, LATE}
        assert registry["ob_l2"] == {EARLY, LATE}
        assert main.app.state.stack.listed == {"BTC": {EARLY, LATE}}


def test_a_relisted_contract_is_replayed_on_the_redial() -> None:
    """The reconnect itself, driven through the real dial loop rather than a double.

    A contract subscribed onto a live socket is in the registry, and the registry is what
    the next open replays — so the second connection is sent both contracts in one frame,
    without anything having re-listed for it. The controller does the redialling, because
    a reconnect asserted only against a fake that reconnects itself proves nothing about
    the code that dials.
    """
    sockets = [SubscribedOnlySocket([]), SubscribedOnlySocket([])]
    handed: list[SubscribedOnlySocket] = []

    def connect(_url):
        socket = sockets[len(handed)] if len(handed) < len(sockets) else sockets[-1]
        handed.append(socket)
        return socket

    adapter = DeltaAdapter(
        feed_factory=lambda sink, **kw: DeltaFeed(sink, connect=connect, **kw)
    )
    early = instrument_from_symbol(EARLY)
    late = instrument_from_symbol(LATE)
    assert early is not None and late is not None
    adapter.subscribe([early])

    async def scenario() -> None:
        controller = ConnectionController(
            adapter, lambda _event: None, retry_delay=0.01, heartbeat_every=1_000.0
        )
        task = asyncio.create_task(controller.run())
        # #93 triage: three bets -- each used to be a fixed sleep guessing how much of
        # the controller's connect/subscribe/redial state machine would run inside it.
        # Everything here is mocked and `retry_delay` is 0.01s, but the controller
        # still runs as a real asyncio task the event loop has to get around to
        # scheduling, so wait on what each next step actually depends on instead.
        await wait_until(
            lambda: EARLY in sockets[0].subscribed,
            timeout=2.0,
            poll=0.005,
            message="the early contract was never subscribed on the first connection",
        )
        adapter.subscribe([late])
        await wait_until(
            lambda: LATE in sockets[0].subscribed,
            timeout=2.0,
            poll=0.005,
            message="the late contract was never subscribed on the live connection",
        )
        # The connection drops. The controller redials and the fresh socket is replayed.
        sockets[0].drop()
        await wait_until(
            lambda: len(handed) == 2 and sockets[1].subscribed == {EARLY, LATE},
            timeout=2.0,
            poll=0.005,
            message="the redial never replayed both contracts onto a second connection",
        )
        controller.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert adapter.feed.registry["ticker"] == {EARLY, LATE}
    assert sockets[0].subscribed == {EARLY, LATE}, "the live addition never went out"
    assert len(handed) == 2, "the drop was not redialled, so nothing replayed"
    assert sockets[1].subscribed == {EARLY, LATE}, (
        "the replay lost the contract that arrived after the first open — a subset "
        "replay is the healthy-connection-zero-messages failure with no error"
    )


# --- what it says out loud --------------------------------------------------------


def test_a_new_listing_is_logged_with_its_count_and_underlying(
    monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """**#51's third acceptance line.** The failure was invisible: it had to be inferred
    from a broken chart six hours later, because nothing anywhere said "I subscribed 20
    contracts" or "I subscribed 4 more". Both sentences now exist, at info, with the
    underlying, how many were new and how many are subscribed in total."""
    from fastapi.testclient import TestClient

    socket = SubscribedOnlySocket([])
    wire_the_app(monkeypatch, tmp_path, socket)

    def listing_records() -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if getattr(record, "event", None) == log_events.FEED_INSTRUMENTS
        ]

    with caplog.at_level(logging.INFO, logger="deltapayoff.main"):
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
            # #93 triage: bet -- this used to be a fixed time.sleep(0.4) guessing how
            # many of the default 0.05s re-list ticks it covered. Wait on the second
            # log record itself, which is what every assertion below reads.
            wait_until_sync(
                lambda: len(listing_records()) >= 2,
                timeout=5.0,
                message="the late listing was never logged",
            )

    records = listing_records()
    assert len(records) == 2, "start-up and the late listing are two separate records"
    assert [record.listed for record in records] == [1, 1]
    assert [record.subscribed for record in records] == [1, 2]
    assert {record.underlying for record in records} == {"BTC"}
    assert {record.venue for record in records} == {"DELTA"}


def test_a_listing_that_cannot_be_read_warns_and_leaves_the_feed_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**#51's failure mode.** The venue's REST endpoint is a different service from its
    websocket and fails separately. A failed re-list is a warning and a retry next cycle,
    never an outage: the loop must outlive it, because a loop that dies leaves the engine
    recording the set it started with and saying nothing — the exact failure this task
    was written to end."""

    class _Unavailable:
        underlyings = ("BTC",)
        venue = "DELTA"

        def __init__(self) -> None:
            self.asks = 0

        async def instruments(self, underlying: str):
            self.asks += 1
            raise TimeoutError("Delta timed out after 10s")

        def subscribe(self, instruments) -> None:  # pragma: no cover - never reached
            raise AssertionError("nothing was listed, so nothing may be subscribed")

    adapter = _Unavailable()
    stack = main.FeedStack(
        events=None, stream=None, writer=None, adapter=adapter,
        supervisor=None, feed_cache=None,
    )

    async def scenario() -> None:
        task = asyncio.create_task(main.relist_forever(stack, interval=0.01))
        # #93 triage: bet -- this used to be a fixed time.sleep(0.1) guessing how many
        # of the 0.01s retry ticks it covered. Wait on the retry count itself, which is
        # what the assertion below reads.
        await wait_until(
            lambda: adapter.asks >= 3,
            timeout=6.0,
            poll=0.005,
            message="the re-list loop never retried after its first failed read",
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.WARNING, logger="deltapayoff.main"):
        asyncio.run(scenario())

    assert adapter.asks >= 3, "the loop gave up after the first failure"
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == log_events.FEED_INSTRUMENTS
    ]
    assert warnings, "a re-list that failed said nothing"
    assert all(record.levelno == logging.WARNING for record in warnings)
    assert stack.listed == {}, "a failed listing must not record contracts as subscribed"


def test_an_unanswerable_venue_at_start_up_is_still_fatal() -> None:
    """The other half of the same decision, and why they cannot be one call site.

    **Driven through `start_feed_stack`, not through `relist_instruments`**, because the
    claim is about what does *not* get started: `relist_instruments` contains no exception
    handling at all, so asserting that it propagates would pin nothing but the absence of
    a `try` nobody wrote. What matters is that the supervisor was never started and no
    background task exists — a feed that connected with an empty registry is the silent
    failure the socket owner exists to prevent.

    The periodic path deliberately swallows the same error. That is why the first listing
    is a direct call and not the loop's first tick: one call site cannot be both fatal and
    forgiving.
    """

    class _Unavailable:
        underlyings = ("BTC",)
        venue = "DELTA"

        async def instruments(self, underlying: str):
            raise TimeoutError("Delta timed out after 10s")

        def subscribe(self, instruments) -> None:  # pragma: no cover - never reached
            raise AssertionError("nothing was listed, so nothing may be subscribed")

    class _Supervisor:
        def __init__(self) -> None:
            self.started = False

        def start(self) -> None:
            self.started = True

    class _Runnable:
        """A stream and a writer that would happily start, so that the assertions below
        are what fails if start-up ever stops being fatal — not an `AttributeError` from
        a double too thin to get that far."""

        async def run(self) -> None:
            await asyncio.Event().wait()

        def sample_chains(self, chains) -> None:  # pragma: no cover - never called
            return None

    supervisor = _Supervisor()
    runnable = _Runnable()
    stack = main.FeedStack(
        events=None, stream=runnable, writer=runnable, adapter=_Unavailable(),
        supervisor=supervisor, feed_cache=None,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(main.start_feed_stack(stack))

    assert not supervisor.started, "the feed was connected with an empty registry"
    assert stack.tasks == [], "background tasks were started over an unlisted venue"
    assert stack.listed == {}
