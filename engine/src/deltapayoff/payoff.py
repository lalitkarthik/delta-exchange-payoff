"""The arithmetic behind `/analyse`: legs in, corner points and metrics out.

Pure, in the sense `forward.py`, `black76.py` and `bars.py` are pure — no socket, no
clock, no filesystem, and nothing here knows whether the legs it was handed came from
the live cache or a stored minute. That is what lets the interesting cases (unbounded
against finite, a breakeven landing exactly on a strike, a maximum at neither end) be
pinned by tests that run in milliseconds without a venue.

**At expiry every leg is worth its intrinsic value**, so a single-expiry strategy's P&L
is a sum of hockey sticks: straight everywhere and kinked only at a strike. That single
fact is why the curve travels as its corners rather than as samples, why the two end
slopes describe everything outside them exactly, and why a breakeven is one division
rather than a search.

This module builds `payoff_models`' own `Curve`, `Metrics` and `Greeks` directly rather
than returning private records for a route to map. One shape, no mapping layer, and the
`extra="forbid"` on those models is what catches a mistyped keyword here before it can
drop a whole section of a 200 response.

Everything is **per one unit of the underlying**. `contract_value` is a lot size the
screen applies at the very end and it never appears below — `docs/settlement.md`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .payoff_models import Curve, Greeks, Metrics, PayoffPoint, Window

#: How many standard deviations the suggested window reaches on each side. Three, so
#: the chart opens on essentially everywhere the underlying can plausibly finish —
#: `docs/payoff-contract.md`. `assumed`, in the sense that it is a framing choice
#: rather than a measurement; nothing downstream depends on it, because the corners and
#: the two end slopes describe the curve outside the window just as exactly as inside.
WINDOW_SIGMAS = 3.0

#: The half-width, in log space, to fall back on when there is no volatility to scale
#: by — an unfitted chain, or an expiry that has already settled. `assumed`: about
#: +/-5%, chosen only so the axis has a width at all. A window is a suggestion, and the
#: alternative here is `low == high`, which is not a chart.
FALLBACK_LOG_HALF_WIDTH = 0.05


#: How close two prices have to be before they are the same price. `assumed`: prices
#: here run to five figures in USD and are quoted to two decimals, so a nanodollar is
#: far below anything the venue can express and far above the rounding in a linear
#: solve. Nothing measured it; it is a floor picked from those two known scales.
PRICE_TOLERANCE = 1e-9


#: How close a P&L has to be to zero before it *is* zero. `assumed`, on the same two
#: scales as `PRICE_TOLERANCE`: a millionth of a dollar is far below the cent these
#: prices are quoted in and comfortably above the rounding a sum of five-figure strikes
#: accumulates. Looser than `PRICE_TOLERANCE` because a P&L is a difference of products
#: of those five-figure numbers where a price is one of them. It decides whether a curve
#: grazing the axis is one breakeven or two, so it is named rather than an `== 0.0`
#: nobody would think to question.
PNL_TOLERANCE = 1e-6


#: About how many rows the payoff table aims for. A screenful a trader reads down
#: without scrolling, and the number the step is chosen to land near — `assumed`, in
#: the sense that it is a legibility choice with nothing downstream depending on it.
TABLE_TARGET_ROWS = 25

#: The round numbers a step is allowed to be, times a power of ten. `assumed` — a
#: legibility choice: a trader reads 500 and 2,500, and nobody reads 291.67.
#:
#: Sized to the frame rather than fixed, which is what the sibling does. Its 50 is
#: NIFTY's uniform grid; BTC's grid is not uniform. `measured` over
#: `engine/tests/fixtures/tickers-btc-04-09-2026.json` (65 strikes, 58,000 to 90,000,
#: spot 77,568): gaps of 100 and 200 between 76,500 and 79,200, 500 through the belly
#: (36 of the 64 gaps) and 1,000 out to both wings (11). No single step is right at both
#: ends of that chain.
_STEP_MANTISSAS = (1.0, 2.0, 2.5, 5.0)


@dataclass(frozen=True)
class PayoffLeg:
    """One leg, reduced to what the arithmetic needs.

    Not `AnalysedLeg`: that carries the canonical instrument string, and pulling a
    strike and a right back out of it is `Instrument.from_canonical`'s job at the route
    boundary, not this module's. Taking the parsed pieces keeps the single validator
    single and keeps this module free of the instrument vocabulary entirely.

    `entry_price` is USD per one unit of the underlying, the scale the ladder quotes on.
    `greeks` are **per one unit and unsigned** — as `compute` solves them — because the
    weighting by `direction * quantity` is this module's job and doing it twice would
    square the quantity.
    """

    strike: float
    is_call: bool
    #: +1 bought, -1 sold. Separate from `quantity`, never one signed number.
    direction: int
    quantity: int
    entry_price: float
    greeks: Greeks | None = None

    @property
    def weight(self) -> float:
        """`direction * quantity` — how much of the strategy this leg is."""
        return float(self.direction * self.quantity)


def intrinsic_value(price: float, strike: float, *, is_call: bool) -> float:
    """What the option is worth once there is no time left in it.

    The whole module rests on this: at expiry there is no volatility, no discount and no
    model, which is why the curve and the metrics survive a chain that could not be
    fitted while the Greeks beside them do not.
    """
    return max(price - strike, 0.0) if is_call else max(strike - price, 0.0)


def pnl_at_expiry(price: float, legs: Sequence[PayoffLeg]) -> float:
    """The strategy's profit or loss if the underlying finishes at `price`.

    `direction * quantity * (intrinsic - entry_price)` summed over the legs: what each
    leg pays less what it cost, signed by which way it was traded.
    """
    return sum(
        leg.weight
        * (intrinsic_value(price, leg.strike, is_call=leg.is_call) - leg.entry_price)
        for leg in legs
    )


def net_premium(legs: Sequence[PayoffLeg]) -> float:
    """What the strategy cost to put on: **positive is paid out, negative received**.

    The sign is the contract's and not a free choice — a credit spread reading as a
    positive number beside a negative max loss is the one thing on the panel a trader
    cannot recover from by squinting.
    """
    return sum(leg.weight * leg.entry_price for leg in legs)


def end_slopes(legs: Sequence[PayoffLeg]) -> tuple[float, float]:
    """P&L per unit of underlying **outside** the lowest and the highest strike.

    Above every strike each call is exercised and each put is dead, so the far right
    slope is the net signed call quantity. Below every strike the puts are the live ones
    and each falls a dollar for every dollar the underlying rises, so the far left slope
    is the net signed put quantity **negated**.

    Read off the leg mix rather than from two sampled points far out: a subtraction of
    two large numbers loses the precision that decides whether a maximum is capped or
    unbounded, and that decision is `null` against a number on screen.
    """
    # `float(...)` because `sum()` over an empty generator returns `int` — an all-call
    # strategy would otherwise put a `0` where the contract's type says `0.0`.
    calls = float(sum(leg.weight for leg in legs if leg.is_call))
    puts = float(sum(leg.weight for leg in legs if not leg.is_call))
    # **`+ 0.0` is not a no-op**, and it is the whole reason this is not one line. IEEE
    # 754 negates positive zero to `-0.0`, so an all-call strategy — which has no puts at
    # all — leaves here with a left slope of minus zero. It compares equal to zero
    # everywhere, survives `JSON.parse` intact, and then renders as `-0.00` on an axis
    # beside four honest numbers. Adding zero folds it back and leaves every other value
    # untouched.
    return -puts + 0.0, calls + 0.0


def suggested_window(
    legs: Sequence[PayoffLeg],
    *,
    anchor: float | None,
    atm_iv: float | None,
    years: float | None,
) -> Window:
    """The range the engine suggests opening the chart on.

    **+/-3 standard deviations, in log space**, around `anchor` — the forward where one
    was fitted, since that is what the terminal distribution is centred on, and spot
    otherwise. The ends are `anchor * exp(-/+3 sigma sqrt(t))`, three deviations of the
    **log** price, which is the quantity Black-76 models as normal. The additive reading
    `anchor * (1 -/+ 3 sigma sqrt(t))` is the more literal one and is broken: it puts the
    low edge below zero once `sigma * sqrt(t)` passes a third — 70% volatility a quarter
    out (`0.70 * sqrt(0.25) = 0.35`), and 296 days at 37%. `docs/payoff-contract.md`
    carries the same ruling.

    **No drift term.** No `- sigma^2 t / 2`: this is a viewing frame, not a probability
    statement, and the correction would shift it without telling the reader anything.

    **A fixed percentage does not transfer.** At 37% volatility +/-6% is 1.6 sigma on a
    3.8-day expiry and 0.33 sigma at 86 days, where it would crop off the whole
    interesting part of the curve.

    **Then widened to hold every strike carrying a leg**, because a wing outside the
    frame draws a capped loss as an uncapped one, and a browser cannot zoom out to
    corners it was never sent.

    The volatility and the time are handed in, not fitted: fitting is `compute`'s job
    and lives on the other side of this seam. Either being absent — an unfitted chain —
    falls back to a nominal width rather than refusing, because the curve and the
    metrics do not need a model and the chart still has to open on something.
    """
    strikes = [leg.strike for leg in legs]
    if anchor is not None and anchor <= 0.0:
        # `null` is not `0`. An absent anchor is an unfitted chain and is handled below;
        # a zero or negative one is a forward or a spot that came out wrong upstream, and
        # substituting for it quietly would frame the chart on a fabrication.
        raise ValueError(f"an anchor must be a positive price; got {anchor}")
    if anchor is None:
        if not strikes:
            raise ValueError("a window needs an anchor or at least one strike")
        centre = (min(strikes) + max(strikes)) / 2
    else:
        centre = anchor

    half_width = 0.0
    if atm_iv is not None and years is not None and atm_iv > 0.0 and years > 0.0:
        half_width = WINDOW_SIGMAS * atm_iv * math.sqrt(years)
    if half_width <= 0.0:
        half_width = FALLBACK_LOG_HALF_WIDTH

    low = centre * math.exp(-half_width)
    high = centre * math.exp(half_width)
    if strikes:
        low = min(low, min(strikes))
        high = max(high, max(strikes))
    return Window(low=low, high=high)


def _ascending(values: Iterable[float]) -> list[float]:
    """Sorted, with values that are the same price to within `PRICE_TOLERANCE` kept
    once.

    `Curve` and `Metrics` refuse a sequence that does not ascend **strictly**, and every
    list this module builds is a union of things that can legitimately coincide — a
    strike sitting exactly on a window end, a table grid line landing on a corner, a
    breakeven found at a kink. Merging them here is what stops a coincidence becoming a
    validation error at the route.
    """
    kept: list[float] = []
    for value in sorted(values):
        if not kept or value - kept[-1] > PRICE_TOLERANCE:
            kept.append(value)
    return kept


def corner_prices(legs: Sequence[PayoffLeg], window: Window) -> list[float]:
    """Where the line bends, plus the two ends of the frame.

    The kinks are the distinct strikes and nothing else: between two of them every leg
    is linear, so the segment between is exactly straight. The window ends are added
    because a chart needs two endpoints to draw between and because the reader is told
    where the engine suggests opening.

    **Every strike, including the ones outside the window.** The window is a suggestion
    and not a clamp, and `slope_left` and `slope_right` are read off the whole leg mix —
    so dropping a strike the frame does not reach leaves a ray that starts at the wrong
    P&L and runs forever. On the butterfly framed 76,000 to 78,000 that ray says 1,100 at
    82,000 where the strategy is worth -900, and it draws perfectly plausibly. Carrying
    the outliers costs one point each and makes the two rays unconditionally exact,
    which is what `docs/payoff-contract.md` promises the reader who zooms out.
    """
    return _ascending([window.low, *(leg.strike for leg in legs), window.high])


def payoff_curve(legs: Sequence[PayoffLeg], window: Window) -> Curve:
    """The chart's line, as corners and two rays rather than as samples.

    Exact rather than sampled, smaller on the wire, and zoomable without limit: drawing
    a straight segment between two given points is rendering, which is all the web app
    is allowed to do.
    """
    slope_left, slope_right = end_slopes(legs)
    return Curve(
        corners=[
            PayoffPoint(price=price, pnl=pnl_at_expiry(price, legs))
            for price in corner_prices(legs, window)
        ],
        slope_left=slope_left,
        slope_right=slope_right,
        window=window,
    )


def _kinks(legs: Sequence[PayoffLeg]) -> list[float]:
    """The distinct strikes: every price at which the line changes slope."""
    return _ascending(leg.strike for leg in legs)


def breakevens(legs: Sequence[PayoffLeg]) -> list[float]:
    """Every price at which the strategy's P&L is zero, ascending.

    Between two kinks the line is straight, so each crossing is **one division** rather
    than a search — and it is solved on the same points the chart is drawn through, so a
    breakeven is a level the line visibly crosses rather than one it nearly does.

    Three sources, deliberately disjoint so that a breakeven is found exactly once:

    * **A kink whose P&L is zero** is reported as itself. This is the case the naive
      version gets wrong twice over: solving on both segments that meet there finds it
      twice, and testing for a strict sign change across each segment finds it not at
      all, because neither segment changes sign.
    * **A segment whose ends straddle zero strictly** contributes the one price between
      them. Segments with a zero end are excluded here precisely because the rule above
      already has it.
    * **The two rays**, outside the outermost kinks, each solved the same way and each
      accepted only strictly outside its kink. The left one is bounded by a price of
      zero, which the underlying cannot go below.

    A **flat segment sitting on zero** — a zero-cost vertical below its own strikes, and
    an easy thing to build — is not a run of breakevens: it contributes only its two
    ends, by the kink rule, which is the honest answer because those are where the
    strategy stops breaking even and starts doing something.
    """
    kinks = _kinks(legs)
    if not kinks:
        return []
    values = [pnl_at_expiry(price, legs) for price in kinks]
    slope_left, slope_right = end_slopes(legs)

    found = [
        price
        for price, pnl in zip(kinks, values, strict=True)
        if abs(pnl) <= PNL_TOLERANCE
    ]

    for index in range(len(kinks) - 1):
        before, after = values[index], values[index + 1]
        straddles = (before < -PNL_TOLERANCE and after > PNL_TOLERANCE) or (
            before > PNL_TOLERANCE and after < -PNL_TOLERANCE
        )
        if straddles:
            left, right = kinks[index], kinks[index + 1]
            found.append(left - before * (right - left) / (after - before))

    if slope_left != 0.0:
        crossing = kinks[0] - values[0] / slope_left
        if 0.0 <= crossing < kinks[0] - PRICE_TOLERANCE:
            found.append(crossing)
    if slope_right != 0.0:
        crossing = kinks[-1] - values[-1] / slope_right
        if crossing > kinks[-1] + PRICE_TOLERANCE:
            found.append(crossing)

    return _ascending(found)


def _extremes(legs: Sequence[PayoffLeg]) -> tuple[float | None, float | None]:
    """The best and worst outcomes, `None` where the tail keeps running.

    Only the **right** tail can be unbounded. The underlying cannot fall below zero, so
    the left ray always terminates and a price of zero is a candidate like any kink —
    which is where a short put finds its worst case, and why that worst case is a large
    finite number rather than `null`.

    A piecewise-linear function takes its extremes at a vertex, so the kinks and the
    floor are the whole search. No sampling: a grid that steps over the vertex reports a
    chord across it, which is how a short straddle comes to peak at 668.59 on a chart
    with 670.75 printed beside it.
    """
    _, slope_right = end_slopes(legs)
    candidates = [pnl_at_expiry(price, legs) for price in [0.0, *_kinks(legs)]]
    return (
        None if slope_right > 0.0 else max(candidates),
        None if slope_right < 0.0 else min(candidates),
    )


def strategy_metrics(legs: Sequence[PayoffLeg]) -> Metrics:
    """The four numbers under the chart, and the ratio between two of them."""
    max_profit, max_loss = _extremes(legs)
    reward_risk = None
    if max_profit is not None and max_loss is not None and max_loss < -PNL_TOLERANCE:
        reward_risk = max_profit / abs(max_loss)
    return Metrics(
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens(legs),
        net_premium=net_premium(legs),
        reward_risk=reward_risk,
    )


def readable_step(span: float) -> float:
    """The roundest step that gets across `span` in about `TABLE_TARGET_ROWS` rows."""
    if span <= 0.0:
        raise ValueError(f"a table needs a window with width; got a span of {span}")
    wanted = span / (TABLE_TARGET_ROWS - 1)
    decade = 10.0 ** math.floor(math.log10(wanted))
    for mantissa in _STEP_MANTISSAS:
        if mantissa * decade >= wanted:
            return mantissa * decade
    return 10.0 * decade


def payoff_table(legs: Sequence[PayoffLeg], window: Window) -> list[PayoffPoint]:
    """The same P&L as the curve, on the grid a trader takes exact figures off.

    **One quantity sampled twice, not two quantities** — which is why it is the same
    `PayoffPoint` type and why the corners are folded in rather than left out. A row at
    every corner is what makes the table and the picture agree at the places that matter
    most: a butterfly's peak is a corner and would otherwise fall between two rows, and
    the table would list the strategy's best outcome as something it never reaches.

    The grid is **snapped to multiples of the step** rather than started at the window's
    left edge, so the rows read 77,000 and 77,500 rather than 74,291.67 and its
    multiples — and so a strike is a row.
    """
    step = readable_step(window.high - window.low)
    first = math.ceil(window.low / step) * step
    rows = math.floor((window.high + PRICE_TOLERANCE - first) / step) + 1
    # Indexed off the first row rather than accumulated, which would drift by a little
    # more with every addition, and clamped because `ceil` on a ratio can land a
    # hair outside the frame it was derived from.
    grid = [
        min(max(first + index * step, window.low), window.high)
        for index in range(max(rows, 0))
    ]
    prices = _ascending([*grid, *corner_prices(legs, window)])
    return [
        PayoffPoint(price=price, pnl=pnl_at_expiry(price, legs)) for price in prices
    ]


def scale_greeks(greeks: Greeks, weight: float) -> Greeks:
    """One leg's exposures at the size it was traded: `direction * quantity` through.

    Public because the per-leg rows of the response are signed and scaled the same way —
    `docs/payoff-contract.md` — and a route doing that multiplication itself would be a
    second place for the convention to live. `entry_price` is deliberately **not** put
    through here: it is what one unit cost rather than what the leg cost, so it stays
    comparable with the bid and ask on the ladder it was taken from.
    """
    return Greeks(
        delta=greeks.delta * weight,
        gamma=greeks.gamma * weight,
        vega=greeks.vega * weight,
        theta=greeks.theta * weight,
        rho=greeks.rho * weight,
    )


def position_greeks(legs: Sequence[PayoffLeg]) -> Greeks | None:
    """The strategy's exposure: every leg weighted, then added.

    **All or nothing.** `None` the moment one leg has no volatility behind it, rather
    than a sum over the legs that happened to solve — that describes a different
    position from the one on screen, and nothing on the screen would say so. The
    contract enforces the same rule from the other side, so a partial sum would be
    refused at the boundary rather than published; this is where it is decided.
    """
    if not legs or any(leg.greeks is None for leg in legs):
        return None
    scaled = [
        scale_greeks(leg.greeks, leg.weight)
        for leg in legs
        if leg.greeks is not None  # already guaranteed above; here for the type checker
    ]
    return Greeks(
        delta=sum(one.delta for one in scaled),
        gamma=sum(one.gamma for one in scaled),
        vega=sum(one.vega for one in scaled),
        theta=sum(one.theta for one in scaled),
        rho=sum(one.rho for one in scaled),
    )
