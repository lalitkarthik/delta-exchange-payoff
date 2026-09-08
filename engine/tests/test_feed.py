"""The socket owner: subscribe, heartbeat, reconnect, resubscribe. No network.

The dangerous behaviour here is not connecting — it is **reconnecting**. A reconnected
socket is a fresh, empty socket and Delta has forgotten every subscription. Fail to send
them again and you get a healthy connection, zero messages, and no error anywhere: the
screen simply stops updating. So the registry is never cleared and is replayed in full on
every open, and that is what most of these tests are about.

A fake connection stands in for the network. It records what was sent, yields scripted
frames, and can close on demand — which makes "pull the cable" an assertion rather than a
manual exercise.

**Since #36 the socket owner decodes nothing.** It publishes the frame verbatim and the
adapter turns it into canonical events, so the last two sections here are frames in,
events out: a scripted connection at one end and `md.option_quote` at the other.

**Since #37 it lives inside `adapters/`**, because `ticker` and `ob_l2` are Delta's words
and the module that subscribes by them belongs in the package that owns the venue.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from deltapayoff.adapters import DeltaAdapter
from deltapayoff.adapters.delta_socket import DeltaFeed
from deltapayoff.fanout import FanOut

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


async def subscribe_while_connected(feed, channel, symbols, seconds=0.1, stop=True):
    """Open the socket, subscribe `symbols` once it is up, then end the connection.

    The sleep either side is what makes this a *live* subscribe rather than another
    registration before the open: the first lets `_pump` reach its read, the second lets
    the send this triggers actually run before the connection is torn down.

    `stop=False` ends the attempt without setting the stop flag, because `stop()` is
    deliberately permanent — a stopped feed stays stopped — and a test that wants a
    second connection out of the same feed must not have asked for the first to be the
    last.
    """
    task = asyncio.create_task(feed.run())
    await asyncio.sleep(seconds)
    feed.subscribe(channel, symbols)
    await asyncio.sleep(seconds)
    if stop:
        feed.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def subscribes(socket):
    """Every subscribe message sent, one dict of channel to symbols per message."""
    return [
        {
            entry["name"]: sorted(entry["symbols"])
            for entry in message["payload"]["channels"]
        }
        for message in socket.sent
        if message.get("type") == "subscribe"
    ]


def test_a_symbol_registered_while_the_socket_is_open_is_sent_to_the_venue() -> None:
    """**Issue #51's other half.** The registry is not the subscription.

    `subscribe` used to only accumulate: the frame that tells Delta about a symbol was
    sent in `_pump`, once, at open. So a contract listed after the socket came up sat in
    the registry unsubscribed until the next reconnect — and a healthy feed does not
    reconnect. Re-listing on a cadence fixes nothing without this.

    Only the newly added symbols go out. Re-sending the whole registry would work too —
    Delta answers a subscribe with the book's current state — but it would answer for
    every already-subscribed contract as well, which on a 782-contract feed is a burst of
    snapshots to say nothing new.
    """
    socket = FakeSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(subscribe_while_connected(feed, "ticker", ["C-BTC-78000-040926"]))

    assert subscribes(socket) == [
        {"ticker": sorted(CHAIN)},
        {"ticker": ["C-BTC-78000-040926"]},
    ], "the newly listed contract never reached the open socket"


def test_a_symbol_already_in_the_registry_is_not_sent_again() -> None:
    """Re-listing hands the same contracts back every cycle. Only the difference is new.

    Without this the venue would be sent the whole chain every re-list, and every one of
    them would be answered with a fresh snapshot — a periodic burst on a feed whose whole
    design is that the socket reader is never made to wait.
    """
    socket = FakeSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(subscribe_while_connected(feed, "ticker", CHAIN))

    assert subscribes(socket) == [
        {"ticker": sorted(CHAIN)}
    ], "the same symbols went out twice"


def test_a_symbol_registered_with_no_socket_open_is_sent_on_the_next_open() -> None:
    """The pre-#51 behaviour, still intact: registering before the socket exists is safe
    and is not a send. This is the branch that must not reach for a socket that is not
    there — `subscribe` is called from `start_feed_stack` before anything dials."""
    socket = FakeSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)
    feed.subscribe("ticker", ["C-BTC-78000-040926"])

    assert socket.sent == []

    asyncio.run(drive(feed))

    assert subscribes(socket) == [{"ticker": sorted([*CHAIN, "C-BTC-78000-040926"])}]


def test_a_symbol_added_while_connected_is_replayed_on_the_next_open() -> None:
    """The addition joins the replay, which is what makes it survive a reconnect.

    A live subscribe that reached the venue but not the registry would work until the
    first drop and then silently stop — the same failure #51 is, one connection later.
    """
    first = FakeSocket([])
    second = FakeSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([first, second]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(
        subscribe_while_connected(feed, "ticker", ["C-BTC-78000-040926"], stop=False)
    )
    asyncio.run(drive(feed))

    assert subscribes(second) == [{"ticker": sorted([*CHAIN, "C-BTC-78000-040926"])}]


def test_a_live_subscribe_that_fails_leaves_the_feed_running_and_the_registry_intact(
    caplog,
) -> None:
    """A send that misses is a gap, not an outage.

    The symbols are in the registry before anything is sent, so the next open replays
    them whatever happened here; letting the exception out would instead end a connection
    that is otherwise delivering, which is a much worse trade than a delayed subscribe.
    """

    class RefusingSocket(FakeSocket):
        async def send(self, raw):
            message = json.loads(raw)
            if message["payload"]["channels"][0]["symbols"] == ["C-BTC-78000-040926"]:
                raise ConnectionResetError("scripted send failure")
            self.sent.append(message)

    socket = RefusingSocket([])
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    with caplog.at_level("WARNING", logger="deltapayoff.adapters.delta_socket"):
        asyncio.run(subscribe_while_connected(feed, "ticker", ["C-BTC-78000-040926"]))

    assert feed.registry["ticker"] == {*CHAIN, "C-BTC-78000-040926"}
    assert feed.last_error is None or "scripted send failure" not in feed.last_error
    assert any(
        "newly listed" in record.message for record in caplog.records
    ), "a subscribe that never reached the venue said nothing"


# --- reconnecting ----------------------------------------------------------------


def test_the_registry_is_replayed_on_every_open() -> None:
    """The failure that produces no error: reconnect, receive nothing, notice hours later.

    A reconnected socket is a fresh, empty socket and Delta has forgotten every
    subscription. This is the socket owner's half of the guard — **every connection it
    opens is sent the complete registry** — and it is now the whole of its half, because
    #39 moved the decision to open a second one to the controller. That the controller
    does open one, and that the replay it produces is complete, is
    `test_controller.py::test_a_dropped_connection_is_redialled_and_resubscribed`.
    """
    first = FakeSocket([ticker_frame(CHAIN[0], 579, 584)], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([first]))
    feed.subscribe("ticker", CHAIN)
    feed.subscribe("ob_l2", CHAIN)

    asyncio.run(drive(feed, seconds=0.3))

    sent = [
        entry
        for message in first.sent
        if message.get("type") == "subscribe"
        for entry in message["payload"]["channels"]
    ]
    assert {c["name"] for c in sent} == {"ticker", "ob_l2"}
    for channel in sent:
        assert sorted(channel["symbols"]) == sorted(CHAIN)


def test_the_registry_survives_the_drop_that_caused_the_reconnect() -> None:
    """Keyed per symbol, never cleared. OpenAlgo's comment explains why per symbol and
    not per message: a message-keyed registry replays a whole batch when one symbol in
    it is rejected."""
    first = FakeSocket([], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([first]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed, seconds=0.2))

    assert feed.registry["ticker"] == set(CHAIN)


# The lifetime reconnect budget, the backoff and the giving up moved to the controller
# in #39, and their tests moved with them: `test_controller.py`, section "the reconnect
# that moved here from the feed". What is left in this file is one connection's worth of
# behaviour, which is all this module does now.


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


def test_the_frame_reaches_the_sink_undecoded() -> None:
    """Since #36 the socket owner decodes nothing: it publishes the frame verbatim, its
    channel and the instant it arrived, and the adapter is the only thing that reads
    inside one. That is what makes "the wire layout lives behind the adapter" true of the
    code and not only of the diagram."""
    bus = FanOut()
    subscription = bus.subscribe("test", maxsize=100)
    ticker, book = ticker_frame(CHAIN[0], 579, 584), book_frame(CHAIN[1], 120, 125)
    feed = DeltaFeed(bus, connect=connector([FakeSocket([ticker, book])]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed))

    records = []
    while not subscription.queue.empty():
        records.append(subscription.queue.get_nowait())

    assert [record.channel for record in records] == ["ticker", "ob_l2"]
    assert [record.symbol for record in records] == [CHAIN[0], CHAIN[1]]
    assert [record.frame for record in records] == [ticker, book]
    assert all(record.received_at > 0 for record in records)


def test_frames_off_the_socket_become_canonical_events() -> None:
    """**Frames in, events out, through the real socket loop.**

    The socket owner and the adapter, wired as the running engine wires them: a scripted
    connection yields one frame on each channel, and what comes out the far end is
    canonical events carrying canonical instruments. Nothing downstream sees that `q[2]`
    was the bid, or that there were two channels at all.
    """
    published: list = []
    adapter = DeltaAdapter(feed_factory=_feed_factory(connector([
        FakeSocket([ticker_frame(CHAIN[0], 579, 584), book_frame(CHAIN[1], 120, 125)])
    ])))

    asyncio.run(_stream(adapter, published))

    assert [type(event).__name__ for event in published] == [
        "OptionReference",
        "IndexQuote",
        "OptionQuote",
    ]
    reference, index, quote = published
    assert reference.instrument.canonical() == "DELTA-BTC-20260904-77600-C"
    assert reference.mark_iv == 0.30
    assert index.underlying == "BTC" and index.spot == 77651.9
    assert quote.instrument.canonical() == "DELTA-BTC-20260904-77600-P"
    assert (quote.bid, quote.ask) == (120.0, 125.0)


def test_a_malformed_frame_does_not_kill_the_feed() -> None:
    """One bad message must not end ingestion.

    This is break 3 from the design: an exception raised while decoding unwinds through
    the read loop and takes the socket with it. The frame is counted and skipped.

    **The count moved with the decode.** `feed.malformed` now counts only what is not
    JSON at all; a frame that parses and then makes no sense is the adapter's
    `undecodable`, because the adapter is the only thing that looks inside one.
    """
    published: list = []
    adapter = DeltaAdapter(feed_factory=_feed_factory(connector([
        FakeSocket([
            {"type": "ticker", "sy": "C-BTC-1-010126", "d": "not a list"},
            book_frame(CHAIN[1], 120, 125),
        ])
    ])))

    asyncio.run(_stream(adapter, published))

    assert adapter.undecodable == 1
    assert adapter.feed.malformed == 0, "it was JSON; it just made no sense"
    assert len(published) == 1, "the frame after the bad one still arrived"


def _feed_factory(connect):
    """Build the real `DeltaFeed` over a scripted connection, for the adapter to own."""

    def factory(sink, **kwargs):
        return DeltaFeed(sink, connect=connect, **kwargs)

    return factory


async def _stream(adapter, published: list) -> None:
    task = asyncio.create_task(adapter.stream(published.append))
    await asyncio.sleep(0.2)
    adapter.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


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


def test_a_connection_that_dies_before_delivering_anything_says_so() -> None:
    """A budget that always resets is as broken as one that never does.

    Delta can accept the handshake and close straight away — a rejected subscribe, a
    throttled IP, an endpoint draining. If merely opening a socket counted as healthy,
    the retry budget would reset on every pass and the feed would reconnect forever:
    **measured at 21 attempts in 0.3 s with a budget of 3** before that was fixed.

    Since #39 the budget is the controller's, so what this module owes it is the *fact*
    it decides on: an attempt that opened and delivered nothing must leave `last_error`
    set, and one that delivered must clear it. Get that wrong and the controller
    restores a budget that was never earned, which is the same forever-reconnecting bug
    one layer up.
    """

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

    async def scenario():
        feed = DeltaFeed(FanOut(), connect=lambda url: DeadOnArrival())
        feed.subscribe("ticker", CHAIN)
        await asyncio.wait_for(feed.run(), timeout=2.0)
        return feed

    feed = asyncio.run(scenario())

    assert feed.connections == 1, "one call to run() is one connection since #39"
    assert feed.messages == 0
    assert feed.last_error is not None, (
        "an attempt that delivered nothing must not look like a healthy one"
    )


def test_a_connection_that_delivered_clears_the_error_that_would_spend_the_budget() -> (
    None
):
    """The other half. `last_error` back to `None` is how the controller is told this
    endpoint works, and it is what restores the lifetime budget."""
    socket = FakeSocket([ticker_frame(CHAIN[0], 579, 584)], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ticker", CHAIN)

    asyncio.run(drive(feed, seconds=0.2))

    assert feed.messages == 1
    assert feed.last_error is None


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
        feed = DeltaFeed(FanOut(), connect=lambda url: Broken())
        feed.subscribe("ticker", CHAIN)
        await asyncio.wait_for(feed.run(), timeout=2.0)
        return feed

    feed = asyncio.run(scenario())

    assert feed.last_error is not None
    assert "subscribe rejected" in feed.last_error


# --- reporting the connection (#38) ----------------------------------------------


def test_open_is_reported_only_after_the_registry_has_gone_out() -> None:
    """**The promise the signal makes.**

    A controller told "open" marks the connection `connected` and the badge goes green.
    If that arrived before the resubscribe, green would mean a fresh, empty socket
    carrying nothing — the failure with no error this module exists to prevent, wearing
    a healthy badge. So the assertion is not that the signal fires but *when*: the
    subscribe message is already on the wire.
    """
    socket = FakeSocket([book_frame("C-BTC-77600-040926", 120, 125)])
    sent_when_opened: list[int] = []
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ob_l2", ["C-BTC-77600-040926"])
    feed.on_open(lambda _detail: sent_when_opened.append(len(socket.sent)))

    asyncio.run(drive(feed))

    assert sent_when_opened == [1]
    assert socket.sent[0]["type"] == "subscribe"


def test_a_dropped_connection_is_reported_with_the_venues_words() -> None:
    closed: list[str] = []
    socket = FakeSocket([], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.on_close(closed.append)

    asyncio.run(drive(feed))

    assert closed
    assert "ConnectionResetError: scripted drop" in closed[0]


def test_a_dial_that_never_opened_is_reported_too() -> None:
    """A controller told only about sockets that had opened would sit in `connecting`
    for the length of an endpoint outage, which reads on a badge as "starting up"."""
    opened: list[str] = []
    closed: list[str] = []

    def refuse(_url):
        raise OSError("no route to host")

    feed = DeltaFeed(FanOut(), connect=refuse)
    feed.on_open(opened.append)
    feed.on_close(closed.append)

    asyncio.run(drive(feed))

    assert opened == []
    assert "OSError: no route to host" in closed[0]


def test_a_listener_that_raises_cannot_take_the_feed_down() -> None:
    """The same rule `publish` follows: a broken consumer is a bug in the consumer, and
    a socket reader is not the place to discover it."""
    published: list = []
    bus = FanOut()
    drained = bus.subscribe("test", maxsize=100)
    socket = FakeSocket([book_frame("C-BTC-77600-040926", 120, 125)])
    feed = DeltaFeed(bus, connect=connector([socket]))
    feed.subscribe("ob_l2", ["C-BTC-77600-040926"])

    def explode(_detail: str) -> None:
        raise RuntimeError("a listener's own bug")

    feed.on_open(explode)

    asyncio.run(drive(feed))

    while not drained.queue.empty():
        published.append(drained.queue.get_nowait())
    assert feed.messages == 1
    assert len(published) == 1


def test_a_stop_is_not_reported_as_a_drop() -> None:
    """Stopping on purpose is not the connection failing, and a badge that flashed
    `reconnecting` on every clean shutdown would teach a person to ignore it.

    **The attempt has to end with the stop already asked for**, which is the only way
    through `run`'s `if not self._stopping` guard. An earlier version of this test idled
    a socket in `recv` and cancelled the task instead: `run` re-raised the
    `CancelledError` before ever reaching the guard, so the assertion held just as well
    with the guard deleted, and the test could not fail.
    """
    closed: list[str] = []

    class StopThenDrop(FakeSocket):
        """The shutdown as it really happens: `stop()` is asked for, and the socket the
        reader is sitting on ends the attempt a moment later."""

        async def recv(self):
            feed.stop()
            raise ConnectionResetError("the socket went with the stop")

    feed = DeltaFeed(FanOut(), connect=connector([StopThenDrop([])]))
    feed.subscribe("ob_l2", CHAIN)
    feed.on_close(closed.append)

    asyncio.run(feed.run())

    assert feed._stopping is True
    assert feed.last_error == "ConnectionResetError: the socket went with the stop"
    assert closed == []


def test_an_open_with_nothing_subscribed_is_not_announced_as_open() -> None:
    """`OPENED` promises a socket that has been **resubscribed**, and an empty registry
    sends no subscribe at all — so announcing it would put a green badge on a socket
    that is guaranteed to deliver nothing, which is the healthy-connection-zero-messages
    failure this module exists to prevent.

    Unreachable from `main.py` today only because it subscribes before it streams;
    nothing enforces that ordering, and #39's supervisor takes over the start sequence.
    Not announcing it leaves the controller in `connecting`, which reaches
    `reconnecting` at its own bound rather than sitting green forever.
    """
    opened: list[str] = []
    socket = FakeSocket([], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.on_open(opened.append)

    asyncio.run(drive(feed))

    assert opened == []
    assert socket.sent == []
    assert feed.empty_opens >= 1


def test_an_open_with_something_subscribed_is_announced_as_before() -> None:
    """The other half of the guard: a registry with symbols in it sends its subscribe
    and reports the open, which is every real connection."""
    opened: list[str] = []
    socket = FakeSocket([], close_after=0)
    feed = DeltaFeed(FanOut(), connect=connector([socket]))
    feed.subscribe("ob_l2", CHAIN)
    feed.on_open(opened.append)

    asyncio.run(drive(feed))

    assert opened
    assert feed.empty_opens == 0


def test_a_close_says_the_socket_delivered_nothing_because_that_is_the_diagnosis() -> (
    None
):
    """**The most diagnostic thing this module knows, told to nobody.**

    "Connected but closed without delivering a message" is the whole
    healthy-socket-zero-messages failure in one sentence, and it is the fact the budget
    rule turns on: Delta accepting a handshake and closing straight away — a rejected
    subscribe, a throttled IP, an endpoint draining — is not a working endpoint. The
    module computed that sentence *after* it had already told its close listeners, so
    what the controller heard was the transport error alone: `ConnectionResetError`,
    which is what a healthy socket dropping in a storm also says. Two very different
    incidents, one indistinguishable close detail.

    The second attempt is the point. `last_error` is instance state that survives across
    `run()` calls, and neither `last_error` test drives more than one attempt, so nothing
    pinned what a *later* close is told.
    """
    delivered_then_reset = FakeSocket([ticker_frame(CHAIN[0], 579, 584)], close_after=0)

    class OpensAndSaysNothing:
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

    async def scenario():
        closes: list[str] = []
        attempts = {"n": 0}

        def connect(url):
            attempts["n"] += 1
            return delivered_then_reset if attempts["n"] == 1 else OpensAndSaysNothing()

        feed = DeltaFeed(FanOut(), connect=connect)
        feed.on_close(closes.append)
        feed.subscribe("ticker", CHAIN)
        await asyncio.wait_for(feed.run(), timeout=2.0)
        await asyncio.wait_for(feed.run(), timeout=2.0)
        return feed, closes

    feed, closes = asyncio.run(scenario())

    assert len(closes) == 2
    assert feed.messages == 1, "only the first attempt delivered"
    # The first attempt delivered, so its close is an ordinary drop and says so.
    assert "without delivering" not in closes[0]
    # The second opened and delivered nothing, which is the diagnosis.
    assert "without delivering a message" in closes[1], (
        "the close listener was told only the transport error, which a healthy socket "
        f"dropping in a storm also reports: {closes[1]!r}"
    )
