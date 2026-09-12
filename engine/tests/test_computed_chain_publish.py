"""The split api's computed-chain publication seam."""

from __future__ import annotations

from datetime import datetime, timezone

from deltapayoff import bars, main
from deltapayoff.events import ComputedChain
from deltapayoff.events.redis_wire import decode, encode, stream_name
from deltapayoff.models import ChainResponse, ChainRow, ComputedLeg, Leg

TS = datetime(2026, 9, 4, 9, 0, 30, 123456, tzinfo=timezone.utc)


def chain(*, fetched_at: str = "2026-09-04T09:00:30Z") -> ChainResponse:
    computed = ComputedLeg(
        iv=0.42,
        iv_leg="call",
        delta=0.51,
        gamma=0.001,
        vega=31.0,
        theta=-8.0,
        rho=1.8,
    )
    return ChainResponse(
        underlying="BTC",
        expiry="04-09-2026",
        quote_currency="USD",
        fetched_at=fetched_at,
        rows=[
            ChainRow(
                strike=77_600.0,
                call=Leg(
                    symbol="C-BTC-77600-040926", computed=computed
                ),
                put=Leg(symbol="P-BTC-77600-040926", computed=None),
            )
        ],
        forward=77_590.4,
        discount=0.9999,
        years_to_expiry=0.001,
        forward_method="fitted",
    )


def test_published_event_flattens_to_the_same_ticks_as_the_chain() -> None:
    event = main.computed_chain_event(chain(), ts_received=TS)

    assert bars.computed_ticks_from_event(event) == bars.computed_ticks_from_chain(
        chain()
    )


def test_published_event_survives_the_redis_wire() -> None:
    event = main.computed_chain_event(chain(), ts_received=TS)
    decoded = decode(
        encode(event), stream=stream_name(event, venue="DELTA")
    )

    assert isinstance(decoded, ComputedChain)
    assert bars.computed_ticks_from_event(decoded) == bars.computed_ticks_from_chain(
        chain()
    )


def test_a_chain_without_a_computed_leg_publishes_that_side_as_none() -> None:
    event = main.computed_chain_event(chain(), ts_received=TS)

    assert event.strikes[0].call is not None
    assert event.strikes[0].put is None
    assert len(bars.computed_ticks_from_event(event)) == 1


def test_an_unparseable_chain_is_counted_and_not_published() -> None:
    published: list[ComputedChain] = []

    errors = main.publish_computed_chains(
        [chain(fetched_at="not-a-timestamp")],
        published.append,
        ts_received=TS,
    )

    assert errors == 1
    assert published == []


def test_split_computed_grace_is_explicit() -> None:
    assert bars.COMPUTED_GRACE_SECONDS == 0.0
    assert bars.COMPUTED_SPLIT_GRACE_SECONDS == 2.0
