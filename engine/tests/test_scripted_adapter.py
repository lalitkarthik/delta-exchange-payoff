"""The test double itself, tested. #38 builds its whole suite on this file's subject.

A double nobody checks is a second implementation nobody trusts: if `Silence` quietly
published something, or `Close` replayed a subset of the registry, every connection test
above it would pass while describing a system that does not exist.
"""

from __future__ import annotations

import asyncio

from deltapayoff.adapters import Adapter, instrument_from_symbol
from deltapayoff.events import OptionQuote
from fakes.scripted_adapter import Close, Frames, Resume, ScriptedAdapter, Silence

SYMBOL = "C-BTC-77600-040926"
BOOK_FRAME = {
    "type": "ob_l2",
    "sy": SYMBOL,
    "ts": 1_788_430_765_832_299,
    "lts": 1_788_430_765_000_000,
    "a": [["125", "12"]],
    "b": [["120", "10"]],
}


def test_frames_then_close_then_silence_then_resume() -> None:
    """**The ticket's own sentence, run.**

    "given *frames, close, silence 20 s, resume*, produces the events, then nothing, then
    the events again" — and the twenty seconds cost this test nothing, because the clock
    is injected.
    """
    published: list = []
    #: `(seconds asked for, events published so far)` at the moment silence began.
    during_silence: list[tuple[float, int]] = []

    async def sleep(seconds: float) -> None:
        during_silence.append((seconds, len(published)))

    adapter = ScriptedAdapter(
        script=[
            Frames("ob_l2", [BOOK_FRAME]),
            Close(),
            Silence(20.0),
            Resume(),
        ],
        sleep=sleep,
    )
    adapter.subscribe([instrument_from_symbol(SYMBOL)])

    asyncio.run(adapter.stream(published.append))

    # ...produces the events...
    assert len(published) == 2
    first, again = published
    assert isinstance(first, OptionQuote)
    assert first.instrument is not None
    assert first.instrument.venue_symbol == SYMBOL
    assert (first.bid, first.ask) == (120.0, 125.0)

    # ...then nothing: the silence began after exactly one event and added none.
    assert during_silence == [(20.0, 1)]
    assert adapter.silences == 1
    assert adapter.silent_seconds == 20.0

    # ...then the events again. A distinct event carrying the same observation, which is
    # what a feed coming back with the book it went away with actually sends.
    assert isinstance(again, OptionQuote)
    assert (again.bid, again.ask) == (first.bid, first.ask)
    assert again.event_id != first.event_id


def test_a_close_replays_every_subscription() -> None:
    """The failure that produces no error: reconnect, receive nothing, notice hours
    later. A reconnected socket is a fresh, empty socket, so the registry is never
    cleared and is replayed in full — and a replay of a subset shows up here as a
    smaller snapshot."""
    adapter = ScriptedAdapter(script=[Close(), Close()])
    adapter.subscribe(
        [instrument_from_symbol(SYMBOL), instrument_from_symbol("P-BTC-77600-040926")]
    )

    asyncio.run(adapter.stream(lambda event: None))

    assert adapter.closes == 2
    assert adapter.connections == 3, "one on open, one per reconnect"
    assert len(adapter.replays) == 3
    for snapshot in adapter.replays:
        assert snapshot == {
            "ticker": {SYMBOL, "P-BTC-77600-040926"},
            "ob_l2": {SYMBOL, "P-BTC-77600-040926"},
        }


def test_subscriptions_registered_before_streaming_are_not_lost() -> None:
    """Accepting subscriptions before anything is connected removes a start-up race the
    caller would otherwise have to know about — the same contract `DeltaFeed` keeps."""
    adapter = ScriptedAdapter(script=[])
    adapter.subscribe([instrument_from_symbol(SYMBOL)])

    asyncio.run(adapter.stream(lambda event: None))

    assert adapter.replays == [{"ticker": {SYMBOL}, "ob_l2": {SYMBOL}}]


def test_a_reconnect_that_replayed_nothing_is_visible() -> None:
    """The whole reason the snapshots are kept: an empty replay is a healthy connection
    carrying zero messages, and it must be an assertion rather than a surprise."""
    adapter = ScriptedAdapter(script=[Close()])

    asyncio.run(adapter.stream(lambda event: None))

    assert adapter.replays == [{}, {}]


def test_stopping_ends_the_script_early() -> None:
    """`stop` is honoured between steps, which is as often as `DeltaFeed` checks its own
    flag."""
    published: list = []

    adapter = ScriptedAdapter(
        script=[Frames("ob_l2", [BOOK_FRAME]), Frames("ob_l2", [BOOK_FRAME])]
    )

    async def run() -> None:
        adapter.stop()
        await adapter.stream(published.append)

    asyncio.run(run())

    assert published == []


def test_resume_before_any_frames_publishes_nothing() -> None:
    """A feed that comes back having never sent anything has nothing to come back with.
    Silence, not an exception: a script is a description, not a program to be validated.
    """
    published: list = []

    asyncio.run(ScriptedAdapter(script=[Resume()]).stream(published.append))

    assert published == []


def test_the_scripted_adapter_satisfies_the_protocol() -> None:
    """It is a double for the interface, not for one class, which is the point: every
    layer above can be driven through the same six members the real adapter fills."""
    assert isinstance(ScriptedAdapter(), Adapter)


def test_the_rest_reads_refuse_rather_than_invent_an_empty_answer() -> None:
    """A `ChainResponse` with no rows renders as a blank ladder and reads as "the venue
    lists nothing". A double that answered one by default would let a test assert against
    a shape nobody set up."""
    adapter = ScriptedAdapter()

    for call in (adapter.expiries("BTC"), adapter.chain_snapshot("BTC", "04-09-2026")):
        try:
            asyncio.run(_await(call))
        except NotImplementedError:
            continue
        raise AssertionError("an unconfigured REST read answered instead of refusing")


async def _await(awaitable):
    return await awaitable
