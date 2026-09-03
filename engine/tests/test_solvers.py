"""S1, Newton-Raphson. Runs the pricing machine backwards.

There is no ground truth for implied volatility — it is not observable, only inverted
out of a price. So the test that carries the weight here is the **round trip**: price at
a known sigma, solve the resulting price, and get that sigma back. It is the one check
available that does not appeal to an authority.

Realistic inputs throughout, taken from `docs/forward.md`: the 04-09-2026 BTC chain,
forward 77,590.39, D 0.999706, T 3.799 days.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone

import pytest

from deltapayoff.black76 import call_price, put_price
from deltapayoff.chain import build_chain
from deltapayoff.forward import f1_parity_fit
from deltapayoff.solvers import (
    MAX_SIGMA,
    MIN_SIGMA,
    implied_vol_brent,
    implied_vol_householder,
    implied_vol_newton,
    solve_chain,
    solve_chain_vectorised,
)

CHAIN = dict(forward=77_590.39, years=0.010408517250126838, discount=0.999706)


@pytest.mark.parametrize("sigma", [0.15, 0.30, 0.45, 0.80, 1.50])
def test_newton_round_trips_a_call_across_the_volatility_range(sigma: float) -> None:
    """Price at sigma, solve, recover sigma. The only check with no authority in it."""
    price = call_price(strike=77_600.0, sigma=sigma, **CHAIN)

    result = implied_vol_newton(price, strike=77_600.0, is_call=True, **CHAIN)

    assert result.converged is True
    assert result.sigma == pytest.approx(sigma, rel=1e-10)


def test_newton_round_trips_a_put() -> None:
    """Puts invert too. A solver that only handles calls silently mis-prices half the
    chain, and every strike below the money is a put in practice."""
    price = put_price(strike=77_000.0, sigma=0.42, **CHAIN)

    result = implied_vol_newton(price, strike=77_000.0, is_call=False, **CHAIN)

    assert result.converged is True
    assert result.sigma == pytest.approx(0.42, rel=1e-10)


def test_newton_converges_in_a_handful_of_iterations() -> None:
    """The reason to use Newton at all. If it needed fifty steps, Brent would be free."""
    price = call_price(strike=77_600.0, sigma=0.45, **CHAIN)

    result = implied_vol_newton(price, strike=77_600.0, is_call=True, **CHAIN)

    assert result.iterations <= 8


def test_newton_refuses_where_vega_has_collapsed() -> None:
    """The failure region the concept predicts, asserted rather than described.

    F = 100, K = 10 gives vega = 2.07e-28. Newton's step divides the price error by
    that, so one iteration lands at a nonsense volatility. It must decline instead —
    a refusal is recoverable, a plausible wrong number is not.
    """
    degenerate = dict(forward=100.0, years=1.0, discount=1.0)
    price = call_price(strike=10.0, sigma=0.20, **degenerate)

    result = implied_vol_newton(price, strike=10.0, is_call=True, **degenerate)

    assert result.converged is False
    assert "vega" in result.reason.lower()


def test_newton_refuses_a_price_below_intrinsic() -> None:
    """A call cannot be worth less than D·(F - K); no sigma produces such a price.

    This is not a solver limitation, it is a broken quote — and on a real chain it will
    arrive eventually. Better named than iterated on.
    """
    intrinsic = CHAIN["discount"] * (CHAIN["forward"] - 60_000.0)

    result = implied_vol_newton(
        intrinsic - 100.0, strike=60_000.0, is_call=True, **CHAIN
    )

    assert result.converged is False
    assert "intrinsic" in result.reason.lower()


def test_newton_never_returns_a_negative_volatility() -> None:
    """Newton's step is unbounded, so a bad iteration can cross zero. A negative sigma
    is not a wrong answer, it is a meaningless one, and it must never leave the solver."""
    for strike in (60_000.0, 70_000.0, 77_600.0, 85_000.0, 95_000.0):
        for sigma in (0.05, 0.45, 2.0):
            price = call_price(strike=strike, sigma=sigma, **CHAIN)
            result = implied_vol_newton(price, strike=strike, is_call=True, **CHAIN)
            assert result.sigma is None or result.sigma > 0.0


# --- the wings ------------------------------------------------------------------


@pytest.mark.parametrize(
    "strike,price,is_call,moneyness",
    [
        (88_000.0, 2.55, True, "13% out of the money, call"),
        (85_000.0, 19.50, True, "10% out, call"),
        (80_000.0, 203.50, True, "3% out, call"),
        (70_000.0, 10.00, False, "10% out, put"),
        (62_500.0, 0.25, False, "19% out, put"),
    ],
)
def test_newton_solves_the_wings_of_a_real_chain(
    strike: float, price: float, is_call: bool, moneyness: str
) -> None:
    """Quotes lifted verbatim from `tickers-btc-04-09-2026.json`, mid of best bid/ask.

    These are ordinary out-of-the-money options with real volume. They are not the
    degenerate region — vega at their true volatility is far from zero. A solver that
    declines them is not being careful, it is failing.

    The Brenner-Subrahmanyam seed is exact only at the money; at 13% out it returns
    0.08%, which is a volatility at which the option is worth nothing and vega has
    underflowed to zero. Newton cannot move from there. Jäckel's point exactly: the
    iteration is not the fragile part, the starting guess is.
    """
    result = implied_vol_newton(price, strike=strike, is_call=is_call, **CHAIN)

    assert result.converged is True, f"{moneyness}: {result.reason}"
    assert 0.05 < result.sigma < 3.0


# --- solving a whole chain ------------------------------------------------------


CAPTURE_TAKEN = datetime(2026, 8, 31, 16, 49, 17, tzinfo=timezone.utc)


def captured(chain_tickers):
    return build_chain("BTC", "04-09-2026", chain_tickers, fetched_at=CAPTURE_TAKEN)


def test_solving_the_captured_chain_reaches_every_two_sided_strike(chain_tickers) -> None:
    """63 of 65. The two misses quote only one side, so there is no mid to invert.

    This number is the seed's doing. With Brenner-Subrahmanyam alone it was 19 — the
    solver reached the money and nothing else. Asserting the count is what stops a
    future change to `_seed` from quietly amputating the wings again.
    """
    chain = captured(chain_tickers)

    solved = solve_chain(chain, f1_parity_fit(chain))

    assert sum(1 for r in solved.values() if r.converged) == 63
    assert len(chain.rows) == 65


def test_solving_inverts_the_out_of_the_money_side(chain_tickers) -> None:
    """Calls above the forward, puts below.

    The OTM option carries no intrinsic value, so its whole price is time value and its
    vega is at its largest. The ITM option on the same strike prices the same
    volatility with most of its value insensitive to it — same answer in theory, far
    worse conditioned in practice.
    """
    chain = captured(chain_tickers)
    forward = f1_parity_fit(chain)

    solved = solve_chain(chain, forward)

    assert solved[88_000.0].is_call is True
    assert solved[62_500.0].is_call is False
    assert all(r.is_call == (k >= forward.forward) for k, r in solved.items())


def test_the_money_volatility_is_about_twenty_eight_percent(chain_tickers) -> None:
    """A sanity anchor with no authority in it: 28% annualised is an unremarkable BTC
    volatility, and a solver returning 3% or 300% here would be visibly broken."""
    chain = captured(chain_tickers)

    solved = solve_chain(chain, f1_parity_fit(chain))

    assert solved[77_600.0].sigma == pytest.approx(0.282, abs=0.005)


# --- S2 Brent, S3 Householder, S4 vectorised ------------------------------------


SOLVERS = {
    "S1": implied_vol_newton,
    "S2": implied_vol_brent,
    "S3": implied_vol_householder,
}


#: Below this fraction of `D·F` the generated price is at or under the floor of double
#: precision and there is nothing left to invert. **Measured**: on this chain a 70,000
#: put at sigma = 5% prices to exactly 0.0, and at 15% to 1.39e-09. Those options are
#: not hard to solve, they are worth nothing.
REPRESENTABLE = 1e-10


@pytest.mark.parametrize("name", ["S1", "S2", "S3"])
@pytest.mark.parametrize("sigma", [0.05, 0.15, 0.30, 0.45, 0.80, 1.50])
@pytest.mark.parametrize("strike", [70_000.0, 77_600.0, 85_000.0])
def test_every_solver_round_trips_or_declines(
    name: str, sigma: float, strike: float
) -> None:
    """The round trip across three solvers, six volatilities and three strikes.

    Each reaches the answer by a different route — Newton steps on vega, Brent shrinks a
    bracket it cannot leave, S3 iterates on the normalised Black function. Agreeing on
    the cases they each solve independently is the evidence that stands in for the
    ground truth implied volatility does not have.

    Both branches are asserted. Where the price is representable the solver must return
    the volatility that produced it; where it has underflowed the solver must decline.
    **Returning a number there would be the real failure**, and it is the failure this
    ticket exists to find.
    """
    is_call = strike >= CHAIN["forward"]
    model = call_price if is_call else put_price
    price = model(strike=strike, sigma=sigma, **CHAIN)
    representable = price / (CHAIN["forward"] * CHAIN["discount"]) > REPRESENTABLE

    result = SOLVERS[name](price, strike=strike, is_call=is_call, **CHAIN)

    if representable:
        assert result.converged is True, result.reason
        assert result.sigma == pytest.approx(sigma, abs=1e-6)
    else:
        assert result.converged is False, (
            f"{name} returned {result.sigma} for a price of {price:.3e}, which carries "
            "no volatility to recover"
        )


def test_brent_cannot_leave_its_bracket() -> None:
    """Brent's guarantee, and the reason to keep it despite being slower.

    Newton's step is unbounded — one bad iteration can land anywhere. Brent maintains
    two bounds it knows the root sits between and only ever shrinks the gap, so there
    is nowhere to diverge to. Asserted on the degenerate case that made Newton report
    a confident wrong answer.
    """
    degenerate = dict(forward=100.0, years=1.0, discount=1.0)
    price = call_price(strike=10.0, sigma=0.20, **degenerate)

    result = implied_vol_brent(price, strike=10.0, is_call=True, **degenerate)

    assert result.sigma is None or MIN_SIGMA <= result.sigma <= MAX_SIGMA


def test_higher_order_iteration_pays_off_across_a_chain_not_at_the_money(
    chain_tickers,
) -> None:
    """S3's claim, measured where it is actually claimable.

    At the money there is nothing to win: the Brenner-Subrahmanyam seed is excellent
    there, and S1 and S3 both take three steps. The gain shows across a whole chain,
    where most strikes are not at the money and the seed is correspondingly worse.

    **Measured** on the captured chain: median iterations S3 = 5, S1 = 8, S2 = 17.
    Brent's seventeen is the price of its guarantee — it cannot diverge because it only
    ever halves an interval, and halving is slow.
    """
    chain = captured(chain_tickers)
    forward = f1_parity_fit(chain)

    counts = {
        name: statistics.median(
            [r.iterations for r in solve_chain(chain, forward, solver=fn).values()
             if r.converged]
        )
        for name, fn in SOLVERS.items()
    }

    assert counts["S3"] < counts["S1"] < counts["S2"]


def test_all_three_solvers_agree_on_the_captured_chain(chain_tickers) -> None:
    """The evidence that stands in for ground truth, on real quotes rather than
    manufactured ones.

    All three reach 63 of 63 strikes and land within 2.4e-05 vol points of each other —
    four thousand times inside this ticket's 0.1 target. Where independent methods agree
    that closely, the assumptions they differ on are not carrying any risk.
    """
    chain = captured(chain_tickers)
    forward = f1_parity_fit(chain)

    solved = {
        name: solve_chain(chain, forward, solver=fn) for name, fn in SOLVERS.items()
    }
    common = [
        k for k in solved["S1"] if all(solved[n][k].converged for n in solved)
    ]

    assert len(common) == 63
    for name in ("S2", "S3"):
        worst = max(abs(solved["S1"][k].sigma - solved[name][k].sigma) for k in common)
        assert worst * 100 < 0.001, f"S1 vs {name} worst gap {worst * 100:.2e} vol points"


def test_householder_declines_when_the_normalised_price_underflows() -> None:
    """A real limit of double precision, not a solver bug — and worth naming as such.

    Normalised Black is a difference of two nearly-equal terms. Deep in the wings at
    low volatility the true price falls below what a double can represent and the
    subtraction returns cancellation noise: at K/F = 1.134 and sigma = 15% it comes out
    at -1.4e-17, a negative price. Reformulating to avoid that cancellation is the
    substance of Jäckel's paper, and is not implemented here.
    """
    result = implied_vol_householder(
        call_price(strike=88_000.0, sigma=0.15, **CHAIN),
        strike=88_000.0,
        is_call=True,
        **CHAIN,
    )

    assert result.converged is False
    assert "underflow" in result.reason.lower()


def test_vectorised_matches_the_scalar_solver_on_a_whole_chain(chain_tickers) -> None:
    """S4 is S1 over the chain at once. Same maths, so the same answers — a vectorised
    solver that quietly disagrees with its scalar twin is worse than no vectorisation."""
    chain = captured(chain_tickers)
    forward = f1_parity_fit(chain)

    scalar = solve_chain(chain, forward)
    vector = solve_chain_vectorised(chain, forward)

    assert set(vector) == set(scalar)
    for strike, expected in scalar.items():
        if expected.converged:
            assert vector[strike].converged is True
            assert vector[strike].sigma == pytest.approx(expected.sigma, abs=1e-9)
