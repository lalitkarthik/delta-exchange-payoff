"""The five Greeks, reported under the sibling project's conventions.

Ported from `payoff-project`'s `black76_greeks`, which is graded against that project's
shipped platform Greeks to **2.2e-16** on delta and **1.1e-11** on theta. The formulae
are that implementation's; only the clock is this venue's.

**Why this is not in `black76`.** That module is the pricing core and the solver depends
on it: `solvers.implied_vol_newton` imports `black76.vega` and divides by it on every
iteration. The conventions below are *reporting* conventions — a vega scaled to a one
percent move is the right number to put on a screen and the wrong number to use as a
Newton step. Keeping them apart means a change to how a Greek is displayed can never
alter how a volatility is recovered.

**The conventions, which are deliberately not all textbook:**

    delta, gamma   undiscounted - no `D`, so delta is bounded by [0, 1] and not [0, D]
    vega           discounted, per volatility point (a 1% move, so divided by 100)
    rho            discounted, per one percent
    theta          a one-day repricing, not the analytic derivative

The asymmetry — delta and gamma dropping `D` while vega and rho keep it — is the sibling
platform's, not ours. It is carried unchanged rather than tidied, because a convention
the desk does not use is a convention that has to be undone at every boundary.

**The clock is ACT/365, and that is the one thing changed in the port.** The sibling runs
a 252-trading-day year in which nothing decays overnight or at weekends, which is correct
for an index that closes. Crypto trades continuously and this venue lists daily expiries
including weekends; every forward and year fraction in this project is already ACT/365,
and every volatility is solved against that same year fraction.

Black-76 does not care which calendar is used. It requires only that **time and
volatility are quoted on the same one**, so a 1/252 step here would be the single
component in the pipeline running on a different clock — which is exactly why it yields
a wrong number and no error. **Measured** on the at-the-money call of the 25-09-2026
expiry, 21.8 days out at a solved 36.63%: a 1/252 step gives a theta of -96.96 USD where
1/365 gives -66.58 USD, overstating it by **1.456x** against a predicted 365/252 =
1.4484. `tests/test_greeks.py` pins this.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from .black76 import (
    _d1_d2,
    _standard_normal_cdf,
    _standard_normal_pdf,
    call_price,
    put_price,
)

#: ACT/365, matching `forward.DAYS_PER_YEAR`. **Not** the sibling's 252 — see above.
DAYS_PER_YEAR = 365.0

#: Vega and rho are quoted per one percent rather than per unit, so both are divided by
#: this. A vega of 0.40 means "40 cents for a one point move in volatility", which is
#: how a desk reads it; the undivided 39.7 answers a question nobody asks.
PER_ONE_PERCENT = 100.0


class Greeks(BaseModel):
    """One leg's five Greeks, in the conventions this module documents."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


def report_greeks(
    forward: float,
    strike: float,
    years: float,
    sigma: float,
    discount: float,
    *,
    is_call: bool,
) -> Greeks:
    """The five Greeks for one leg at one volatility.

    `years` and `sigma` must be on the same calendar — ACT/365 throughout this project.

    Raises `ValueError` at or past expiry. The *price* there is well defined and is what
    a payoff chart draws, but the exposures are not: gamma divides by a time scaling
    that has gone to zero, and theta is the change over a day that no longer exists.
    Returning zeros would be a fabricated number of exactly the kind this project
    refuses elsewhere.
    """
    if years <= 0.0:
        raise ValueError(
            "the Greeks are undefined at expiry - price there instead; "
            f"got years={years}"
        )
    if sigma <= 0.0:
        raise ValueError(f"sigma must be positive to report Greeks; got {sigma}")
    if forward <= 0.0 or strike <= 0.0:
        # `_d1_d2` takes the log of `forward / strike`, so a non-positive either side
        # is a domain error rather than a degenerate case. A parity fit can in
        # principle return a negative forward while still implying a plausible rate,
        # and that must surface here rather than as `math domain error` from three
        # frames deeper.
        raise ValueError(
            "forward and strike must both be positive to report Greeks; "
            f"got forward={forward}, strike={strike}"
        )

    d1, d2 = _d1_d2(forward, strike, years, sigma)
    price_now = (call_price if is_call else put_price)(
        forward, strike, years, sigma, discount
    )

    # The rate implied by the discount factor, used only to re-discount one day nearer
    # expiry. It never enters or leaves the interface: this project takes a forward and
    # a discount factor, never a spot and a rate, because the implied rate is unstable
    # as T approaches zero.
    rate = -math.log(discount) / years if discount > 0.0 else 0.0
    years_next = max(years - 1.0 / DAYS_PER_YEAR, 0.0)
    if years_next <= 0.0:
        # Inside the last day. Repricing at T = 0 is the discounted intrinsic value,
        # which `call_price` already returns, so the difference remains meaningful.
        repriced = (call_price if is_call else put_price)(
            forward, strike, 0.0, sigma, math.exp(-rate * years_next)
        )
    else:
        repriced = (call_price if is_call else put_price)(
            forward, strike, years_next, sigma, math.exp(-rate * years_next)
        )

    return Greeks(
        # Undiscounted, matching the sibling. Vega and rho below DO carry the discount.
        delta=(
            _standard_normal_cdf(d1) if is_call else _standard_normal_cdf(d1) - 1.0
        ),
        gamma=_standard_normal_pdf(d1) / (forward * sigma * math.sqrt(years)),
        vega=(
            discount * forward * _standard_normal_pdf(d1) * math.sqrt(years)
        ) / PER_ONE_PERCENT,
        theta=repriced - price_now,
        rho=(
            (discount * strike * years * _standard_normal_cdf(d2))
            if is_call
            else -(discount * strike * years * _standard_normal_cdf(-d2))
        ) / PER_ONE_PERCENT,
    )
