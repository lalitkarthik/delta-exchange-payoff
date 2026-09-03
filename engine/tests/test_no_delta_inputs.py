"""Delta's own IV and Greeks are reference columns. They are never an input.

This is the ticket's strictest rule and the easiest one to break by accident, because
`Leg` carries `mark_iv`, `bid_iv`, `ask_iv` and all five Greeks right alongside the bid
and ask we legitimately use. One stray read and the study would be measuring how well we
imitate Delta rather than what the prices imply.

The check is behavioural rather than textual. Grepping the source for `mark_iv` proves
nothing about what runs; corrupting every Delta-published number to nonsense and
demanding identical output proves it directly. If any of these fields were consumed
anywhere, at least one number downstream would move.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from deltapayoff.chain import build_chain
from deltapayoff.forward import (
    f1_parity_fit,
    f2_single_strike,
    f3_carry,
    f4_spot,
    sweep_widths,
)
from deltapayoff.solvers import (
    implied_vol_brent,
    implied_vol_householder,
    implied_vol_newton,
    solve_chain,
    solve_chain_vectorised,
)

CAPTURE_TAKEN = datetime(2026, 8, 31, 16, 49, 17, tzinfo=timezone.utc)

#: Every field Delta publishes that is its own opinion rather than an observed price.
#: `mark_price` is on this list too: it is Delta's model output, not a traded or quoted
#: number, so fitting through it would recover Delta's forward rather than the market's.
DELTA_OPINIONS = ("mark_iv", "bid_iv", "ask_iv")
DELTA_GREEKS = ("delta", "gamma", "theta", "vega", "rho", "spot")


def corrupted(tickers):
    """The same chain with every Delta-published opinion replaced by nonsense.

    Quoted prices, strikes, `spot_price` and the symbols are untouched — those are the
    inputs the study is allowed to have.
    """
    poisoned = copy.deepcopy(tickers)
    for ticker in poisoned:
        for field in DELTA_OPINIONS:
            if ticker.get("quotes") is not None:
                ticker["quotes"][field] = "9999.0"
        for field in DELTA_GREEKS:
            if ticker.get("greeks") is not None:
                ticker["greeks"][field] = "-12345.0"
        ticker["mark_price"] = "1.0"
        ticker["mark_vol"] = "9999.0"
    return poisoned


@pytest.fixture
def pair(chain_tickers):
    honest = build_chain("BTC", "04-09-2026", chain_tickers, fetched_at=CAPTURE_TAKEN)
    poisoned = build_chain(
        "BTC", "04-09-2026", corrupted(chain_tickers), fetched_at=CAPTURE_TAKEN
    )
    return honest, poisoned


def test_the_corruption_actually_reaches_the_chain(pair) -> None:
    """Guard on the guard. If the poisoning silently did nothing, every test below would
    pass for the wrong reason and this file would be worthless."""
    honest, poisoned = pair

    assert honest.rows[10].call.mark_iv != poisoned.rows[10].call.mark_iv
    assert poisoned.rows[10].call.mark_iv == 9999.0
    assert poisoned.rows[10].call.delta == -12345.0
    assert poisoned.rows[10].call.mark == 1.0
    # ...while the quotes the study is allowed to read are untouched.
    assert honest.rows[10].call.bid == poisoned.rows[10].call.bid
    assert honest.spot == poisoned.spot


@pytest.mark.parametrize(
    "method", [f1_parity_fit, f2_single_strike, f3_carry, f4_spot]
)
def test_no_forward_moves_when_delta_opinions_are_poisoned(pair, method) -> None:
    """All four forwards come from quotes, strikes and spot. Nothing else."""
    honest, poisoned = pair

    assert method(honest).forward == method(poisoned).forward
    assert method(honest).discount == method(poisoned).discount


def test_the_whole_sweep_is_unmoved(pair) -> None:
    """Including the unwindowed fit, which touches every strike on the board."""
    honest, poisoned = pair

    assert [r.forward for r in sweep_widths(honest)] == [
        r.forward for r in sweep_widths(poisoned)
    ]


@pytest.mark.parametrize(
    "solver", [implied_vol_newton, implied_vol_brent, implied_vol_householder]
)
def test_no_implied_volatility_moves_when_delta_opinions_are_poisoned(
    pair, solver
) -> None:
    """The one that matters most. Every IV in this study is inverted out of a bid/ask
    midpoint under a forward we recovered ourselves — never seeded from, checked
    against, or nudged toward Delta's published figure."""
    honest, poisoned = pair
    forward = f1_parity_fit(honest)

    clean = solve_chain(honest, forward, solver=solver)
    dirty = solve_chain(poisoned, f1_parity_fit(poisoned), solver=solver)

    assert {k: v.sigma for k, v in clean.items()} == {
        k: v.sigma for k, v in dirty.items()
    }


def test_the_vectorised_solver_is_clean_too(pair) -> None:
    """S4 reaches into the chain differently — arrays rather than a walk — so it gets
    its own assertion rather than inheriting one."""
    honest, poisoned = pair

    clean = solve_chain_vectorised(honest, f1_parity_fit(honest))
    dirty = solve_chain_vectorised(poisoned, f1_parity_fit(poisoned))

    assert {k: v.sigma for k, v in clean.items()} == {
        k: v.sigma for k, v in dirty.items()
    }
