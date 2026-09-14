"""The pure payoff core: legs in, corners and metrics out. No network, no clock, no file.

**Every expected number here was worked by hand and is written as a literal.** Several
are lifted straight out of `docs/payoff-contract.md`'s own worked example, which is the
strongest source of truth available: it was written before either side existed. Nothing
below recomputes an expectation the way the module computes it — a payoff engine whose
tests re-derive the payoff proves only that the arithmetic is self-consistent.

Prior art: `test_forward.py`, which plants a chain obeying parity exactly so the answer
is a literal rather than the code under test speaking to itself.
"""

from __future__ import annotations

import ast
import inspect
import math

import pytest

from deltapayoff import payoff
from deltapayoff.payoff import (
    PayoffLeg,
    end_slopes,
    net_premium,
    payoff_curve,
    payoff_table,
    pnl_at_expiry,
    position_greeks,
    readable_step,
    scale_greeks,
    strategy_metrics,
    suggested_window,
)
from deltapayoff.payoff_models import Curve, Greeks, Window

# `docs/payoff-contract.md`'s worked example: one BTC 77000 call, bought at 1240.
CONTRACT_CALL = PayoffLeg(
    strike=77_000.0, is_call=True, direction=1, quantity=1, entry_price=1240.0
)

# A short strangle: sell the 70000 put for 900 and the 85000 call for 1100, taking in
# 2,000 and keeping it if BTC finishes between the two.
SHORT_STRANGLE = [
    PayoffLeg(
        strike=70_000.0, is_call=False, direction=-1, quantity=1, entry_price=900.0
    ),
    PayoffLeg(
        strike=85_000.0, is_call=True, direction=-1, quantity=1, entry_price=1100.0
    ),
]


def test_a_long_call_is_worth_its_intrinsic_value_less_the_premium() -> None:
    """The three corner P&Ls in `docs/payoff-contract.md`'s own response example.

    Below and at the strike the call expires worthless and the loss is the premium;
    4,000 above it the intrinsic value is 4,000 and the P&L is 4,000 - 1,240 = 2,760.
    """
    assert pnl_at_expiry(74_000.0, [CONTRACT_CALL]) == -1240.0
    assert pnl_at_expiry(77_000.0, [CONTRACT_CALL]) == -1240.0
    assert pnl_at_expiry(81_000.0, [CONTRACT_CALL]) == 2760.0

# A long call butterfly: buy the 74000 call for 4,000, sell two 77000 calls for 2,000
# each, buy the 80000 call for 900. Net 900 paid out for a capped structure.
BUTTERFLY = [
    PayoffLeg(
        strike=74_000.0, is_call=True, direction=1, quantity=1, entry_price=4000.0
    ),
    PayoffLeg(
        strike=77_000.0, is_call=True, direction=-1, quantity=2, entry_price=2000.0
    ),
    PayoffLeg(
        strike=80_000.0, is_call=True, direction=1, quantity=1, entry_price=900.0
    ),
]


def test_net_premium_is_positive_when_paid_out_and_negative_when_received() -> None:
    """A debit and a credit, on the one axis `docs/payoff-contract.md` fixes.

    The long call costs 1,240 and the number is +1,240. The strangle takes in
    900 + 1,100 and the number is -2,000. The opposite convention is just as defensible
    and is the one half the industry uses, which is exactly why it is pinned here.
    """
    assert net_premium([CONTRACT_CALL]) == 1240.0
    assert net_premium(SHORT_STRANGLE) == -2000.0


def test_the_end_slopes_are_the_net_call_and_put_quantities() -> None:
    """Far enough out, only the legs that are in the money still move.

    Above every strike every call is exercised and every put is dead, so the line rises
    one dollar per dollar for each net long call: the long call has +1, the short
    strangle -1, and the butterfly's +1 - 2 + 1 = 0, which is what caps it. Below every
    strike the puts are the ones alive and each net long put falls one for one, so the
    sign flips: the strangle's short put gives +1 and the two all-call strategies 0.
    """
    assert end_slopes([CONTRACT_CALL]) == (0.0, 1.0)
    assert end_slopes(SHORT_STRANGLE) == (1.0, -1.0)
    assert end_slopes(BUTTERFLY) == (0.0, 0.0)


def test_a_zero_slope_is_zero_and_never_minus_zero() -> None:
    """`-0.0` is a real float, a valid JSON number, and a lie on a chart.

    An all-call strategy has no puts, so the left slope is `-sum(nothing)` — and IEEE 754
    says the negation of positive zero is **negative** zero. It compares equal to zero
    everywhere, so nothing in this suite or in the browser catches it; it survives
    `JSON.parse` intact and then `Number(-0).toFixed(2)` renders `"-0.00"` on an axis
    beside four honest numbers, which is exactly the kind of detail that makes a reader
    stop trusting the other four.

    `copysign` is the assertion because `== 0.0` cannot tell the two apart.
    """
    left, right = end_slopes([CONTRACT_CALL])

    assert math.copysign(1.0, left) == 1.0, "the left slope came back as minus zero"
    assert math.copysign(1.0, right) == 1.0
    assert (left, right) == (0.0, 1.0)

    # The butterfly is the other way in: both ends are zero, and its left one is the
    # negation of a genuine zero sum rather than of an empty one.
    flat_left, flat_right = end_slopes(BUTTERFLY)
    assert math.copysign(1.0, flat_left) == 1.0
    assert math.copysign(1.0, flat_right) == 1.0


def test_the_window_opens_three_standard_deviations_either_side_of_the_anchor() -> None:
    """Hand-worked: 3 sigma at 40% volatility over a quarter of a year.

    3 * 0.40 * sqrt(0.25) = 3 * 0.40 * 0.5 = **0.60** in log space. From a table of
    exponentials, e^0.60 = 1.8221188 and e^-0.60 = 0.5488116, so an 80,000 anchor opens
    on 80,000 * 0.5488116 = **43,904.93** and 80,000 * 1.8221188 = **145,769.50**.

    Log space rather than a percentage of the anchor, because 3 sigma in price terms
    runs the low end negative the moment volatility * sqrt(time) passes a third — 70%
    volatility three months out, which is an ordinary week on this venue.
    """
    window = suggested_window(
        [CONTRACT_CALL], anchor=80_000.0, atm_iv=0.40, years=0.25
    )
    assert window.low == pytest.approx(43_904.93088752, rel=1e-10)
    assert window.high == pytest.approx(145_769.50403124, rel=1e-10)


def test_the_window_widens_to_hold_a_strike_that_falls_outside_three_sigma() -> None:
    """A wing outside the frame draws a capped loss as an uncapped one.

    Three sigma at 10% volatility over 0.01 years is 3 * 0.10 * 0.1 = 0.03 in log
    space — an 80,000 anchor framing 77,635.64 to 82,436.36, which holds neither of the
    strangle's strikes. Both ends are pushed out to the strikes themselves, exactly, so
    the outermost corner is on the edge rather than off it.
    """
    window = suggested_window(
        SHORT_STRANGLE, anchor=80_000.0, atm_iv=0.10, years=0.01
    )
    assert window.low == 70_000.0
    assert window.high == 85_000.0


def test_the_curve_is_the_window_ends_and_a_corner_at_every_strike() -> None:
    """`docs/payoff-contract.md`'s response example, field for field.

    Its `curve` block is three corners — 74,000 at -1,240, the strike at -1,240 and
    81,000 at 2,760 — with `slope_left` 0.0 and `slope_right` 1.0. Those numbers were
    written into the contract before either side of it existed, which makes them the
    one expectation in this file that no implementation could have suggested.
    """
    curve = payoff_curve([CONTRACT_CALL], Window(low=74_000.0, high=81_000.0))

    assert [(point.price, point.pnl) for point in curve.corners] == [
        (74_000.0, -1240.0),
        (77_000.0, -1240.0),
        (81_000.0, 2760.0),
    ]
    assert curve.slope_left == 0.0
    assert curve.slope_right == 1.0
    assert curve.window.low == 74_000.0


def test_a_long_call_risks_the_premium_and_can_make_anything() -> None:
    """`docs/payoff-contract.md`'s `metrics` block, field for field.

    `max_profit` null because the right-hand slope is +1 and never stops; `max_loss`
    **-1240**, a P&L on the chart's own axis rather than a magnitude to be sign-flipped
    in the reader's head; one breakeven at 77,000 + 1,240 = **78,240**; and
    `reward_risk` null, because a ratio against unlimited has no meaning and a large
    number in its place would read as a good trade.
    """
    metrics = strategy_metrics([CONTRACT_CALL])

    assert metrics.max_profit is None
    assert metrics.max_loss == -1240.0
    assert metrics.breakevens == [78_240.0]
    assert metrics.net_premium == 1240.0
    assert metrics.reward_risk is None


def test_a_short_strangle_keeps_the_credit_and_can_lose_without_limit() -> None:
    """Worked by hand from the two strikes and the 2,000 taken in.

    Anywhere between 70,000 and 85,000 both options expire worthless and the whole
    credit is kept, so max profit is **2,000** — a maximum at neither end of the chart.
    Above 85,000 the short call costs a dollar for every dollar, forever, so max loss is
    **null** and the ratio with it. The two breakevens are the credit walked out from
    each strike: 70,000 - 2,000 = **68,000** and 85,000 + 2,000 = **87,000**.
    """
    metrics = strategy_metrics(SHORT_STRANGLE)

    assert metrics.max_profit == 2000.0
    assert metrics.max_loss is None
    assert metrics.breakevens == [68_000.0, 87_000.0]
    assert metrics.net_premium == -2000.0
    assert metrics.reward_risk is None


def test_a_butterfly_is_capped_at_both_ends() -> None:
    """Worked by hand: 4,000 - 2 * 2,000 + 900 = **900** paid out.

    At 77,000 the long 74000 call is worth 3,000 and the other three legs are worthless,
    so the P&L is 3,000 - 900 = **2,100**. Outside the wings the legs cancel and all
    that is left is the 900, so max loss is **-900** at both ends and everywhere below
    74,000. Reward:risk is 2,100 / 900 = **7/3**.

    The breakevens are the debit walked in from each wing: 74,000 + 900 = **74,900** and
    80,000 - 900 = **79,100**.
    """
    metrics = strategy_metrics(BUTTERFLY)

    assert metrics.max_profit == 2100.0
    assert metrics.max_loss == -900.0
    assert metrics.breakevens == [74_900.0, 79_100.0]
    assert metrics.net_premium == 900.0
    assert metrics.reward_risk == pytest.approx(7 / 3)


def test_a_breakeven_that_lands_exactly_on_a_strike_is_found_once() -> None:
    """A synthetic long forward: buy the 77000 call for 1,000, sell the 77000 put for
    1,000.

    It costs nothing, and its P&L is exactly `price - 77,000` everywhere, so the one
    price at which it breaks even **is** a strike. That makes it the case both obvious
    implementations get wrong: solving each segment that meets at the kink reports
    77,000 twice, and testing each segment for a strict change of sign reports it not at
    all, because neither segment changes sign — the left one runs up to zero and the
    right one away from it.
    """
    synthetic_forward = [
        PayoffLeg(
            strike=77_000.0, is_call=True, direction=1, quantity=1, entry_price=1000.0
        ),
        PayoffLeg(
            strike=77_000.0, is_call=False, direction=-1, quantity=1, entry_price=1000.0
        ),
    ]

    metrics = strategy_metrics(synthetic_forward)

    assert metrics.breakevens == [77_000.0]
    assert metrics.net_premium == 0.0
    # Nothing is left of it if BTC goes to zero, and it has no ceiling.
    assert metrics.max_loss == -77_000.0
    assert metrics.max_profit is None


def test_a_tail_lying_flat_on_zero_is_one_breakeven_and_not_a_range() -> None:
    """A zero-cost put spread: buy the 76000 put for 1,000, sell the 77000 put for
    1,000.

    Above 77,000 both expire worthless and the P&L is flat at exactly zero, for every
    price forever. That is not an unbounded run of breakevens and it is not two: the
    only price worth reporting is 77,000, where the strategy stops breaking even and
    starts losing. Below 76,000 it is flat at **-1,000**, which is also its max loss.
    """
    zero_cost_put_spread = [
        PayoffLeg(
            strike=76_000.0, is_call=False, direction=1, quantity=1, entry_price=1000.0
        ),
        PayoffLeg(
            strike=77_000.0, is_call=False, direction=-1, quantity=1, entry_price=1000.0
        ),
    ]

    metrics = strategy_metrics(zero_cost_put_spread)

    assert metrics.breakevens == [77_000.0]
    assert metrics.max_loss == -1000.0
    assert metrics.max_profit == 0.0
    # A real zero, and not `None`: the best this does is break even, and 0 / 1,000 is a
    # ratio that exists. `None` would say the ratio is meaningless, which is a different
    # and better-sounding claim.
    assert metrics.reward_risk == 0.0


def test_a_strategy_that_cannot_lose_gets_no_ratio_rather_than_a_division_by_zero() -> (
    None
):
    """The same spread the other way up: sell the 76000 put for 1,000, buy the 77000
    put for 1,000.

    It cost nothing and it cannot lose: the worst it does is zero, above 77,000. So
    `max_loss` is **0** — a real zero, and not `null`, which would say it can lose
    everything — and `reward_risk` is `null`, because there is no loss to divide the
    1,000 by. The naive ratio raises `ZeroDivisionError` here, and the naive fix
    publishes `inf`, which is not JSON.
    """
    zero_cost_put_spread = [
        PayoffLeg(
            strike=76_000.0, is_call=False, direction=-1, quantity=1, entry_price=1000.0
        ),
        PayoffLeg(
            strike=77_000.0, is_call=False, direction=1, quantity=1, entry_price=1000.0
        ),
    ]

    metrics = strategy_metrics(zero_cost_put_spread)

    assert metrics.net_premium == 0.0
    assert metrics.max_loss == 0.0
    assert metrics.max_profit == 1000.0
    assert metrics.reward_risk is None


def test_the_table_steps_across_the_window_on_round_prices() -> None:
    """The contract's own 74,000-to-81,000 frame, read as a table.

    7,000 wide across the two dozen or so rows a trader can take in is a little under
    300, and the next round number up the 1-2-2.5-5 ladder is **500** — so the rows run
    74,000, 74,500, ... 81,000, fifteen of them, on prices that are strikes on this
    chain rather than 74,291.67 and its multiples.

    The P&Ls are the long call's again and were worked by hand: flat at -1,240 until the
    strike, then 500 better every row. 78,500 is 1,500 of intrinsic value less the 1,240
    premium = **260**, the first row in profit, and 81,000 is the 2,760 the contract's
    own corner list ends on.
    """
    table = payoff_table([CONTRACT_CALL], Window(low=74_000.0, high=81_000.0))

    assert [point.price for point in table] == [
        74_000.0, 74_500.0, 75_000.0, 75_500.0, 76_000.0, 76_500.0, 77_000.0,
        77_500.0, 78_000.0, 78_500.0, 79_000.0, 79_500.0, 80_000.0, 80_500.0,
        81_000.0,
    ]
    assert table[6].pnl == -1240.0
    assert table[9].pnl == 260.0
    assert table[-1].pnl == 2760.0


def test_the_table_carries_every_corner_so_the_peak_is_a_row() -> None:
    """A frame whose ends are not round numbers, around the butterfly.

    32,914.90 across two dozen rows wants about 1,371, so the step is **2,000** — and
    2,000 steps snapped to their own multiples run 64,000, 66,000, ... 94,000, which
    steps straight over the 77,000 peak. Folding the corners back in is what stops the
    table listing this strategy's best outcome as 1,100 at 76,000 when the chart beside
    it draws **2,100** at the strike, and stops the two ends of the frame going
    unlisted.
    """
    window = Window(low=62_281.70, high=95_196.60)
    table = payoff_table(BUTTERFLY, window)
    prices = [point.price for point in table]

    assert prices[0] == 62_281.70
    assert prices[-1] == 95_196.60
    assert prices[3] - prices[2] == 2000.0
    assert 77_000.0 in prices
    assert table[prices.index(77_000.0)].pnl == 2100.0

    # One quantity sampled twice: the two must agree wherever they share a price.
    curve = payoff_curve(BUTTERFLY, window)
    for corner in curve.corners:
        assert table[prices.index(corner.price)].pnl == corner.pnl


# The per-leg exposures `docs/payoff-contract.md` prints in its own example response,
# and a plausible second set for a put. Both are per one unit and unsigned, as
# `compute` solves them — the weighting is the payoff core's job.
CONTRACT_CALL_GREEKS = Greeks(
    delta=0.5231, gamma=0.0000312, vega=0.4118, theta=-66.58, rho=0.129
)
A_PUTS_GREEKS = Greeks(delta=-0.30, gamma=0.00002, vega=0.20, theta=-40.0, rho=-0.05)


def test_position_greeks_weight_each_leg_by_direction_and_quantity() -> None:
    """One long call and two short puts, added up by hand.

    Weights +1 and -2, so delta is 0.5231 + 0.6 = **1.1231** and theta, the number a
    short option makes positive, is -66.58 + 80 = **13.42**. Gamma crosses zero:
    0.0000312 - 0.00004 = **-0.0000088**, which is the whole point of summing signed
    rather than summing magnitudes.
    """
    legs = [
        PayoffLeg(
            strike=77_000.0,
            is_call=True,
            direction=1,
            quantity=1,
            entry_price=1240.0,
            greeks=CONTRACT_CALL_GREEKS,
        ),
        PayoffLeg(
            strike=70_000.0,
            is_call=False,
            direction=-1,
            quantity=2,
            entry_price=900.0,
            greeks=A_PUTS_GREEKS,
        ),
    ]

    total = position_greeks(legs)

    assert total is not None
    assert total.delta == pytest.approx(1.1231)
    assert total.gamma == pytest.approx(-0.0000088)
    assert total.vega == pytest.approx(0.0118)
    assert total.theta == pytest.approx(13.42)
    assert total.rho == pytest.approx(0.229)


def test_one_leg_without_greeks_leaves_the_position_with_none_at_all() -> None:
    """Not a sum over the legs that happened to solve.

    A strike whose out-of-the-money leg has no two-sided quote yields no volatility and
    so no Greeks, and adding up the rest would put a delta on screen for a position
    nobody holds — with nothing on that screen saying a leg was left out.
    """
    solved = PayoffLeg(
        strike=77_000.0,
        is_call=True,
        direction=1,
        quantity=1,
        entry_price=1240.0,
        greeks=CONTRACT_CALL_GREEKS,
    )
    unsolved = PayoffLeg(
        strike=120_000.0, is_call=True, direction=-1, quantity=1, entry_price=5.0
    )

    assert position_greeks([solved, unsolved]) is None
    assert position_greeks([solved]) is not None


def test_a_leg_is_scaled_and_signed_but_its_entry_price_is_not() -> None:
    """Two sold, so every exposure is the per-unit one times -2.

    Delta -0.30 becomes **+0.60**, theta -40.00 becomes **+80.00**: selling a put is
    long the underlying and collects the decay. The five together are what the response
    prints beside that leg.
    """
    scaled = scale_greeks(A_PUTS_GREEKS, -2.0)

    assert scaled.delta == pytest.approx(0.60)
    assert scaled.gamma == pytest.approx(-0.00004)
    assert scaled.vega == pytest.approx(-0.40)
    assert scaled.theta == pytest.approx(80.0)
    assert scaled.rho == pytest.approx(0.10)


def test_the_window_still_opens_when_there_is_no_volatility_to_scale_by() -> None:
    """An unfitted chain has no at-the-money volatility, and the chart still opens.

    The curve and the metrics need no model at all — a P&L at expiry is intrinsic value
    and a subtraction — so a missing volatility must not take the frame down with it.
    The fall-back half-width is nominal and the strikes still widen it, so a butterfly
    with wings at 74,000 and 80,000 is framed around both of them.
    """
    window = suggested_window(BUTTERFLY, anchor=77_000.0, atm_iv=None, years=None)

    assert window.low < 74_000.0
    assert window.high > 80_000.0


def test_the_payoff_core_imports_nothing_that_could_reach_out() -> None:
    """A socket, a clock or a file would make this module untestable in milliseconds.

    Checked against the module's own import statements rather than `sys.modules`, which
    is full of things Pydantic dragged in. The named four are the ones that would do the
    damage; the assertion is an exact set, so a fifth cannot arrive unnoticed either.
    """
    tree = ast.parse(inspect.getsource(payoff))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))

    assert imported == {
        "__future__",
        "math",
        "collections.abc",
        "dataclasses",
        ".payoff_models",
    }


def read_off_the_chart(curve: Curve, price: float) -> float:
    """What the browser draws at `price`, given only what the contract sends it.

    Straight segments between the corners, and the two end slopes extended beyond them —
    `docs/payoff-contract.md`'s rendering rule, written out so a test can hold the engine
    to the promise that those rays are exact **however far the reader zooms**. Nothing
    here consults the legs, which is the point: it sees exactly what the wire carries.
    """
    if price <= curve.corners[0].price:
        first = curve.corners[0]
        return first.pnl + curve.slope_left * (price - first.price)
    if price >= curve.corners[-1].price:
        last = curve.corners[-1]
        return last.pnl + curve.slope_right * (price - last.price)
    for left, right in zip(curve.corners, curve.corners[1:], strict=False):
        if left.price <= price <= right.price:
            share = (price - left.price) / (right.price - left.price)
            return left.pnl + share * (right.pnl - left.pnl)
    raise AssertionError("the corners did not cover the price")


def test_the_rays_stay_exact_on_a_window_narrower_than_the_strikes() -> None:
    """A frame that holds none of the butterfly's wings, and a chart still correct
    outside it.

    `docs/payoff-contract.md` calls the window a suggestion rather than a clamp and
    promises the two rays are exact however far out the reader zooms. Dropping the
    corners that fall outside the frame breaks that quietly: the slopes are still read
    off the whole leg mix, so the ray leaving 78,000 would run flat at 1,100 forever
    while the strategy is worth **-900** at 82,000, and the ray leaving 76,000 would say
    1,100 at 75,000 where the strategy is worth **100**. Both worked by hand from the
    four leg prices; both draw perfectly plausibly.
    """
    curve = payoff_curve(BUTTERFLY, Window(low=76_000.0, high=78_000.0))

    assert read_off_the_chart(curve, 75_000.0) == 100.0
    assert read_off_the_chart(curve, 82_000.0) == -900.0
    assert [point.price for point in curve.corners] == [
        74_000.0, 76_000.0, 77_000.0, 78_000.0, 80_000.0,
    ]


def test_the_step_ladder_has_a_rung_between_two_and_five() -> None:
    """55 across two dozen rows wants 2.29, and the answer is **2.5**, not 5.

    Without the 2.5 rung every frame in that band doubles its step and halves its rows,
    which is where a table stops being worth reading. The rungs either side are pinned
    with it: 200 for a 4,600-wide frame and 1,000 for a 20,000-wide one, both worked by
    hand from `span / 24`.
    """
    assert readable_step(55.0) == 2.5
    assert readable_step(4_600.0) == 200.0
    assert readable_step(20_000.0) == 1000.0


def test_a_flat_segment_on_zero_between_two_strikes_gives_its_two_ends() -> None:
    """A call ladder priced so the middle of the curve lies exactly on the axis.

    Buy the 74000 call for 4,000, sell the 76000 call for 3,000, buy the 78000 call for
    1,000 — 2,000 paid out, which is exactly the 76,000 - 74,000 the first spread is
    worth, so the P&L is **flat at zero the whole way from 76,000 to 78,000**. Worked by
    hand: -2,000 anywhere below 74,000, zero across the shelf, and rising a dollar for a
    dollar above 78,000.

    Two breakevens, not a range and not the one crossing a sign test would find, because
    the shelf never changes sign. They are the two prices at which the strategy stops
    breaking even and starts doing something, which is what a reader wants marked.
    """
    call_ladder = [
        PayoffLeg(
            strike=74_000.0, is_call=True, direction=1, quantity=1, entry_price=4000.0
        ),
        PayoffLeg(
            strike=76_000.0, is_call=True, direction=-1, quantity=1, entry_price=3000.0
        ),
        PayoffLeg(
            strike=78_000.0, is_call=True, direction=1, quantity=1, entry_price=1000.0
        ),
    ]

    metrics = strategy_metrics(call_ladder)

    assert metrics.breakevens == [76_000.0, 78_000.0]
    assert pnl_at_expiry(77_000.0, call_ladder) == 0.0
    assert metrics.max_loss == -2000.0
    assert metrics.max_profit is None


def test_a_strike_landing_on_a_window_end_is_one_corner_and_not_two() -> None:
    """The strangle framed by its own widening, where `low` **is** the 70,000 strike.

    `Curve` refuses corners that do not ascend **strictly**, so a window end and a strike
    at the same price would raise at the route rather than draw — a 500 for a strategy
    whose only crime is having its wing where the frame ends. Which is the ordinary case,
    not a corner one: `suggested_window` widens to the strikes, so the outermost strike
    sits exactly on an end whenever it falls outside three sigma.
    """
    window = suggested_window(SHORT_STRANGLE, anchor=80_000.0, atm_iv=0.10, years=0.01)
    assert (window.low, window.high) == (70_000.0, 85_000.0)

    curve = payoff_curve(SHORT_STRANGLE, window)

    assert [(point.price, point.pnl) for point in curve.corners] == [
        (70_000.0, 2000.0),
        (85_000.0, 2000.0),
    ]


def test_an_anchor_of_zero_is_refused_rather_than_treated_as_an_absent_one() -> None:
    """`null` is not `0`, here as everywhere else on this project.

    An absent anchor is an unfitted chain and falls back to the strikes. A zero one is a
    forward or a spot that came out wrong upstream, and framing the chart on it — or
    silently substituting the strikes for it — would put a plausible axis under a
    fabrication.
    """
    with pytest.raises(ValueError, match="positive price"):
        suggested_window([CONTRACT_CALL], anchor=0.0, atm_iv=0.40, years=0.25)
