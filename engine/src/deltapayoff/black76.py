"""Black-76: the pricing machine, running forwards only.

Black-76 prices from the **forward**. Black-Scholes prices from spot plus a rate. They
are the same model in different clothes, and Black-76 is the one that fits here, because
there is no risk-free rate for BTC — feeding Black-Scholes an `r` means feeding it the
borrowed 6.5% that `docs/forward.md` measures as 2.4x the rate this venue implies.

Delta's options are vanilla and USD-settled (`docs/settlement.md`), so these are the
textbook formulae with no correction term, and every price is USD per unit of underlying.

    d1 = (ln(F/K) + sigma^2·T/2) / (sigma·sqrt(T))
    d2 = d1 - sigma·sqrt(T)

    C  = D·(F·Phi(d1) - K·Phi(d2))
    P  = D·(K·Phi(-d2) - F·Phi(-d1))

M2 (Black-Scholes on spot) and the solvers live elsewhere; this module only runs the
machine in the direction it goes.
"""

from __future__ import annotations

import math

#: Below this, `sigma·sqrt(T)` is indistinguishable from zero in double precision and
#: `d1` would divide by it. The option is worth its intrinsic value there.
_DEGENERATE = 1e-12


def _standard_normal_cdf(x: float) -> float:
    """Phi. `math.erf` rather than a series: correctly rounded, and in the stdlib."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _standard_normal_pdf(x: float) -> float:
    """phi, the density. This is the shape vega inherits, including its collapse."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(
    forward: float, strike: float, years: float, sigma: float
) -> tuple[float, float]:
    spread = sigma * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * spread * spread) / spread
    return d1, d1 - spread


def _is_degenerate(forward: float, strike: float, years: float, sigma: float) -> bool:
    return (
        forward <= 0.0
        or strike <= 0.0
        or years <= 0.0
        or sigma <= 0.0
        or sigma * math.sqrt(years) < _DEGENERATE
    )


def call_price(
    forward: float, strike: float, years: float, sigma: float, discount: float
) -> float:
    """`C = D·(F·Phi(d1) - K·Phi(d2))`, USD per unit of underlying."""
    if _is_degenerate(forward, strike, years, sigma):
        return discount * max(forward - strike, 0.0)
    d1, d2 = _d1_d2(forward, strike, years, sigma)
    return discount * (
        forward * _standard_normal_cdf(d1) - strike * _standard_normal_cdf(d2)
    )


def put_price(
    forward: float, strike: float, years: float, sigma: float, discount: float
) -> float:
    """`P = D·(K·Phi(-d2) - F·Phi(-d1))`.

    Written directly rather than as `call_price(...) - discount*(forward - strike)`, so
    that `test_put_call_parity_holds_on_our_own_prices` is a real check on both formulae
    rather than a restatement of one of them.
    """
    if _is_degenerate(forward, strike, years, sigma):
        return discount * max(strike - forward, 0.0)
    d1, d2 = _d1_d2(forward, strike, years, sigma)
    return discount * (
        strike * _standard_normal_cdf(-d2) - forward * _standard_normal_cdf(-d1)
    )


def vega(
    forward: float, strike: float, years: float, sigma: float, discount: float
) -> float:
    """`D·F·phi(d1)·sqrt(T)` — the price move per unit change in sigma.

    Vega is two things at once, and both matter downstream. It is the Newton step size
    in S1, and it is the reason the solve fails when it is small: a price whose vega is
    near zero carries almost no information about the volatility that produced it. The
    `phi` in the middle is why — it decays like `exp(-d1^2/2)`, so far from the money
    vega does not shrink, it vanishes.
    """
    if _is_degenerate(forward, strike, years, sigma):
        return 0.0
    d1, _ = _d1_d2(forward, strike, years, sigma)
    return discount * forward * _standard_normal_pdf(d1) * math.sqrt(years)


def delta(
    forward: float, strike: float, years: float, sigma: float, discount: float
) -> float:
    """`dC/dF = D·Phi(d1)`, the call's sensitivity to the forward.

    Note this is delta with respect to the **forward**, not to spot. Delta's own feed
    publishes a spot delta; the two are recorded side by side in T2's matrix rather than
    graded against each other.
    """
    if _is_degenerate(forward, strike, years, sigma):
        return discount * (1.0 if forward > strike else 0.0)
    d1, _ = _d1_d2(forward, strike, years, sigma)
    return discount * _standard_normal_cdf(d1)
