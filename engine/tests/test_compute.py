"""Enrichment: a chain in, the same chain with our own volatility and Greeks out.

The chains here are **priced at a volatility chosen by hand**, using this project's own
Black-76, and then handed to `enrich` to see whether it recovers that number. The
expected answer is therefore a literal fixed by the construction rather than something
the solver computes for itself, which is what makes a round-trip test worth keeping.

Pricing both legs from one forward also makes `C - P = D·(F - K)` hold exactly at every
strike, so F1 recovers the planted forward and discount too. One construction, two
properties.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deltapayoff.black76 import call_price, put_price
from deltapayoff.compute import enrich
from deltapayoff.models import ChainResponse, ChainRow, Leg

PLANTED_VOL = 0.40
PLANTED_FORWARD = 80_000.0
PLANTED_DISCOUNT = 0.999
PLANTED_SPOT = 79_900.0

#: 30 calendar days, so `year_fraction` returns exactly 30/365 and the prices below are
#: struck at the same T the solver will use.
TAKEN = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
EXPIRY = (TAKEN + timedelta(days=30)).strftime("%d-%m-%Y")
PLANTED_YEARS = 30.0 / 365.0


def priced_chain(
    strikes: list[float],
    vol: float = PLANTED_VOL,
    quote: bool = True,
    discount: float = PLANTED_DISCOUNT,
) -> ChainResponse:
    """A chain whose every leg is worth exactly what Black-76 says at `vol`.

    Bid and ask are set to the same number, so the midpoint the solver inverts *is* the
    planted price with no spread to widen the answer.

    `quote=False` strips the quotes from every leg, leaving a chain that is listed and
    untradeable — the shape roughly 40% of real strikes arrive in.
    """
    rows = []
    for strike in strikes:
        call = call_price(PLANTED_FORWARD, strike, PLANTED_YEARS, vol, discount)
        put = put_price(PLANTED_FORWARD, strike, PLANTED_YEARS, vol, discount)
        rows.append(
            ChainRow(
                strike=strike,
                # `mark_iv` and the Greeks below are Delta's reference columns. They are
                # deliberately absurd so that any test asserting they survive untouched
                # cannot accidentally be reading one of ours.
                call=Leg(
                    symbol=f"C-BTC-{strike:.0f}-{EXPIRY}",
                    bid=call if quote else None,
                    ask=call if quote else None,
                    mark=call,
                    mark_iv=9.99,
                    delta=9.99,
                    gamma=9.99,
                    theta=9.99,
                    vega=9.99,
                    rho=9.99,
                ),
                put=Leg(
                    symbol=f"P-BTC-{strike:.0f}-{EXPIRY}",
                    bid=put if quote else None,
                    ask=put if quote else None,
                    mark=put,
                    mark_iv=9.99,
                    delta=9.99,
                ),
            )
        )
    return ChainResponse(
        underlying="BTC",
        expiry=EXPIRY,
        spot=PLANTED_SPOT,
        atm_strike=80_000.0,
        fetched_at=TAKEN.strftime("%Y-%m-%dT%H:%M:%SZ"),
        rows=rows,
    )


#: Seven strikes, not five. `forward.MIN_PAIRS` is 5, so a five-strike chain that
#: loses one pair stops being a trusted fit — which would make the "one hole does
#: not stop the others" test below silently test the trust threshold instead.
STRIKES = [
    65_000.0, 70_000.0, 75_000.0, 80_000.0, 85_000.0, 90_000.0, 95_000.0,
]


def test_recovers_the_volatility_the_chain_was_priced_at() -> None:
    """The round trip. Every strike was priced at 40%; every strike should come back.

    This is the test that would catch a wrong forward, a wrong year fraction, a solver
    that has stopped converging, or an out-of-the-money side chosen on the wrong
    comparison — all of which produce plausible numbers and no exception.
    """
    enriched = enrich(priced_chain(STRIKES))

    recovered = [row.call.computed.iv for row in enriched.rows]
    assert all(iv is not None for iv in recovered)
    assert recovered == pytest.approx([PLANTED_VOL] * len(STRIKES), rel=1e-6)


def test_recovers_the_planted_forward_and_discount() -> None:
    """Parity holds exactly in the construction, so F1 has an exact answer to find."""
    enriched = enrich(priced_chain(STRIKES))

    assert enriched.forward == pytest.approx(PLANTED_FORWARD, rel=1e-9)
    assert enriched.discount == pytest.approx(PLANTED_DISCOUNT, rel=1e-9)
    assert enriched.years_to_expiry == pytest.approx(PLANTED_YEARS, rel=1e-12)


def test_delta_reference_columns_survive_untouched() -> None:
    """Ours are added, never substituted. This is the guard on that decision.

    The fixture plants 9.99 in every one of Delta's fields precisely so that a value
    read back as 9.99 proves the enrichment did not overwrite it.
    """
    enriched = enrich(priced_chain(STRIKES))

    for row in enriched.rows:
        assert row.call.mark_iv == 9.99
        assert row.call.delta == 9.99
        assert row.call.gamma == 9.99
        assert row.call.vega == 9.99
        assert row.put.mark_iv == 9.99


def test_both_legs_of_a_strike_carry_the_same_volatility() -> None:
    """Put-call parity guarantees one volatility per strike, so both sides show it."""
    enriched = enrich(priced_chain(STRIKES))

    for row in enriched.rows:
        assert row.call.computed.iv == pytest.approx(row.put.computed.iv, rel=1e-12)


def test_the_volatility_names_the_leg_it_was_solved_from() -> None:
    """Calls above the forward, puts below. The out-of-the-money side is inverted."""
    enriched = enrich(priced_chain(STRIKES))

    by_strike = {row.strike: row.call.computed.iv_leg for row in enriched.rows}

    assert by_strike[70_000.0] == "put"
    assert by_strike[75_000.0] == "put"
    assert by_strike[85_000.0] == "call"
    assert by_strike[90_000.0] == "call"


def test_call_and_put_greeks_differ_on_the_same_strike() -> None:
    """One volatility per strike, but two legs — and they are not the same option.

    Delta is the clearest case: a call's is positive and a put's is negative, and they
    differ by exactly one. A bug that computed the call's Greeks and copied them to the
    put would pass every volatility test above and fail here.
    """
    enriched = enrich(priced_chain(STRIKES))
    row = next(r for r in enriched.rows if r.strike == 80_000.0)

    assert row.call.computed.delta > 0.0
    assert row.put.computed.delta < 0.0
    assert row.put.computed.delta == pytest.approx(
        row.call.computed.delta - 1.0, rel=1e-12
    )
    # Gamma and vega are properties of the strike, not of the side, so they match.
    assert row.call.computed.gamma == pytest.approx(row.put.computed.gamma, rel=1e-12)
    assert row.call.computed.vega == pytest.approx(row.put.computed.vega, rel=1e-12)


def test_a_strike_with_no_quote_reports_absence_and_a_reason() -> None:
    """`None` is not zero. An unquoted strike has no volatility, not a volatility of 0.

    This is the same rule the REST path applies to Delta's own `"0"` and `""`, and it
    is the project's standing objection to forward-filling in one more place.
    """
    enriched = enrich(priced_chain(STRIKES, quote=False))

    for row in enriched.rows:
        assert row.call.computed.iv is None
        assert row.call.computed.delta is None
        assert row.call.computed.iv_reason != ""


def test_one_unquoted_strike_does_not_stop_the_others_computing() -> None:
    """A hole in the chain is a hole, not a failure. The rest still solves."""
    chain = priced_chain(STRIKES)
    broken = next(r for r in chain.rows if r.strike == 90_000.0)
    broken.call.bid = broken.call.ask = None
    broken.put.bid = broken.put.ask = None

    enriched = enrich(chain)

    solved = [r for r in enriched.rows if r.call.computed.iv is not None]
    assert len(solved) == len(STRIKES) - 1
    assert all(r.call.computed.iv == pytest.approx(PLANTED_VOL, rel=1e-6) for r in solved)


def test_an_empty_chain_is_returned_unchanged_rather_than_raising() -> None:
    """No rows means no forward to fit. That is a fact about the chain, not an error."""
    empty = priced_chain([])

    enriched = enrich(empty)

    assert enriched.rows == []
    assert enriched.forward is None


def test_a_chain_neither_method_can_price_refuses_the_whole_chain() -> None:
    """When both the regression and the money strike fail, nothing gets a volatility.

    Refusing matters. Pricing a chain against a forward nobody believes produces a full
    ladder of plausible, uniformly wrong numbers with nothing to signal it — louder and
    cheaper to say so.

    Both routes have to be closed to reach this. Three strikes is below `MIN_PAIRS`, so
    the parity regression will not be trusted; stripping the quotes from the money strike
    closes F2, which needs both of its legs to invert.
    """
    thin = priced_chain([79_000.0, 80_000.0, 81_000.0])
    money = next(r for r in thin.rows if r.strike == 80_000.0)
    money.call.bid = money.call.ask = None
    money.put.bid = money.put.ask = None

    enriched = enrich(thin)

    assert enriched.forward is None
    for row in enriched.rows:
        assert row.call.computed.iv is None
        assert "forward" in row.call.computed.iv_reason


def test_a_thin_chain_still_prices_through_the_money_strike() -> None:
    """Below `MIN_PAIRS` the regression has no line to fit, but parity still has a point.

    F2 needs one strike quoting both sides, not five, so a sparse chain is priceable even
    though the discount has to be assumed rather than fitted.
    """
    enriched = enrich(priced_chain([79_000.0, 80_000.0, 81_000.0]))

    assert enriched.forward_method == "F2"
    assert enriched.forward == pytest.approx(PLANTED_FORWARD, rel=1e-4)


def test_a_flat_discount_falls_back_to_f2_rather_than_blanking() -> None:
    """The front-expiry bug, pinned.

    Under a day to expiry the true discount factor is within a few parts per hundred
    thousand of exactly 1, so the rate implied by it is quote noise. `f1_parity_fit`
    will not trust a fit whose implied rate is non-positive, which makes trusting the
    forward a coin flip on the last digit of a quote — measured on the live 04-09-2026
    chain, the fitted discount flapped between 0.99997892 and 1.00001939 and the rate
    with it, between +1.03% and -0.95%, blanking every Greek on the screen each time it
    went negative.

    The forward itself is not in doubt: F1 and F2 agreed to four parts per million
    across those same ticks. So a chain whose *discount* cannot be fitted falls back to
    assuming one, which is exactly what F2 is for, rather than refusing to price at all.

    A discount of exactly 1.0 reproduces the condition: the implied rate is 0, and
    `trusted` requires it to be strictly positive.
    """
    chain = priced_chain(STRIKES, discount=1.0)

    enriched = enrich(chain)

    assert enriched.forward_method == "F2"
    assert enriched.forward == pytest.approx(PLANTED_FORWARD, rel=1e-6)
    assert all(row.call.computed.iv is not None for row in enriched.rows)


def test_the_fallback_is_only_reached_when_f1_is_untrusted() -> None:
    """A healthy chain still uses the parity regression, which fits the discount too."""
    enriched = enrich(priced_chain(STRIKES))

    assert enriched.forward_method == "F1"
