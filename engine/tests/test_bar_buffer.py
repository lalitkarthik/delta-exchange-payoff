"""The split api's in-memory copy of sealed bars.

The events are built through the same writer hand-off as the store process, then folded
back through the buffer. No test here opens Redis or reads a file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deltapayoff.bar_buffer import BUFFER_HORIZON_SECONDS, BarBuffer
from deltapayoff.events import BarTable, OptionBar
from deltapayoff.store import (
    COMPUTED_SCHEMA,
    REFERENCE_SCHEMA,
    SCHEMA,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
    translate_bar_columns,
)
from test_store import bar as quote_bar
from test_store import computed_bar, reference, spot

MINUTE = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
TS_RECEIVED = datetime(2026, 9, 4, 9, 0, 8, 123456, tzinfo=timezone.utc)


def _published_event(bar, table: BarTable, schema) -> OptionBar:
    return OptionBar(
        source="bar-writer",
        instrument=None,
        table=table,
        underlying=bar.underlying,
        minute=bar.minute,
        columns=translate_bar_columns(
            {name: getattr(bar, name) for name in schema}, schema, to_wire=True
        ),
        ts_received=TS_RECEIVED,
    )


@pytest.mark.parametrize(
    ("table", "schema", "make_bar"),
    [
        (BarTable.QUOTE, SCHEMA, quote_bar),
        (BarTable.REFERENCE, REFERENCE_SCHEMA, reference),
        (BarTable.SPOT, SPOT_SCHEMA, spot),
        (BarTable.COMPUTED, COMPUTED_SCHEMA, computed_bar),
    ],
)
def test_a_published_bar_folds_back_to_the_same_bar(
    table, schema, make_bar, tmp_path: Path
) -> None:
    original = make_bar(minute=MINUTE)
    published: list[OptionBar] = []
    writer = BarWriter(BarStore(tmp_path), publish=published.append)
    store = writer.stores[list(BarTable).index(table)]
    writer._hand_to_store(store, [original], table)

    buffer = BarBuffer()
    buffer.apply(published[0])

    assert buffer.rows(table) == [original]
    assert published[0].instrument is None


def test_a_redelivered_bar_replaces_its_identity() -> None:
    original = spot(minute=MINUTE, close=77651.9)
    replacement = spot(minute=MINUTE, close=77699.9)
    first = _published_event(original, BarTable.SPOT, SPOT_SCHEMA)
    second = _published_event(replacement, BarTable.SPOT, SPOT_SCHEMA)
    buffer = BarBuffer()

    buffer.apply(first)
    buffer.apply(second)

    assert buffer.rows(BarTable.SPOT) == [replacement]
    assert buffer.stats()["bars"] == 1


def test_old_bars_are_evicted_against_the_newest_data_minute() -> None:
    newest = MINUTE + timedelta(minutes=7)
    old = spot(minute=MINUTE)
    boundary = spot(minute=newest - timedelta(seconds=BUFFER_HORIZON_SECONDS))
    latest = spot(minute=newest)
    buffer = BarBuffer()

    buffer.apply(_published_event(old, BarTable.SPOT, SPOT_SCHEMA))
    buffer.apply(_published_event(boundary, BarTable.SPOT, SPOT_SCHEMA))
    buffer.apply(_published_event(latest, BarTable.SPOT, SPOT_SCHEMA))

    assert buffer.rows(BarTable.SPOT) == [boundary, latest]
    assert buffer.stats()["evicted"] == 1


def test_an_unknown_column_is_malformed_and_not_folded() -> None:
    event = _published_event(quote_bar(minute=MINUTE), BarTable.QUOTE, SCHEMA)
    event = event.model_copy(
        update={"columns": {**event.columns, "not_a_store_column": 1}}
    )
    buffer = BarBuffer()

    buffer.apply(event)

    assert buffer.rows(BarTable.QUOTE) == []
    assert buffer.stats()["malformed"] == 1


def test_rows_are_ascending_and_return_a_fresh_list() -> None:
    buffer = BarBuffer()
    later = spot(minute=MINUTE + timedelta(minutes=1))
    earlier = spot(minute=MINUTE)
    buffer.apply(_published_event(later, BarTable.SPOT, SPOT_SCHEMA))
    buffer.apply(_published_event(earlier, BarTable.SPOT, SPOT_SCHEMA))

    rows = buffer.rows(BarTable.SPOT)
    rows.clear()

    assert [bar.minute for bar in buffer.rows(BarTable.SPOT)] == [
        earlier.minute,
        later.minute,
    ]


def test_run_without_attach_raises() -> None:
    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="attach"):
            await BarBuffer().run()

    asyncio.run(scenario())


def test_attach_is_lossless_on_a_non_redis_bus_too() -> None:
    """The buffer never accepts a drop-oldest subscription, on either kind of bus.

    The two feed-state caches hold a latest value, so dropping an older one costs
    nothing. A sealed bar is not a state: drop one and the split read paths carry a
    hole at the right edge that no later event refills.

    This asserted the fake's exact call kwargs before, which pinned an implementation
    detail of a branch `build_consumer_stack` cannot reach -- it always constructs a
    `RedisBus`. It now asserts the property that would matter if the branch ever did
    become reachable, which is the only reason to keep the branch at all.
    """

    class FakeBus:
        def __init__(self) -> None:
            self.calls = []

        def subscribe(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return object()

    bus = FakeBus()

    subscription = BarBuffer().attach(bus, maxsize=17, name="test-buffer")

    assert subscription is not None
    assert len(bus.calls) == 1
    args, kwargs = bus.calls[0]
    assert args == ("test-buffer",)
    assert kwargs["maxsize"] == 17
    assert kwargs["lossless"] is True
