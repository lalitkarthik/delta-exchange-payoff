"""The socket owner: subscribe, heartbeat, reconnect, resubscribe. No network.

The dangerous behaviour here is not connecting — it is **reconnecting**. A reconnected
socket is a fresh, empty socket and Delta has forgotten every subscription. Fail to send
them again and you get a healthy connection, zero messages, and no error anywhere: the
screen simply stops updating. So the registry is never cleared and is replayed in full on
every open, and that is what most of these tests are about.

A fake connection stands in for the network. It records what was sent, yields scripted
frames, and can close on demand — which makes "pull the cable" an assertion rather than a
manual exercise.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from deltapayoff.fanout import FanOut
from deltapayoff.feed import DeltaFeed

CHAIN = ["C-BTC-77600-040926", "P-BTC-77600-040926"]


class FakeSocket:
    """One connection's worth of scripted behaviour.

    `script` is a list of frames to yield. `close_after` makes the connection drop once
    that many frames have been read, which is how a network failure is simulated.
    """

    def __init__(self, script, close_after=None):
        self.script = list(script)
        self.close_after = close_after
        self.sent: list[dict] = []
        self.pings = 0
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.close_after is not None and len(self.script) <= self.close_after:
            self.closed = True
            raise ConnectionResetError("scripted drop")
        if not self.script:
            await asyncio.sleep(3600)  # idle; the test cancels
        return json.dumps(self.script.pop(0))

    async def ping(self):
        # Real `websockets.ping()` returns a future that resolves when the pong arrives.
        # An already-done future mirrors that without leaving an un-awaited coroutine.
        self.pings += 1
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True
        return False


def connector(sockets):
    """A connect function handing out the given fakes in order."""
    remaining = list(sockets)

    def connect(url):
        return remaining.pop(0) if remaining else FakeSocket([])

    return connect


def ticker_frame(symbol, bid, ask):
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": "77651.9",
        "ts": 1788430765832299,
        "d": [
            {
                "s": symbol,
                "i": 1,
                "m": "580.6",
                "q": [str(ask), "100", str(bid), "200", None],
                "qiv": ["0.31", "0.29", "0.30"],
                "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
                "oi": ["100", "200"],
            }
        ],
    }


def book_frame(symbol, bid, ask):
    return {
        "type": "ob_l2",
        "sy": symbol,
        "ts": 1,
        "lts": 1,
        "a": [[str(ask), "10"]],
        "b": [[str(bid), "10"]],
    }


async def drive(feed, seconds=0.2):
    task = asyncio.create_task(feed.run())
    await asyncio.sleep(seconds)
    feed.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- subscribing -----------------------------------------------------------------


def test_the_registry_is_sent_on_open() -> None:
    """Both channels, in one message each. Step 1 measured that Delta accepts at least
    300 symbols per subscribe message on both, so a whole chain needs no batching."""
    socket = FakeSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)
    feed.subscribe("ob_l2", CHAIN)

    asyncio.run(drive(feed))

    channels = [
        entry
        for message in socket.sent
        if message.get("type") == "subscribe"
        for entry in message["payload"]["channels"]
    ]
    assert {c["name"] for c in channels} == {"ticker", "ob_l2"}
    for channel in channels:
        assert sorted(channel["symbols"]) == sorted(CHAIN)


def test_symbols_registered_before_connecting_are_not_lost() -> None:
    """Subscriptions are accepted before the socket exists and sent once it opens.
    Otherwise start-up ordering becomes a race the caller has to know about."""
    socket = FakeSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ob_l2", ["C-BTC-77600-040926"])
    feed.subscribe("ob_l2", ["P-BTC-77600-040926"])

    asyncio.run(drive(feed))

    sent = [m for m in socket.sent if m.get("type") == "subscribe"]
    symbols = sent[0]["payload"]["channels"][0]["symbols"]
    assert sorted(symbols) == sorted(CHAIN)


# --- reconnecting ----------------------------------------------------------------


def test_a_dropped_connection_resubscribes_everything() -> None:
    """The failure that produces no error: reconnect, receive nothing, notice hours later.

    The first socket drops after its scripted frames. The second must be sent the same
    complete registry — not a subset, and not nothing.
    """
    first = FakeSocket([ticker_frame(CHAIN[0], 579, 584)], close_after=0)
    second = FakeSocket([])
    feed = DeltaFeed(
        FanOut(), connect=connector([first, second]), retry_delay=0.01
    )
    feed.subscribe("ticker", CHAIN)
    feed.subscribe("ob_l2", CHAIN)

    asyncio.run(drive(feed, seconds=0.3))

    resent = [
        entry
        for message in second.sent
        if message.get("type") == "subscribe"
        for entry in message["payload"]["channels"]
    ]
    assert {c["name"] for c in resent} == {"ticker", "ob_l2"}
    for channel in resent:
        assert sorted(channel["symbols"]) == sorted(CHAIN)


def test_the_registry_survives_the_drop_that_caused_the_reconnect() -> None:
    """Keyed per symbol, never cleared. OpenAlgo's comment explains why per symbol and
    not per message: a message-keyed registry replays a whole batch when one symbol in
    it is rejected."""
    first = FakeSocket([], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([first]), retry_delay=0.01)
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed, seconds=0.2))

    assert feed.registry["ticker"] == set(CHAIN)


def test_the_retry_budget_resets_after_a_healthy_connection() -> None:
    """A cumulative retry counter looks correct and dies after a month.

    OpenAlgo records the bug in a comment: a long-lived feed that reconnects once a day
    silently exhausts a lifetime budget and never comes back. A connection that came up
    and delivered data has proved the endpoint works, so the budget is restored.
    """
    healthy = FakeSocket([ticker_frame(CHAIN[0], 579, 584)], close_after=0)
    feed = DeltaFeed(
        FanOut(), connect=connector([healthy]), retry_delay=0.01, max_retries=2
    )
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed, seconds=0.2))

    assert feed.connections >= 2
    assert feed.consecutive_failures == 0


# --- heartbeats ------------------------------------------------------------------


def test_heartbeats_are_sent_on_the_interval() -> None:
    """A quiet connection and a dead one look identical over TCP.

    Delta's documented 60 s idle disconnect did not reproduce in a 75 s test on this
    project, so it is treated as unverified and heartbeats are sent regardless. 30 s is
    OpenAlgo's interval; the test uses 10 ms to keep the suite fast.
    """
    socket = FakeSocket([])
    feed = DeltaFeed(
        FanOut(), connect=connector([socket]), heartbeat_seconds=0.01
    )
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed, seconds=0.15))

    assert socket.pings >= 3


# --- what comes out --------------------------------------------------------------


def test_decoded_records_reach_the_fan_out() -> None:
    """The feed publishes decoded `Leg`s, not raw frames. A consumer should never have
    to know that `q[2]` is the bid."""
    bus = FanOut()
    subscription = bus.subscribe("test", maxsize=100)
    socket = FakeSocket(
        [ticker_frame(CHAIN[0], 579, 584), book_frame(CHAIN[1], 120, 125)]
    )
    feed = DeltaFeed(bus, connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed))

    records = []
    while not subscription.queue.empty():
        records.append(subscription.queue.get_nowait())

    assert len(records) == 2
    by_symbol = {r.symbol: r for r in records}
    assert by_symbol[CHAIN[0]].channel == "ticker"
    assert by_symbol[CHAIN[0]].bid == 579.0
    assert by_symbol[CHAIN[0]].ask == 584.0
    assert by_symbol[CHAIN[1]].channel == "ob_l2"
    assert by_symbol[CHAIN[1]].bid == 120.0


def test_a_malformed_frame_does_not_kill_the_feed() -> None:
    """One bad message must not end ingestion.

    This is break 3 from the design: an exception raised while decoding unwinds through
    the read loop and takes the socket with it. The frame is counted and skipped.
    """
    bus = FanOut()
    subscription = bus.subscribe("test", maxsize=100)
    socket = FakeSocket(
        [
            {"type": "ticker", "sy": "C-BTC-1-010126", "d": "not a list"},
            ticker_frame(CHAIN[0], 579, 584),
        ]
    )
    feed = DeltaFeed(bus, connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed))

    assert feed.malformed == 1
    assert subscription.queue.qsize() == 1


def test_subscription_acknowledgements_are_not_published_as_data() -> None:
    """Delta replies to a subscribe with a `subscriptions` message. It is control
    traffic; publishing it as a quote would put a record with no prices on the bus."""
    bus = FanOut()
    subscription = bus.subscribe("test", maxsize=100)
    socket = FakeSocket(
        [
            {"type": "subscriptions", "channels": [{"name": "ticker", "symbols": CHAIN}]},
            ticker_frame(CHAIN[0], 579, 584),
        ]
    )
    feed = DeltaFeed(bus, connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed))

    assert subscription.queue.qsize() == 1


def test_the_feed_counts_what_it_saw() -> None:
    """#3 asks for measured throughput. The counters are where that comes from."""
    socket = FakeSocket([ticker_frame(CHAIN[0], 579, 584) for _ in range(5)])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed))

    assert feed.messages == 5
    assert feed.bytes_read > 0


def test_an_unknown_channel_is_refused_before_the_socket_opens() -> None:
    """Delta retired `v2/ticker`, `l1_orderbook` and `l2_orderbook` on 31 July 2026 and
    now rejects them as invalid. Catching that here turns a silent empty stream into an
    error at the call site."""
    feed = DeltaFeed(FanOut())

    with pytest.raises(ValueError):
        feed.subscribe("v2/ticker", CHAIN)


def test_a_connection_that_dies_before_delivering_anything_is_a_failure() -> None:
    """A budget that always resets is as broken as one that never does.

    Delta can accept the handshake and close straight away — a rejected subscribe, a
    throttled IP, an endpoint draining. If merely opening a socket counted as healthy,
    the retry budget would reset on every pass and `run()` would reconnect forever:
    **measured at 21 attempts in 0.3 s with `max_retries=3`** before this was fixed. At
    the production one-second delay that exhausts the 150-connections-per-5-minutes
    budget in about two and a half minutes and keeps hammering.

    So a connection counts as healthy only once it has delivered a message. That is what
    the module docstring always claimed and what the code did not do.
    """
    attempts = 0

    class DeadOnArrival:
        async def send(self, raw):
            pass

        async def recv(self):
            raise ConnectionResetError("closed straight away")

        async def ping(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    def connect(url):
        nonlocal attempts
        attempts += 1
        return DeadOnArrival()

    async def scenario():
        feed = DeltaFeed(
            FanOut(), connect=connect, retry_delay=0.001, max_retries=3
        )
        feed.subscribe("ticker", CHAIN)
        await asyncio.wait_for(feed.run(), timeout=2.0)
        return feed

    feed = asyncio.run(scenario())

    assert feed.consecutive_failures > feed.max_retries
    assert attempts <= feed.max_retries + 2, f"{attempts} attempts; it never gave up"


def test_the_reason_a_connection_ended_is_recorded() -> None:
    """A feed that silently reconnects forever is undiagnosable.

    Swallowing the exception leaves `messages` frozen, no counter moving and nothing in
    the logs — the operator's only symptom is a screen that stopped updating, which is
    precisely the silent failure the resubscribe logic exists to prevent.
    """

    class Broken:
        async def send(self, raw):
            raise RuntimeError("subscribe rejected")

        async def recv(self):
            raise ConnectionResetError

        async def ping(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    async def scenario():
        feed = DeltaFeed(
            FanOut(), connect=lambda url: Broken(), retry_delay=0.001, max_retries=1
        )
        feed.subscribe("ticker", CHAIN)
        await asyncio.wait_for(feed.run(), timeout=2.0)
        return feed

    feed = asyncio.run(scenario())

    assert feed.last_error is not None
    assert "subscribe rejected" in feed.last_error
