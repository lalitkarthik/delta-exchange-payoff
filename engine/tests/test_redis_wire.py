"""The grammar and the envelope, as bytes on a Redis stream.

`docs/design/cloud/nomenclature.md` is the authority for both, and #58's decision record
is why. This file pins the two halves that a producer and a consumer must agree on before
either can be written: the **key** an event belongs on, and the **fields** it becomes.

Nothing here opens a connection. The encoder is a pure function of an event and the
decoder is a pure function of a field mapping, which is what lets the whole grammar be
proven without Redis, Docker or a clock.

**The last test in this file is the anti-drift one.** `tools/measure_payload_size.py`
carries the encoder every byte-size number in #58 was measured through. If the engine's
encoder and that one ever disagree, every figure in the decision record starts describing
a wire we no longer write, silently. So they are compared byte for byte.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from deltapayoff.events import (
    BarTable,
    Instrument,
    OptionBar,
    OptionQuote,
    Right,
    registry,
)
from deltapayoff.events.redis_wire import (
    ENVELOPE_FIELDS,
    StreamMismatch,
    decode,
    encode,
    stream_name,
    stream_names,
    stream_type,
)

# The ten built samples, one per catalogued type, live beside the catalogue's own tests.
# Imported rather than restated so an eleventh event type has exactly one place to be
# sampled.
from test_events import SAMPLES

TS_RECEIVED = datetime(2026, 6, 1, 12, 0, 0, 212600, tzinfo=timezone.utc)


# ------------------------------------------------------------------ the round trip


@pytest.mark.parametrize("type_name", sorted(registry()))
def test_every_catalogued_event_round_trips_over_the_wire(type_name: str) -> None:
    """The whole point of the envelope: nine types, one decode path.

    `parse_event` is the only decoder there is, and reassembling the flat envelope with
    the JSON payload has to hand it exactly the mapping it already took.
    """
    event = SAMPLES[type_name]
    assert decode(encode(event)) == event


def test_the_envelope_is_flat_and_the_payload_is_one_json_object() -> None:
    """Decision B: seven envelope fields as Redis fields, everything else as JSON."""
    fields = encode(SAMPLES["md.option_quote"])

    assert fields["type"] == b"md.option_quote"
    assert fields["schema_version"] == b"1"
    assert fields["instrument"] == b"DELTA-BTC-20260627-60000-C-USD"
    assert fields["venue_symbol"] == b"C-BTC-60000-270626"
    payload = json.loads(fields["payload"])
    assert payload["bid"] == 1250.0
    # `null` is not `0` and it is not the string "None" either — it is a JSON null,
    # inside the one field JSON has a null in.
    assert payload["ask"] is None
    assert set(fields) - {"payload"} <= set(ENVELOPE_FIELDS) | {"venue_symbol"}
    assert "type" not in payload and "event_id" not in payload


def test_absent_is_omitted_and_never_spelled() -> None:
    """Rule 1. cryptofeed writes the string `"None"`; we write no field at all."""
    bare = OptionQuote(
        source="delta",
        ts_received=TS_RECEIVED,
        instrument=Instrument(
            venue="DELTA",
            underlying="BTC",
            expiry=date(2026, 6, 27),
            strike=Decimal("60000"),
            right=Right.CALL,
        ),
    )
    fields = encode(bare)

    assert "ts_venue" not in fields, "a venue stamp nobody gave must not be spelled"
    assert "venue_symbol" not in fields, "an instrument parsed back carries none"
    assert decode(fields) == bare


def test_the_field_order_is_the_tabled_one() -> None:
    """Rule 5, and it is a compression argument, not a tidiness one: a stream whose
    entries repeat one field set in one order is the case a listpack compresses against
    its master entry."""
    fields = encode(SAMPLES["md.option_quote"])
    assert list(fields) == [
        "type",
        "event_id",
        "schema_version",
        "source",
        "ts_received",
        "ts_venue",
        "instrument",
        "venue_symbol",
        "payload",
    ]


def test_a_payload_whose_type_disagrees_with_its_stream_is_an_error() -> None:
    """Rule 6. The stream name is a claim about its contents, and a consumer that trusts
    it must be able to."""
    fields = encode(SAMPLES["md.option_quote"])
    with pytest.raises(StreamMismatch):
        decode(fields, stream="md.option_reference:DELTA:BTC")


# ------------------------------------------------------------------ the key grammar


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ("alert", "alert"),
        ("heartbeat:DELTA", "heartbeat"),
        ("md.option_quote:DELTA:BTC", "md.option_quote"),
    ],
)
def test_stream_type_reads_the_event_type_from_the_first_section(
    stream: str, expected: str
) -> None:
    assert stream_type(stream) == expected


def test_stream_names_do_not_include_an_environment_section() -> None:
    keys = stream_names(venues=("DELTA",), underlyings=("BTC", "ETH"))

    assert len(keys) == 15
    assert not any(key.startswith(("dev:", "prod:")) for key in keys)
    assert all(key.count(":") <= 2 for key in keys)


def test_stream_mismatch_reads_the_type_from_the_first_stream_section() -> None:
    fields = encode(SAMPLES["md.option_quote"])

    with pytest.raises(StreamMismatch):
        decode(fields, stream="heartbeat:md.option_quote")


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("md.option_quote", "md.option_quote:DELTA:BTC"),
        ("md.option_reference", "md.option_reference:DELTA:BTC"),
        ("md.index_quote", "md.index_quote:DELTA:BTC"),
        ("md.option_bar", "md.option_bar:DELTA:BTC"),
        ("computed.chain", "computed.chain:DELTA:BTC"),
        ("feed.connection", "feed.connection:DELTA"),
        ("heartbeat", "heartbeat:DELTA"),
        ("alert", "alert"),
        ("control.command", "control.command:DELTA"),
        ("store.state", "store.state:DELTA"),
    ],
)
def test_each_event_lands_on_the_key_the_nomenclature_names(
    type_name: str, expected: str
) -> None:
    """§1's table of nine, one row per assertion.

    The venue and the underlying are upper case wherever they were drawn from — the
    samples spell `venue` and `adapter` two different ways. `venue="DELTA"` is the
    publisher's configuration, and it is what answers for `md.index_quote` and
    `computed.chain`, which name no venue of their own.
    """
    assert stream_name(SAMPLES[type_name], venue="DELTA") == expected


def test_a_spot_bar_without_an_instrument_uses_its_payload_underlying() -> None:
    event = OptionBar(
        source="bar-writer",
        instrument=None,
        underlying="BTC",
        table=BarTable.SPOT,
        minute=TS_RECEIVED,
        ts_received=TS_RECEIVED,
        columns={},
    )

    assert stream_name(event, venue="DELTA") == "md.option_bar:DELTA:BTC"


def test_an_event_naming_no_venue_is_refused_rather_than_keyed_by_its_source() -> None:
    """`computed.chain`'s `source` is `chain-cache`, the component that built it.

    Keying on it would publish to `computed.chain:CHAIN-CACHE:BTC` — a stream no
    configured reader lists, and therefore a stream nobody reads with nothing saying so.
    """
    with pytest.raises(ValueError, match="comes from configuration"):
        stream_name(SAMPLES["computed.chain"])


def test_the_configured_key_list_is_the_fourteen_and_is_never_discovered() -> None:
    """§1: a reader builds its key list from configuration and never from the keyspace.

    Fourteen keys for one venue and two underlyings — five per-underlying types times
    two, three per-venue types, and `alert`.
    """
    keys = stream_names(venues=("DELTA",), underlyings=("BTC", "ETH"))

    assert len(keys) == 15
    assert "md.option_quote:DELTA:ETH" in keys
    assert "alert" in keys
    assert "control.command:DELTA" in keys
    assert list(keys) == sorted(keys), "a stable order: two services build one list"
    assert not any(key.startswith(("dev:", "prod:")) for key in keys)
    assert all(key.count(":") <= 2 for key in keys)


# ------------------------------------------------------------------ no drift


def test_the_encoder_is_byte_for_byte_the_one_the_sizes_were_measured_through() -> None:
    """#58's 1,051.5 MB and every byte count beside it came out of
    `tools/measure_payload_size.encode_json_envelope_flat`. Two encoders that drift make
    that number a description of a wire nobody writes.
    """
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parents[2] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    measure = pytest.importorskip(
        "measure_payload_size", reason="the #58 sizing tool needs msgpack and protobuf"
    )

    for type_name in ("md.option_quote", "md.option_reference", "md.index_quote"):
        event = SAMPLES[type_name]
        theirs = measure.encode_json_envelope_flat(event)
        ours = encode(event)
        assert dict(ours) == dict(theirs), type_name
