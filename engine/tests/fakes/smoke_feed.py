"""The one frame the Compose smoke test drives through feed, store and disk.

Its ts is read from the clock rather than fixed because bars._Watermarked._bucket
refuses a tick whose minute is at or before the watermark. BarWriter advances that
watermark on its first pass, so the 2026-09-04 fixture stamp would be counted late
and no bar would ever be written.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from deltapayoff.adapters import instrument_from_symbol
from fakes.scripted_adapter import Frames, ScriptedAdapter, Silence

SYMBOL = "C-BTC-77600-040926"


def book_frame(now: float) -> dict[str, Any]:
    """Return the one top-of-book frame used by the smoke run."""
    stamp = int(now * 1_000_000)
    return {
        "type": "ob_l2",
        "sy": SYMBOL,
        "ts": stamp,
        "lts": stamp,
        "a": [["125", "12"]],
        "b": [["120", "10"]],
    }


def build_adapter(
    underlyings: tuple[str, ...] = ("BTC",),
    *,
    now: Callable[[], float] = time.time,
) -> ScriptedAdapter:
    """Build one frame and then quiet. ScriptedAdapter.stream returns when its script is
    exhausted, and ConnectionController.run reads a returned stream as a closed socket
    and redials, so a script with no tail would replay the frame on a backoff loop.
    Silence(3600.0) follows the frame to keep it quiet.
    """
    reading = now()
    instrument = instrument_from_symbol(SYMBOL)
    assert instrument is not None
    return ScriptedAdapter(
        script=[
            Frames("ob_l2", [book_frame(reading)], received_at=reading),
            Silence(3600.0),
        ],
        underlyings=tuple(underlyings),
        listings={"BTC": [instrument]},
    )
