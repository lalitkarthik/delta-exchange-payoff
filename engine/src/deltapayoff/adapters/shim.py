"""**Temporary. #37 deletes this file, and it must not grow a feature.**

The expand half of an expand–contract leaves two forms of the same fact alive at once.
The adapter emits canonical events; the chain cache and the bar writer still consume
`feed.Quote`. This is the one named place that produces the old form, so that deleting it
is a deletion and not an archaeology.

**Why it is fed by the adapter and not subscribed to the bus.** #36 specifies the shim as
a bus subscriber turning events back into quote records. It cannot be, and the reason is
worth writing down because it is #37's problem next: **four things the consumers read
today exist only in the venue's frame and have no field in the catalogue.**

* **`lts`**, the last-trade stamp on `ob_l2` — `bars.tick_from_quote` reads it into the
  quote bars' `last_lts` column.
* **`to[0]`**, turnover — `bars.samples_from_ticker` reads it into the reference bars'
  `turnover` column.
* **`i`**, Delta's `product_id` — `chain.build_leg` reads it into `Leg.product_id`, which
  reaches the browser.
* **the `ticker` frame's own `q` bid and ask** — the quote bars' fallback for a contract
  whose book is silent, and the `from_book` flag that says which of the two a bar came
  from. `md.option_quote` carries no channel, so even emitting one per ticker frame would
  lose the provenance.

A shim that rebuilt a frame from `md.option_quote` and `md.option_reference` would drop
all four **silently**: three columns would fill with nulls and the fourth would quietly
stop producing rows, with every number still plausible. That is the failure this project
keeps refusing, so the frame travels verbatim and the shim is handed it. The catalogue is
not extended here to close the gap, because #35 owns those types and a field added in
passing is how a catalogue stops being an authority.

**#37 must close the four rows above** — by adding optional fields to the catalogue with a
`schema_version` left at 1, since adding a field with a default is a compatible change by
the rule in `docs/design/events.md`, or by deciding in writing that a column is not worth
carrying. Deleting this file without answering them loses data.

The bid and ask are handed in rather than decoded again: the adapter has just read them
out of the frame, and reading Delta's array offsets a second time in a second module is
precisely the hazard `wire.py` exists to concentrate in one file.
"""

from __future__ import annotations

from typing import Any

from ..feed import Quote, VenueMessage


class LegacyQuoteBridge:
    """Republishes one venue frame as today's `feed.Quote`. Nothing else, ever."""

    __slots__ = ("bus", "republished")

    def __init__(self, bus: Any) -> None:
        #: The bus the chain cache and the bar writer subscribe to. Not the bus the
        #: adapter publishes events on: keeping the two apart is what lets the old
        #: consumers stay untouched, since a `Quote` and an `Event` share no attribute
        #: and either consumer would raise on the other's records.
        self.bus = bus
        #: What crossed the bridge. Should fall to zero when #37 lands, which is the
        #: number that proves the bridge is unused before it is deleted.
        self.republished = 0

    def republish(
        self, message: VenueMessage, *, bid: float | None, ask: float | None
    ) -> None:
        """Publish the old record. **Never raises into the socket reader.**

        The adapter calls this between socket reads, so anything that could suspend or
        throw here would suspend or end the connection.
        """
        self.bus.publish(
            Quote(
                symbol=message.symbol,
                channel=message.channel,
                bid=bid,
                ask=ask,
                received_at=message.received_at,
                frame=message.frame,
            )
        )
        self.republished += 1
