"""What a producer and a consumer are allowed to know about the thing between them.

**The broker is undecided and should stay that way.** `fanout.py` argues at length why an
in-process fan-out is right for one venue, one user and 82 KB/s, and why the seam sits
exactly where OpenAlgo puts ZeroMQ. This protocol is that seam written down: a producer
calls `publish`, a consumer calls `subscribe`, and neither is opened again if the fan-out
is later replaced.

**No behaviour is defined here.** The queue policy — drop-oldest by default, lossless as
a watermark — belongs to `fanout.py`, which documents it and `tests/test_fanout.py`, which
pins it. This file only names the two methods.

**Structural, and `fanout.py` is not edited to say so.** `FanOut` satisfies this protocol
by having the two methods, and `tests/test_events.py` pins that both ways: an `isinstance`
check, and a `Bus`-annotated binding a type checker reads.

Making `FanOut` inherit `Bus` was tried and reverted. A `Protocol`'s methods are not
abstract, so an explicit subclass implementing *neither* still constructs and inherits
`...`-bodied stubs returning `None`: a missing `publish` would stop raising
`AttributeError` and start dropping messages quietly. It also makes the conformance test
unfalsifiable, since `isinstance` against a nominal subclass is `True` whatever the class
contains, and it would have pulled pydantic into a module importing only `asyncio`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # Only under type checking, so that naming the concrete subscription here costs the
    # bus interface no runtime dependency on the implementation behind it.
    from ..fanout import Subscription


@runtime_checkable
class Bus(Protocol):
    """One producer, many independent consumers. Implemented today by `FanOut`."""

    def publish(self, record: Any) -> None:
        """Offer `record` to every subscriber. **Synchronous, and never blocks.**

        The socket handler calls this between reads, so anything that could suspend here
        suspends the socket.
        """
        ...

    def subscribe(
        self, name: str, maxsize: int, lossless: bool = False
    ) -> Subscription:
        """Register a consumer.

        `maxsize` is a hard ceiling under the default policy and a **watermark** under
        `lossless=True`, where the queue is unbounded and the backlog is counted instead.
        """
        ...
