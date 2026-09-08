"""Both series on one axis, at one lookback. Pure; no store and no network.

The two halves are built by hand here so the assertions are about the joining rather than
about the estimators or the index, both of which have their own suites.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deltapayoff.iv_index import ContractIv
from deltapayoff.models import ChainResponse, ChainRow, ComputedLeg, Leg
from deltapayoff.realised_vol import Bar
from deltapayoff.volatility import (
    contract_ivs_from_chains,
    lookback_bounds,
    volatility_series,
)

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
START = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)

#: Ten days, sampled hourly. Not a minute-scale lookback, because `N` drives the implied
#: tenor as well as the realised window and the index refuses any tenor under seven days —
#: so a test that plants a sixty-minute lookback is testing a combination the instrument
#: is built to reject. Hourly bars keep it to 480 rows.
LOOKBACK = 10 * DAY
INTERVAL = HOUR


def walking_bars(count: int, *, skip: set[int] | None = None) -> list[Bar]:
    """A price that alternates up and down a fixed amount, so volatility is non-zero.

    `skip` names buckets that never arrived — no row at all, per the never-forward-fill
    rule, rather than a row repeating the previous close.
    """
    skip = skip or set()
    bars = []
    for index in range(count):
        if index in skip:
            continue
        close = 80_000.0 + (10.0 if index % 2 else 0.0)
        bars.append(
            Bar(
                at=START + index * INTERVAL,
                open=close,
                high=close + 5.0,
                low=close - 5.0,
                close=close,
            )
        )
    return bars


def iv_rows_at(level: float = 0.40) -> list[ContractIv]:
    """A flat term structure at 8 and 60 days, bracketing a ten-day tenor."""
    return [
        ContractIv(
            expiry=expiry,
            strike=strike,
            iv=level,
            forward=80_005.0,
            years_to_expiry=days / 365.0,
        )
        for expiry, days in (("near", 8.0), ("far", 60.0))
        for strike in (80_000.0, 80_500.0)
    ]


def iv_by_bucket(count: int) -> dict[datetime, list[ContractIv]]:
    return {START + n * INTERVAL: iv_rows_at() for n in range(count)}


def test_contemporaneous_points_carry_both_series_on_one_timestamp() -> None:
    """One point per step, each with an IV and a realised volatility for the same minute.

    The realised figure at `t` looks back over `[t - N, t]`; the implied figure at `t` is
    the index read at `t`. That is what "contemporaneous" means and it is the default
    because it is what traders read — the endpoint's job is to make it available and to
    make the *other* mode available beside it.
    """
    bars = walking_bars(480)  # twenty days of hours

    series = volatility_series(
        spot_bars=bars,
        iv_rows=iv_by_bucket(480),
        lookback=LOOKBACK,
        interval=INTERVAL,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY,
        end=START + 19 * DAY,
        step=12 * HOUR,
    )

    assert series.alignment == "contemporaneous"
    assert series.estimators == ["log"]
    assert len(series.points) == 9

    for point in series.points:
        assert point.iv is not None
        assert point.rv["log"] is not None
        # 240 one-hour steps in a ten-day window.
        assert point.returns["log"] == 240
        assert point.coverage["log"] == 1.0


def test_lag_alignment_puts_implied_against_the_realisation_that_followed_it() -> None:
    """IV at `t` is compared with the movement over `[t, t + N]`, not `[t - N, t]`.

    Contemporaneous is what vendors draw, but at any point the two lines describe windows
    `2N` apart end to end, so the visible spread is not the premium. Any figure reported
    *as* the premium has to come from this mode.

    The tell is that the two modes disagree: the realised figure attached to a given
    timestamp is a different window's answer.
    """
    bars = walking_bars(480, skip={200, 201, 202})

    def run(alignment: str) -> dict[datetime, float | None]:
        series = volatility_series(
            spot_bars=bars,
            iv_rows=iv_by_bucket(480),
            lookback=LOOKBACK,
            interval=INTERVAL,
            estimators=["log"],
            alignment=alignment,
            start=START + 2 * DAY,
            end=START + 6 * DAY,
            step=DAY,
        )
        return {point.at: point.rv["log"] for point in series.points}

    contemporaneous = run("contemporaneous")
    lagged = run("lag")

    assert set(contemporaneous) == set(lagged)
    assert contemporaneous != lagged


def test_the_trailing_window_has_implied_present_and_realised_null() -> None:
    """The last `N` days have no realisation yet — that answer has not happened.

    It is rendered as a null on a point that exists, deliberately, rather than as an
    omitted point or a zero. An omitted point makes the client infer the reason from a
    hole; a zero says the market was perfectly calm, which is a claim rather than an
    absence.
    """
    bars = walking_bars(480)  # twenty days, so the last ten cannot look forward

    series = volatility_series(
        spot_bars=bars,
        iv_rows=iv_by_bucket(480),
        lookback=LOOKBACK,
        interval=INTERVAL,
        estimators=["log", "parkinson"],
        alignment="lag",
        start=START + 12 * DAY,
        end=START + 19 * DAY,
        step=DAY,
    )

    trailing = [point for point in series.points if point.at > START + 9 * DAY]
    assert trailing, "the fixture must reach into the window that has not finished"

    for point in trailing:
        assert point.iv is not None, "implied is known now; realised is not"
        for name in ("log", "parkinson"):
            assert name in point.rv, "the key is present even when the value is not"
            assert point.rv[name] is None
            assert point.rv[name] != 0


def test_bounds_are_limited_by_the_history_held_when_that_is_the_shorter() -> None:
    """Twenty days of bars against a term structure reaching sixty: history wins.

    This is the case that binds for the first month of recording, and it is the reason
    the bound is computed rather than fixed. A screen showing a slider that runs to sixty
    days over twenty days of data would refuse everything past twenty and look broken.
    """
    bounds = lookback_bounds(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),
        interval=INTERVAL,
    )

    assert abs(bounds.max_days - 19.958333333333332) < 1e-9
    assert bounds.binding == "history"
    assert "history" in bounds.detail
    assert bounds.usable


def test_bounds_are_limited_by_the_term_structure_when_that_is_the_shorter() -> None:
    """Ninety days of bars against a term structure reaching sixty: the market wins.

    Past the longest listed expiry the index would be extrapolating, which R3 refuses.
    """
    bounds = lookback_bounds(
        spot_bars=walking_bars(90 * 24),
        iv_rows=iv_by_bucket(90 * 24),
        interval=INTERVAL,
    )

    assert abs(bounds.max_days - 60.0) < 1e-9
    assert bounds.binding == "term_structure"


def test_the_lower_bound_never_goes_under_the_shortest_listed_expiry() -> None:
    """`N` drives the implied tenor too, so below the front expiry there is no IV.

    The observation floor alone would allow a lookback of a few hours here. The index
    would return nothing for it, and the chart would draw one line and call it a
    comparison.
    """
    bounds = lookback_bounds(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),
        interval=INTERVAL,
    )

    assert bounds.min_days >= 8.0


def test_bounds_say_so_when_nothing_at_all_is_usable() -> None:
    """Half an hour of spot against a term structure starting at eight days.

    This is the store as it actually stands on 2026-09-06 — thirty minutes of bars — and
    the honest answer is that no lookback works yet, said in a form the screen can print.
    """
    bounds = lookback_bounds(
        spot_bars=walking_bars(30),
        iv_rows=iv_by_bucket(30),
        interval=MINUTE,
    )

    assert not bounds.usable
    assert bounds.binding == "history"


def test_a_window_with_holes_reports_its_coverage_rather_than_hiding_them() -> None:
    """The value is computed from the returns that exist, and says how many that was.

    Never-forward-fill means a gap is a missing row, so a window over a bad patch and a
    window over a good one produce numbers that look identical. `coverage` is the only
    thing that distinguishes them, which is why it is per estimator and on every point.

    The return estimators lose more than the range estimators to the same hole: a return
    needs two adjacent bars and a high-low range needs one, so a three-bar hole costs the
    log estimator four returns and Parkinson three bars.
    """
    holed = walking_bars(480, skip={300, 301, 302})

    series = volatility_series(
        spot_bars=holed,
        iv_rows=iv_by_bucket(480),
        lookback=LOOKBACK,
        interval=INTERVAL,
        estimators=["log", "parkinson"],
        alignment="contemporaneous",
        start=START + 14 * DAY,
        end=START + 14 * DAY,
        step=DAY,
    )

    point = series.points[0]
    assert point.rv["log"] is not None, "a holed window still has an answer"
    assert point.coverage["log"] < 1.0
    assert point.coverage["parkinson"] < 1.0
    assert point.returns["log"] == 236  # 240 steps, four lost to a three-bar hole
    assert point.returns["parkinson"] == 238  # 241 buckets, three bars missing
    assert point.coverage["parkinson"] > point.coverage["log"]


def test_a_coarser_sampling_interval_thins_the_returns_it_reports() -> None:
    """Asking for four-hourly sampling over a ten-day window gives 60 returns, not 240.

    The interval is a real control, not a label: the bars are rolled up before any
    estimator sees them. Reporting 240 while having used 60 would make the payload's own
    count a lie, and that count is the only thing on screen telling the reader how much
    the estimate rests on.
    """
    series = volatility_series(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),
        lookback=LOOKBACK,
        interval=4 * HOUR,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY,
        end=START + 15 * DAY,
        step=DAY,
    )

    assert series.interval_seconds == 4 * 3600
    assert series.points[0].returns["log"] == 60


def test_the_valid_sampling_intervals_are_offered_for_the_lookback_asked_for() -> None:
    """A ten-day window supports coarse sampling; a ten-day one at 1d does not.

    R5 offers a choice rather than imposing one derived value, so the endpoint has to say
    which choices are legitimate at this `N` — an interval leaving fewer than the
    observation floor's worth of returns is not one of them.
    """
    series = volatility_series(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),
        lookback=10 * DAY,
        interval=HOUR,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY,
        end=START + 15 * DAY,
        step=DAY,
    )

    # Ten days is 240 hours: 1m, 5m, 15m, 30m, 1h and 4h all clear thirty returns; 1d
    # gives ten and does not.
    assert "1h" in series.valid_intervals
    assert "4h" in series.valid_intervals
    assert "1d" not in series.valid_intervals


def test_implied_is_read_from_the_most_recent_minute_at_or_before_the_point() -> None:
    """An output step that lands between stored minutes still finds an implied figure.

    The step is `stride * interval`, and `start` is the first bar plus the lookback — so a
    lookback of 10.01 days puts every point 24 seconds off a minute boundary. Demanding an
    exact key match there would make the implied line vanish entirely, silently, for no
    reason a reader could see. Nothing about the data changed; only the arithmetic of
    where the points fell.

    Reading backwards is also the honest direction: implied volatility at 12:00:24 is what
    was implied at 12:00, not what will be implied at 12:01.
    """
    offset = timedelta(seconds=24)

    series = volatility_series(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),
        lookback=LOOKBACK,
        interval=INTERVAL,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY + offset,
        end=START + 16 * DAY + offset,
        step=12 * HOUR,
    )

    assert all(point.iv is not None for point in series.points)


def test_implied_is_not_carried_forward_across_a_long_silence() -> None:
    """Stale is not the same as current, and a chart may not present it as current.

    The IV rows here stop halfway through the record. Past that point the most recent
    stored figure is hours old, and drawing it would forward-fill the implied line — the
    one thing this project refuses everywhere else.
    """
    truncated = {
        at: rows
        for at, rows in iv_by_bucket(480).items()
        if at <= START + 10 * DAY
    }

    series = volatility_series(
        spot_bars=walking_bars(480),
        iv_rows=truncated,
        lookback=LOOKBACK,
        interval=INTERVAL,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY,
        end=START + 19 * DAY,
        step=DAY,
    )

    assert all(point.iv is None for point in series.points)


def test_implied_is_scaled_from_its_annualised_solve_to_the_lookback_window() -> None:
    """A 40% annualised IV over a ten-day window is `0.40 * sqrt(10/365)` = 6.62%.

    Implied volatility is a Black-76 parameter and is annualised **by construction** —
    the model only balances when sigma and T share a calendar. Realised volatility here is
    a window standard deviation with no scaling up. Serving the first raw beside the
    second would put a 40% line above a 7% line and call the gap a risk premium, when the
    entire gap would be a unit mismatch.

    Oracle: `0.40 * sqrt(10/365) = 0.06620847108818943`, worked by hand.
    """
    series = volatility_series(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),  # a flat 40% term structure
        lookback=10 * DAY,
        interval=INTERVAL,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY,
        end=START + 15 * DAY,
        step=DAY,
    )

    implied = series.points[0].iv
    assert implied is not None
    assert abs(implied - 0.06620847108818943) < 1e-12


def test_a_year_is_365_days_here_and_not_252() -> None:
    """Crypto trades weekends and this venue lists weekend expiries.

    A 1/252 year was measured overstating theta by 1.456x (`docs/greeks.md`). Using it
    here would inflate every implied point by `sqrt(365/252)` = 1.204 — a 20% premium
    conjured out of a calendar.
    """
    series = volatility_series(
        spot_bars=walking_bars(480),
        iv_rows=iv_by_bucket(480),
        lookback=10 * DAY,
        interval=INTERVAL,
        estimators=["log"],
        alignment="contemporaneous",
        start=START + 15 * DAY,
        end=START + 15 * DAY,
        step=DAY,
    )

    implied = series.points[0].iv
    assert implied is not None
    with_252 = 0.40 * (10 / 252) ** 0.5
    assert abs(implied - with_252) > 0.01


# ---------------------------------------------------------------------------
# The floor is a tenor that resolves, not the shortest expiry ever listed.
# ---------------------------------------------------------------------------


def _leg(symbol: str, *, iv: float | None) -> Leg:
    """A leg the way the live path builds one: Delta's fields, plus ours under `computed`.

    IV sits at `leg.computed.iv` and never at `leg.iv` — the top-level `*_iv` fields are
    Delta's own and are reference columns this engine never reads
    (`tests/test_no_delta_inputs.py` pins that). A helper here rather than inline so a
    test cannot accidentally assert against the venue's figure.
    """
    return Leg(symbol=symbol, computed=ComputedLeg(iv=iv, iv_leg="call" if iv else None))


def _minute_with_shortest(shortest_days: float, at: datetime) -> list[ContractIv]:
    """One minute's contract rows whose shortest usable expiry is `shortest_days`."""
    return [
        ContractIv(
            expiry=f"{days:.0f}d",
            strike=strike,
            iv=0.4,
            forward=80_000.0,
            years_to_expiry=days / 365.0,
        )
        for days in (shortest_days, shortest_days + 30.0)
        for strike in (79_500.0, 80_500.0)
    ]


def test_the_floor_is_a_tenor_most_minutes_can_actually_serve() -> None:
    """One minute listing a 7.5-day expiry must not set the floor for all of them.

    `atm_iv` refuses to extrapolate below the shortest expiry **at that minute**, so a
    lookback chosen from the shortest expiry ever *seen anywhere in the range* resolves
    at the handful of minutes that happened to list it and nowhere else. On the stored
    data that is the difference between an implied line of 99 points and one of 24, all
    24 inside a single half-hour — which draws as a dot and reads as a broken chart
    rather than as a bound the reader was allowed to pick badly.

    Nineteen minutes here cannot serve a tenor under 11 days and one can serve 7.5. The
    floor must follow the nineteen.
    """
    at = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
    iv_rows = {
        at + timedelta(minutes=n): _minute_with_shortest(11.0, at + timedelta(minutes=n))
        for n in range(19)
    }
    iv_rows[at + timedelta(minutes=19)] = _minute_with_shortest(7.5, at)

    bounds = lookback_bounds(
        spot_bars=walking_bars(60 * 24 * 40),
        iv_rows=iv_rows,
        interval=timedelta(hours=1),
    )

    assert bounds.min_days == pytest.approx(11.0), (
        "the floor must be a tenor most minutes bracket, not the one minute that "
        f"listed a nearer expiry (got {bounds.min_days})"
    )


# ---------------------------------------------------------------------------
# The live edge: the chain cache's solved ladders as one more implied minute.
# ---------------------------------------------------------------------------


def test_the_live_chain_becomes_one_implied_minute() -> None:
    """Solved ladders from the cache convert to the same rows the store yields.

    **The cache holds one frame per contract and no history**, so this can only ever add
    the newest minute — it is a live right edge, not a backfill, and the function is
    named for what it does rather than for what a reader might hope. What it buys is
    that the implied line reaches *now* instead of stopping at the last flush, which on
    a five-minute flush cadence is the difference between a chart that looks current and
    one that looks stalled.

    The forward and the time to expiry are taken from the chain that carried them, never
    recomputed here: `docs/implied-vol.md` §2.1 measures the forward as the axis IV
    disagreement turns on, so a second derivation of it would be a second answer.
    """
    at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    chain = ChainResponse(
        underlying="BTC",
        expiry="18-09-2026",
        fetched_at=at.isoformat(),
        spot=80_000.0,
        forward=80_120.0,
        years_to_expiry=0.0274,
        atm_strike=80_000.0,
        discount=0.999,
        forward_method="F1",
        rows=[
            ChainRow(strike=80_000.0, call=_leg("C-80000", iv=0.42)),
        ],
    )

    rows = contract_ivs_from_chains([chain], at=at)

    assert list(rows) == [at]
    assert [r.iv for r in rows[at]] == [0.42]
    assert [r.strike for r in rows[at]] == [80_000.0]
    assert [r.forward for r in rows[at]] == [80_120.0]
    assert [r.years_to_expiry for r in rows[at]] == [0.0274]


def test_a_leg_the_solver_declined_contributes_no_row() -> None:
    """No IV, no row. A strike with no volatility is not a strike at volatility zero.

    The same rule the ladder obeys on screen and the store obeys on disk: a declined
    solve is an absence, and inventing a row for it would put a zero into the index's
    strike interpolation, dragging the at-the-money figure toward nothing.
    """
    at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    chain = ChainResponse(
        underlying="BTC",
        expiry="18-09-2026",
        fetched_at=at.isoformat(),
        spot=80_000.0,
        forward=80_120.0,
        years_to_expiry=0.0274,
        atm_strike=80_000.0,
        discount=0.999,
        forward_method="F1",
        rows=[
            ChainRow(strike=80_000.0, call=_leg("C-80000", iv=None)),
            ChainRow(strike=80_500.0, call=_leg("C-80500", iv=0.39)),
        ],
    )

    rows = contract_ivs_from_chains([chain], at=at)

    assert [r.strike for r in rows[at]] == [80_500.0]
