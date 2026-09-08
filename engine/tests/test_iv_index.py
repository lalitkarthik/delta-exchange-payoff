"""The constant-maturity ATM implied volatility index. Pure, no network.

Every expected value is worked by hand off the definition and pasted as a literal, so an
assertion cannot agree with the implementation merely by sharing its arithmetic.
"""

from __future__ import annotations

import pytest

from deltapayoff.iv_index import (
    ContractIv,
    atm_iv,
    atm_iv_for_expiry,
    atm_iv_nearest,
)

FORWARD = 78_400.0


def expiry_rows(
    expiry: str,
    days: float,
    ivs: dict[float, float],
    forward: float = FORWARD,
) -> list[ContractIv]:
    """One expiry's strikes, carrying the chain-level forward and time to expiry."""
    return [
        ContractIv(
            expiry=expiry,
            strike=strike,
            iv=iv,
            forward=forward,
            years_to_expiry=days / 365.0,
        )
        for strike, iv in ivs.items()
    ]


def test_atm_iv_interpolates_between_the_two_strikes_bracketing_the_forward() -> None:
    """The forward sits 80% of the way from 78,000 to 78,500, so the IV does too.

    Oracle worked by hand: `0.50 + 0.8 * (0.40 - 0.50) = 0.42`.
    """
    rows = expiry_rows("25-09-2026", 30.0, {78_000.0: 0.50, 78_500.0: 0.40})

    result = atm_iv(rows, tenor_days=30.0)

    assert result is not None
    assert abs(result.iv - 0.42) < 1e-12


def test_nearest_strike_would_have_given_a_different_answer() -> None:
    """Pinning the choice rather than assuming it.

    Snapping to the nearest strike would read 78,500's 0.40, because the forward at
    78,400 is nearer to it. Interpolating reads 0.42. The gap is the sawtooth that
    nearest-strike puts on the chart every time the forward crosses a strike midpoint —
    a step that is not volatility moving but a different contract being read.
    """
    rows = expiry_rows("25-09-2026", 30.0, {78_000.0: 0.50, 78_500.0: 0.40})

    result = atm_iv(rows, tenor_days=30.0)

    assert result is not None
    nearest = 0.40
    assert abs(result.iv - nearest) > 0.01


def test_expiries_are_blended_in_total_variance_not_in_volatility() -> None:
    """14 days at 60% and 42 days at 40%, asked at 28 days.

    Total variance is `sigma^2 * T`: 0.013808 at 14 days, 0.018411 at 42. Twenty-eight
    days sits halfway between them in `T`, so the blended total variance is 0.016110 and
    the volatility is `sqrt(0.016110 / (28/365)) = 0.458258`.

    Oracle worked by hand from those five numbers.
    """
    rows = [
        *expiry_rows("18-09-2026", 14.0, {78_000.0: 0.60, 78_500.0: 0.60}),
        *expiry_rows("16-10-2026", 42.0, {78_000.0: 0.40, 78_500.0: 0.40}),
    ]

    result = atm_iv(rows, tenor_days=28.0)

    assert result is not None
    assert abs(result.iv - 0.458257569495584) < 1e-12


def test_interpolating_in_volatility_instead_would_give_a_different_answer() -> None:
    """The choice is pinned, not assumed.

    A straight line drawn between two volatilities gives 0.50 here against the correct
    0.4583 — a 9% difference on the same two inputs. Variance is additive across time and
    volatility is not, so the naive line is a curve in the quantity that actually
    accumulates, and between two expiries it can imply **negative forward variance** — a
    term structure that can be arbitraged against itself. CBOE's VIX and Deribit's DVOL
    both interpolate in total variance for this reason.
    """
    rows = [
        *expiry_rows("18-09-2026", 14.0, {78_000.0: 0.60, 78_500.0: 0.60}),
        *expiry_rows("16-10-2026", 42.0, {78_000.0: 0.40, 78_500.0: 0.40}),
    ]

    result = atm_iv(rows, tenor_days=28.0)

    assert result is not None
    interpolated_in_volatility = 0.50
    assert abs(result.iv - interpolated_in_volatility) > 0.04


def test_an_expiry_under_seven_days_cannot_enter_the_index() -> None:
    """A 3-day expiry at an absurd 300% is present in the rows and absent from the answer.

    Near expiry an option's implied volatility becomes extremely noisy. This repo has
    already measured the front expiry's own forward fit implying a 43.1% rate at 1.139
    days — outside its own 0-30% gate (`docs/greeks.md` section 6). VIX excludes options
    under a week for the same reason.

    Asked at 30 days the bracket is 14 and 42, so the 3-day expiry is nowhere near it
    either way; the assertion is that it is excluded *by rule* rather than by luck, which
    is why the answer must equal the one computed without it at all.
    """
    sane = [
        *expiry_rows("18-09-2026", 14.0, {78_000.0: 0.60, 78_500.0: 0.60}),
        *expiry_rows("16-10-2026", 42.0, {78_000.0: 0.40, 78_500.0: 0.40}),
    ]
    noisy = [*expiry_rows("07-09-2026", 3.0, {78_000.0: 3.0, 78_500.0: 3.0}), *sane]

    with_noise = atm_iv(noisy, tenor_days=30.0)
    without = atm_iv(sane, tenor_days=30.0)

    assert with_noise is not None and without is not None
    assert with_noise.iv == without.iv
    assert with_noise.excluded_under_floor == 1
    assert without.excluded_under_floor == 0
    assert with_noise.near_days >= 7.0


def test_a_tenor_the_short_end_cannot_reach_is_refused_not_extrapolated() -> None:
    """Below the shortest surviving expiry there is no opinion to interpolate between.

    With the 3-day expiry excluded by the floor, a 5-day tenor has nothing beneath it. An
    answer here would be an extrapolation of the front of the term structure, which is
    the noisiest part of it and the part the floor exists to keep out.
    """
    rows = [
        *expiry_rows("07-09-2026", 3.0, {78_000.0: 3.0, 78_500.0: 3.0}),
        *expiry_rows("18-09-2026", 14.0, {78_000.0: 0.60, 78_500.0: 0.60}),
    ]

    assert atm_iv(rows, tenor_days=5.0) is None


def test_a_tenor_beyond_the_longest_expiry_is_refused() -> None:
    """Past the longest listed expiry there is no market opinion at all."""
    rows = [
        *expiry_rows("18-09-2026", 14.0, {78_000.0: 0.60, 78_500.0: 0.60}),
        *expiry_rows("16-10-2026", 42.0, {78_000.0: 0.40, 78_500.0: 0.40}),
    ]

    assert atm_iv(rows, tenor_days=90.0) is None
    assert atm_iv(rows, tenor_days=42.0) is not None


def test_the_expiry_roll_is_weighted_to_zero_rather_than_stepped() -> None:
    """The moment an expiry crosses the tenor, its neighbour's weight is already gone.

    A ladder at 16, ~30 and 58 days, asked at 30. Just before the middle expiry crosses
    the tenor it is the *far* bracket and the 16-day expiry carries a weight of 0.00071.
    Just after, the middle expiry is the *near* bracket carrying 0.99964 and the 16-day
    expiry has left entirely.

    So the departing expiry's contribution reaches zero exactly as it exits: the index
    moves by 7.5e-05 across the hand-over — **measured** from the two blends worked by
    hand — rather than stepping. Rolling early or late would produce a visible jump that
    is not volatility moving.
    """
    ladder = {78_000.0: 0.0, 78_500.0: 0.0}  # levels are set per expiry below

    def rungs(middle_days: float) -> list[ContractIv]:
        return [
            *expiry_rows("a", 16.0, dict.fromkeys(ladder, 0.55)),
            *expiry_rows("b", middle_days, dict.fromkeys(ladder, 0.45)),
            *expiry_rows("c", 58.0, dict.fromkeys(ladder, 0.40)),
        ]

    before = atm_iv(rungs(30.01), tenor_days=30.0)
    after = atm_iv(rungs(29.99), tenor_days=30.0)

    assert before is not None and after is not None
    assert before.near_days == 16.0 and before.near_weight < 0.001
    assert after.near_days == 29.99 and after.near_weight > 0.999
    assert abs(before.iv - after.iv) < 1e-3


def test_a_minute_whose_bracketing_data_is_absent_returns_nothing() -> None:
    """Four ways for the answer not to exist, and none of them is a fallback number."""
    assert atm_iv([], tenor_days=30.0) is None

    # Every strike above the forward: the smile has no lower wing to interpolate from.
    one_sided = expiry_rows("25-09-2026", 30.0, {79_000.0: 0.5, 79_500.0: 0.5})
    assert atm_iv(one_sided, tenor_days=30.0) is None

    # Only expiries the floor removes.
    front_only = expiry_rows("07-09-2026", 3.0, {78_000.0: 0.5, 78_500.0: 0.5})
    assert atm_iv(front_only, tenor_days=30.0) is None

    # A tenor of zero has no variance to divide by.
    fine = expiry_rows("25-09-2026", 30.0, {78_000.0: 0.5, 78_500.0: 0.5})
    assert atm_iv(fine, tenor_days=0.0) is None


def test_the_forward_decides_the_strikes_not_the_spot() -> None:
    """Two chains identical but for the forward give two different ATM levels.

    The forward is read off the stored column. Approximating it with spot would read the
    smile at the wrong point, and on a skewed smile — which is every real one — that is a
    bias in the index rather than noise in it.
    """
    smile = {78_000.0: 0.50, 78_500.0: 0.40}
    at_78400 = atm_iv(expiry_rows("x", 30.0, smile, forward=78_400.0), tenor_days=30.0)
    at_78100 = atm_iv(expiry_rows("x", 30.0, smile, forward=78_100.0), tenor_days=30.0)

    assert at_78400 is not None and at_78100 is not None
    assert abs(at_78400.iv - 0.42) < 1e-12
    assert abs(at_78100.iv - 0.48) < 1e-12


# ---------------------------------------------------------------------------
# The nearest expiry, read directly. No interpolation, so nothing to refuse.
# ---------------------------------------------------------------------------


def _expiry_rows(*, days: float, level: float) -> list[ContractIv]:
    """One expiry whose two strikes straddle the forward at the same volatility.

    Flat across the strikes on purpose: these tests are about *which expiry* is read,
    and a smile would make the expected value depend on the strike interpolation that
    `test_atm_iv_interpolates_between_the_two_strikes_bracketing_the_forward` already
    covers.
    """
    strikes = {FORWARD - 500.0: level, FORWARD + 500.0: level}
    return expiry_rows(f"{days:g}d", days, strikes)


def test_the_nearest_expiry_answers_where_a_constant_maturity_index_declines() -> None:
    """A tenor no pair of expiries brackets still has a nearest expiry.

    This is the whole reason the mode exists. `atm_iv` blends between the two expiries
    either side of a target and refuses when there is no pair — which on the recorded
    data was most minutes, because the target came from a slider and the term structure
    is whatever Delta happened to list. The implied series came back as three points on a
    chart of 433 and read as a broken instrument rather than as a refusal.

    Reading the nearest expiry outright has no target and therefore nothing to bracket,
    so it answers at every minute that has one solvable expiry at all.
    """
    rows = _expiry_rows(days=11.0, level=0.40) + _expiry_rows(days=41.0, level=0.44)

    # No pair brackets eight days: every listed expiry is further out than that.
    assert atm_iv(rows, tenor_days=8.0) is None

    nearest = atm_iv_nearest(rows)
    assert nearest is not None
    assert nearest.iv == pytest.approx(0.40)
    assert nearest.tenor_days == pytest.approx(11.0)


def test_the_nearest_expiry_skips_the_dying_front_contract() -> None:
    """The seven-day floor still applies, and it is not a detail.

    Vega collapses as time to expiry goes to zero, so a one-tick price change moves the
    solved volatility by tens of points — the smile screen measures a median of 62.5% and
    a maximum of **400.5%** on a front expiry 4.4 hours out, against a median near 40%
    everywhere else on the board. "Nearest expiry" taken literally would make the implied
    line a plot of that instability, rolling to a fresh dying contract every day or two,
    and the reader would be looking at expiry mechanics rather than at what the market
    expects.
    """
    rows = (
        _expiry_rows(days=0.2, level=3.90)
        + _expiry_rows(days=11.0, level=0.40)
        + _expiry_rows(days=41.0, level=0.44)
    )

    nearest = atm_iv_nearest(rows)

    assert nearest is not None
    assert nearest.tenor_days == pytest.approx(11.0)
    assert nearest.excluded_under_floor == 1


def test_nothing_past_the_floor_is_nothing_rather_than_the_front_contract() -> None:
    """A board holding only a dying contract yields no reading at all.

    Falling back to the excluded expiry would put the 400% figure on the chart precisely
    when there was nothing to check it against.
    """
    assert atm_iv_nearest(_expiry_rows(days=0.2, level=3.90)) is None


def test_one_named_expiry_is_read_at_every_minute() -> None:
    """The series tracks a contract, not whichever expiry happens to be nearest.

    **Reading "the nearest" per minute makes the line a plot of recording gaps.** The
    tenor scales the figure by `sqrt(T/365)`, so a minute whose thin record happened to
    capture only a 21-day expiry prints 8.7% where its neighbour, holding a 7-day one at
    the same 36% annualised, prints 5.2%. Measured on the stored data: a near-vertical
    stroke spanning 70% of the axis inside half an hour, none of which was the market.

    Naming the expiry once and reading it at every minute removes the artefact entirely,
    and is what someone watching a volatility term actually does — they follow a
    contract.
    """
    rows = _expiry_rows(days=11.0, level=0.40) + _expiry_rows(days=41.0, level=0.44)

    assert atm_iv_for_expiry(rows, "11d").iv == pytest.approx(0.40)
    assert atm_iv_for_expiry(rows, "41d").iv == pytest.approx(0.44)


def test_a_minute_missing_the_named_expiry_yields_nothing() -> None:
    """No fallback to a different contract. That is the artefact coming back.

    A minute the chosen expiry is absent from is a hole, and a hole in this series is
    already drawn as a break — the same discipline as a leg with no volatility carrying
    no Greeks.
    """
    rows = _expiry_rows(days=41.0, level=0.44)

    assert atm_iv_for_expiry(rows, "11d") is None
