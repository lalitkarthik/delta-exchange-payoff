"""Fold ``md.option_bar`` events into the rows a bar store would hold in memory.

The horizon is derived from the store's flush cadence: one complete ``FLUSH_SECONDS``
interval is retained, with 60 seconds of headroom for seal grace, the minute boundary and
bus transit. Eviction uses the newest folded data minute, never a wall clock.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any

from . import log_events, store
from .bars import ComputedBar, QuoteBar, ReferenceBar, SpotBar
from .events import BarTable, OptionBar
from .logging_setup import log_event
from .redis_bus import RedisBus

logger = logging.getLogger(__name__)

BUFFER_HORIZON_SECONDS = store.FLUSH_SECONDS + 60.0

TABLES = {
    BarTable.QUOTE: (store.SCHEMA, QuoteBar),
    BarTable.REFERENCE: (store.REFERENCE_SCHEMA, ReferenceBar),
    BarTable.SPOT: (store.SPOT_SCHEMA, SpotBar),
    BarTable.COMPUTED: (store.COMPUTED_SCHEMA, ComputedBar),
}


class BarBuffer:
    """A bounded, replay-safe collection of the latest bar per table identity."""

    def __init__(self) -> None:
        self._bars: dict[tuple[BarTable, str, datetime, str | None], Any] = {}
        self.newest_minute: datetime | None = None
        self.skipped = 0
        self.malformed = 0
        self.evicted = 0
        self._subscription: Any = None

    def apply(self, event: Any) -> None:
        """Fold one event, counting unrelated and malformed records."""
        if not isinstance(event, OptionBar):
            self.skipped += 1
            return

        schema, bar_type = TABLES[event.table]
        expected_fields = {field.name for field in fields(bar_type)}
        schema_fields = set(schema)
        columns = event.columns
        unknown = set(columns) - schema_fields
        missing = schema_fields - set(columns)
        schema_only = schema_fields - expected_fields
        if unknown or missing or schema_only:
            self._malformed(
                event,
                f"columns disagree with {event.table.value} schema: "
                f"unknown={sorted(unknown)}, missing={sorted(missing)}, "
                f"undeclared={sorted(schema_only)}",
            )
            return

        try:
            values = store.translate_bar_columns(columns, schema, to_wire=False)
            values["underlying"] = event.underlying
            values["minute"] = event.minute
            bar = bar_type(**values)
        except Exception as error:
            self._malformed(event, f"could not decode columns: {error}")
            return

        symbol = None if event.table is BarTable.SPOT else bar.symbol
        key = (event.table, bar.underlying, bar.minute, symbol)
        self._bars[key] = bar
        if self.newest_minute is None or bar.minute > self.newest_minute:
            self.newest_minute = bar.minute
        cutoff = self.newest_minute - timedelta(seconds=BUFFER_HORIZON_SECONDS)
        for candidate_key, candidate in list(self._bars.items()):
            if candidate.minute < cutoff:
                del self._bars[candidate_key]
                self.evicted += 1

    def _malformed(self, event: OptionBar, detail: str) -> None:
        self.malformed += 1
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "malformed md.option_bar was dropped: %s",
            detail,
            table=event.table.value,
        )

    def rows(self, table: BarTable) -> list[Any]:
        """Return a fresh, minute-ascending snapshot for one table."""
        return sorted(
            (bar for key, bar in self._bars.items() if key[0] is table),
            key=lambda bar: (bar.minute, getattr(bar, "symbol", "")),
        )

    def stats(self) -> dict[str, Any]:
        bars = list(self._bars.values())
        per_table = {
            table.value: sum(1 for key in self._bars if key[0] is table)
            for table in BarTable
        }
        minutes = {bar.minute for bar in bars}
        return {
            "bars": len(bars),
            "per_table": per_table,
            "minutes": len(minutes),
            "oldest_minute": min(minutes) if minutes else None,
            "newest_minute": self.newest_minute,
            "skipped": self.skipped,
            "malformed": self.malformed,
            "evicted": self.evicted,
        }

    def attach(
        self, bus: Any, maxsize: int = store.QUEUE_WATERMARK, name: str = "bar-buffer"
    ) -> Any:
        """Attach the lossless bar-event subscription used by the split api."""
        if isinstance(bus, RedisBus):
            self._subscription = bus.subscribe(
                name,
                maxsize=maxsize,
                lossless=True,
                group_start="$",
                event_types=("md.option_bar",),
            )
        else:
            # lossless on BOTH branches, for the same reason. The feed-state caches
            # hold a latest value, so dropping an older one costs nothing. A sealed
            # bar is not a state: drop one and the split read paths carry a hole at
            # the right edge that no later event refills. This branch called
            # `bus.subscribe(name, maxsize=maxsize)`, which defaults to drop-oldest
            # and contradicted this method's own docstring.
            self._subscription = bus.subscribe(name, maxsize=maxsize, lossless=True)
        return self._subscription

    async def run(self) -> None:
        """Drain the attached subscription until cancelled."""
        if self._subscription is None:
            raise RuntimeError("attach() the bar buffer before running it")
        while True:
            self.apply(await self._subscription.queue.get())
