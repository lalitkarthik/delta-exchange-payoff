"""The reported Greeks, and the conventions they are reported under.

Every expected value here is derived **without** touching the code under test. The
at-the-money forms are used throughout because `F == K` collapses `d1` to
`sigma·sqrt(T)/2` and shares no arithmetic with a general `d1`/`d2` implementation:

    d1 = 0.1, d2 = -0.1        at F = K = 100, T = 1, sigma = 0.20
    Phi(0.1)  = 0.539827837277029
    Phi(-0.1) = 0.460172162722971
    phi(0.1)  = 0.3969525474770118

**The discount is 0.95 wherever a convention is under test**, never 1.0. At `D = 1.0`
a discounted and an undiscounted Greek are the same number, so a test written that way
passes under either convention and pins neither.
"""

from __future__ import annotations

import pytest

from deltapayoff.greeks import report_greeks

# F = K = 100, T = 1, sigma = 20%. `discount` varies per test.
ATM = {"forward": 100.0, "strike": 100.0, "years": 1.0, "sigma": 0.20}

PHI_D1 = 0.3969525474770118
CDF_D1 = 0.539827837277029
CDF_MINUS_D2 = 0.539827837277029
CDF_D2 = 0.460172162722971


def test_gamma_matches_the_at_the_money_closed_form() -> None:
    """`phi(d1) / (F·sigma·sqrt(T))` = 0.3969525474770118 / 20."""
    greeks = report_greeks(**ATM, discount=1.0, is_call=True)

    assert greeks.gamma == pytest.approx(PHI_D1 / 20.0, rel=1e-12)


def test_gamma_is_reported_undiscounted() -> None:
    """The sibling's convention: gamma carries no `D`.

    At `D = 0.95` a discounted gamma would be 5% smaller. Asserting the undiscounted
    value is what makes this test able to fail.
    """
    greeks = report_greeks(**ATM, discount=0.95, is_call=True)

    assert greeks.gamma == pytest.approx(PHI_D1 / 20.0, rel=1e-12)


def test_call_delta_is_reported_undiscounted() -> None:
    """`Phi(d1)`, not `D·Phi(d1)`. Bounded by [0, 1] rather than by [0, D]."""
    greeks = report_greeks(**ATM, discount=0.95, is_call=True)

    assert greeks.delta == pytest.approx(CDF_D1, rel=1e-12)


def test_put_delta_is_the_call_delta_minus_one() -> None:
    """Parity, and the reason the put's delta is negative.

    Asserted as a relationship between two calls rather than as a stored constant, so
    it holds for any correct implementation rather than for one set of inputs.
    """
    call = report_greeks(**ATM, discount=0.95, is_call=True)
    put = report_greeks(**ATM, discount=0.95, is_call=False)

    assert put.delta == pytest.approx(call.delta - 1.0, rel=1e-12)


def test_vega_is_discounted_and_per_volatility_point() -> None:
    """`D·F·phi(d1)·sqrt(T) / 100` — the move for a **1%** change, not a 100% one.

    Vega and rho keep their `D` while delta and gamma do not. That asymmetry is the
    sibling platform's, and it is carried here deliberately rather than tidied.
    """
    greeks = report_greeks(**ATM, discount=0.95, is_call=True)

    assert greeks.vega == pytest.approx(0.95 * 100.0 * PHI_D1 / 100.0, rel=1e-12)


def test_call_rho_is_discounted_and_per_one_percent() -> None:
    """`D·K·T·Phi(d2) / 100`."""
    greeks = report_greeks(**ATM, discount=0.95, is_call=True)

    assert greeks.rho == pytest.approx(0.95 * 100.0 * 1.0 * CDF_D2 / 100.0, rel=1e-12)


def test_put_rho_is_negative_and_uses_the_other_tail() -> None:
    """`-D·K·T·Phi(-d2) / 100`. A put gains value as rates fall."""
    greeks = report_greeks(**ATM, discount=0.95, is_call=False)

    assert greeks.rho == pytest.approx(
        -0.95 * 100.0 * 1.0 * CDF_MINUS_D2 / 100.0, rel=1e-12
    )


def test_theta_is_one_calendar_day_not_one_trading_day() -> None:
    """The trap in T8, pinned.

    Theta here is a repricing: what the option is worth after one day has passed and
    nothing else has moved. The independent value comes from the **analytic** theta,
    which shares no arithmetic with a repricing implementation:

        dC/dt = -F·phi(d1)·sigma / (2·sqrt(T))   at r = 0
              = -100 · 0.3969525474770118 · 0.20 / 2
              = -3.9695254747701180 per year

    One calendar day of that is `-3.9695254747701180 / 365 = -0.010875412259644159`.

    A 1/252 trading-day step would give -0.015752085, which is 1.448x larger and fails
    this tolerance by a factor of roughly 450. That is the whole point of the test: the
    sibling project's Greeks are verified under a 252-day year in which nothing decays
    at weekends, and this venue trades every day of the year.
    """
    greeks = report_greeks(**ATM, discount=1.0, is_call=True)

    analytic_per_year = -100.0 * PHI_D1 * 0.20 / 2.0
    assert greeks.theta == pytest.approx(analytic_per_year / 365.0, rel=1e-3)


def test_theta_is_negative_for_a_long_option() -> None:
    """Time decay costs the holder. True for a call and a put alike."""
    call = report_greeks(**ATM, discount=0.95, is_call=True)
    put = report_greeks(**ATM, discount=0.95, is_call=False)

    assert call.theta < 0.0
    assert put.theta < 0.0
