"""The live chain cache: latest frame per symbol, rebuilt into a chain on demand.

This computes nothing. It keeps the most recent frame each contract sent and hands the
collection to `wire.chain_from_frames`, which is the same decoder the REST path's tests
already cover. What it adds is *which* frames belong to the chain a browser asked for,
and the answer that there is no chain yet.

No network. Frames come from the captured fixtures, or are built inline where the test is
about the cache rather than the decoding.
"""

from __future__ import annotations

import asyncio

from deltapayoff.fanout import FanOut
from deltapayoff.feed import Quote
from deltapayoff.stream import ChainStream

EXPIRY = "04-09-2026"
OTHER_EXPIRY = "11-09-2026"


def ticker(symbol, bid, ask, spot="77651.9"):
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": spot,
        "ts": 1,
        "d": [
            {
                "s": symbol,
                "i": 1,
                "m": "580.6",
                "q": [str(ask), "10", str(bid), "20", None],
                "qiv": ["0.31", "0.29", "0.30"],
                "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
                "oi": ["100", "200"],
            }
        ],
    }


def book(symbol, bid, ask):
    return {
        "type": "ob_l2",
        "sy": symbol,
        "ts": 1,
        "lts": 1,
        "a": [[str(ask), "10"]],
        "b": [[str(bid), "10"]],
    }


def quote(frame, channel):
    return Quote(
        symbol=frame["sy"], channel=channel, received_at=0.0, frame=frame
    )


def test_a_chain_is_none_until_something_has_arrived() -> None:
    """An empty cache is not an empty chain.

    Returning a `ChainResponse` with no rows would render as a blank ladder and read as
    'Delta lists nothing', when the truth is that the socket has not spoken yet.
    """
    stream = ChainStream()

    assert stream.chain("BTC", EXPIRY) is None


def test_applying_frames_builds_the_chain_they_describe() -> None:
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("P-BTC-77600-040926", 120, 125), "ticker"))

    chain = stream.chain("BTC", EXPIRY)

    assert chain is not None
    assert [row.strike for row in chain.rows] == [77_600.0]
    assert chain.rows[0].call.bid == 579.0
    assert chain.rows[0].put.ask == 125.0
    assert chain.spot == 77_651.9


def test_only_the_requested_expiry_is_included() -> None:
    """The feed carries every listed expiry on one connection. A chain screen shows one.

    Without this filter every strike of every expiry would be folded onto the same
    ladder by strike, silently mixing contracts that settle weeks apart.
    """
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-BTC-80000-110926", 200, 210), "ticker"))

    front = stream.chain("BTC", EXPIRY)
    later = stream.chain("BTC", OTHER_EXPIRY)

    assert [r.strike for r in front.rows] == [77_600.0]
    assert [r.strike for r in later.rows] == [80_000.0]


def test_only_the_requested_underlying_is_included() -> None:
    """ETH strikes are three orders of magnitude below BTC's, so a leak here would not
    look like an error — it would look like a chain with a very wide ladder."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-ETH-4000-040926", 12, 14), "ticker"))

    chain = stream.chain("BTC", EXPIRY)

    assert [r.strike for r in chain.rows] == [77_600.0]
    assert [r.strike for r in stream.chain("ETH", EXPIRY).rows] == [4_000.0]


def test_the_latest_frame_wins() -> None:
    """The cache holds one frame per contract, not a history. A quote that arrived two
    seconds ago is not evidence of anything once a newer one exists."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-BTC-77600-040926", 601, 607), "ticker"))

    chain = stream.chain("BTC", EXPIRY)

    assert chain.rows[0].call.bid == 601.0
    assert chain.rows[0].call.ask == 607.0


def test_the_book_overrides_the_ticker_quote() -> None:
    """Both channels carry the top of book; `ob_l2` refreshes every 508 ms against
    `ticker`'s 5001 ms. Taking the book's copy is where the freshness comes from."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(book("C-BTC-77600-040926", 601, 607), "ob_l2"))

    chain = stream.chain("BTC", EXPIRY)

    assert chain.rows[0].call.bid == 601.0
    # The ticker frame still supplies everything the book does not carry.
    assert chain.rows[0].call.delta == 0.55
    assert chain.rows[0].call.oi == 100.0


def test_a_book_frame_alone_is_not_a_chain_row() -> None:
    """`ob_l2` carries no spot, no Greeks and no open interest. A row built from it
    alone would render as a mostly empty line rather than as a quote."""
    stream = ChainStream()
    stream.apply(quote(book("C-BTC-77600-040926", 601, 607), "ob_l2"))

    assert stream.chain("BTC", EXPIRY) is None


def test_the_stream_drains_the_bus_it_subscribes_to() -> None:
    """Wired to the `FanOut`, not to the socket. The feed publishes and never waits."""

    async def scenario():
        bus = FanOut()
        stream = ChainStream()
        stream.attach(bus, maxsize=100)

        bus.publish(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
        bus.publish(quote(ticker("P-BTC-77600-040926", 120, 125), "ticker"))

        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return stream.chain("BTC", EXPIRY)

    chain = asyncio.run(scenario())

    assert chain is not None
    assert chain.rows[0].call is not None
    assert chain.rows[0].put is not None


def test_symbols_for_an_expiry_are_reported_for_subscribing() -> None:
    """The feed needs a symbol list to subscribe. It comes from REST at start-up, but
    the stream knows what it has actually seen, which is what the screen can show."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-BTC-80000-110926", 200, 210), "ticker"))

    assert stream.symbols("BTC", EXPIRY) == ["C-BTC-77600-040926"]


def test_a_real_captured_chain_rebuilds_from_the_cache(
    ws_ticker_frames, ws_book_frames
) -> None:
    """End to end on the 136-symbol capture: every frame in, one full chain out."""
    stream = ChainStream()
    for frame in ws_ticker_frames.values():
        stream.apply(quote(frame, "ticker"))
    for frame in ws_book_frames.values():
        stream.apply(quote(frame, "ob_l2"))

    chain = stream.chain("BTC", EXPIRY)

    assert len(chain.rows) == 69
    assert chain.spot is not None
    assert chain.atm_strike is not None
