"""The key an event belongs on, and the fields it becomes. #58's decision, as code.

`docs/design/cloud/nomenclature.md` is the authority and this file implements it, so
every rule below is a citation rather than an invention:

* **§1 the grammar** — `{event_type}:{VENUE}[:{UNDERLYING}]`, fourteen keys for one
  venue and two underlyings, and **no discovery**: `stream_names` builds the list from
  configuration because `XREAD` takes an explicit key list and has no wildcard, so a
  stream discovered late is a stream that was silently not read. That is #51 restaged.
* **§2 the envelope** — flat, one Redis field per envelope key, and the type's own keys as
  one JSON object in `payload`. Absent is **omitted, never spelled**: a Redis field is a
  binary string with no null in it, and `null` is not `0` is this catalogue's oldest rule,
  so the absent values live inside `payload`, where JSON has a real null.
* **§2 rule 3** — reassembling the envelope with the payload and calling `parse_event` is
  the only decode there is. `UnknownEventType`, `UnknownSchemaVersion`, `extra="forbid"`
  and the non-finite refusal all keep holding the line over the wire with **no second
  decode path** written to hold it in.
* **§2 rule 6** — a `type` on the wire that disagrees with the stream it arrived on is an
  error, not a preference. `decode(fields, stream=...)` is where that is refused.

**No Redis import.** Encoding is a pure function of an event and decoding a pure function
of a mapping, which is what lets the entire grammar be proven with no connection, no
container and no clock. `redis_bus.py` is the half that dials.

**The venue for an event that names none.** §1 draws `{VENUE}` from `Instrument.venue` or
the event's `adapter`. `md.index_quote` and `computed.chain` carry neither — the first
names an underlying and the second an expiry — so the venue comes from the publisher's
**configuration**, as the `venue` argument. `source` is deliberately not consulted: it
names the component that built the event, so `computed.chain` would publish to
`computed.chain:CHAIN-CACHE:BTC` and a reader taking the configured list would never
see it. A publisher configured with no venue at all is refused loudly rather than allowed
to invent one, because a mis-keyed stream is a stream nobody reads and nothing says so.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .envelope import Event, parse_event
from .instrument import Instrument

#: The seven envelope keys, **in the order §2 tables them**, plus `venue_symbol`, which
#: travels beside `instrument` because the canonical string deliberately omits it.
#:
#: Order is not tidiness. Redis "stores the field-value pairs in the same order you
#: provide them", and a stream whose entries repeat one field set in one order is the
#: case its listpack compresses against the node's master entry.
ENVELOPE_FIELDS = (
    "type",
    "event_id",
    "schema_version",
    "source",
    "ts_received",
    "ts_venue",
    "instrument",
    "venue_symbol",
)

#: Event types whose stream carries an underlying, i.e. `{type}:{VENUE}:{UNDER}`.
#: The hot ones plus the two that name an underlying in their payload.
WITH_UNDERLYING = (
    "md.option_quote",
    "md.option_reference",
    "md.index_quote",
    "md.option_bar",
    "computed.chain",
)

#: Event types whose stream is per venue and carries no underlying. One socket per venue
#: for the first two; the third is **inbound**, and per venue because a DELTA feed must
#: never read an NSE command.
WITH_VENUE_ONLY = ("feed.connection", "heartbeat", "control.command")

#: The one type whose stream carries neither a venue nor an underlying. `adapter` is
#: nullable and no reader wants a subset: the logger and the Discord consumer take all
#: of them.
UNSCOPED = ("alert",)


class StreamMismatch(ValueError):
    """An entry whose `type` field disagrees with the stream it arrived on.

    §2 rule 6. The stream name is a claim about its contents; a consumer that trusts it
    must be able to. Carries both names so the log record says what was actually there.
    """

    def __init__(self, stream: str, type_name: str) -> None:
        self.stream = stream
        self.type_name = type_name
        super().__init__(
            f"an entry of type {type_name!r} arrived on {stream!r}, which claims "
            f"{stream_type(stream)!r}"
        )


def stream_type(stream: str) -> str:
    """The `{event_type}` section of a key: the first colon-separated section."""
    return stream.split(":")[0]


def stream_name(event: Event, *, venue: str = "") -> str:
    """The one key `event` belongs on.

    `venue` is the configured venue, used by the two events that name neither an
    instrument nor an adapter; see this module's docstring for why it is a parameter and
    not a constant, and why `source` is not consulted in its place.
    """
    if event.type in UNSCOPED:
        return event.type

    resolved = _venue_of(event, venue)
    if event.type in WITH_VENUE_ONLY:
        return f"{event.type}:{resolved}"
    return f"{event.type}:{resolved}:{_underlying_of(event)}"


def stream_names(
    *, venues: Iterable[str], underlyings: Iterable[str]
) -> tuple[str, ...]:
    """Every key this configuration can produce or read. **Sorted, and never scanned.**

    Fourteen for one venue and two underlyings. A reader takes this list whole; the
    underlyings in it are the same configured set the feed is given, which is what makes
    "what does this service read" one answer rather than two.
    """
    names: list[str] = list(UNSCOPED)
    for venue in venues:
        upper = venue.upper()
        names += [f"{t}:{upper}" for t in WITH_VENUE_ONLY]
        for underlying in underlyings:
            names += [
                f"{t}:{upper}:{underlying.upper()}" for t in WITH_UNDERLYING
            ]
    return tuple(sorted(names))


def encode(event: Event) -> dict[str, bytes]:
    """One event as the field mapping `XADD` takes. #58's decision B, envelope-flat JSON.

    Byte-for-byte the encoder `tools/measure_payload_size.py` sized every #58 number
    through; `tests/test_redis_wire.py` pins that the two have not drifted.
    """
    fields: dict[str, bytes] = {
        "type": event.type.encode(),
        "event_id": event.event_id.encode(),
        "schema_version": str(event.schema_version).encode(),
        "source": event.source.encode(),
        "ts_received": event.ts_received.isoformat().encode(),
    }
    if event.ts_venue is not None:
        fields["ts_venue"] = event.ts_venue.isoformat().encode()
    if event.instrument is not None:
        fields["instrument"] = event.instrument.canonical().encode()
        if event.instrument.venue_symbol:
            fields["venue_symbol"] = event.instrument.venue_symbol.encode()
    fields["payload"] = json.dumps(
        _payload(event), separators=(",", ":")
    ).encode()
    return fields


def decode(fields: Mapping[Any, Any], *, stream: str | None = None) -> Event:
    """A stream entry back into the typed event that was published.

    `fields` is what redis-py hands back — bytes keys and bytes values on a connection
    that does not decode, or `str` on one that does; both are accepted, because which one
    a caller built is not a property of the wire.

    Raises `StreamMismatch` when `stream` is given and disagrees, and then whatever
    `parse_event` raises: the refusals are the catalogue's and are not re-implemented.
    """
    flat = {_text(key): value for key, value in fields.items()}
    payload = json.loads(flat.pop("payload", b"{}"))
    data: dict[str, Any] = {key: _text(value) for key, value in flat.items()}
    data.update(payload)

    type_name = data.get("type", "")
    if stream is not None and type_name != stream_type(stream):
        raise StreamMismatch(stream, type_name)

    if "schema_version" in data:
        # It went out as text because a Redis field is bytes; the version check in
        # `parse_event` is an integer comparison and `"1" != 1`.
        data["schema_version"] = int(data["schema_version"])
    if "instrument" in data:
        data["instrument"] = Instrument.from_canonical(
            data["instrument"], venue_symbol=data.pop("venue_symbol", None)
        )
    return parse_event(data)


def _payload(event: Event) -> dict[str, Any]:
    """The type's own keys, JSON-shaped. `null` stays `null`."""
    dumped = event.model_dump(mode="json")
    return {key: value for key, value in dumped.items() if key not in ENVELOPE_FIELDS}


def _text(value: Any) -> Any:
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


def _venue_of(event: Event, configured: str) -> str:
    if event.instrument is not None:
        return event.instrument.venue.upper()
    adapter = getattr(event, "adapter", None)
    if adapter:
        return str(adapter).upper()
    if not configured:
        raise ValueError(
            f"{event.type!r} names neither an instrument nor an adapter, so its venue "
            "comes from configuration; none was given"
        )
    return configured.upper()


def _underlying_of(event: Event) -> str:
    if event.instrument is not None:
        return event.instrument.underlying.upper()
    return str(getattr(event, "underlying", "")).upper()
