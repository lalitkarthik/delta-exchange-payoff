"""Tests for the one-frame adapter used by the Compose smoke run."""

from __future__ import annotations

from deltapayoff import feed_main
from deltapayoff.delta_client import DeltaClient
from deltapayoff.events import OptionQuote
from fakes.decoder import delta_decoder
from fakes.scripted_adapter import Frames, ScriptedAdapter, Silence
from fakes.smoke_feed import SYMBOL, book_frame, build_adapter

READING = 1_788_430_800.0


def test_book_frame_uses_the_injected_reading_for_both_venue_timestamps() -> None:
    frame = book_frame(READING)

    assert frame == {
        "type": "ob_l2",
        "sy": SYMBOL,
        "ts": 1_788_430_800_000_000,
        "lts": 1_788_430_800_000_000,
        "a": [["125", "12"]],
        "b": [["120", "10"]],
    }


def test_build_adapter_has_one_frame_then_silence_and_the_matching_listing() -> None:
    adapter = build_adapter(("BTC",), now=lambda: READING)

    assert isinstance(adapter, ScriptedAdapter)
    assert len(adapter.script) == 2
    assert isinstance(adapter.script[0], Frames)
    assert adapter.script[0].channel == "ob_l2"
    assert len(adapter.script[0].frames) == 1
    assert adapter.script[0].received_at == READING
    assert isinstance(adapter.script[1], Silence)
    assert adapter.listings["BTC"][0].venue_symbol == SYMBOL


def test_the_single_smoke_frame_decodes_to_a_quote_event() -> None:
    adapter = build_adapter(("BTC",), now=lambda: READING)
    step = adapter.script[0]
    assert isinstance(step, Frames)

    events = delta_decoder()(step.channel, step.frames[0], step.received_at)

    assert any(isinstance(event, OptionQuote) for event in events)


def test_smoke_factory_resolves_through_the_feed_environment_seam(monkeypatch) -> None:
    monkeypatch.setenv("DELTA_FEED_ADAPTER", "fakes.smoke_feed:build_adapter")

    adapter = feed_main.build_adapter(DeltaClient(client=object()), ("BTC",))

    assert isinstance(adapter, ScriptedAdapter)
