"""Realised volatility: what the underlying actually did, measured five ways.

Pure, in the manner of `forward.py` and `bars.py` — bars in, numbers out, no socket, no
clock, no filesystem.

**A gap is not a long return.** Our bars are never forward-filled, so a minute with no
arrivals produces no row at all. The step across that hole spans two minutes rather than
one, and it is not a sample from the one-minute return distribution — feeding it to a
standard deviation inflates the estimate wherever the feed hiccupped, which is exactly
where a reader would least suspect it. Every return estimator here therefore drops the
returns that span a gap and **reports how many it kept**, so an estimate computed from
half a window says so rather than looking identical to one computed from all of it.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLC bar, source-agnostic on purpose.

    Deliberately not `SpotBar`: the estimators must read our stored spot series *and*
    whatever the venue's index candles turn out to be, and a type named for one of them
    would quietly make the other a special case.
    """

    at: datetime
    open: float
    high: float
    low: float
    close: float


class RealisedVol(BaseModel):
    """One estimator's answer, with the count it was computed from beside it.

    `observations` and `expected` are both here because their **ratio** is the only thing
    that distinguishes a solid estimate from a lucky one, and a single number cannot
    carry it. A volatility of 0.9% from 8,640 returns and the same figure from 12 are the
    same number and different facts.
    """

    estimator: str
    value: float | None
    #: Returns (or bars, for the range estimators) the estimate actually used.
    observations: int
    #: How many there would have been had every bucket in the window arrived.
    expected: int

    @property
    def coverage(self) -> float:
        """`observations / expected`, or 0.0 for a window that spans nothing."""
        return self.observations / self.expected if self.expected else 0.0


def _spans(bars: list[Bar], interval: timedelta) -> int:
    """One-interval steps between the first and last bar, gaps included."""
    if len(bars) < 2:
        return 0
    return round((bars[-1].at - bars[0].at) / interval)


def _adjacent_pairs(
    bars: list[Bar], interval: timedelta
) -> list[tuple[Bar, Bar]]:
    """Consecutive bars exactly one interval apart. The others are holes, not returns."""
    return [
        (earlier, later)
        for earlier, later in zip(bars, bars[1:], strict=False)
        if later.at - earlier.at == interval
    ]


def _sample_deviation(values: list[float]) -> float | None:
    """`sqrt( sum (x - xbar)^2 / (W - 1) )`. `None` under two observations.

    **The mean is subtracted**, following `docs/realised-volatility.md`. This departs
    from variance-swap convention, which does not subtract it — the gap between our two
    chart lines therefore contains a drift term that is not a risk premium. Negligible at
    short windows, growing with the window and with trending markets, which is precisely
    when the chart is interesting. Chosen knowingly; see issue #30.
    """
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def log_return_vol(bars: list[Bar], *, interval: timedelta) -> RealisedVol:
    """Sample standard deviation of `ln(C_t / C_{t-1})` over adjacent bars."""
    pairs = _adjacent_pairs(bars, interval)
    returns = [math.log(later.close / earlier.close) for earlier, later in pairs]
    return RealisedVol(
        estimator="log",
        value=_sample_deviation(returns),
        observations=len(returns),
        expected=_spans(bars, interval),
    )


def simple_return_vol(bars: list[Bar], *, interval: timedelta) -> RealisedVol:
    """Sample standard deviation of `(C_t - C_{t-1}) / C_{t-1}` over adjacent bars.

    Kept alongside the log version rather than instead of it. Over one-minute bars the
    two agree to several decimal places because `ln(1 + x) ~= x` for small `x`, so seeing
    them overlap on the chart is free reassurance that the bars are sane — and seeing
    them separate would say the bars are not.
    """
    pairs = _adjacent_pairs(bars, interval)
    returns = [
        (later.close - earlier.close) / earlier.close for earlier, later in pairs
    ]
    return RealisedVol(
        estimator="simple",
        value=_sample_deviation(returns),
        observations=len(returns),
        expected=_spans(bars, interval),
    )


def _range_result(estimator: str, terms: list[float], bars: list[Bar],
                  interval: timedelta) -> RealisedVol:
    """Mean the per-bar variance terms and take the root.

    **No mean subtraction and no `W - 1`.** These three estimators are not sample
    standard deviations of anything; each term is already an unbiased estimate of the
    bar's variance under the estimator's own assumptions, so the window's variance is
    their average. `docs/realised-volatility.md` writes `1/W` for exactly this reason
    where it writes `1/(W-1)` for the two return estimators.

    **A gap costs a range estimator one bar, not two.** A high-low range needs no
    neighbour, so a hole removes only the missing bar's own term — which is why the
    coverage on these three lines will sit above the coverage on the other two whenever
    the feed has hiccupped. That difference is real and should not be levelled out.
    """
    if not terms:
        return RealisedVol(
            estimator=estimator, value=None, observations=0,
            expected=_spans(bars, interval) + 1 if bars else 0,
        )
    variance = sum(terms) / len(terms)
    return RealisedVol(
        estimator=estimator,
        # Rogers-Satchell and Garman-Klass can both return a negative sum on a small
        # window: they are unbiased, not non-negative. A negative variance has no root
        # and is reported as no answer rather than as a zero that would read as calm.
        value=math.sqrt(variance) if variance >= 0.0 else None,
        observations=len(terms),
        expected=_spans(bars, interval) + 1,
    )


def parkinson(bars: list[Bar], *, interval: timedelta) -> RealisedVol:
    """`sqrt( sum ln(H_i/L_i)^2 / (4 W ln 2) )`.

    Roughly five times more efficient per bar than close-to-close, because it reads what
    happened *inside* the bar. A bar that travelled 100 -> 105 -> 100 has a zero
    close-to-close return and obvious volatility; this sees it.

    It assumes zero drift, so a hard trend is charged as volatility. That is not a defect
    to correct here — it is what makes the spread against Rogers-Satchell informative.
    """
    scale = 4.0 * math.log(2.0)
    terms = [math.log(bar.high / bar.low) ** 2 / scale for bar in bars]
    return _range_result("parkinson", terms, bars, interval)


def garman_klass(bars: list[Bar], *, interval: timedelta) -> RealisedVol:
    """`sqrt( mean_i[ 0.5 ln(H/L)^2 - (2 ln 2 - 1) ln(C/O)^2 ] )`.

    Parkinson's range term with the open-close term added, extracting more from the same
    four numbers. Like Parkinson it assumes zero drift.

    **The `A` in `docs/realised-volatility.md` section B2 is one here.** That document
    writes `A = 252` inline; 252 is wrong for an asset that trades weekends, and a
    calendar literal buried in an estimator is how two lines on one chart come to disagree
    about the year without either of them saying so. Scaling is `scale_to_window`'s job
    and takes the interval as an argument.
    """
    weight = 2.0 * math.log(2.0) - 1.0
    terms = [
        0.5 * math.log(bar.high / bar.low) ** 2
        - weight * math.log(bar.close / bar.open) ** 2
        for bar in bars
    ]
    return _range_result("garman_klass", terms, bars, interval)


def rogers_satchell(bars: list[Bar], *, interval: timedelta) -> RealisedVol:
    """`sqrt( mean_i[ ln(H/C)ln(H/O) + ln(L/C)ln(L/O) ] )`.

    Built to stay unbiased when the price is trending. Parkinson and Garman-Klass both
    assume zero drift and read a strong trend as extra volatility; this separates them —
    on a perfectly smooth ramp every term is zero.

    **This is the appropriate range estimator for a 24/7 market**, and it is why
    Yang-Zhang is absent from this module. Yang-Zhang's entire contribution over
    Rogers-Satchell is its overnight term `Var(ln(O_i / C_{i-1}))`, which requires a
    market that closes. Crypto never does, so that term collapses toward zero and
    Yang-Zhang degenerates into a more expensive Rogers-Satchell.
    """
    terms = [
        math.log(bar.high / bar.close) * math.log(bar.high / bar.open)
        + math.log(bar.low / bar.close) * math.log(bar.low / bar.open)
        for bar in bars
    ]
    return _range_result("rogers_satchell", terms, bars, interval)


def scale_to_window(
    per_bar: float | None, *, interval: timedelta, window: timedelta
) -> float | None:
    """Express a per-bar volatility over a window. `sigma * sqrt(window / interval)`.

    **The one place time enters this module**, and it takes the sampling interval as an
    argument rather than reading a constant. Variance is additive across time and
    volatility is not, so the count enters under a square root.

    **The window is the caller's, not the data's.** A window with holes in it is still
    that long; scaling by the observations that survived would understate volatility
    exactly where the feed was worst, which is the wrong direction for an instrument
    whose subject is how much movement there was. The thinning of the sample is reported
    separately, as `RealisedVol.coverage`.

    **Nothing here is annualised.** Both series on the chart are expressed over the
    lookback window instead, so the reader sees *"the market expected an 11.5% move over
    N days; it delivered 9%"*. Annualising is the convention, and it hides `N` inside a
    constant — two charts at different lookbacks would carry identical-looking axes while
    answering different questions.
    """
    if per_bar is None:
        return None
    return per_bar * math.sqrt(window / interval)


#: The five, by the name they answer to on the wire and on a checkbox.
#:
#: **Yang-Zhang and GARCH are absent by decision, not oversight.** Yang-Zhang's whole
#: contribution over Rogers-Satchell is its overnight term, which needs a market that
#: closes; this one never does. GARCH is a fitted model producing a *forecast*, which
#: makes it the same kind of object as implied volatility rather than the same kind as
#: realised — charting it here would be a model-versus-market comparison wearing the
#: clothes of a market-versus-realisation one. It is the obvious next line and it is not
#: this module.
ESTIMATORS: dict[str, Callable[..., RealisedVol]] = {
    "simple": simple_return_vol,
    "log": log_return_vol,
    "parkinson": parkinson,
    "garman_klass": garman_klass,
    "rogers_satchell": rogers_satchell,
}


def resample(bars: list[Bar], interval: timedelta) -> list[Bar]:
    """Roll bars up into buckets of `interval`. Open first, close last, extremes over all.

    The store holds minutes; every sampling interval above `1m` is built here rather than
    stored, so a reader can change its mind about the interval without a migration.

    **A bucket with no bars produces no bar**, at every resolution and not only at the
    stored one. Filling it with the previous close would give that bucket a zero return,
    and zero returns suppress volatility — so a forward-filled gap reads as *calm* exactly
    where the feed was worst.

    Buckets are aligned to the epoch rather than to the first bar, so two ranges over the
    same data cut their five-minute boundaries in the same places. Aligning to the first
    bar would make the answer depend on where the reader started looking.
    """
    if not bars:
        return []

    seconds = int(interval.total_seconds())
    buckets: dict[int, list[Bar]] = {}
    for bar in sorted(bars, key=lambda item: item.at):
        key = int(bar.at.timestamp()) // seconds
        buckets.setdefault(key, []).append(bar)

    rolled: list[Bar] = []
    for key in sorted(buckets):
        group = buckets[key]
        rolled.append(
            Bar(
                at=group[0].at.replace(microsecond=0)
                - timedelta(seconds=int(group[0].at.timestamp()) % seconds),
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
            )
        )
    return rolled


# -- the rolling path ------------------------------------------------------------------
#
# Every estimator above is a sum over its window, which is why a rolling form exists at
# all: prefix sums make each window O(1) instead of O(window). **Measured** on the demo
# store, a fourteen-day window of one-minute bars costs about 15 ms a point to recompute
# from scratch, so a six-hundred-point chart would take twenty-seven seconds and the
# screen would never draw. With prefix sums the whole series costs one pass.
#
# This is a second implementation of five formulas and therefore a second thing that can
# be wrong about them. `test_rolling_agrees_exactly_with_the_window_at_a_time_functions`
# is what earns it: the two paths are asserted equal, on a series with holes in it,
# for every estimator and at every timestamp.


def _per_bar_terms(bars: list[Bar], estimator: str) -> list[tuple[datetime, float]]:
    """One `(timestamp, value)` per observation, in the same order the window path uses.

    The three range estimators put one value on each bar, stamped with that bar's own
    time. The two return estimators put one value on each **adjacent pair**, stamped with
    the *earlier* bar's time — which is what makes the window arithmetic below come out
    identical to `_adjacent_pairs`: a pair sits inside `[t - N, t]` exactly when its
    earlier bar sits inside `[t - N, t - interval]`.
    """
    if estimator == "parkinson":
        scale = 4.0 * math.log(2.0)
        return [(bar.at, math.log(bar.high / bar.low) ** 2 / scale) for bar in bars]
    if estimator == "garman_klass":
        weight = 2.0 * math.log(2.0) - 1.0
        return [
            (
                bar.at,
                0.5 * math.log(bar.high / bar.low) ** 2
                - weight * math.log(bar.close / bar.open) ** 2,
            )
            for bar in bars
        ]
    if estimator == "rogers_satchell":
        return [
            (
                bar.at,
                math.log(bar.high / bar.close) * math.log(bar.high / bar.open)
                + math.log(bar.low / bar.close) * math.log(bar.low / bar.open),
            )
            for bar in bars
        ]
    raise KeyError(estimator)


def _per_pair_returns(
    bars: list[Bar], interval: timedelta, estimator: str
) -> list[tuple[datetime, float]]:
    pairs = _adjacent_pairs(bars, interval)
    if estimator == "log":
        return [
            (earlier.at, math.log(later.close / earlier.close))
            for earlier, later in pairs
        ]
    if estimator == "simple":
        return [
            (earlier.at, (later.close - earlier.close) / earlier.close)
            for earlier, later in pairs
        ]
    raise KeyError(estimator)


RETURN_ESTIMATORS = ("simple", "log")


def rolling(
    bars: list[Bar],
    *,
    estimator: str,
    interval: timedelta,
    lookback: timedelta,
    timestamps: list[datetime],
) -> list[RealisedVol]:
    """One estimator at many timestamps, in one pass. Same answers as the plain path."""
    if estimator not in ESTIMATORS:
        raise KeyError(estimator)

    ordered = sorted(bars, key=lambda bar: bar.at)
    bar_times = [bar.at for bar in ordered]
    is_return = estimator in RETURN_ESTIMATORS
    observations = (
        _per_pair_returns(ordered, interval, estimator)
        if is_return
        else _per_bar_terms(ordered, estimator)
    )
    stamps = [stamp for stamp, _ in observations]

    # Prefix sums, one entry longer than the data so a window is one subtraction.
    total = [0.0]
    total_squared = [0.0]
    for _, value in observations:
        total.append(total[-1] + value)
        total_squared.append(total_squared[-1] + value * value)

    results: list[RealisedVol] = []
    for at in timestamps:
        window_start = at - lookback

        # `expected` is a property of the window the caller asked for, not of the
        # observations that survived inside it — that difference is the coverage.
        first = bisect_left(bar_times, window_start)
        last = bisect_right(bar_times, at)
        if last - first >= 2:
            spans = round((bar_times[last - 1] - bar_times[first]) / interval)
        else:
            spans = 0
        expected = spans if is_return else (spans + 1 if last > first else 0)

        upper = at - interval if is_return else at
        low = bisect_left(stamps, window_start)
        high = bisect_right(stamps, upper)
        count = high - low
        if count == 0:
            results.append(
                RealisedVol(
                    estimator=estimator, value=None, observations=0, expected=expected
                )
            )
            continue

        sum_1 = total[high] - total[low]
        sum_2 = total_squared[high] - total_squared[low]

        if is_return:
            if count < 2:
                value = None
            else:
                # `sum(x^2) - sum(x)^2 / n` is the mean-subtracted sum of squares, and is
                # what makes one pass enough. Clamped at zero: the two terms are close
                # when the mean dominates the spread, and floating point can take their
                # difference a few ulps below zero, which has no root.
                centred = sum_2 - sum_1 * sum_1 / count
                value = math.sqrt(max(centred, 0.0) / (count - 1))
        else:
            variance = sum_1 / count
            value = math.sqrt(variance) if variance >= 0.0 else None

        results.append(
            RealisedVol(
                estimator=estimator, value=value, observations=count, expected=expected
            )
        )
    return results
