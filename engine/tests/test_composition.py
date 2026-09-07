"""How the engine is wired, and the bridge that keeps today's consumers working.

Two buses now: the adapter publishes canonical events on one, and the shim republishes
today's `feed.Quote` on the other, which is what the chain cache and the bar writer still
read. That arrangement is the expand half of an expand–contract and #37 removes half of
it, so it is asserted here rather than assumed — a shim that quietly stopped republishing
would leave the ladder frozen with nothing saying why.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from deltapayoff import main
from deltapayoff.adapters import DeltaAdapter, LegacyQuoteBridge
from deltapayoff.events import Event
from deltapayoff.fanout import FanOut
from deltapayoff.feed import DeltaFeed, Quote
from deltapayoff.stream import ChainStream

CALL = "C-BTC-77600-040926"
PUT = "P-BTC-77600-040926"
EXPIRY = "04-09-2026"


def ticker_frame(symbol: str, bid: float, ask: float) -> dict:
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": "77651.9",
        "ts": 1_788_430_765_832_299,
        "d": [
            {
                "s": symbol,
                "i": 1,
                "m": "580.6",
                "q": [str(ask), "100", str(bid), "200", None],
                "qiv": ["0.31", "0.29", "0.30"],
                "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
                "oi": ["100", "200"],
                "ohlc": ["1", "2", "3", "4"],
                "to": ["5", "5"],
            }
        ],
    }


class _Socket:
    """A scripted connection. Yields the frames, then idles until the test cancels."""

    def __init__(self, script) -> None:
        self.script = list(script)

    async def send(self, raw) -> None:
        json.loads(raw)

    async def recv(self):
        if not self.script:
            await asyncio.sleep(3600)
        return json.dumps(self.script.pop(0))

    async def ping(self):
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def _adapter(quotes: FanOut, script) -> DeltaAdapter:
    """The real adapter over the real socket owner, with the real shim behind it."""

    def factory(sink, **kwargs):
        return DeltaFeed(sink, connect=lambda url: _Socket(script), **kwargs)

    return DeltaAdapter(feed_factory=factory, legacy=LegacyQuoteBridge(quotes))


async def _drive(adapter, events: FanOut, seconds: float = 0.2) -> None:
    task = asyncio.create_task(adapter.stream(events.publish))
    await asyncio.sleep(seconds)
    adapter.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- the bridge -------------------------------------------------------------------


def test_the_shim_turns_a_socket_frame_into_the_record_the_cache_reads() -> None:
    """**The tracer bullet through the expand half.**

    A frame arrives on the socket, the adapter decodes it once, the shim republishes it
    in the old shape, and the chain cache — untouched by this ticket — builds a ladder
    from it. If the bridge stopped bridging, this is the test that says so.
    """
    quotes, events = FanOut(), FanOut()
    stream = ChainStream()
    stream.attach(quotes)
    adapter = _adapter(
        quotes, [ticker_frame(CALL, 579, 584), ticker_frame(PUT, 120, 125)]
    )

    async def scenario() -> None:
        await _drive(adapter, events)
        while not stream._subscription.queue.empty():
            stream.apply(stream._subscription.queue.get_nowait())

    asyncio.run(scenario())

    chain = stream.chain("BTC", EXPIRY)
    assert chain is not None, "the shim published nothing the chain cache could use"
    assert chain.spot == pytest.approx(77651.9)
    strikes = {row.strike for row in chain.rows}
    assert 77600.0 in strikes
    row = next(row for row in chain.rows if row.strike == 77600.0)
    assert row.call is not None and row.call.bid == 579.0
    assert row.put is not None and row.put.bid == 120.0


def test_the_two_buses_carry_two_different_things() -> None:
    """An `Event` and a `feed.Quote` share no attribute, so a consumer handed the wrong
    one raises rather than misreading it. That is why there are two buses and not one
    with a filter: the separation is structural instead of conventional."""
    quotes, events = FanOut(), FanOut()
    from_quotes = quotes.subscribe("test-quotes", maxsize=100)
    from_events = events.subscribe("test-events", maxsize=100)
    adapter = _adapter(quotes, [ticker_frame(CALL, 579, 584)])

    asyncio.run(_drive(adapter, events))

    published_quotes = _drain(from_quotes)
    published_events = _drain(from_events)

    assert [type(record) for record in published_quotes] == [Quote]
    assert all(isinstance(record, Event) for record in published_events)
    assert [record.type for record in published_events] == [
        "md.option_reference",
        "md.index_quote",
    ]


def _drain(subscription) -> list:
    drained = []
    while not subscription.queue.empty():
        drained.append(subscription.queue.get_nowait())
    return drained


# --- the stack --------------------------------------------------------------------


def test_the_stack_gives_each_consumer_the_queue_policy_it_needs(monkeypatch) -> None:
    """Unchanged by the refactor, and the reason it must stay unchanged is in
    `fanout.py`: drop-oldest under load systematically shaves the highs and lows the bars
    exist to capture, which is a bias and not noise."""
    monkeypatch.setattr(main, "DeltaFeed", lambda sink, **kwargs: _NoFeed(sink))

    stack = main.build_feed_stack(client=None)
    stats = stack.quotes.stats()

    assert stats["bar-writer"]["lossless"] is True
    assert stats["chain-stream"]["lossless"] is False
    assert stack.events.stats() == {}, "nothing subscribes to the event bus until #37"
    assert stack.shim.bus is stack.quotes
    assert stack.feed is stack.adapter.feed


class _NoFeed:
    def __init__(self, sink) -> None:
        self.sink = sink


# --- which underlyings are recorded ----------------------------------------------


def test_btc_alone_unless_the_environment_says_otherwise(monkeypatch) -> None:
    """ETH is #43, and #33 requires the feed's rate and bandwidth measured for sixty
    seconds after it is enabled before the cost is called fine."""
    monkeypatch.delenv(main.LIVE_UNDERLYINGS_ENV, raising=False)

    assert main.live_underlyings() == ("BTC",)


def test_the_recorded_set_is_configuration(monkeypatch) -> None:
    """Configuration read at start-up, not a constant in the application module — which
    is the whole of user story 7."""
    monkeypatch.setenv(main.LIVE_UNDERLYINGS_ENV, " btc , eth ")

    assert main.live_underlyings() == ("BTC", "ETH")


def test_an_underlying_delta_does_not_list_is_refused_loudly(
    monkeypatch, caplog
) -> None:
    """Delta answers a request for an asset it does not list with an empty ticker list,
    so a typo would otherwise produce a feed that connects, subscribes nothing and
    records nothing — with no error anywhere."""
    monkeypatch.setenv(main.LIVE_UNDERLYINGS_ENV, "BTC,DOGE")

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        assert main.live_underlyings() == ("BTC",)

    assert any("DOGE" in record.getMessage() for record in caplog.records)


def test_a_configuration_naming_nothing_valid_still_records_btc(
    monkeypatch, caplog
) -> None:
    """Recording BTC is a better answer to a bad config line than recording nothing."""
    monkeypatch.setenv(main.LIVE_UNDERLYINGS_ENV, "DOGE")

    with caplog.at_level(logging.ERROR, logger=main.logger.name):
        assert main.live_underlyings() == ("BTC",)

    assert caplog.records, "it fell back silently"
