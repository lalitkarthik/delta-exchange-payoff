"""Broker adapters: the protocol, the Delta implementation, and the temporary shim.

**An adapter is the one class that knows a venue.** The interface is `base.Adapter`; how
this package is built inside is `docs/design/lld/adapter.md`, and what crosses out of it
is `docs/design/events.md`.

`shim.LegacyQuoteBridge` is **not** part of the abstraction. It is the expand half of an
expand–contract pair, exported here only so the application module can wire it, and #37
deletes it.
"""

from __future__ import annotations

from .base import Adapter, Publish
from .delta import VENUE, DeltaAdapter, instrument_from_symbol
from .shim import LegacyQuoteBridge

__all__ = [
    "VENUE",
    "Adapter",
    "DeltaAdapter",
    "LegacyQuoteBridge",
    "Publish",
    "instrument_from_symbol",
]
