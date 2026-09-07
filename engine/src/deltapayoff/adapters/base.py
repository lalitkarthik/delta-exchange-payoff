"""What a venue must be able to do, and nothing more.

**An adapter is the one class that knows a venue.** Everything it says outward is in our
language — canonical `Instrument`s and catalogued `Event`s — and everything it hears
inward is in the venue's: the socket, the REST calls, the symbol spelling, the channel
names, the wire layout. Nothing downstream of an adapter ever sees venue JSON.

**The protocol demands only what a feed needs**, which is the whole test of the
abstraction: a venue with a different shape — NSE spells the same contract
`NIFTY-20260908-25500CE`, in rupees, in lots, off a different calendar — must be able to
fill it without the interface bending. Six members, in three groups:

* **Describe yourself** — `venue`, `underlyings`.
* **Feed** — `instruments`, `subscribe`, `stream`, `stop`.
* **Read** — `expiries`, `chain_snapshot`, the two venue REST reads the screens need.

The two REST reads are here rather than beside the adapter because the venue client *is*
part of knowing a venue: `/expiries` and `/chain` are answered from the venue's own
snapshot, and a second venue answers them from its own. Putting them anywhere else means
a second module learns a second venue.

**Structural, not nominal, and for the reason `events/bus.py` records.** A `Protocol`'s
methods are not abstract, so an explicit subclass implementing none of them still
constructs and inherits `...`-bodied stubs returning `None` — a missing `stream` would
stop raising `AttributeError` and start delivering silence. Conformance is checked by
`isinstance` against the structural protocol, which does fail when a member goes missing.

**Reconnect is not in this interface, and that is deliberate for exactly one ticket.**
Backoff, the lifetime budget, subscription replay and staleness live inside the Delta
adapter today because that is where `feed.py` already has them, correct and tested. #38
lifts them into a connection controller wrapped around this protocol, and #39 puts a
supervisor over that. Moving them early would mean writing the state machine twice.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..events import Event, Instrument

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from ..models import ChainResponse, ExpiriesResponse

#: What `stream` is handed. **Synchronous and never blocking**, because the socket reader
#: calls it between reads: anything that could suspend here suspends the socket. It is
#: `Bus.publish` in practice, and typed as a plain callable so an adapter can be driven
#: by a test that appends to a list.
Publish = Callable[[Event], None]


@runtime_checkable
class Adapter(Protocol):
    """One venue, behind six members."""

    @property
    def venue(self) -> str:
        """The venue's short name, upper case — `DELTA`, later `NSE`.

        The same string every `Instrument` this adapter builds carries, and the `source`
        on every event it emits, so a log line and a cache key agree without a lookup.
        """
        ...

    @property
    def underlyings(self) -> tuple[str, ...]:
        """The underlyings this adapter records. **Configuration, not a constant.**

        Read at start-up so that adding an asset is a deployment decision rather than a
        code change. The engine subscribes every listed contract of every one of these.
        """
        ...

    async def instruments(self, underlying: str) -> list[Instrument]:
        """Every contract the venue lists for one underlying, as canonical instruments.

        The venue's own symbol rides along in `venue_symbol`, so subscribing needs no
        reverse lookup.
        """
        ...

    def subscribe(self, instruments: Iterable[Instrument]) -> None:
        """Register instruments to be streamed. **Safe before anything is connected.**

        Accepting subscriptions before the socket exists removes a start-up race the
        caller would otherwise have to know about, and the registry is what a reconnect
        replays.
        """
        ...

    async def stream(self, publish: Publish) -> None:
        """Emit canonical events into `publish` until `stop` is called.

        Returns rather than raises when the adapter gives up — an exhausted reconnect
        budget is an ordinary end, and the caller learns of it by the coroutine
        finishing.
        """
        ...

    def stop(self) -> None:
        """Ask `stream` to return. Synchronous, and safe to call before `stream` runs."""
        ...

    async def expiries(self, underlying: str) -> ExpiriesResponse:
        """Every expiry the venue lists for one underlying, ascending."""
        ...

    async def chain_snapshot(self, underlying: str, expiry: str) -> ChainResponse:
        """The pivoted ladder for one underlying and expiry, from the venue's REST."""
        ...


__all__ = ["Adapter", "Instrument", "Publish"]
