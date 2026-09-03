"""Our own volatility and Greeks, added to a chain that arrived carrying Delta's.

**A chain in, the same chain out with `computed` populated.** Pure: no socket, no clock,
no network, no state. Every rule this feature has is reachable and testable here, which
is why it is a function rather than a method on the thing that happens to call it.

**Why we compute at all, when Delta publishes both.** Two reasons, and the first is the
project's whole thesis. Delta republishes its `ticker` every **5001 ms** while the order
book underneath moves every **508 ms** (both measured on a live connection), so the
venue's implied volatility is derived from prices that have already moved — up to 9.8x
stale. And Delta fits its volatility to its own `mark_price`, which is its model's
output, so reading it back is reading Delta's opinion of Delta's opinion. Inverting the
bid/ask midpoint asks the market instead.

**One volatility per strike, from the out-of-the-money leg.** In theory either side
implies the same number — put-call parity guarantees it. In practice the out-of-the-money
option holds no intrinsic value, so its entire price is time value and its vega is at its
largest, while the in-the-money option prices the same volatility with most of its value
insensitive to it. Same answer, far better conditioned. That single number is written to
**both** legs of the strike, with `iv_leg` naming the side it came from.

**Delta's fields are never read here and never written.** They travel beside ours as
reference columns so the two can be compared, which is what makes any agreement between
them evidence rather than construction. `tests/test_no_delta_inputs.py` enforces the
first half of that; `test_compute.py` enforces the second.

**The forward is F1, the parity regression across every paired strike** — no window.
`docs/forward.md` measured the alternatives: sliced by time to expiry the disagreement
between F1 and a spot-as-forward reaches 1.626 volatility points, so the forward has to
be recovered from prices rather than assumed. The solver is S1, Newton-Raphson; all four
solvers were measured to agree to 2.5e-05 volatility points, so the choice is cost and
familiarity rather than accuracy.
"""

from __future__ import annotations

from .forward import (
    ASSUMED_RATE,
    MIN_PAIRS,
    assumed_discount,
    f1_parity_fit,
    f2_single_strike,
    mid,
    year_fraction,
)
from .greeks import report_greeks
from .models import ChainResponse, ComputedLeg, Leg
from .solvers import implied_vol_newton

#: Said when a leg exists but nobody is quoting it, so there is no midpoint to invert.
NO_QUOTE = "no two-sided quote on the out-of-the-money leg"

#: Said when the strike's own leg is missing entirely rather than merely unquoted.
NO_LEG = "the out-of-the-money leg is not listed at this strike"


def _computed_for(
    leg: Leg | None,
    iv: float | None,
    iv_leg: str | None,
    reason: str,
    *,
    is_call: bool,
    forward: float,
    strike: float,
    years: float,
    discount: float,
) -> ComputedLeg:
    """One leg's block. Greeks only where a volatility was actually recovered.

    A leg with no volatility gets no Greeks either. Reporting Greeks at some default
    volatility would put five plausible numbers on the screen that describe nothing,
    which is the failure mode this project keeps refusing.
    """
    if leg is None or iv is None:
        return ComputedLeg(iv=None, iv_leg=iv_leg, iv_reason=reason)

    greeks = report_greeks(
        forward=forward,
        strike=strike,
        years=years,
        sigma=iv,
        discount=discount,
        is_call=is_call,
    )
    return ComputedLeg(
        iv=iv,
        iv_leg=iv_leg,
        iv_reason="",
        delta=greeks.delta,
        gamma=greeks.gamma,
        vega=greeks.vega,
        theta=greeks.theta,
        rho=greeks.rho,
    )


def enrich(chain: ChainResponse) -> ChainResponse:
    """`chain` with our volatility and Greeks attached to every leg.

    Returns a **new** response; the input is not mutated. A chain with no rows, or one
    whose forward cannot be fitted, comes back with `computed` blocks that say so rather
    than raising — an unfittable chain is a market condition, not a bug, and the screen
    still has quotes worth rendering.
    """
    if not chain.rows:
        return chain.model_copy(deep=True)

    fit = f1_parity_fit(chain)
    years = year_fraction(chain)

    if not fit.trusted and years > 0.0:
        # **A failed gate discredits the discount, not the forward.**
        #
        # F1 recovers both from one line: the forward is where `C - P` crosses zero, and
        # the discount is the line's slope. Those are different features with very
        # different noise. `docs/forward.md` §4 measured it — across window choices the
        # forward spans $1.23 on a $77,590 number, 1.6 basis points, while the implied
        # rate runs -17.1% to +9.4%. A crossing is an interpolation inside the strike
        # range and noise barely moves it; a slope is a tilt measured across that range,
        # where a small error is a large one in `D`.
        #
        # Near expiry this stops being academic. Under a day out the true discount is
        # within a few parts per hundred thousand of 1, so its implied rate is pure
        # quote noise: measured on the live 04-09-2026 chain a second apart, the fitted
        # discount moved between 0.99997892 and 1.00001939, taking the rate from +1.03%
        # to -0.95%. The gate rightly refuses a negative rate — but that let a sign test
        # on noise decide whether the entire chain priced.
        #
        # So the forward stands and only the discount is replaced, with the same 6.5%
        # the sibling project assumes. There is no risk-free rate for BTC; this is a
        # borrowed constant and `forward_method` says so rather than passing it off as
        # a fit.
        if fit.forward is not None and fit.n_pairs >= MIN_PAIRS:
            fit = fit.model_copy(
                update={
                    "method": "F1+assumed-rate",
                    "discount": assumed_discount(chain),
                    "implied_rate": ASSUMED_RATE,
                    "trusted": True,
                }
            )
        else:
            # Too few pairs for the crossing to be an interpolation worth trusting
            # either. F2 needs one strike quoting both sides rather than five, so a
            # sparse chain is still priceable — from a single point, and with the same
            # assumed discount.
            fallback = f2_single_strike(chain)
            if fallback.trusted and fallback.forward is not None:
                fit = fallback

    if not fit.trusted or fit.forward is None or years <= 0.0:
        # No forward, or the contract has settled. Every strike gets the same honest
        # refusal rather than a volatility inverted against a guess.
        reason = (
            "the forward could not be recovered from this chain, by parity "
            "regression or from the money strike"
            if years > 0.0
            else "the contract has expired; Greeks are undefined"
        )
        rows = []
        for row in chain.rows:
            blank = ComputedLeg(iv=None, iv_reason=reason)
            rows.append(
                row.model_copy(
                    update={
                        "call": row.call.model_copy(update={"computed": blank})
                        if row.call
                        else None,
                        "put": row.put.model_copy(update={"computed": blank})
                        if row.put
                        else None,
                    }
                )
            )
        return chain.model_copy(
            update={"rows": rows, "years_to_expiry": years, "forward_method": fit.method}
        )

    forward, discount = fit.forward, fit.discount
    rows = []

    for row in chain.rows:
        # Calls above the forward, puts below: whichever leg is out of the money.
        is_call = row.strike >= forward
        source = row.call if is_call else row.put
        iv_leg = "call" if is_call else "put"

        iv: float | None = None
        reason = NO_LEG if source is None else NO_QUOTE

        price = mid(source)
        if price is not None and price > 0.0:
            solved = implied_vol_newton(
                price,
                forward=forward,
                strike=row.strike,
                years=years,
                discount=discount,
                is_call=is_call,
            )
            if solved.converged and solved.sigma is not None:
                iv, reason = solved.sigma, ""
            else:
                # The solver's own account, kept verbatim. "Did not converge" and "vega
                # collapsed" are different facts about the chain and worth telling apart.
                reason = solved.reason or "the solver did not converge"

        rows.append(
            row.model_copy(
                update={
                    "call": row.call.model_copy(
                        update={
                            "computed": _computed_for(
                                row.call, iv, iv_leg, reason,
                                is_call=True,
                                forward=forward,
                                strike=row.strike,
                                years=years,
                                discount=discount,
                            )
                        }
                    )
                    if row.call
                    else None,
                    "put": row.put.model_copy(
                        update={
                            "computed": _computed_for(
                                row.put, iv, iv_leg, reason,
                                is_call=False,
                                forward=forward,
                                strike=row.strike,
                                years=years,
                                discount=discount,
                            )
                        }
                    )
                    if row.put
                    else None,
                }
            )
        )

    return chain.model_copy(
        update={
            "rows": rows,
            "forward": forward,
            "discount": discount,
            "years_to_expiry": years,
            "forward_method": fit.method,
        }
    )
