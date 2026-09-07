"""Delta's own IV and Greeks are reference columns. They are never an input.

This is the ticket's strictest rule and the easiest one to break by accident, because
`Leg` carries `mark_iv`, `bid_iv`, `ask_iv` and all five Greeks right alongside the bid
and ask we legitimately use. One stray read and the study would be measuring how well we
imitate Delta rather than what the prices imply.

The check is behavioural rather than textual. Grepping the source for `mark_iv` proves
nothing about what runs; corrupting every Delta-published number to nonsense and
demanding identical output proves it directly. If any of these fields were consumed
anywhere, at least one number downstream would move.

**Two paths, since #37.** The REST snapshot still arrives as ticker dictionaries and is
poisoned here as it always was. The live ladder no longer arrives as frames at all - it is
folded out of `md.option_reference`, which carries the venue's IV and its five greeks as
payload fields - so the last section of this file poisons *the event* and demands the same
silence. The reference event is what #37 put between the venue and the solver, and an
invariant tested on one of two paths is an invariant with a hole in it.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from deltapayoff.chain import build_chain, chain_from_legs
from deltapayoff.compute import enrich
from deltapayoff.events import IndexQuote, OptionReference
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
from deltapayoff.stream import leg_from_events
from fakes.decoder import events_from_frame

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


# --- the same rule, on the event path -------------------------------------------


#: What `md.option_reference` carries that is the venue's **opinion** rather than an
#: observed price: its three implied vols, its five greeks, and its mark, which is a model
#: output and not a number anyone traded at.
#:
#: `bid` and `ask` are deliberately **not** on this list. #37 added them to the event, and
#: they are quotes: the same numbers `md.option_quote` carries, and the numbers every
#: implied volatility in this project is inverted out of. Poisoning them would test that
#: the solver ignores its own input.
POISONED_EVENT_FIELDS = {
    "bid_iv": 9999.0,
    "ask_iv": 9999.0,
    "mark_iv": 9999.0,
    "delta": -12345.0,
    "gamma": -12345.0,
    "theta": -12345.0,
    "vega": -12345.0,
    "rho": -12345.0,
    "mark": 1.0,
}


def ladder_from_events(ticker_frames, book_frames, taken, *, poison: bool):
    """The live ladder, folded the way the chain cache folds it, optionally poisoned.

    Frames go through the real adapter, so these are the producer's events and not this
    file's idea of them. Only the venue's opinions are overwritten; the quotes, the
    strikes and the spot the study is allowed to read arrive untouched.
    """
    references = {}
    quotes = {}
    spot = None

    for frame in ticker_frames.values():
        for event in events_from_frame("ticker", frame):
            if isinstance(event, IndexQuote):
                spot = event.spot if spot is None else spot
            elif isinstance(event, OptionReference):
                if poison:
                    event = event.model_copy(update=POISONED_EVENT_FIELDS)
                references[event.instrument.canonical()] = event
    for frame in book_frames.values():
        for event in events_from_frame("ob_l2", frame):
            quotes[event.instrument.canonical()] = event

    legs = []
    for key, reference in references.items():
        instrument = reference.instrument
        legs.append(
            (
                float(instrument.strike),
                instrument.right.side,
                leg_from_events(instrument, reference, quotes.get(key)),
            )
        )
    return chain_from_legs("BTC", "04-09-2026", legs, spot, fetched_at=taken)


@pytest.fixture
def event_pair(ws_ticker_frames, ws_book_frames, ws_captured_at):
    honest = ladder_from_events(
        ws_ticker_frames, ws_book_frames, ws_captured_at, poison=False
    )
    poisoned = ladder_from_events(
        ws_ticker_frames, ws_book_frames, ws_captured_at, poison=True
    )
    return honest, poisoned


def test_the_event_corruption_actually_reaches_the_ladder(event_pair) -> None:
    """Guard on the guard, for the second path. If poisoning the event silently did
    nothing, every assertion below it would pass for the wrong reason."""
    honest, poisoned = event_pair

    assert len(honest.rows) == len(poisoned.rows) == 69
    honest_leg = honest.rows[10].call
    poisoned_leg = poisoned.rows[10].call

    assert honest_leg.mark_iv != poisoned_leg.mark_iv
    assert poisoned_leg.mark_iv == 9999.0
    assert poisoned_leg.delta == -12345.0
    assert poisoned_leg.mark == 1.0
    # ...while the quotes and the spot the study is allowed to read are untouched.
    assert honest_leg.bid == poisoned_leg.bid
    assert honest_leg.ask == poisoned_leg.ask
    assert honest.spot == poisoned.spot
    assert honest.spot is not None


@pytest.mark.parametrize("method", [f1_parity_fit, f2_single_strike, f3_carry, f4_spot])
def test_no_forward_moves_when_the_reference_event_is_poisoned(
    event_pair, method
) -> None:
    """All four forwards come from quotes, strikes and spot. The reference event carries
    none of those three and must move none of them."""
    honest, poisoned = event_pair

    assert method(honest).forward == method(poisoned).forward
    assert method(honest).discount == method(poisoned).discount


@pytest.mark.parametrize(
    "solver", [implied_vol_newton, implied_vol_brent, implied_vol_householder]
)
def test_no_implied_volatility_moves_when_the_reference_event_is_poisoned(
    event_pair, solver
) -> None:
    """**The one that matters most, on the path #37 built.** Every IV on the live ladder
    is inverted out of a bid/ask midpoint under a forward we recovered ourselves - never
    seeded from, checked against, or nudged toward the venue's published figure, which now
    travels on the same event as the quotes rather than on a frame of its own.
    """
    honest, poisoned = event_pair
    clean = solve_chain(honest, f1_parity_fit(honest), solver=solver)
    dirty = solve_chain(poisoned, f1_parity_fit(poisoned), solver=solver)

    assert {k: v.sigma for k, v in clean.items()} == {
        k: v.sigma for k, v in dirty.items()
    }
    assert any(result.sigma is not None for result in clean.values()), (
        "nothing solved at all, so this proves nothing"
    )


def test_the_whole_enrichment_is_unmoved_by_a_poisoned_event(event_pair) -> None:
    """End to end: what a browser is sent for the poisoned ladder is what it is sent for
    the honest one, everywhere our own numbers live.

    `compute.enrich` is the function the chain cache actually calls, so this is the ladder
    a screen would render rather than a solver called in isolation.
    """
    honest, poisoned = event_pair
    clean, dirty = enrich(honest), enrich(poisoned)

    assert (clean.forward, clean.discount) == (dirty.forward, dirty.discount)
    ours = [
        (row.strike, leg.computed)
        for row in clean.rows
        for leg in (row.call, row.put)
        if leg is not None
    ]
    theirs = [
        (row.strike, leg.computed)
        for row in dirty.rows
        for leg in (row.call, row.put)
        if leg is not None
    ]
    assert ours == theirs
    assert any(computed.iv is not None for _, computed in ours), "nothing was solved"
