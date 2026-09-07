"""Broker adapters: the protocol, and the Delta implementation behind it.

**An adapter is the one class that knows a venue.** The interface is `base.Adapter`; how
this package is built inside is `docs/design/lld/adapter.md`; what crosses out of it is
`docs/design/events.md`.

**Everything that names a Delta channel is in here**, and that is the shape #37 left the
package in: `delta_socket` owns the connection and the two channel names, `delta` owns the
frame-to-event decode. Outside this package nothing knows the venue had channels at all.

`shim.LegacyQuoteBridge` — the expand half of the expand-contract pair — is **gone**, with
the `feed.Quote` record it existed to keep alive. The chain cache and the bar writer take
canonical events off the bus.
"""

from __future__ import annotations

from .base import Adapter, ConnectionListener, ConnectionSignal, Publish
from .delta import VENUE, DeltaAdapter, instrument_from_symbol
from .delta_socket import DeltaFeed

#: **The channel names are deliberately not re-exported here.** `delta.py` imports them
#: from `delta_socket` and so does every test that needs one; hoisting them to the package
#: root would put the venue's vocabulary one import closer to the boundary this package
#: exists to hold, for no caller that wanted it.
__all__ = [
    "VENUE",
    "Adapter",
    "ConnectionListener",
    "ConnectionSignal",
    "DeltaAdapter",
    "DeltaFeed",
    "Publish",
    "instrument_from_symbol",
]
