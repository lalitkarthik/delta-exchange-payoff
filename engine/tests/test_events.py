"""The envelope, the registry, the instrument, and the bus interface.

**The catalogue is `docs/design/events.md` and it is the authority.** One test here parses
that document's section headings and asserts the registry holds exactly the same ten
names, so a type added in code without a paragraph, or a paragraph without a type, fails
the suite rather than drifting quietly.

Nothing in this file touches a socket, a disk or a clock. Both timestamps are passed in,
because a producer knows when a frame arrived and this package does not.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from deltapayoff.events import (
    Alert,
    BarTable,
    Bus,
    ChainLeg,
    ChainStrike,
    ComputedChain,
    ConnectionState,
    ControlCommand,
    Event,
    FeedConnection,
    Heartbeat,
    IndexQuote,
    Instrument,
    InstrumentParseError,
    OptionBar,
    OptionQuote,
    OptionReference,
    Right,
    StoreState,
    UnknownEventType,
    UnknownSchemaVersion,
    known_schema_version,
    parse_event,
    registry,
)
from deltapayoff.fanout import FanOut

REPO = Path(__file__).resolve().parents[2]
EVENTS_DOC = REPO / "docs" / "design" / "events.md"
EVENTS_LLD = REPO / "docs" / "design" / "lld" / "events.md"

TS_VENUE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
#: 212.6 ms later, which is the `measured` p50 arrival lag on `ob_l2` quoted in the
#: catalogue. Nothing depends on the value; it is written this way so a reader of a
#: failure sees a plausible lag rather than a round number.
TS_RECEIVED = datetime(2026, 6, 1, 12, 0, 0, 212600, tzinfo=timezone.utc)

INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 6, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270626",
)

#: The envelope every sample shares. `source` is deliberately not in here: it names the
#: component that built the event, and six of the nine are not built by the adapter.
ENVELOPE = {
    "event_id": "0f7c2a9e5b1d4c8fa3e6b0d2c4f18a97",
    "ts_venue": TS_VENUE,
    "ts_received": TS_RECEIVED,
}

#: One built instance per catalogued type. Keyed by the type name so the round-trip test
#: can be parametrised over the registry itself and fail loudly on a type nobody sampled.
SAMPLES: dict[str, Event] = {
    "md.option_quote": OptionQuote(
        **ENVELOPE,
        source="delta",
        instrument=INSTRUMENT,
        bid=1250.0,
        bid_size=4.0,
        ask=None,
        ask_size=None,
    ),
    "md.option_reference": OptionReference(
        **ENVELOPE,
        source="delta",
        instrument=INSTRUMENT,
        mark=1275.5,
        last_price=1270.0,
        oi=812.0,
        oi_value_usd=None,
        oi_change_usd_6h=-4210.0,
        tick_size=0.5,
        bid_iv=0.5123,
        ask_iv=0.5311,
        mark_iv=0.5217,
        delta=0.4812,
        gamma=0.000031,
        theta=-18.4,
        vega=61.2,
        rho=7.1,
    ),
    "md.index_quote": IndexQuote(
        **ENVELOPE, source="delta", underlying="BTC", spot=77568.2
    ),
    "md.option_bar": OptionBar(
        **ENVELOPE,
        source="bar-writer",
        instrument=INSTRUMENT,
        table=BarTable.QUOTE,
        minute=TS_VENUE,
        columns={"mid_open": 1250.0, "mid_close": 1262.5, "mid_ticks": 61},
    ),
    "computed.chain": ComputedChain(
        **ENVELOPE,
        source="chain-cache",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        fetched_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        forward=77_612.4,
        discount=0.9987,
        forward_method="fitted",
        years_to_expiry=0.0548,
        model_version="F1+assumed-6.5 / S1-newton / ACT365 / mid-OTM",
        solver="S1-newton",
        strikes=(
            ChainStrike(
                strike=Decimal("60000"),
                call=ChainLeg(
                    symbol="C-BTC-60000-270626",
                    iv=0.5217,
                    iv_leg="put",
                    delta=0.4812,
                    gamma=0.000031,
                    vega=61.2,
                    theta=-18.4,
                    rho=7.1,
                ),
            ),
            ChainStrike(
                strike=Decimal("62000"),
                put=ChainLeg(
                    symbol="P-BTC-62000-270626", iv=None, iv_reason="vega too small"
                ),
            ),
        ),
    ),
    "feed.connection": FeedConnection(
        **ENVELOPE,
        source="controller",
        adapter="delta",
        from_state=ConnectionState.CONNECTING,
        to_state=ConnectionState.CONNECTED,
        reason="socket open and every subscription replayed",
    ),
    "heartbeat": Heartbeat(
        **ENVELOPE,
        source="controller",
        adapter="delta",
        state=ConnectionState.CONNECTED,
        last_message_age_seconds=0.4,
    ),
    "alert": Alert(
        **ENVELOPE,
        source="controller",
        severity="warning",
        code="reconnect_budget_low",
        detail="two attempts left of five",
        adapter="delta",
    ),
    "control.command": ControlCommand(
        **ENVELOPE, source="operator", adapter="delta", command="pause"
    ),
    "store.state": StoreState(
        **ENVELOPE,
        source="store",
        recording=True,
        buffered_rows=12,
        rows_written=345,
        replay_gap_entries=2,
        already_flushed=3,
        flush_errors=1,
        generation=17,
    ),
}


def documented_event_types() -> set[str]:
    """The nine names, read out of the catalogue's own section headings.

    Keyed off the code span in an `###` heading rather than a line number, so
    reformatting the document, reordering the sections or rewriting the prose after the
    em dash does not move the goalposts.
    """
    text = EVENTS_DOC.read_text(encoding="utf-8")
    return set(re.findall(r"^###\s+`([^`]+)`", text, flags=re.MULTILINE))


# --------------------------------------------------------------------------- instrument


def test_the_canonical_string_is_the_one_spelling() -> None:
    assert INSTRUMENT.canonical() == "DELTA-BTC-20260627-60000-C-USD"


def test_the_instrument_round_trips_through_its_canonical_string() -> None:
    """The canonical string does not carry the venue's own symbol, so a parse that is
    handed one back reproduces the whole record."""
    parsed = Instrument.from_canonical(
        INSTRUMENT.canonical(), venue_symbol=INSTRUMENT.venue_symbol
    )
    assert parsed == INSTRUMENT


def test_a_parsed_instrument_without_a_venue_symbol_equals_one_built_without_it() -> None:
    bare = Instrument(
        venue="DELTA",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        strike=Decimal("60000"),
        right=Right.CALL,
    )
    assert Instrument.from_canonical(bare.canonical()) == bare


def test_quote_currency_and_settlement_currency_default_to_usd() -> None:
    """The sensible default `docs/design/events.md` §Versioning permits: adding an
    optional field with a default is compatible, so an instrument built exactly as
    every caller built one before I1 still gets a currency rather than `None`."""
    bare = Instrument(
        venue="DELTA",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        strike=Decimal("60000"),
        right=Right.CALL,
    )
    assert bare.quote_currency == "USD"
    assert bare.settlement_currency == "USD"


def test_settlement_currency_does_not_travel_in_the_canonical_string() -> None:
    """Only the quote currency is the canonical string's last token — #57 fixed the
    six-part form as `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY` with the **quote**
    currency alone. A round trip through `from_canonical` therefore cannot recover a
    `settlement_currency` that was set to something other than the class default; it
    comes back at the default instead, which is documented rather than silently lost."""
    instrument = Instrument(
        venue="DELTA",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        strike=Decimal("60000"),
        right=Right.CALL,
        quote_currency="USD",
        settlement_currency="USD",
    )
    assert instrument.canonical() == "DELTA-BTC-20260627-60000-C-USD"
    parsed = Instrument.from_canonical(instrument.canonical())
    assert parsed.quote_currency == "USD"
    assert parsed.settlement_currency == "USD"


def test_the_strike_parses_as_a_decimal_and_not_a_float() -> None:
    """`60000`, not `60000.0`. A float strike would print a trailing zero into cache
    keys and log lines and would not compare equal to the venue's own integer."""
    parsed = Instrument.from_canonical("DELTA-BTC-20260627-60000-C-USD")
    assert isinstance(parsed.strike, Decimal)
    assert parsed.strike == Decimal("60000")
    assert str(parsed.strike) == "60000"


@pytest.mark.parametrize(
    ("strike", "printed"),
    [
        (Decimal("60000"), "60000"),
        (Decimal("60000.0"), "60000"),
        (Decimal("60000.00"), "60000"),
        (Decimal("1234.5"), "1234.5"),
        (Decimal("1234.50"), "1234.5"),
        (Decimal("0.5"), "0.5"),
        (Decimal("0"), "0"),
    ],
)
def test_the_strike_prints_without_trailing_zeros(strike: Decimal, printed: str) -> None:
    """A fractional strike keeps its fraction; an integral one loses its point. Neither
    is allowed to come out in exponent form, which is what `Decimal.normalize` alone
    would do to `60000`."""
    instrument = Instrument(
        venue="DELTA",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        strike=strike,
        right=Right.PUT,
    )
    assert instrument.canonical() == f"DELTA-BTC-20260627-{printed}-P-USD"
    assert Instrument.from_canonical(instrument.canonical()).strike == strike


def test_a_put_prints_p() -> None:
    instrument = Instrument(
        venue="DELTA",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        strike=Decimal("60000"),
        right=Right.PUT,
    )
    assert instrument.canonical() == "DELTA-BTC-20260627-60000-P-USD"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "DELTA-BTC-20260627-60000",
        # The pre-I1 five-part form. #57 fixed the six-part shape and #60 (I1) makes
        # it real: a string with no currency token is refused loudly rather than
        # defaulted, so a caller cannot silently keep addressing contracts the old way.
        "DELTA-BTC-20260627-60000-C",
        "DELTA-BTC-20260627-60000-C-USD-EXTRA",
        "DELTA-BTC-2026627-60000-C-USD",
        "DELTA-BTC-20260627-sixty-C-USD",
        "DELTA-BTC-20260627-60000-X-USD",
        "DELTA-BTC-20260632-60000-C-USD",
        # The six-part shape with an unreadable currency token: too short, lower
        # case, and not alphabetic, respectively.
        "DELTA-BTC-20260627-60000-C-US",
        "DELTA-BTC-20260627-60000-C-usd",
        "DELTA-BTC-20260627-60000-C-12A",
    ],
)
def test_an_unparseable_canonical_string_raises_rather_than_guessing(text: str) -> None:
    with pytest.raises(InstrumentParseError):
        Instrument.from_canonical(text)


def test_an_instrument_is_frozen() -> None:
    with pytest.raises(ValidationError):
        INSTRUMENT.strike = Decimal("62000")  # type: ignore[misc]


# ---------------------------------------------------------------------------- envelope


def test_every_catalogued_type_has_a_sample() -> None:
    """Guards the parametrised tests below: a new event class with no sample would
    otherwise silently skip every round-trip assertion."""
    assert set(SAMPLES) == set(registry())


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_every_event_round_trips_through_json(type_name: str) -> None:
    event = SAMPLES[type_name]
    parsed = parse_event(event.model_dump_json())
    assert type(parsed) is type(event)
    assert parsed == event


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_every_event_carries_the_envelope(type_name: str) -> None:
    event = SAMPLES[type_name]
    assert event.type == type_name
    assert event.schema_version == (2 if type_name == "computed.chain" else 1)
    assert event.ts_venue == TS_VENUE
    assert event.ts_received == TS_RECEIVED
    assert registry()[type_name] is type(event)


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_a_parsed_event_cannot_be_mutated(type_name: str) -> None:
    parsed = parse_event(SAMPLES[type_name].model_dump_json())
    with pytest.raises(ValidationError):
        parsed.source = "somebody else"  # type: ignore[misc]


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_an_event_forbids_a_field_nobody_declared(type_name: str) -> None:
    payload = SAMPLES[type_name].model_dump(mode="json")
    payload["surprise"] = 1
    with pytest.raises(ValidationError):
        parse_event(payload)


def test_parsing_an_unregistered_type_raises_the_named_error() -> None:
    """The difference between a typo becoming an error and a typo becoming a silently
    ignored message."""
    payload = SAMPLES["md.option_quote"].model_dump(mode="json")
    payload["type"] = "md.option_qoute"
    with pytest.raises(UnknownEventType) as caught:
        parse_event(payload)
    assert "md.option_qoute" in str(caught.value)


def test_parsing_a_payload_with_no_type_at_all_raises_the_same_error() -> None:
    payload = SAMPLES["md.option_quote"].model_dump(mode="json")
    del payload["type"]
    with pytest.raises(UnknownEventType):
        parse_event(payload)


def test_parsing_a_version_this_build_does_not_know_raises() -> None:
    """**#35 left this out on purpose and #37 is the consumer that fills it in.**

    A version is bumped when a field *changes meaning*, so a payload at an unknown version
    is one whose fields this build would read with the wrong meaning, silently. That is
    the same objection to plausible-and-wrong that makes an unregistered `type` raise, and
    `docs/design/events.md` says so: a consumer that does not know a version it receives
    must fail loudly rather than guess.
    """
    payload = SAMPLES["md.option_quote"].model_dump(mode="json")
    payload["schema_version"] = 2

    with pytest.raises(UnknownSchemaVersion) as caught:
        parse_event(payload)

    message = str(caught.value)
    assert "md.option_quote" in message
    assert "2" in message and "1" in message, "the error names neither version"


@pytest.mark.parametrize("version", [0, -1, "1", 1.0, True, None])
def test_a_version_that_is_not_the_known_integer_raises(version) -> None:
    """`True` is in this list because `bool` is an `int` in Python and `True == 1`, so a
    payload spelling its version `true` would otherwise be accepted as version 1."""
    payload = SAMPLES["md.option_quote"].model_dump(mode="json")
    payload["schema_version"] = version

    with pytest.raises(UnknownSchemaVersion):
        parse_event(payload)


def test_a_payload_omitting_the_version_takes_the_class_default() -> None:
    """An older producer that never wrote the field is not an unknown version; it is a
    producer at the version this class declares, which is what the default means."""
    payload = SAMPLES["md.option_quote"].model_dump(mode="json")
    del payload["schema_version"]

    assert parse_event(payload).schema_version == 1


def test_computed_chain_version_one_is_unknown_to_the_v2_reader() -> None:
    payload = SAMPLES["computed.chain"].model_dump(mode="json")
    payload["schema_version"] = 1

    with pytest.raises(UnknownSchemaVersion, match="computed.chain.*2"):
        parse_event(payload)


def test_computed_chain_fetched_at_must_be_aware() -> None:
    payload = SAMPLES["computed.chain"].model_dump()
    payload["fetched_at"] = datetime(2026, 6, 1, 12, 0, 0)

    with pytest.raises(ValidationError, match="fetched_at.*timezone"):
        ComputedChain.model_validate(payload)


def test_control_command_defaults_to_feed_and_store_reconnect_is_invalid() -> None:
    command = ControlCommand(
        **ENVELOPE, source="operator", adapter="delta", command="pause"
    )
    assert command.target == "feed"
    with pytest.raises(ValidationError, match="store.*reconnect|reconnect.*store"):
        ControlCommand(
            **ENVELOPE,
            source="operator",
            adapter="delta",
            command="reconnect",
            target="store",
        )


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_every_type_has_the_catalogued_schema_version(type_name: str) -> None:
    """Only `computed.chain` has left version 1; the other types remain compatible."""
    expected = 2 if type_name == "computed.chain" else 1
    assert known_schema_version(registry()[type_name]) == expected


def test_parse_accepts_the_bytes_a_transport_would_hand_it() -> None:
    event = SAMPLES["md.option_quote"]
    assert parse_event(event.model_dump_json().encode("utf-8")) == event


def test_registering_a_type_twice_raises() -> None:
    """Two classes under one name would make `parse_event` depend on import order."""
    from deltapayoff.events.envelope import register

    with pytest.raises(ValueError):

        @register
        class Duplicate(OptionQuote):
            type: str = "md.option_quote"


# ----------------------------------------------------------------- the code and the doc


def test_the_registry_holds_exactly_the_types_the_catalogue_names() -> None:
    """One test, so the document and the code cannot drift apart silently."""
    documented = documented_event_types()
    assert len(documented) == 10, (
        f"parsed {len(documented)} type headings out of {EVENTS_DOC}; "
        "the heading shape this test keys off has changed"
    )
    assert set(registry()) == documented


def test_the_events_low_level_design_exists_and_the_index_links_it() -> None:
    """The index is a map and a row saying "not yet written" beside a written design is
    the kind of quiet staleness the folder's own rules refuse."""
    assert EVENTS_LLD.is_file()
    index = (EVENTS_LLD.parent / "index.md").read_text(encoding="utf-8")
    events_row = [line for line in index.splitlines() if line.startswith("| Events")]
    assert len(events_row) == 1
    assert "(events.md)" in events_row[0]


def documented_payload_fields(type_name: str) -> set[str]:
    """The field names the catalogue lists in one event's **Payload** bullet.

    Read from the first sentence of that bullet, with parenthetical asides stripped
    first — they carry code spans that are values rather than fields, such as
    `command`'s `pause`, `resume`, `reconnect`.
    """
    text = EVENTS_DOC.read_text(encoding="utf-8")
    section = re.search(
        rf"^###\s+`{re.escape(type_name)}`(.*?)(?=^###\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section is not None, f"no section for {type_name} in {EVENTS_DOC}"
    bullet = re.search(
        r"\*\*Payload\*\*(.*?)(?:\.\s|\.$)", section.group(1), flags=re.DOTALL
    )
    assert bullet is not None, f"no Payload bullet for {type_name}"
    without_asides = re.sub(r"\([^)]*\)", " ", bullet.group(1))
    # `[a-z][a-z0-9_]*` and not `[a-z_]+`: the latter silently skips every code span
    # carrying a digit, which is exactly how `oi_change_usd_6h` went unchecked.
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", without_asides))


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_every_field_the_catalogue_names_exists_on_the_class(type_name: str) -> None:
    """`catalogue.py` claims to be the document field for field. This is what checks it.

    A subset rather than an equality: the code carries two fields the document describes
    in prose instead of naming in a code span — `md.option_bar`'s `columns` ("that
    table's columns") and `computed.chain`'s `solver` ("the solver that produced it") —
    so equality would fail on a difference that is not drift. The direction that matters
    is this one: a field renamed or dropped in code while the document still names it.
    """
    cls = registry()[type_name]
    available = _nested_model_fields(cls)
    documented = documented_payload_fields(type_name)
    assert documented, f"parsed no payload fields for {type_name}"
    assert documented <= available, (
        f"{type_name}: {sorted(documented - available)} named in {EVENTS_DOC.name} "
        f"but absent from {cls.__name__}"
    )


def _nested_model_fields(model: type[BaseModel]) -> set[str]:
    available = set(model.model_fields)
    for field in model.model_fields.values():
        annotation = field.annotation
        candidates = getattr(annotation, "__args__", ()) or ()
        if isinstance(annotation, type):
            candidates += (annotation,)
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                available |= _nested_model_fields(candidate)
    return available


# ------------------------------------------------------------------- the wire is numbers


@pytest.mark.parametrize("type_name", sorted(SAMPLES))
def test_no_number_reaches_the_wire_as_a_string(type_name: str) -> None:
    """**Every decimal is a JSON number or `null`, never a string.** Pydantic quotes a
    `Decimal` by default, so a strike would otherwise ship as `"60000"` — asserted on the
    JSON text, because the parsed object reads a quoted number back happily and cannot
    tell the difference."""
    text = SAMPLES[type_name].model_dump_json()
    assert not re.search(r'"strike":\s*"', text), text


def test_an_integral_strike_ships_as_an_integer_and_reads_back_exact() -> None:
    payload = json.loads(SAMPLES["md.option_quote"].model_dump_json())
    assert payload["instrument"]["strike"] == 60000
    assert isinstance(payload["instrument"]["strike"], int)
    parsed = parse_event(SAMPLES["md.option_quote"].model_dump_json())
    assert parsed.instrument is not None
    assert str(parsed.instrument.strike) == "60000"


def test_a_fractional_strike_ships_as_a_number_and_reads_back_exact() -> None:
    instrument = Instrument(
        venue="DELTA",
        underlying="BTC",
        expiry=date(2026, 6, 27),
        strike=Decimal("1234.5"),
        right=Right.PUT,
    )
    payload = json.loads(instrument.model_dump_json())
    assert payload["strike"] == 1234.5
    assert not isinstance(payload["strike"], str)
    assert Instrument.model_validate(payload).strike == Decimal("1234.5")


def test_a_chain_strike_also_ships_its_strike_as_a_number() -> None:
    payload = json.loads(SAMPLES["computed.chain"].model_dump_json())
    assert payload["strikes"][0]["strike"] == 60000
    assert not isinstance(payload["strikes"][0]["strike"], str)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_price_is_refused_rather_than_becoming_an_absent_quote(
    bad: float,
) -> None:
    """`NaN` serialises to JSON `null` by default, which would make a garbage number
    indistinguishable from a quote that was never there."""
    with pytest.raises(ValidationError):
        OptionQuote(**ENVELOPE, source="delta", instrument=INSTRUMENT, bid=bad)


def test_a_non_finite_value_inside_a_bars_columns_is_refused_too() -> None:
    with pytest.raises(ValidationError):
        OptionBar(
            **ENVELOPE,
            source="bar-writer",
            instrument=INSTRUMENT,
            table=BarTable.QUOTE,
            minute=TS_VENUE,
            columns={"mid_close": float("nan")},
        )


def test_an_implied_volatility_of_zero_is_refused() -> None:
    """**`iv` is `null` and never `0`.** A solved volatility of zero is not a thing; an
    unsolved strike carries `None` and an `iv_reason`."""
    with pytest.raises(ValidationError):
        ChainStrike(strike=Decimal("60000"), iv=0.0)


def test_a_chain_leg_carries_its_venue_symbol_and_greeks() -> None:
    leg = ChainLeg(symbol="C-BTC-60000-270626", iv=0.5217, delta=0.4812)
    assert leg.symbol == "C-BTC-60000-270626"
    assert leg.delta == 0.4812


def test_a_hyphen_in_a_venue_or_underlying_is_refused_at_construction() -> None:
    """`canonical` joins on `-`, so a part containing one would produce a string its own
    parser rejects."""
    for field in ("venue", "underlying"):
        parts = {
            "venue": "DELTA",
            "underlying": "BTC",
            "expiry": date(2026, 6, 27),
            "strike": Decimal("60000"),
            "right": Right.CALL,
        }
        parts[field] = "TWO-WORDS"
        with pytest.raises(ValidationError):
            Instrument(**parts)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the null instruments


@pytest.mark.parametrize(
    "type_name", ["md.index_quote", "computed.chain", "feed.connection"]
)
def test_the_events_the_catalogue_says_carry_no_instrument_refuse_one(
    type_name: str,
) -> None:
    """`instrument` is `null` on these three because they are about an underlying, an
    expiry and an adapter respectively — not about one contract."""
    cls = registry()[type_name]
    payload = SAMPLES[type_name].model_dump(mode="json")
    payload["instrument"] = INSTRUMENT.model_dump(mode="json")
    assert SAMPLES[type_name].instrument is None
    with pytest.raises(ValidationError):
        cls.model_validate(payload)


# --------------------------------------------------------------------------------- bus


def test_the_fan_out_is_a_bus() -> None:
    """The producers and consumers of #37 are written against this protocol, so that
    replacing the fan-out with a broker opens neither of them."""
    bus: Bus = FanOut()
    assert isinstance(bus, Bus)


def test_the_bus_protocol_keeps_the_fan_outs_queue_semantics() -> None:
    """No behaviour change: `subscribe` still takes the watermark and the policy, and
    `tests/test_fanout.py` is the authority on what they do."""
    bus: Bus = FanOut()
    lossless = bus.subscribe("writer", maxsize=2, lossless=True)
    bounded = bus.subscribe("screen", maxsize=2)
    for n in range(4):
        bus.publish(n)
    assert lossless.queue.qsize() == 4
    assert lossless.over_capacity == 2
    assert bounded.queue.qsize() == 2
    assert bounded.dropped == 2


# --------------------------------------------------------------------- aware stamps


@pytest.mark.parametrize("field", ["ts_venue", "ts_received"])
def test_a_stamp_with_no_timezone_is_refused(field: str) -> None:
    """**A naive stamp is not assumed to be UTC.**

    Pydantic parses an ISO string with no offset into a naive `datetime`, and a naive one
    cannot be subtracted from an aware one — `bars._micros` raises `TypeError` on it,
    inside the bar writer's drain loop, which does not guard. The writer task would die
    and all four tables would stop, with one log line as the only symptom. So it is
    refused at the boundary, where the traceback still names the producer.

    Assuming UTC was the alternative and it is worse: the gap between the two stamps is
    this project's arrival-lag column, and inventing a zone would put a number in it that
    nobody measured.
    """
    payload = SAMPLES["md.option_quote"].model_dump(mode="json")
    payload[field] = "2026-09-04T10:00:00"

    with pytest.raises(ValidationError) as caught:
        parse_event(payload)

    assert "timezone" in str(caught.value)


def test_the_aware_stamps_the_adapter_builds_are_accepted() -> None:
    """Guard on the guard: the rule above must not refuse the producer's own events."""
    event = SAMPLES["md.option_quote"]

    assert event.ts_received.tzinfo is not None
    assert parse_event(event.model_dump(mode="json")) == event
