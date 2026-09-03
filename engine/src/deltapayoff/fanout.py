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

Open, for #5: the storage writer wants the opposite policy, because a dropped message
there is a permanent hole in the historical record rather than a stale price nobody
wanted. That is a per-subscription choice and belongs here when #5 needs it; it is not
built yet.
"""

from __future__ import annotations

import asyncio
from typing import Any


class Subscription:
    """One consumer's queue, and the count of what it could not keep up with."""

    __slots__ = ("name", "queue", "dropped", "offered")

    def __init__(self, name: str, maxsize: int) -> None:
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.offered = 0

    def offer(self, record: Any) -> None:
        """Put `record` on the queue, discarding the oldest if it is full.

        Never blocks and never awaits. `put_nowait` raises when full, and the oldest is
        removed to make room rather than the newest being refused.
        """
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

    def subscribe(self, name: str, maxsize: int) -> Subscription:
        """Register a consumer. `maxsize` must be positive.

        There is no unbounded option on purpose. An unbounded queue does not fail when
        the consumer falls behind; it fails an hour later, somewhere else, out of memory.
        """
        if maxsize < 1:
            raise ValueError(f"maxsize must be at least 1; got {maxsize}")
        if name in self._subscriptions:
            raise ValueError(f"a subscriber named {name!r} already exists")
        subscription = Subscription(name, maxsize)
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

    def stats(self) -> dict[str, dict[str, int]]:
        """What each consumer received and what it could not keep up with."""
        return {
            name: {
                # `offered` counts what reached the queue, not what the consumer
                # read — the bus cannot know the latter. `offered - dropped` is what
                # survived to be readable; `queued` is what is waiting right now.
                "offered": subscription.offered,
                "dropped": subscription.dropped,
                "queued": subscription.queue.qsize(),
            }
            for name, subscription in self._subscriptions.items()
        }
