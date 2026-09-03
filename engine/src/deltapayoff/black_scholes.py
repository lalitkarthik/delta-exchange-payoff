"""M2: Black-Scholes, pricing from spot and a rate.

    d1 = (ln(S/K) + (r + sigma^2/2)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)

    C  = S*Phi(d1) - K*e^(-rT)*Phi(d2)
    P  = K*e^(-rT)*Phi(-d2) - S*Phi(-d1)

**This is Black-76 in different clothes, and the identity is exact:**

    BS(S, r)  ==  B76(F = S*e^(rT), D = e^(-rT))

Asserted to machine precision over twenty combinations in `tests/test_black_scholes.py`.
Written out longhand here rather than delegating to `black76`, so that the test compares
two independent expressions rather than restating one of them.

The consequence is worth stating plainly, because it decides the shape of T2's agreement
matrix. **M1-versus-M2 is not a second axis.** The only thing M2 can disagree with M1
about is the forward its rate implies, and that comparison already has a name in #2 - it
is F3, the carry forward at the same borrowed 6.5%. So a matrix crossing models against
forwards would carry a duplicated column, and this module exists to prove that rather
than to add one.

The rate itself remains the problem. There is no risk-free rate for BTC, and
`docs/forward.md` measures the borrowed 6.5% at 2.4x the rate this venue implies. M2
cannot be used without choosing one; M1 on a parity forward never has to.
"""

from __future__ import annotations

import math

from .black76 import _standard_normal_cdf, _standard_normal_pdf

#: Matches `black76._DEGENERATE`; below this `sigma*sqrt(T)` is zero in double precision.
_DEGENERATE = 1e-12


def _degenerate(spot: float, strike: float, years: float, sigma: float) -> bool:
    return (
        spot <= 0.0
        or strike <= 0.0
        or years <= 0.0
        or sigma <= 0.0
        or sigma * math.sqrt(years) < _DEGENERATE
    )


def _d1_d2(
    spot: float, strike: float, years: float, sigma: float, rate: float
) -> tuple[float, float]:
    spread = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / spread
    return d1, d1 - spread


def bs_call_price(
    spot: float, strike: float, years: float, sigma: float, rate: float
) -> float:
    """`C = S*Phi(d1) - K*e^(-rT)*Phi(d2)`."""
    discount = math.exp(-rate * years)
    if _degenerate(spot, strike, years, sigma):
        return max(spot - strike * discount, 0.0)
    d1, d2 = _d1_d2(spot, strike, years, sigma, rate)
    return spot * _standard_normal_cdf(d1) - strike * discount * _standard_normal_cdf(d2)


def bs_put_price(
    spot: float, strike: float, years: float, sigma: float, rate: float
) -> float:
    """`P = K*e^(-rT)*Phi(-d2) - S*Phi(-d1)`."""
    discount = math.exp(-rate * years)
    if _degenerate(spot, strike, years, sigma):
        return max(strike * discount - spot, 0.0)
    d1, d2 = _d1_d2(spot, strike, years, sigma, rate)
    return strike * discount * _standard_normal_cdf(-d2) - spot * _standard_normal_cdf(
        -d1
    )


def bs_vega(
    spot: float, strike: float, years: float, sigma: float, rate: float
) -> float:
    """`S*phi(d1)*sqrt(T)`.

    Identical to Black-76's vega under the same substitution, which matters more than it
    looks: vega decides where every solver in this project fails, so if the two models
    disagreed about it they would have different failure regions and the identity above
    would not be worth much.
    """
    if _degenerate(spot, strike, years, sigma):
        return 0.0
    d1, _ = _d1_d2(spot, strike, years, sigma, rate)
    return spot * _standard_normal_pdf(d1) * math.sqrt(years)
