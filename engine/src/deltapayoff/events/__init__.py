"""Canonical events: the envelope, the registry, the instrument, and the bus interface.

**An event is the contract between two parts that never call each other.** The catalogue
is `docs/design/events.md` and it is the authority; how this package is built inside is
`docs/design/lld/events.md`.

**Nothing consumes any of this yet.** #36 gives the adapter that produces the market-data
events, #37 moves the feed, the chain cache and the bar writer onto them. Importing
`catalogue` here is what fills the registry, so `registry()` is complete for anyone who
imported this package at all.
"""

from __future__ import annotations

from .bus import Bus
from .catalogue import (
    Alert,
    BarTable,
    ChainStrike,
    ComputedChain,
    ConnectionState,
    ControlCommand,
    FeedConnection,
    Heartbeat,
    IndexQuote,
    OptionBar,
    OptionQuote,
    OptionReference,
)
from .envelope import Event, UnknownEventType, parse_event, register, registry
from .instrument import Instrument, InstrumentParseError, Right, format_strike

__all__ = [
    "Alert",
    "BarTable",
    "Bus",
    "ChainStrike",
    "ComputedChain",
    "ConnectionState",
    "ControlCommand",
    "Event",
    "FeedConnection",
    "Heartbeat",
    "IndexQuote",
    "Instrument",
    "InstrumentParseError",
    "OptionBar",
    "OptionQuote",
    "OptionReference",
    "Right",
    "UnknownEventType",
    "format_strike",
    "parse_event",
    "register",
    "registry",
]
