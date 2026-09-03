"""Running the pricing machine backwards: implied volatility.

Black-76 takes a volatility and returns a price. We observe the price and want the
volatility, and **no formula inverts it** — so you search. Guess a sigma, price it,
compare against what the market shows, adjust, repeat.

S1 is Newton-Raphson. It uses vega — the derivative of price with respect to sigma — to
step straight at the answer:

    sigma <- sigma + (observed - model(sigma)) / vega(sigma)

That division is both why it is fast and how it fails. **Where vega collapses, Newton
divides the price error by almost nothing and the step explodes.** Vega collapses exactly
where the price stops carrying information about volatility: deep in the money, far out
of the money, close to expiry. In those regions there is no answer to find, and the
correct behaviour is to decline rather than to return the number one bad iteration lands
on. A refusal is recoverable; a plausible wrong number is not.

S2 (Brent), S3 (Jäckel) and S4 (vectorised) answer the same question differently, and T2
exists to measure where they disagree.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from .black76 import call_price, put_price, vega
from .forward import mid, year_fraction

#: Vega, as a fraction of `D·F`, below which sigma is not identifiable from the price.
#:
#: Expressed relative to `D·F` because vega scales with the forward: an absolute
#: threshold that is strict on a $100 forward is meaningless on a $77,590 one.
#:
#: The number this guards against is not divergence. **Measured**: at F=100, K=10, T=1,
#: a price generated at sigma=20% solves to sigma=0.391 in 19 iterations and reports
#: convergence, because both volatilities reproduce that price to within 1e-8. Vega at
#: the answer is 3.6e-7, or 3.6e-9 of `D·F`. Where vega is that small many volatilities
#: produce one price, so the price does not pin sigma down and there is nothing to find.
MIN_RELATIVE_VEGA = 1e-6

#: Price agreement that counts as solved, in USD per unit of underlying. Delta's option
#: tick size is 0.1, so a hundredth of a cent is far inside anything observable.
PRICE_TOLERANCE = 1e-8

MAX_ITERATIONS = 50

#: Sigma is clamped into this band between steps. Newton is unbounded and a single bad
#: iteration can cross zero or run to thousands of percent; neither is an answer.
MIN_SIGMA = 1e-6
MAX_SIGMA = 10.0


class SolveResult(BaseModel):
    """One solver's answer, and why it stopped."""

    method: str
    sigma: float | None = None
    iterations: int = 0
    converged: bool = False
    reason: str = ""
    is_call: bool | None = None


def _seed(
    price: float, forward: float, strike: float, years: float, discount: float
) -> float:
    """Where to start Newton. Two approximations, and the larger of them wins.

    **Brenner-Subrahmanyam**, `sigma ~ sqrt(2*pi/T) · C / (D·F)`, is a good seed at the
    money and useless away from it. **Measured**: on the captured chain it seeds a call
    13% out of the money at 0.08%, a volatility at which that option is worth nothing
    and vega has underflowed to exactly zero. Newton cannot move from there, and the
    whole wing of the chain came back unsolved.

    **Manaster-Koehler**, `sigma* = sqrt(2·|ln(F/K)| / T)`, is the volatility at which
    vega is *maximised* for this strike. Starting at the top of the vega hill is what
    makes Newton's descent monotone, which is the property Manaster and Koehler proved
    in 1982. It goes to zero at the money, which is exactly where the other one is good.

    Taking the maximum uses each where it is strong: B-S near the money, M-K in the
    wings. This is Jäckel's thesis in miniature — the iteration was never the fragile
    part, the starting guess was.
    """
    if forward <= 0 or strike <= 0 or years <= 0:
        return 0.5
    brenner = math.sqrt(2.0 * math.pi / years) * price / (discount * forward)
    manaster = math.sqrt(2.0 * abs(math.log(forward / strike)) / years)
    return min(max(brenner, manaster, MIN_SIGMA), MAX_SIGMA)


def implied_vol_newton(
    price: float,
    forward: float,
    strike: float,
    years: float,
    discount: float,
    is_call: bool,
) -> SolveResult:
    """S1. Newton-Raphson with analytic vega."""
    model = call_price if is_call else put_price

    intrinsic = discount * (
        max(forward - strike, 0.0) if is_call else max(strike - forward, 0.0)
    )
    if price < intrinsic - PRICE_TOLERANCE:
        # No sigma produces this. It is a broken quote, not a hard solve, and naming it
        # costs nothing where iterating on it would burn fifty steps to say the same.
        return SolveResult(
            method="S1",
            reason=f"price {price:.6f} is below intrinsic {intrinsic:.6f}",
        )

    floor = MIN_RELATIVE_VEGA * discount * forward
    sigma = _seed(price, forward, strike, years, discount)
    for iteration in range(1, MAX_ITERATIONS + 1):
        modelled = model(
            forward=forward, strike=strike, years=years, sigma=sigma, discount=discount
        )
        error = price - modelled
        if abs(error) < PRICE_TOLERANCE:
            # Matching the price is necessary and not sufficient. Where vega is tiny
            # many sigmas match it, so the answer must also be identifiable — checked
            # here rather than only along the way, because a good seed can land inside
            # a flat region on the first step and never test the slope at all.
            settled = vega(
                forward=forward,
                strike=strike,
                years=years,
                sigma=sigma,
                discount=discount,
            )
            if settled < floor:
                return SolveResult(
                    method="S1",
                    iterations=iteration,
                    reason=(
                        f"vega {settled:.3e} at the solution is below {floor:.3e}; the "
                        "price is reproduced by a wide range of volatilities, so this "
                        "one is not identifiable"
                    ),
                )
            return SolveResult(
                method="S1",
                sigma=sigma,
                iterations=iteration,
                converged=True,
                reason="converged",
            )

        slope = vega(
            forward=forward, strike=strike, years=years, sigma=sigma, discount=discount
        )
        if slope < floor:
            return SolveResult(
                method="S1",
                iterations=iteration,
                reason=(
                    f"vega {slope:.3e} is below {floor:.3e}, {MIN_RELATIVE_VEGA:.0e} of "
                    "D*F; the price carries no information about volatility here"
                ),
            )

        sigma = min(max(sigma + error / slope, MIN_SIGMA), MAX_SIGMA)

    return SolveResult(
        method="S1",
        iterations=MAX_ITERATIONS,
        reason=f"did not converge in {MAX_ITERATIONS} iterations",
    )


def solve_chain(chain, forward) -> dict[float, SolveResult]:
    """Implied volatility for every strike in a snapshot, under one forward.

    **The out-of-the-money side is inverted**: calls above the forward, puts below. In
    theory either side of a strike implies the same volatility — put-call parity
    guarantees it. In practice the OTM option carries no intrinsic value, so its entire
    price is time value and its vega is at its largest, while the ITM option prices the
    same number with most of its value insensitive to it. Same answer, far better
    conditioned.

    Returns one entry per strike, including the refusals — a strike that could not be
    solved is a fact about the chain, not an absence.
    """
    years = year_fraction(chain)
    solved: dict[float, SolveResult] = {}
    for row in chain.rows:
        is_call = row.strike >= forward.forward
        price = mid(row.call if is_call else row.put)
        if price is None or price <= 0.0:
            solved[row.strike] = SolveResult(
                method="S1", is_call=is_call, reason="no two-sided quote on the OTM side"
            )
            continue
        result = implied_vol_newton(
            price,
            forward=forward.forward,
            strike=row.strike,
            years=years,
            discount=forward.discount,
            is_call=is_call,
        )
        solved[row.strike] = result.model_copy(update={"is_call": is_call})
    return solved
