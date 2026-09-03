"""One producer, many independent consumers, in one process.

The socket handler must never run inside a consumer. If it does, a slow disk flush or a
raised exception stops it reading the socket, the operating system's receive buffer
fills, and **Delta closes the connection** — nobody enforced a limit, we simply failed to
keep up. So the handler publishes and returns; each consumer reads its own queue in its
own task.

**Why this and not ZeroMQ.** OpenAlgo fans out over ZeroMQ because it serves 36 brokers
and many users across separate processes. We have one venue, one user and 82 KB/s. A
broker process would cost a deployment step, a failure mode and a serialise-deserialise
round trip per message, and buy nothing. So the seam sits exactly where OpenAlgo puts the
bus and an in-process fan-out fills it; promoting to ZeroMQ later replaces what is behind
the seam rather than rewriting the producer. T4 step 5 measures what this indirection
costs, because that measurement is the argument either way and it should not be assumed.

**Overflow drops the oldest.** A quote from four seconds ago is not slightly worse than
the current one, it is worthless — nobody trades on it. A consumer that falls behind
should skip to now rather than faithfully replay a backlog nobody wants. The alternatives
are worse: an unbounded queue is a memory leak with good manners, and blocking the
producer reinvents the failure above.

**Every drop is counted.** A silent drop is a lie, and this project has twice been
damaged by numbers that looked plausible and were not.

**The storage writer takes the opposite policy, and the reason recorded here before was
wrong.** It said a drop there is "a permanent hole in the historical record". Under
one-minute bars it is not — a dropped tick perturbs a bar rather than removing a record,
and the bar still exists. The real problem is worse than a hole because it is invisible:
drops happen *under load*, load is when price moves fastest, and so drop-oldest
**systematically shaves the highs and the lows**, which are precisely the columns the
bars exist to capture. That is a bias, not noise, and nothing in the output says so.

So the policy is per subscription. `lossless=True` gives a queue with no ceiling, and
`maxsize` stops being a ceiling and becomes a **watermark**: every offer made while the
queue already sits at or above it is counted, and the deepest backlog is kept. That is
what stops an unbounded queue being a memory leak with good manners — it does not fail
when the consumer falls behind, it fails an hour later somewhere else, out of memory,
unless somebody is counting. `over_capacity` moving is the fact to act on.

Lossless is deliberately not the default. The screen wants the old policy: a quote from
four seconds ago is worthless to it, and replaying a backlog nobody wanted is the wrong
kind of faithful.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Subscription:
    """One consumer's queue, and the count of what it could not keep up with.

    Two policies, chosen at subscribe time and never mixed. Drop-oldest bounds the queue
    at `capacity` and evicts to make room. Lossless leaves the queue unbounded and treats
    `capacity` as a watermark to count against instead.
    """

    __slots__ = (
        "name",
        "queue",
        "dropped",
        "offered",
        "lossless",
        "capacity",
        "over_capacity",
        "backlog_peak",
    )

    def __init__(self, name: str, maxsize: int, lossless: bool = False) -> None:
        self.name = name
        self.lossless = lossless
        #: What `maxsize` meant. A ceiling under drop-oldest, a watermark under lossless.
        self.capacity = maxsize
        # `maxsize=0` is asyncio's spelling of unbounded. It is used only on the lossless
        # path, where refusing or evicting a record is the thing being ruled out.
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=0 if lossless else maxsize)
        self.dropped = 0
        self.offered = 0
        #: Offers made while the queue already stood at or above `capacity`.
        self.over_capacity = 0
        #: The deepest the queue has ever been. Lossless only — under drop-oldest it is
        #: `capacity` by construction and says nothing.
        self.backlog_peak = 0

    def offer(self, record: Any) -> None:
        """Put `record` on the queue. **Never blocks and never awaits.**

        Under drop-oldest, `put_nowait` raises when full and the oldest is removed to
        make room — the newest is never the one refused, because a quote from four
        seconds ago is worthless rather than merely worse.

        Under lossless nothing is refused at all; the depth is counted instead.
        """
        if self.lossless:
            depth = self.queue.qsize()
            if depth >= self.capacity:
                self.over_capacity += 1
            self.queue.put_nowait(record)
            # Read after the put, so the peak is the true depth including this record.
            self.backlog_peak = max(self.backlog_peak, depth + 1)
            self.offered += 1
            return

        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - a reader raced us
                pass
            try:
                self.queue.put_nowait(record)
            except asyncio.QueueFull:  # pragma: no cover - a writer raced us
                self.dropped += 1
                return
        self.offered += 1


class FanOut:
    """Copies each published record into every subscriber's queue."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, Subscription] = {}
        self.published = 0

    def subscribe(
        self, name: str, maxsize: int, lossless: bool = False
    ) -> Subscription:
        """Register a consumer. `maxsize` must be positive under either policy.

        Under the default policy `maxsize` is a hard ceiling and overflow drops the
        oldest. Under `lossless=True` it is a watermark: the queue itself is unbounded,
        and `maxsize` is the depth past which the backlog starts being counted. It is
        still required, and still must be positive, because an unbounded queue with no
        number attached to it is the memory leak this module refuses — the counter is
        what makes a backup a fact to act on rather than an out-of-memory an hour later.
        """
        if maxsize < 1:
            raise ValueError(f"maxsize must be at least 1; got {maxsize}")
        if name in self._subscriptions:
            raise ValueError(f"a subscriber named {name!r} already exists")
        subscription = Subscription(name, maxsize, lossless=lossless)
        self._subscriptions[name] = subscription
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a consumer. Ordinary, not a failure — a browser tab closing does it."""
        self._subscriptions.pop(subscription.name, None)

    def publish(self, record: Any) -> None:
        """Offer `record` to every subscriber. **Synchronous, and never blocks.**

        This is the whole contract. The handler calls it between reads of the socket, so
        anything that could suspend here suspends the socket.
        """
        self.published += 1
        for subscription in self._subscriptions.values():
            subscription.offer(record)

    def stats(self) -> dict[str, dict[str, int | bool]]:
        """What each consumer received and what it could not keep up with."""
        return {
            name: {
                # `offered` counts what reached the queue, not what the consumer
                # read — the bus cannot know the latter. `offered - dropped` is what
                # survived to be readable; `queued` is what is waiting right now.
                "offered": subscription.offered,
                "dropped": subscription.dropped,
                "queued": subscription.queue.qsize(),
                "lossless": subscription.lossless,
                # Zero under drop-oldest, where `dropped` is already the signal. Under
                # lossless these two are the only warning there is.
                "over_capacity": subscription.over_capacity,
                "backlog_peak": subscription.backlog_peak,
            }
            for name, subscription in self._subscriptions.items()
        }
