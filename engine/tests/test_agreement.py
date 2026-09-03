"""The agreement machinery, against the eight-expiry capture.

`tickers-btc-multi-expiry.json` is a verbatim `GET /v2/tickers` for BTC options with no
expiry filter, captured 2026-09-02T08:40:14Z. 588 contracts across eight expiries from
half a day to 85 days out — the span the time slice needs.
"""

from __future__ import annotations

import pytest

from deltapayoff.agreement import (
    MONEYNESS_BANDS,
    TIME_BANDS,
    band_for,
    chains_by_expiry,
    compare,
    days_to_expiry,
    utc,
)

CAPTURE = utc("2026-09-02T08:40:14Z")


@pytest.fixture
def chains(multi_expiry_tickers):
    return chains_by_expiry("BTC", multi_expiry_tickers, CAPTURE)


def test_the_capture_splits_into_eight_expiries_in_date_order(chains) -> None:
    """Ordered by date, not by string — `03-09-2026` sorts before `30-10-2026` only if
    the comparison parses them."""
    assert list(chains) == [
        "03-09-2026",
        "04-09-2026",
        "05-09-2026",
        "11-09-2026",
        "18-09-2026",
        "25-09-2026",
        "30-10-2026",
        "27-11-2026",
    ]


def test_the_expiries_span_one_day_to_eighty_six(chains) -> None:
    """The front expiry is the stress case: `sqrt(T)` multiplies every vega in the book,
    so a one-day contract carries far less of it than a three-month one at the same
    moneyness. Snapshot 2026-09-02T08:40Z, settlement at 12:00Z, so the 3 September
    expiry is 1.14 days out and not the half day a calendar-date subtraction suggests.
    """
    days = [days_to_expiry(c) for c in chains.values()]

    assert days[0] == pytest.approx(1.139, abs=0.01)
    assert days[-1] == pytest.approx(86.139, abs=0.01)
    assert days == sorted(days)


def test_every_expiry_lands_in_a_time_band(chains) -> None:
    """A contract falling outside every band would be silently dropped from the matrix.

    Three of the four bands are occupied. **`under a day` is empty on this capture** —
    Delta lists a daily expiry, but the nearest one was still 1.14 days out when the
    snapshot was taken. Asserted as empty rather than quietly unmentioned, because a
    band with no data is a gap in the study and not a result.
    """
    bands = [band_for(days_to_expiry(c), TIME_BANDS) for c in chains.values()]

    assert set(bands) == {"1 to 7 days", "7 to 30 days", "over 30 days"}
    assert bands.count("1 to 7 days") == 3
    assert bands.count("7 to 30 days") == 3
    assert bands.count("over 30 days") == 2


def test_moneyness_bands_cover_the_boundaries() -> None:
    """Half-open on the left, so a strike exactly at a boundary lands in exactly one
    band. 0.98 is at the money; 0.9799 is not."""
    assert band_for(0.9799, MONEYNESS_BANDS) == "OTM put side"
    assert band_for(0.98, MONEYNESS_BANDS) == "at the money"
    assert band_for(1.0199, MONEYNESS_BANDS) == "at the money"
    assert band_for(1.02, MONEYNESS_BANDS) == "OTM call side"


def test_compare_reports_gaps_in_vol_points_from_hand_checked_inputs() -> None:
    """Four strikes, gaps of 0.01, 0.02, 0.03 and 0.10 in decimal — so 1, 2, 3 and 10
    vol points. Median of the middle two is 2.5; the worst is 10."""
    left = {1.0: 0.30, 2.0: 0.30, 3.0: 0.30, 4.0: 0.30}
    right = {1.0: 0.31, 2.0: 0.32, 3.0: 0.33, 4.0: 0.40}

    result = compare("A", left, "B", right)

    assert result.n == 4
    assert result.median == pytest.approx(2.5)
    assert result.worst == pytest.approx(10.0)
    assert result.within_target is False


def test_compare_scores_only_the_strikes_both_methods_solved() -> None:
    """A declined strike is not a disagreement.

    Scoring refusals as gaps would rank the most careful solver worst, which inverts the
    thing the matrix is for.
    """
    left = {1.0: 0.30, 2.0: 0.30, 3.0: 0.30}
    right = {1.0: 0.3001, 2.0: 0.3002}

    result = compare("A", left, "B", right)

    assert result.n == 2
    assert result.within_target is True


def test_compare_returns_nothing_when_no_strike_is_shared() -> None:
    """Zero overlap is not agreement of zero. Reporting 0.0 would read as perfect."""
    assert compare("A", {1.0: 0.3}, "B", {2.0: 0.3}) is None
