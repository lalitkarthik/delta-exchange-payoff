"""The real Delta decode, with no socket behind it.

**A test above the adapter should not hand-build events**, because a hand-built event is
one more copy of the catalogue with no way to notice when the producer stops agreeing with
it. Every consumer test in this suite therefore feeds a captured frame through
`DeltaAdapter.events_from_frame` — the same function the live path calls — and asserts on
what the consumer did with the events that came out.

That is the seam #33 names third, and #37 leans on it hard: the chain cache, the bar
writer and the websocket endpoint all stopped taking a venue record and started taking
events, and what makes those tests worth anything is that the events are the producer's.
"""

from __future__ import annotations

from typing import Any

from deltapayoff.adapters import DeltaAdapter
from deltapayoff.events import Event

#: An arrival stamp this file chose, so nothing here reads a clock. Only `ts_received`
#: comes from it; the venue's own `ts` is what every consumer buckets on.
ARRIVED_AT = 1_788_430_800.5


class NoSocket:
    """A socket owner that owns no socket, so building an adapter dials nothing."""

    def __init__(self, sink: Any, **_kwargs: Any) -> None:
        self.sink = sink

    def subscribe(self, channel: str, symbols: Any) -> None:
        return None

    async def run(self) -> None:
        return None

    def stop(self) -> None:
        return None


def delta_decoder():
    """`events_from_frame` bound to a fresh adapter. Fresh, so nothing leaks between
    scripted connections or between tests."""
    return DeltaAdapter(feed_factory=NoSocket).events_from_frame


_DECODE = delta_decoder()


def events_from_frame(
    channel: str, frame: dict[str, Any], received_at: float = ARRIVED_AT
) -> list[Event]:
    """One captured frame to the canonical events the live path would publish for it.

    Shares one decoder across calls, which is safe because the decoder holds no state
    that changes what it emits — only counters. A caller that wants its own counters
    calls `delta_decoder()`.
    """
    return _DECODE(channel, frame, received_at)
