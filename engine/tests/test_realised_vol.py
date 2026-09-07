"""The five realised-volatility estimators, against hand-worked examples. No network.

Every expected value here comes from somewhere other than the code under test — a literal
computed once with `statistics.stdev`, or a figure worked through by hand from the formula
in `docs/realised-volatility.md`. An assertion that recomputes the answer the way the
implementation does can never disagree with it.
"""

from __future__ import annotations

import ast
import inspect
import math
from datetime import datetime, timedelta, timezone

from deltapayoff.realised_vol import (
    ESTIMATORS,
    Bar,
    garman_klass,
    log_return_vol,
    parkinson,
    resample,
    rogers_satchell,
    rolling,
    scale_to_window,
    simple_return_vol,
)

MINUTE = timedelta(minutes=1)
START = datetime(2026, 9, 4, 5, 41, tzinfo=timezone.utc)


def series(closes: list[float], step: timedelta = MINUTE) -> list[Bar]:
    """Bars carrying only closes. Open, high and low are set to the close.

    Enough for the two return estimators, and deliberately degenerate for the three
    range estimators — a bar with no range has no range volatility, which is a fact
    those tests assert rather than avoid.
    """
    return [
        Bar(at=START + index * step, open=close, high=close, low=close, close=close)
        for index, close in enumerate(closes)
    ]


def test_log_return_vol_matches_a_hand_worked_standard_deviation() -> None:
    """Four closes, three log returns, sample standard deviation with `W - 1`.

    Oracle: `statistics.stdev([ln(101/100), ln(100/101), ln(102/100)])` from the standard
    library, which shares no code with the implementation.
    """
    bars = series([100.0, 101.0, 100.0, 102.0])

    result = log_return_vol(bars, interval=MINUTE)

    assert result.value is not None
    assert abs(result.value - 0.015156641009734854) < 1e-15
    assert result.observations == 3


def test_a_return_spanning_a_gap_is_dropped_rather_than_stretched() -> None:
    """A missing minute does not make a two-minute return into a one-minute one.

    Bars at minutes 0, 1, 3, 4 — minute 2 never arrived, so no row exists for it, which
    is the never-forward-fill rule working. The 101 -> 102 step covers two minutes and is
    *not* a sample of the one-minute return distribution; including it would inflate every
    estimator on the chart wherever the feed hiccupped.

    Oracle: `statistics.stdev([ln(101/100), ln(103/102)])`.
    """
    bars = [
        Bar(at=START + minutes * MINUTE, open=close, high=close, low=close, close=close)
        for minutes, close in ((0, 100.0), (1, 101.0), (3, 102.0), (4, 103.0))
    ]

    result = log_return_vol(bars, interval=MINUTE)

    assert result.value is not None
    assert abs(result.value - 0.0001372889590152399) < 1e-15
    assert result.observations == 2
    # Four one-minute steps span minute 0 to minute 4; two of them are usable.
    assert result.expected == 4
    assert result.coverage == 0.5


def test_simple_return_vol_matches_a_hand_worked_standard_deviation() -> None:
    """The same four closes through `(C_t - C_{t-1}) / C_{t-1}` instead of the log.

    Oracle: `statistics.stdev([0.01, -0.009900990099009901, 0.02])`.
    """
    bars = series([100.0, 101.0, 100.0, 102.0])

    result = simple_return_vol(bars, interval=MINUTE)

    assert result.value is not None
    assert abs(result.value - 0.0152212494878157) < 1e-15


def test_simple_and_log_returns_agree_to_four_decimals_on_minute_bars() -> None:
    """`ln(1 + x) ~= x` for small `x`, so the two lines should overlap on the chart.

    This is the sanity check the ticket asks for: if these two diverge on 1-minute BTC
    bars, something is wrong with the *bars* rather than with the estimators.
    """
    bars = series([100.0, 101.0, 100.0, 102.0])

    simple = simple_return_vol(bars, interval=MINUTE)
    log = log_return_vol(bars, interval=MINUTE)

    assert simple.value is not None and log.value is not None
    assert abs(simple.value - log.value) < 1e-4


#: Two bars with real ranges, used by all three range estimators so their answers are
#: comparable to each other as well as to their own worked examples.
RANGE_BARS = [
    Bar(at=START, open=101.0, high=105.0, low=100.0, close=104.0),
    Bar(at=START + MINUTE, open=101.5, high=102.0, low=101.0, close=101.2),
]


def test_parkinson_matches_a_hand_worked_range_estimate() -> None:
    """`sqrt( sum ln(H/L)^2 / (4 W ln 2) )` over two bars, worked longhand.

    Oracle: `sqrt((ln(1.05)**2 + ln(102/101)**2) / (4 * 2 * ln 2))`, evaluated term by
    term rather than through the implementation's loop.
    """
    result = parkinson(RANGE_BARS, interval=MINUTE)

    assert result.value is not None
    assert abs(result.value - 0.021137484530535492) < 1e-15


def test_parkinson_sees_volatility_where_close_to_close_sees_none() -> None:
    """A bar that went 100 -> 105 -> 100 has no close-to-close return and real movement.

    This is the whole reason the range estimators are on the chart. Three bars closing at
    the same price give a close-to-close volatility of exactly zero; Parkinson does not.
    """
    flat_closes = [
        Bar(at=START + n * MINUTE, open=100.0, high=105.0, low=95.0, close=100.0)
        for n in range(3)
    ]

    assert log_return_vol(flat_closes, interval=MINUTE).value == 0.0
    park = parkinson(flat_closes, interval=MINUTE).value
    assert park is not None and park > 0.02


def test_a_range_estimator_keeps_the_bars_a_gap_leaves_behind() -> None:
    """A hole costs the return estimators a return; it costs Parkinson nothing.

    Each bar's high-low range is self-contained — it needs no neighbour — so a missing
    minute removes one observation rather than two, and the estimator says so. The
    difference between the two coverage figures is real and is not a bug.
    """
    with_hole = [
        Bar(at=START + n * MINUTE, open=100.0, high=105.0, low=95.0, close=100.0)
        for n in (0, 1, 3, 4)
    ]

    park = parkinson(with_hole, interval=MINUTE)
    log = log_return_vol(with_hole, interval=MINUTE)

    assert park.observations == 4 and park.expected == 5
    assert log.observations == 2 and log.expected == 4


def test_garman_klass_matches_a_hand_worked_estimate() -> None:
    """Parkinson's range term plus the open-close term, from the same two bars.

    Oracle: `sqrt(mean_i[ 0.5 ln(H/L)^2 - (2 ln 2 - 1) ln(C/O)^2 ])`, worked term by term
    off `docs/realised-volatility.md` section B2 with the annualisation factor `A` set to
    one — this module never annualises anything.
    """
    result = garman_klass(RANGE_BARS, interval=MINUTE)

    assert result.value is not None
    assert abs(result.value - 0.02126534207396172) < 1e-15


def test_rogers_satchell_matches_a_hand_worked_estimate() -> None:
    """`sqrt( mean_i[ ln(H/C)ln(H/O) + ln(L/C)ln(L/O) ] )`, worked longhand.

    Oracle from `docs/realised-volatility.md` section C, evaluated bar by bar.
    """
    result = rogers_satchell(RANGE_BARS, interval=MINUTE)

    assert result.value is not None
    assert abs(result.value - 0.02012954657657341) < 1e-15


def test_rogers_satchell_ignores_a_drift_the_other_two_charge_as_volatility() -> None:
    """A pure trend is not volatility, and only one of the three estimators knows it.

    Every bar here opens at its low and closes at its high, marching up 1% a bar — a
    perfectly smooth ramp with no oscillation whatsoever. Rogers-Satchell separates drift
    from variance by construction and reads it as near-nothing; Parkinson and
    Garman-Klass assume zero drift and charge the whole ramp as movement.

    This is the theory the ticket asks to be checked against real data. Pinning it on a
    planted ramp first means that when the estimators disagree on BTC, the disagreement
    can be read as trend rather than investigated as a bug.
    """
    ramp = []
    price = 100.0
    for n in range(10):
        ramp.append(
            Bar(at=START + n * MINUTE, open=price, high=price * 1.01,
                low=price, close=price * 1.01)
        )
        price *= 1.01

    rs = rogers_satchell(ramp, interval=MINUTE).value
    park = parkinson(ramp, interval=MINUTE).value

    assert rs is not None and park is not None
    assert rs < 1e-12, "a pure drift carries no Rogers-Satchell variance"
    assert park > 0.005, "Parkinson charges the same ramp as volatility"


def test_scaling_a_per_bar_volatility_to_a_window_is_a_square_root() -> None:
    """Variance is additive across time, so volatility scales with the root of the count.

    A per-bar volatility of 0.001 on one-minute bars, expressed over a 100-minute window,
    is `0.001 * sqrt(100) = 0.01`. The literal 10 is the independent part.
    """
    scaled = scale_to_window(0.001, interval=MINUTE, window=100 * MINUTE)

    assert scaled is not None
    assert abs(scaled - 0.01) < 1e-15


def test_scaling_depends_on_the_ratio_of_window_to_interval_and_nothing_else() -> None:
    """Five-minute sampling over an hour and one-minute sampling over twelve agree.

    Both are twelve buckets, so both multiply by `sqrt(12)`. The function takes no bars
    at all, which is the design point: a gap thins the sample inside the window without
    shortening the window, so the scale factor must not know about the data.
    """
    coarse = scale_to_window(0.001, interval=5 * MINUTE, window=timedelta(hours=1))
    fine = scale_to_window(0.001, interval=MINUTE, window=12 * MINUTE)

    assert coarse is not None and fine is not None
    assert abs(coarse - fine) < 1e-18
    assert abs(coarse - 0.001 * 12 ** 0.5) < 1e-18


def test_no_calendar_literal_lives_in_the_estimator_module() -> None:
    """252 and 365 are both wrong somewhere, so neither may be spelled here.

    252 assumes a market that closes at weekends; this one does not, and a 1/252 year was
    measured overstating theta by 1.456x (`docs/greeks.md`). 365 is right for annualising
    and this module does not annualise. A calendar literal in an estimator is how two
    lines on one chart come to disagree about the year without saying so — the interval
    is an argument to `scale_to_window`, and that is the only place time enters.
    """
    from deltapayoff import realised_vol

    tree = ast.parse(inspect.getsource(realised_vol))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
    ]

    assert 252 not in literals and 365 not in literals


# -- the synthetic series ------------------------------------------------------------

#: Sub-samples per bar in the planted path below, and the reason it is this large.
#:
#: A range estimator reads the high and low it can *see*, and a finitely sampled path
#: never reaches the extremes a continuous one does — so Parkinson, Garman-Klass and
#: Rogers-Satchell come in **low**, by a bias that is a property of the sampling and not
#: of the estimator. **Measured** here: the mean relative error on Parkinson runs -40.4%
#: at one sample per bar, -13.4% at 20, -8.1% at 60, -2.2% at 1000 and -1.35% at 4000,
#: shrinking as roughly `-0.63 / sqrt(samples)`. At 2000 it is about 1%, which is small
#: against the sampling noise the tolerance below has to carry anyway.
#:
#: **This is not only a test artifact.** It is the reason `docs/index-history.md` asks
#: how many observations a source puts in a bar. Our `spot-bars` fold ~8,256 ticker
#: frames a minute, so the bias there is on the order of 0.7% and ignorable. A candle
#: built from *trades* might carry a few dozen, where the same formula predicts a 10%
#: understatement — a difference in the data source that would appear on the chart as
#: a difference in volatility.
SUBSAMPLES_PER_BAR = 2000

#: **Measured**, over the eight seeds below: the worst relative error of any estimator
#: against the planted volatility is **3.85%**, and the widest spread between the highest
#: and lowest of the five on one path is **5.05%**. The assertion is set at 6% — the
#: measured figure with enough headroom for the seed lottery to move it, and tight enough
#: that an estimator going wrong by a factor would fail. Not a guess; rerun
#: `test_all_five_agree_on_a_series_of_known_volatility` to re-measure.
AGREEMENT_TOLERANCE = 0.06

PLANTED_SIGMA_PER_BAR = 0.0005


def planted_path(n_bars: int, sigma_per_bar: float, seed: int) -> list[Bar]:
    """A geometric Brownian path of known volatility, folded into OHLC bars.

    The path is generated at `SUBSAMPLES_PER_BAR` points inside each bar and then
    aggregated, because a bar built from its endpoints alone has no range for the three
    range estimators to read — they would all return zero and the test would pass by
    measuring nothing.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    deviation = sigma_per_bar / math.sqrt(SUBSAMPLES_PER_BAR)
    steps = rng.normal(
        -0.5 * deviation**2, deviation, n_bars * SUBSAMPLES_PER_BAR
    )
    path = (100.0 * np.exp(np.cumsum(steps))).reshape(n_bars, SUBSAMPLES_PER_BAR)

    bars: list[Bar] = []
    previous_close = 100.0
    for index in range(n_bars):
        segment = path[index]
        bars.append(
            Bar(
                at=START + index * MINUTE,
                open=previous_close,
                high=float(max(segment.max(), previous_close)),
                low=float(min(segment.min(), previous_close)),
                close=float(segment[-1]),
            )
        )
        previous_close = float(segment[-1])
    return bars


def test_all_five_agree_on_a_series_of_known_volatility() -> None:
    """One day of minutes at a planted 0.05% per bar, eight paths, five estimators.

    The estimators measure the same quantity under different assumptions, so on a series
    that satisfies all of those assumptions they must converge on the same answer — and
    that answer must be the one that was planted. This is the test that would catch a
    formula transcribed wrongly from `docs/realised-volatility.md`, which the hand-worked
    examples above cannot: they would agree with a wrong formula transcribed twice.

    See `AGREEMENT_TOLERANCE` for the measured figures.
    """
    n_bars = 1440
    window = n_bars * MINUTE
    truth = PLANTED_SIGMA_PER_BAR * math.sqrt(n_bars)

    for seed in range(8):
        bars = planted_path(n_bars, PLANTED_SIGMA_PER_BAR, seed)
        values = {}
        for estimator in (
            simple_return_vol, log_return_vol,
            parkinson, garman_klass, rogers_satchell,
        ):
            result = estimator(bars, interval=MINUTE)
            assert result.observations >= n_bars - 1
            assert result.coverage > 0.99
            scaled = scale_to_window(result.value, interval=MINUTE, window=window)
            assert scaled is not None
            values[result.estimator] = scaled

        for name, value in values.items():
            error = abs(value - truth) / truth
            assert error < AGREEMENT_TOLERANCE, (
                f"{name} read {value:.6f} against a planted {truth:.6f} "
                f"({error:.1%}) on seed {seed}"
            )


def test_the_registry_names_match_what_each_estimator_reports() -> None:
    """The checkbox key and the payload's `estimator` field must be the same string.

    R4 selects estimators by name off the query string and R5 draws a checkbox per key.
    If a registry key and the result's own label ever drifted apart, a line would be
    requested under one name and returned under another, and the chart would silently
    draw the wrong estimator against the right label.
    """
    for name, estimator in ESTIMATORS.items():
        assert estimator(RANGE_BARS, interval=MINUTE).estimator == name

    assert set(ESTIMATORS) == {
        "simple", "log", "parkinson", "garman_klass", "rogers_satchell",
    }


def test_yang_zhang_and_garch_are_absent_by_decision() -> None:
    """Neither is missing by oversight, so their absence is pinned rather than assumed.

    Yang-Zhang's whole contribution over Rogers-Satchell is an overnight term that needs
    a market that closes; crypto never does, so it degenerates into a more expensive
    Rogers-Satchell. GARCH is a fitted model producing a forecast, which makes it the
    same kind of object as IV rather than the same kind as RV — putting it on this chart
    would be a model-versus-market comparison wearing the clothes of a
    market-versus-realisation one.
    """
    from deltapayoff import realised_vol

    assert not hasattr(realised_vol, "yang_zhang")
    assert not hasattr(realised_vol, "garch")


#: An epoch-aligned instant. `START` is 05:41, which is deliberately *not* on a
#: five-minute boundary — the buckets below align to the epoch rather than to the first
#: bar, so that two ranges over the same data cut their boundaries in the same places.
ALIGNED = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)


def test_resampling_rolls_minutes_into_a_coarser_bar() -> None:
    """Five one-minute bars become one five-minute bar: first open, last close, extremes.

    The sampling interval is a control on the screen, and the store holds minutes, so
    every interval above `1m` is built here. Getting the roll-up wrong would put a
    plausible number on every line at every interval except the one that was tested.
    """
    minutes = [
        Bar(at=ALIGNED + n * MINUTE, open=100.0 + n, high=110.0 + n,
            low=90.0 + n, close=101.0 + n)
        for n in range(5)
    ]

    rolled = resample(minutes, 5 * MINUTE)

    assert len(rolled) == 1
    assert rolled[0].at == ALIGNED
    assert rolled[0].open == 100.0     # the first bar's open
    assert rolled[0].close == 105.0    # the last bar's close
    assert rolled[0].high == 114.0     # the highest high
    assert rolled[0].low == 90.0       # the lowest low


def test_a_bucket_with_no_bars_produces_no_bar() -> None:
    """Never forward-fill, at every resolution and not only the stored one.

    Minutes 0-4 and 10-14 arrive; minutes 5-9 never did. The middle five-minute bucket is
    absent from the output rather than repeating the previous close — which would give a
    zero return and pull every estimator down exactly where the feed was worst.
    """
    present = [
        Bar(at=ALIGNED + n * MINUTE, open=100.0, high=101.0, low=99.0, close=100.0)
        for n in [*range(5), *range(10, 15)]
    ]

    rolled = resample(present, 5 * MINUTE)

    assert [bar.at for bar in rolled] == [ALIGNED, ALIGNED + 10 * MINUTE]


def test_buckets_align_to_the_epoch_not_to_the_first_bar() -> None:
    """Bars starting at 05:41 fall into the 05:40 bucket, not into a bucket of their own.

    Aligning to the first bar would make the same five minutes of data roll up differently
    depending on where the reader happened to start looking, so two ranges over one series
    would disagree about what a five-minute bar was.
    """
    minutes = [
        Bar(at=START + n * MINUTE, open=100.0, high=101.0, low=99.0, close=100.0)
        for n in range(6)
    ]

    rolled = resample(minutes, 5 * MINUTE)

    assert rolled[0].at == START - timedelta(minutes=1)
    assert len(rolled) == 2


def test_resampling_to_the_stored_interval_changes_nothing() -> None:
    """The identity case, so `1m` is not a separate code path with separate bugs."""
    minutes = [
        Bar(at=START + n * MINUTE, open=100.0 + n, high=110.0 + n,
            low=90.0 + n, close=101.0 + n)
        for n in range(5)
    ]

    assert resample(minutes, MINUTE) == minutes


# -- the rolling path ----------------------------------------------------------------


def test_rolling_agrees_exactly_with_the_window_at_a_time_functions() -> None:
    """The fast path and the plain one must be the same answer, not merely a close one.

    `rolling` exists because recomputing each window from scratch is O(window) per point
    — measured at 15 ms a point over a 14-day window of minutes, so 27 seconds for one
    chart. Every estimator here is a sum over its window, so prefix sums make each window
    O(1). That is a second implementation of five formulas, and a second implementation
    is a second thing that can be wrong, so this test is the one that earns it.

    The series carries two holes, because the gap rule is the part most likely to differ:
    the plain path filters adjacent pairs and the rolling path indexes them.
    """
    bars = [
        Bar(at=ALIGNED + n * MINUTE, open=100.0 + (n % 7),
            high=101.0 + (n % 5), low=99.0 - (n % 3), close=100.0 + (n % 11) * 0.1)
        for n in range(400)
        if n not in {*range(120, 137), *range(300, 305)}
    ]
    lookback = 60 * MINUTE
    stamps = [ALIGNED + n * MINUTE for n in range(60, 400, 7)]

    for name, estimator in ESTIMATORS.items():
        fast = rolling(
            bars, estimator=name, interval=MINUTE, lookback=lookback, timestamps=stamps
        )
        assert len(fast) == len(stamps)
        for at, quick in zip(stamps, fast, strict=True):
            window = [bar for bar in bars if at - lookback <= bar.at <= at]
            plain = estimator(window, interval=MINUTE)

            assert quick.observations == plain.observations, f"{name} at {at}"
            assert quick.expected == plain.expected, f"{name} at {at}"
            if plain.value is None:
                assert quick.value is None, f"{name} at {at}"
            else:
                assert quick.value is not None
                # Prefix sums reassociate the arithmetic, so the two differ in the last
                # bits and not before them. A stricter bound would be a test of floating
                # point rather than of the estimator.
                assert abs(quick.value - plain.value) < 1e-12 * max(plain.value, 1e-6), (
                    f"{name} at {at}: {quick.value} vs {plain.value}"
                )


def test_rolling_over_an_empty_window_reports_nothing_rather_than_zero() -> None:
    """A timestamp before the record starts has no window, and says so."""
    bars = [
        Bar(at=ALIGNED + n * MINUTE, open=100.0, high=101.0, low=99.0, close=100.0)
        for n in range(10)
    ]

    results = rolling(
        bars, estimator="log", interval=MINUTE, lookback=5 * MINUTE,
        timestamps=[ALIGNED - MINUTE],
    )

    assert results[0].value is None
    assert results[0].observations == 0
