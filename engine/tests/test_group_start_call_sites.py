"""#97: what `group_start` each production lossless call site actually resolves to.

The bus's default moved from `"0"` to `"$"` in #97. The three services that subscribe
losslessly on a `RedisBus` already stated `group_start="$"` themselves, so the move must
leave all three exactly where they were — and that has to be *proven*, not read off the
three keyword arguments by eye. Reading a call site tells you what was typed there; it
does not tell you what the bus built from it, and #86 is the standing proof that those
are not the same question.

So each test here drives the real entry point — `AlertConsumer.subscribe`,
`BarBuffer.attach`, `store_main._prepare_process` — against a real `RedisBus` on
fakeredis, and asserts the group start id on the subscription object the bus returned.

Nothing here starts the bus, touches a socket, or reads a clock: `subscribe` registers a
subscription and only spawns a reader once a client exists, so an unstarted bus is enough
to see what it built.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import fakeredis
import fakeredis.aioredis
import pytest

from deltapayoff import store_main
from deltapayoff.alert_consumer import AlertConsumer
from deltapayoff.bar_buffer import BarBuffer
from deltapayoff.redis_bus import BusConfig, RedisBus

#: The three services that take a lossless subscription on a `RedisBus`, and the file and
#: line of the call site each one is here to pin.
CALL_SITES = "alert_consumer.py:65, bar_buffer.py:131, store_main.py:172"


def _bus() -> RedisBus:
    """A `RedisBus` over its own fakeredis server, unstarted."""
    server = fakeredis.FakeServer()

    def factory(_config: BusConfig) -> Any:
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=False)

    return RedisBus(
        BusConfig(
            venue="DELTA",
            underlyings=("BTC",),
            batch_ms=60_000,
            read_block_ms=0,
            read_count=10,
            idle_sleep_seconds=0.001,
            # Age trimming off: nothing here publishes, and a trim floor taken from the
            # real clock would depend on the hour the suite runs.
            retention_seconds=1_000_000_000.0,
        ),
        client_factory=factory,
    )


def test_the_discord_consumer_subscribes_at_the_head() -> None:
    """`alert_consumer.py:65`. Decision #66's "no replay", at the group's creation."""
    subscription = AlertConsumer(_bus()).subscribe()

    assert subscription.lossless is True
    assert subscription.group_start == "$"


def test_the_bar_buffer_subscribes_at_the_head() -> None:
    """`bar_buffer.py:131`. The split api's bar cache holds a tail, not a history."""
    subscription = BarBuffer().attach(_bus())

    assert subscription.lossless is True
    assert subscription.group_start == "$"


def test_the_store_subscribes_at_the_head_on_a_first_start(tmp_path: Path) -> None:
    """`store_main.py:172`, and the one that matters.

    Record 0010: the store starts from its checkpoint, and on a first start from the head
    taken once as a concrete id — never `0`. This is the call site where `0` would have
    cost the most: the store is the only writer of the four bar tables, and a group
    created at `0` would fold the whole retained window into bars that already exist.
    With an empty root there is no checkpoint, so this is the first-start path.
    """

    async def scenario():
        process = await store_main._prepare_process(root=tmp_path, bus=_bus())
        return process.subscription, process.control_subscription

    subscription, control = asyncio.run(scenario())

    assert subscription.lossless is True
    assert subscription.start_ids == {}, "an empty root is the first-start path"
    assert subscription.group_start == "$"
    # The control reader is drop-oldest and creates no consumer group at all. It states
    # no `group_start`, correctly — it has no opinion to state — and #97's default is
    # what it therefore gets.
    assert control.lossless is False
    assert control.group_start == "$"


def test_a_deliberate_zero_is_still_reachable() -> None:
    """`"0"` is made explicit by #97, not removed.

    A reader that genuinely wants everything Redis still holds — a backfill, a forensic
    drain after an incident, or a configured stream absent from a checkpoint — still has
    the value. It simply has to type it, which is the whole of the change.
    """
    bus = _bus()

    subscription = bus.subscribe("backfill", maxsize=10, lossless=True, group_start="0")

    assert subscription.group_start == "0"


def test_group_start_still_admits_exactly_two_values() -> None:
    """The validation #97 did not change: two values, and nothing else."""
    bus = _bus()

    with pytest.raises(ValueError, match="must be '0' or '\\$'"):
        bus.subscribe("wrong", maxsize=10, lossless=True, group_start="$$")
