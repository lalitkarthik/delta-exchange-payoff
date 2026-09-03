"""Black-76, the machine that runs forwards. No solving here — that is S1 onwards.

Every expected value below is derived without touching the code under test. The at-the-
money price and vega come from the closed form that holds when `F == K`, which shares no
arithmetic with a general `d1`/`d2` implementation:

    C_atm = D·F·(2·Phi(sigma·sqrt(T)/2) - 1)
    vega_atm = D·F·phi(sigma·sqrt(T)/2)·sqrt(T)

The rest are analytic limits — statements that must hold for *any* correct Black-76,
which is what makes them worth asserting.
"""

from __future__ import annotations

import pytest

from deltapayoff.black76 import call_price, delta, put_price, vega


def test_at_the_money_call_matches_the_closed_form() -> None:
    """F = K = 100, T = 1, sigma = 20%, D = 1.

    Independent value: 100 · (2·Phi(0.1) - 1) = 7.965567455405798.
    """
    price = call_price(forward=100.0, strike=100.0, years=1.0, sigma=0.20, discount=1.0)

    assert price == pytest.approx(7.965567455405798, rel=1e-12)


def test_at_the_money_vega_matches_the_closed_form() -> None:
    """Same inputs. Independent value: 100 · phi(0.1) · 1 = 39.69525474770118.

    Vega is the Newton step size in S1, so an error here becomes a solver error there.
    """
    result = vega(forward=100.0, strike=100.0, years=1.0, sigma=0.20, discount=1.0)

    assert result == pytest.approx(39.69525474770118, rel=1e-12)


def test_deep_in_the_money_call_delta_goes_to_one() -> None:
    """A call so far in the money it is a forward contract: one unit of delta.

    F = 100, K = 10 gives d1 = 11.6129, and Phi of that is 1.0 to double precision.
    """
    result = delta(forward=100.0, strike=10.0, years=1.0, sigma=0.20, discount=1.0)

    assert result == pytest.approx(1.0, abs=1e-12)


def test_at_the_money_delta_is_just_above_a_half() -> None:
    """Phi(sigma·sqrt(T)/2) = Phi(0.1) = 0.539827837277029.

    Worth asserting the exact value rather than "about 0.5": the half is the folk
    version, and the drift above it is real.
    """
    result = delta(forward=100.0, strike=100.0, years=1.0, sigma=0.20, discount=1.0)

    assert result == pytest.approx(0.539827837277029, rel=1e-12)


def test_vega_collapses_far_from_the_money() -> None:
    """The fact that explains every solver failure in this ticket.

    At F = 100, K = 10 vega is 2.07e-28. Newton divides the price error by that number,
    so S1 must refuse this region rather than step into it.
    """
    result = vega(forward=100.0, strike=10.0, years=1.0, sigma=0.20, discount=1.0)

    assert result < 1e-20


def test_put_call_parity_holds_on_our_own_prices() -> None:
    """C - P = D(F - K), for prices this module produced itself.

    #2 uses parity to recover the forward from Delta's prices. This asserts the same
    identity holds on ours — if it does not, the pricer and the forward disagree about
    what a forward means, and every IV downstream inherits that.
    """
    args = dict(forward=77_590.39, years=0.0104085, sigma=0.45, discount=0.999706)

    for strike in (60_000.0, 77_500.0, 77_600.0, 90_000.0):
        difference = call_price(strike=strike, **args) - put_price(strike=strike, **args)
        expected = args["discount"] * (args["forward"] - strike)
        assert difference == pytest.approx(expected, rel=1e-12)
