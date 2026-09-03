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

S2 (Brent), S3 (Jaeckel-shaped) and S4 (vectorised) answer the same question differently,
and T2 exists to measure where they disagree.

Sources, with what was taken from each, are in `docs/implied-vol.md` §7:

* Jaeckel, "Let's Be Rational", Wilmott 2015, doi:10.1002/wilm.10395, C source at
  <https://www.jaeckel.org/LetsBeRational.7z> — S3's normalisation and higher-order
  step. **Not** his four-branch rational initial guess, which is the part that gets his
  method to two iterations for all inputs.
* Manaster and Koehler, Journal of Finance 37(1), 1982, pp. 227-230,
  doi:10.1111/j.1540-6261.1982.tb01105.x — the seed below, and the single change that
  took this project from 19 of 65 strikes solved to 63 of 65.
* Brenner and Subrahmanyam, Financial Analysts Journal 44(5), 1988 — the other seed.
* Brent, *Algorithms for Minimization without Derivatives*, 1973, ch. 4 — S2.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from .black76 import _standard_normal_cdf, call_price, put_price, vega
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


def _identifiable(
    sigma: float, forward: float, strike: float, years: float, discount: float
) -> tuple[bool, float]:
    """Whether a recovered sigma is pinned down by the price, and the vega that says so.

    **This belongs to the problem, not to any one solver.** All three found the same
    trap independently: a price can be reproduced to machine precision by a wide band of
    volatilities wherever vega is small, and every solver will happily report one of
    them. Newton hit it first because its step divides by vega, but Brent and the
    Householder iteration walk into it just as readily and with no warning at all.

    Measured against `D·F` rather than absolutely, because vega scales with the forward.
    """
    slope = vega(
        forward=forward, strike=strike, years=years, sigma=sigma, discount=discount
    )
    return slope >= MIN_RELATIVE_VEGA * discount * forward, slope


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


def solve_chain(chain, forward, solver=None) -> dict[float, SolveResult]:
    """Implied volatility for every strike in a snapshot, under one forward.

    **The out-of-the-money side is inverted**: calls above the forward, puts below. In
    theory either side of a strike implies the same volatility — put-call parity
    guarantees it. In practice the OTM option carries no intrinsic value, so its entire
    price is time value and its vega is at its largest, while the ITM option prices the
    same number with most of its value insensitive to it. Same answer, far better
    conditioned.

    Returns one entry per strike, including the refusals — a strike that could not be
    solved is a fact about the chain, not an absence.

    `solver` selects S1, S2 or S3; it defaults to S1. The chain-walking and the
    OTM-side choice are identical whichever is used, so a disagreement between two runs
    is a disagreement between solvers and nothing else.
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
        result = (solver or implied_vol_newton)(
            price,
            forward=forward.forward,
            strike=row.strike,
            years=years,
            discount=forward.discount,
            is_call=is_call,
        )
        solved[row.strike] = result.model_copy(update={"is_call": is_call})
    return solved


# --- S2: Brent ------------------------------------------------------------------


def implied_vol_brent(
    price: float,
    forward: float,
    strike: float,
    years: float,
    discount: float,
    is_call: bool,
) -> SolveResult:
    """S2. Brent's method: bracket the answer and never leave the bracket.

    Newton's step is unbounded, so a single bad iteration can land anywhere. Brent keeps
    two bounds it knows the root lies between and only ever shrinks the gap. **It cannot
    diverge, because there is nowhere to diverge to.** Inside the bracket it uses inverse
    quadratic interpolation where that is well behaved and falls back to bisection where
    it is not, so it is fast in the easy middle and merely slow at the edges.

    Price is monotone increasing in sigma, so `[MIN_SIGMA, MAX_SIGMA]` brackets any
    solvable price and the bracket check doubles as a validity check on the quote.
    """
    model = call_price if is_call else put_price

    def excess(sigma: float) -> float:
        return (
            model(
                forward=forward,
                strike=strike,
                years=years,
                sigma=sigma,
                discount=discount,
            )
            - price
        )

    a, b = MIN_SIGMA, MAX_SIGMA
    fa, fb = excess(a), excess(b)
    if fa > 0.0:
        return SolveResult(method="S2", reason="price is below the lower bracket bound")
    if fb < 0.0:
        return SolveResult(method="S2", reason="price is above the upper bracket bound")

    c, fc = a, fa
    step = previous = b - a
    for iteration in range(1, MAX_ITERATIONS + 1):
        if fb * fc > 0.0:
            c, fc = a, fa
            step = previous = b - a
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb

        tolerance = 2.0 * 1e-16 * abs(b) + 0.5 * MIN_SIGMA
        middle = 0.5 * (c - b)
        if abs(middle) <= tolerance or fb == 0.0:
            settled, slope = _identifiable(b, forward, strike, years, discount)
            if not settled:
                return SolveResult(
                    method="S2",
                    iterations=iteration,
                    reason=(
                        f"vega {slope:.3e} at the solution is below the identifiability "
                        "floor; the bracket contains many volatilities that reproduce "
                        "this price"
                    ),
                )
            return SolveResult(
                method="S2",
                sigma=b,
                iterations=iteration,
                converged=True,
                reason="converged",
            )

        if abs(previous) >= tolerance and abs(fa) > abs(fb):
            ratio = fb / fa
            if a == c:
                numerator, denominator = 2.0 * middle * ratio, 1.0 - ratio
            else:
                first, second = fa / fc, fb / fc
                numerator = ratio * (
                    2.0 * middle * first * (first - second) - (b - a) * (second - 1.0)
                )
                denominator = (first - 1.0) * (second - 1.0) * (ratio - 1.0)
            if numerator > 0.0:
                denominator = -denominator
            numerator = abs(numerator)
            if 2.0 * numerator < min(
                3.0 * middle * denominator - abs(tolerance * denominator),
                abs(previous * denominator),
            ):
                previous, step = step, numerator / denominator
            else:
                previous = step = middle
        else:
            previous = step = middle

        a, fa = b, fb
        b += step if abs(step) > tolerance else math.copysign(tolerance, middle)
        fb = excess(b)

    return SolveResult(
        method="S2",
        sigma=b,
        iterations=MAX_ITERATIONS,
        reason=f"did not converge in {MAX_ITERATIONS} iterations",
    )


# --- S3: normalised Black with a third-order Householder step -------------------


def _normalised_black(x: float, s: float) -> float:
    """Black's formula stripped to two parameters.

    `x = ln(F/K)` and `s = sigma*sqrt(T)`. Price is then `D*sqrt(F*K)*b(x, s)`, so the
    solve loses the forward, the strike and the time and becomes one equation in one
    unknown. That reduction is the first move in Jaeckel's paper, and it is why the same
    iteration behaves identically across every strike and expiry.
    """
    return math.exp(x / 2.0) * _standard_normal_cdf(x / s + s / 2.0) - math.exp(
        -x / 2.0
    ) * _standard_normal_cdf(x / s - s / 2.0)


def _normalised_derivatives(x: float, s: float) -> tuple[float, float, float]:
    """`db/ds` and its next two derivatives.

    `db/ds` collapses to a single Gaussian because `e^(x/2)*phi(d+)` equals
    `e^(-x/2)*phi(d-)`: the two terms differ only by the sign of `dd/ds`, so everything
    else cancels. A third-order step needs three derivatives, which is the whole reason
    this reduction is worth making.
    """
    first = math.exp(-x * x / (2.0 * s * s) - s * s / 8.0) / math.sqrt(2.0 * math.pi)
    slope = x * x / s**3 - s / 4.0
    return first, first * slope, first * (slope * slope - 3.0 * x * x / s**4 - 0.25)


def implied_vol_householder(
    price: float,
    forward: float,
    strike: float,
    years: float,
    discount: float,
    is_call: bool,
) -> SolveResult:
    """S3. Third-order Householder iteration on the normalised Black function.

    **This is the shape of Jaeckel's "Let's Be Rational", not the whole of it.** It takes
    his reduction to `b(x, s)` and his use of a higher-order step, which together reach
    machine precision in two or three iterations where first-order Newton needs eight.
    It does not implement his rational initial guess, nor - more importantly - his
    reformulation of `b` to avoid cancellation.

    That omission has a measured cost. `b` is a difference of two nearly-equal terms, so
    deep in the wings at low volatility the true price falls below double precision and
    the subtraction returns noise: at `K/F = 1.134` and `sigma = 15%` it evaluates to
    **-1.4e-17**, a negative price. Those cases are declined rather than solved, and
    fixing them properly is what the rest of that paper is for.
    """
    if forward <= 0.0 or strike <= 0.0 or years <= 0.0 or discount <= 0.0:
        return SolveResult(method="S3", reason="degenerate inputs")

    scale = discount * math.sqrt(forward * strike)
    x = math.log(forward / strike)
    # A put converts to the call at the same strike by parity, so one iteration serves
    # both sides rather than two code paths that can drift apart.
    call_equivalent = price if is_call else price + discount * (forward - strike)
    beta = call_equivalent / scale
    if beta <= 0.0:
        return SolveResult(
            method="S3",
            reason=(
                f"normalised price {beta:.3e} underflowed to zero or below; the true "
                "price is beneath double precision in this wing"
            ),
        )

    s = max(math.sqrt(2.0 * abs(x)), MIN_SIGMA)
    previous_step = math.inf
    for iteration in range(1, MAX_ITERATIONS + 1):
        error = _normalised_black(x, s) - beta
        first, second, third = _normalised_derivatives(x, s)
        if first <= 0.0:
            return SolveResult(
                method="S3",
                iterations=iteration,
                reason="normalised vega underflowed; no step is defined here",
            )

        ratio = error / first
        h2, h3 = second / first, third / first
        denominator = 1.0 - ratio * h2 + ratio * ratio * h3 / 6.0
        step = (
            -ratio * (1.0 - ratio * h2 / 2.0) / denominator
            if denominator != 0.0
            else -ratio
        )
        s = max(s + step, 1e-12)

        magnitude = abs(step)
        settled = magnitude <= 1e-13 * max(s, 1.0)
        # A step that stopped shrinking is the cancellation noise floor in `b`, not a
        # failure to converge - it is the best a double can do here.
        stalled = iteration > 2 and magnitude >= previous_step
        if settled or stalled:
            recovered = s / math.sqrt(years)
            pinned, slope = _identifiable(recovered, forward, strike, years, discount)
            if not pinned:
                return SolveResult(
                    method="S3",
                    iterations=iteration,
                    reason=(
                        f"vega {slope:.3e} at the solution is below the identifiability "
                        "floor; this price does not pin a volatility down"
                    ),
                )
            return SolveResult(
                method="S3",
                sigma=recovered,
                iterations=iteration,
                converged=True,
                reason="converged" if settled else "converged to the noise floor",
            )
        previous_step = magnitude

    return SolveResult(
        method="S3",
        iterations=MAX_ITERATIONS,
        reason=f"did not converge in {MAX_ITERATIONS} iterations",
    )


# --- S4: the whole chain at once ------------------------------------------------


def solve_chain_vectorised(chain, forward) -> dict[float, SolveResult]:
    """S4. S1's algorithm, applied to every strike simultaneously with NumPy.

    Identical maths to `solve_chain`, so identical answers - a vectorised solver that
    quietly disagrees with its scalar twin is worse than no vectorisation at all, and
    `test_vectorised_matches_the_scalar_solver_on_a_whole_chain` is what holds it to
    that. What changes is the shape of the work: one array of 65 strikes stepping
    together, rather than 65 independent loops.

    **NumPy has no error function**, so the normal CDF comes from `scipy.special.ndtr`.
    That is a real cost of this method and worth stating plainly: S1, S2 and S3 need
    nothing outside the standard library, and S4 pulls in two substantial dependencies
    to go faster. Whether the speed is worth them is what the timings decide.

    Vectorising Newton means every strike takes the same number of iterations - the loop
    runs until the slowest one finishes. The converged entries are simply masked out of
    later steps, so the saving is in per-element overhead, not in iteration count.
    """
    import numpy as np
    from scipy.special import ndtr

    years = year_fraction(chain)
    root_years = math.sqrt(years)
    d_f = forward.discount * forward.forward
    floor = MIN_RELATIVE_VEGA * d_f

    strikes, prices, calls = [], [], []
    unquoted: dict[float, SolveResult] = {}
    for row in chain.rows:
        is_call = row.strike >= forward.forward
        price = mid(row.call if is_call else row.put)
        if price is None or price <= 0.0:
            unquoted[row.strike] = SolveResult(
                method="S4", is_call=is_call, reason="no two-sided quote on the OTM side"
            )
            continue
        strikes.append(row.strike)
        prices.append(price)
        calls.append(is_call)

    if not strikes:
        return unquoted

    k = np.asarray(strikes, dtype=float)
    observed = np.asarray(prices, dtype=float)
    is_call = np.asarray(calls, dtype=bool)
    f, d = forward.forward, forward.discount

    def price_and_vega(sigma):
        spread = sigma * root_years
        d1 = (np.log(f / k) + 0.5 * spread * spread) / spread
        d2 = d1 - spread
        call = d * (f * ndtr(d1) - k * ndtr(d2))
        put = d * (k * ndtr(-d2) - f * ndtr(-d1))
        density = np.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
        return np.where(is_call, call, put), d * f * density * root_years

    intrinsic = d * np.where(is_call, np.maximum(f - k, 0.0), np.maximum(k - f, 0.0))
    below = observed < intrinsic - PRICE_TOLERANCE

    brenner = math.sqrt(2.0 * math.pi / years) * observed / d_f
    manaster = np.sqrt(2.0 * np.abs(np.log(f / k)) / years)
    sigma = np.clip(np.maximum(brenner, manaster), MIN_SIGMA, MAX_SIGMA)

    live = ~below
    iterations = np.zeros_like(sigma, dtype=int)
    done = np.zeros_like(sigma, dtype=bool)
    starved = np.zeros_like(sigma, dtype=bool)

    for _ in range(MAX_ITERATIONS):
        if not live.any():
            break
        modelled, slope = price_and_vega(sigma)
        error = observed - modelled
        iterations = np.where(live, iterations + 1, iterations)

        settled = live & (np.abs(error) < PRICE_TOLERANCE)
        # Matching the price is necessary and not sufficient: where vega is tiny many
        # volatilities match it, so the answer must also be identifiable.
        done |= settled & (slope >= floor)
        starved |= (settled & (slope < floor)) | (live & ~settled & (slope < floor))
        live = live & ~settled & (slope >= floor)

        stepped = sigma + np.where(live, error / np.where(slope > 0, slope, 1.0), 0.0)
        sigma = np.clip(stepped, MIN_SIGMA, MAX_SIGMA)

    solved = dict(unquoted)
    for index, strike in enumerate(strikes):
        if below[index]:
            reason = (
                f"price {observed[index]:.6f} is below intrinsic {intrinsic[index]:.6f}"
            )
        elif done[index]:
            reason = "converged"
        elif starved[index]:
            reason = "vega below the identifiability floor"
        else:
            reason = f"did not converge in {MAX_ITERATIONS} iterations"
        solved[strike] = SolveResult(
            method="S4",
            sigma=float(sigma[index]) if done[index] else None,
            iterations=int(iterations[index]),
            converged=bool(done[index]),
            reason=reason,
            is_call=bool(is_call[index]),
        )
    return solved
