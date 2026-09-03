"""The four forwards, against planted chains and one captured one. No network.

The synthetic chains here are built so that `C - P = D (F - K)` holds *exactly* at
every strike, for a `D` and `F` chosen by hand. That makes the expected answer a
literal rather than something the code under test computes for itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from deltapayoff.chain import build_chain
from deltapayoff.forward import (
    compare_forwards,
    f1_parity_fit,
    f2_single_strike,
    f3_carry,
    f4_spot,
    sweep_widths,
    year_fraction,
)
from deltapayoff.models import ChainResponse, ChainRow, Leg

# Planted by hand. `D` is a plausible 30-day discount, `F` sits above spot.
PLANTED_DISCOUNT = 0.9946
PLANTED_FORWARD = 78_400.0
PLANTED_SPOT = 78_000.0


def planted_chain(
    strikes: list[float],
    forward: float = PLANTED_FORWARD,
    discount: float = PLANTED_DISCOUNT,
) -> ChainResponse:
    """A chain obeying parity exactly, with prices carried on bid and ask.

    The call price is arbitrary — parity constrains only the difference — so it is
    set to a fixed 1500 and the put is whatever parity then requires.
    """
    rows = []
    for strike in strikes:
        call_price = 1500.0
        put_price = call_price - discount * (forward - strike)
        rows.append(
            ChainRow(
                strike=strike,
                call=Leg(
                    symbol=f"C-BTC-{int(strike)}-041026", bid=call_price, ask=call_price
                ),
                put=Leg(
                    symbol=f"P-BTC-{int(strike)}-041026", bid=put_price, ask=put_price
                ),
            )
        )
    return ChainResponse(
        underlying="BTC",
        expiry="04-10-2026",
        spot=PLANTED_SPOT,
        atm_strike=78_000.0,
        fetched_at="2026-09-04T12:00:00Z",
        rows=rows,
    )


def test_parity_fit_recovers_a_planted_forward_and_discount() -> None:
    chain = planted_chain([77_000, 77_500, 78_000, 78_500, 79_000, 79_500, 80_000])

    result = f1_parity_fit(chain)

    # Tolerance is floating-point noise only. A forward wrong by even one cent, or a
    # discount wrong in the ninth decimal, still fails here.
    assert result.forward == pytest.approx(PLANTED_FORWARD, rel=1e-9)
    assert result.discount == pytest.approx(PLANTED_DISCOUNT, rel=1e-9)


def test_one_sided_strikes_are_excluded_from_the_fit() -> None:
    """Parity needs a pair. A call with no put quotes nothing about the forward.

    The unpaired calls below carry the same arbitrary 1500 as every other call. Let one
    into the fit as `C - P` with a missing put read as zero and it lands 1500 above the
    parity line, dragging both the slope and the crossing with it.
    """
    chain = planted_chain([77_500, 78_000, 78_500, 79_000, 79_500])
    for strike in (60_000.0, 95_000.0):
        chain.rows.append(
            ChainRow(
                strike=strike,
                call=Leg(symbol=f"C-BTC-{int(strike)}-041026", bid=1500.0, ask=1500.0),
                put=None,
            )
        )

    result = f1_parity_fit(chain)

    assert result.forward == pytest.approx(PLANTED_FORWARD, rel=1e-9)
    assert result.discount == pytest.approx(PLANTED_DISCOUNT, rel=1e-9)


# --- time to expiry -------------------------------------------------------------


def test_year_fraction_counts_from_the_snapshot_to_noon_utc_on_expiry_day() -> None:
    """Delta settles options at 12:00 UTC.

    **Measured**: `/v2/products/P-BTC-90000-040926` gives `settlement_time`
    `2026-09-04T12:00:00Z`, and the 31 July expiry gives `2026-07-31T12:00:00Z`.

    Snapshot at noon on 4 Sep, expiry noon on 4 Oct, is 30 whole days. ACT/365.
    """
    chain = planted_chain([78_000])
    chain.fetched_at = "2026-09-04T12:00:00Z"
    chain.expiry = "04-10-2026"

    assert year_fraction(chain) == pytest.approx(30 / 365, rel=1e-12)


# --- the gate -------------------------------------------------------------------


def test_a_clean_fit_is_trusted_and_reports_its_implied_rate() -> None:
    """Seven paired strikes, exact parity, D = 0.9946 over 30 days.

    r = -ln(0.9946) / (30/365) = 6.5878%, computed by hand — comfortably inside the
    0 < r < 30% band, so the verdict is `trusted`.
    """
    chain = planted_chain([77_000, 77_500, 78_000, 78_500, 79_000, 79_500, 80_000])

    result = f1_parity_fit(chain)

    assert result.trusted is True
    assert result.implied_rate == pytest.approx(0.06587803120156924, rel=1e-9)


def test_fewer_than_five_paired_strikes_is_not_trusted() -> None:
    """Four pairs is too thin a line to read a slope off, however clean it looks."""
    chain = planted_chain([78_000, 78_500, 79_000, 79_500])

    result = f1_parity_fit(chain)

    assert result.trusted is False


def test_a_discount_above_one_is_not_trusted() -> None:
    """D > 1 means being paid to wait. r = -ln(1.0035)/(30/365) = -4.25%, so the gate
    rejects it — this is the failure `payoff-project` saw on 25 of its 376 minutes."""
    chain = planted_chain(
        [77_000, 77_500, 78_000, 78_500, 79_000, 79_500, 80_000], discount=1.0035
    )

    result = f1_parity_fit(chain)

    assert result.implied_rate == pytest.approx(-0.04250898592677937, rel=1e-9)
    assert result.trusted is False


def test_an_implied_rate_above_thirty_percent_is_not_trusted() -> None:
    """D = exp(-0.35 * 30/365) = 0.97164 implies 35%, outside the band."""
    chain = planted_chain(
        [77_000, 77_500, 78_000, 78_500, 79_000, 79_500, 80_000],
        discount=0.9716427110819134,
    )

    result = f1_parity_fit(chain)

    assert result.implied_rate == pytest.approx(0.35, rel=1e-9)
    assert result.trusted is False


# --- the ATM +/- x window -------------------------------------------------------


def test_width_restricts_the_fit_to_x_strikes_either_side_of_the_money() -> None:
    """21 strikes listed, ATM at 78000, width 3 -> the 7 strikes 76500..79500.

    The window exists because the wings are where quotes go stale and spreads blow out,
    and the slope — hence the discount — is exactly what wide wings corrupt.
    """
    strikes = [76_000 + 500 * i for i in range(21)]
    chain = planted_chain(strikes)
    assert chain.atm_strike == 78_000.0

    result = f1_parity_fit(chain, width=3)

    assert result.n_pairs == 7
    assert result.strike_range == (76_500.0, 79_500.0)


# --- the three methods that assume something ------------------------------------


def test_single_strike_parity_inverts_the_money_strike() -> None:
    """F2 = K* + (C(K*) - P(K*)) / D, with D assumed from r = 6.5%.

    Hand-derived on the planted chain: K* = 78000, C - P = 0.9946 * 400 = 397.84,
    D = exp(-0.065 * 30/365) = 0.9946717798365894, so F2 = 78399.97113426236.

    Note it misses the planted 78400 by 2.9 cents. That miss *is* the assumed rate
    being slightly wrong, and it is the whole difference between F1 and F2.
    """
    chain = planted_chain([77_000, 77_500, 78_000, 78_500, 79_000])

    result = f2_single_strike(chain)

    assert result.forward == pytest.approx(78_399.97113426236, rel=1e-12)
    assert result.discount == pytest.approx(0.9946717798365894, rel=1e-12)


def test_carry_forward_grows_spot_at_the_assumed_rate() -> None:
    """F3 = S * exp(r T) = 78000 * exp(0.065 * 30/365) = 78417.82744938669.

    This one never looks at a single option price. It is the number to beat.
    """
    chain = planted_chain([77_000, 77_500, 78_000, 78_500, 79_000])

    result = f3_carry(chain)

    assert result.forward == pytest.approx(78_417.82744938669, rel=1e-12)


def test_spot_forward_is_spot_and_asserts_zero_basis() -> None:
    """F4 = S. Wrong by construction whenever carry is not zero, and included so that
    #4 can measure how wrong. On crypto that may be less wrong than it is on NIFTY."""
    chain = planted_chain([77_000, 77_500, 78_000, 78_500, 79_000])

    result = f4_spot(chain)

    assert result.forward == PLANTED_SPOT
    assert result.discount == 1.0


# --- the sweep ------------------------------------------------------------------


def test_sweep_returns_one_result_per_width() -> None:
    """The ticket sweeps x in {3, 5, 7, 9}. Each answer carries the width that made it,
    because they are only comparable if you can tell them apart."""
    # 21 strikes centred on the 78000 money strike, so no window clips an edge.
    chain = planted_chain([78_000 + 500 * i for i in range(-10, 11)])

    results = sweep_widths(chain)

    assert [r.width for r in results] == [3, 5, 7, 9, None]
    assert [r.n_pairs for r in results] == [7, 11, 15, 19, 21]


def test_a_window_clips_rather_than_reaching_past_the_end_of_the_chain() -> None:
    """The money strike is not always centred. Here it is second from the bottom, so
    ATM+/-5 can only reach 77500..80000 — seven strikes, not eleven. Clipping keeps the
    fit honest; reflecting or padding would invent strikes that are not quoted."""
    chain = planted_chain([77_500, 78_000, 78_500, 79_000, 79_500, 80_000])

    result = f1_parity_fit(chain, width=5)

    assert result.n_pairs == 6
    assert result.strike_range == (77_500.0, 80_000.0)


# --- against the captured chain -------------------------------------------------


CAPTURE_TAKEN = datetime(2026, 8, 31, 16, 49, 17, tzinfo=timezone.utc)


def captured_chain(chain_tickers) -> ChainResponse:
    """The 04-09-2026 BTC chain as captured, stamped with the snapshot's own clock.

    The `time` field on every ticker in that capture reads 2026-08-31T16:49:17Z, so
    that is the moment the chain describes. Letting `build_chain` default to `now()`
    would make T drift with the calendar and the test rot.
    """
    return build_chain("BTC", "04-09-2026", chain_tickers, fetched_at=CAPTURE_TAKEN)


def test_f1_and_f2_agree_within_a_few_dollars_on_the_captured_chain(
    chain_tickers,
) -> None:
    """Two different methods, one fitted and one inverted at a single strike, landing
    on the same number. Neither is graded against the other anywhere else, so this is
    the closest thing to independent corroboration the chain can offer."""
    chain = captured_chain(chain_tickers)

    f2 = f2_single_strike(chain)

    for result in sweep_widths(chain):
        assert abs(result.forward - f2.forward) < 5.0


def test_the_forward_is_robust_across_windows_while_the_rate_is_not(
    chain_tickers,
) -> None:
    """The ticket's headline, asserted rather than asserted-to-be-true-in-prose.

    F and D come from different features of the same line: F is where it crosses zero,
    D is its slope. Widening the window barely moves a crossing and completely changes
    a slope. On this capture the forwards span about a dollar and the implied rates
    span twenty-six percentage points.
    """
    results = sweep_widths(captured_chain(chain_tickers))
    forwards = [r.forward for r in results]
    rates = [r.implied_rate for r in results]

    assert max(forwards) - min(forwards) < 2.0
    assert max(rates) - min(rates) > 0.20


def test_the_gate_rejects_the_narrow_windows_on_the_captured_chain(chain_tickers) -> None:
    """Three of the four windows imply a negative rate — being paid to wait — and the
    gate catches all three. Without it the caller would receive `D = 1.0018` with no
    indication that anything was wrong, because OLS reports no error."""
    results = {r.width: r for r in sweep_widths(captured_chain(chain_tickers))}

    assert [w for w, r in results.items() if not r.trusted] == [3, 5, 7]
    assert all(results[w].implied_rate < 0 for w in (3, 5, 7))


# --- end to end -----------------------------------------------------------------


def test_compare_forwards_answers_every_method_with_a_timing(chain_tickers) -> None:
    """The ticket's end-to-end: a chain snapshot in, a forward, a discount, a verdict
    and a timing out, for each of the seven answers (F1 at four widths, then F2-F4)."""
    results = compare_forwards(captured_chain(chain_tickers), runs=5)

    assert [r.method for r in results] == ["F1"] * 5 + ["F2", "F3", "F4"]
    assert [r.width for r in results] == [3, 5, 7, 9, None, None, None, None]
    for result in results:
        assert result.forward is not None
        assert result.discount is not None
        assert result.timing is not None
        assert result.timing.runs == 5


def test_comparison_survives_a_chain_with_no_two_sided_strikes(
    absent_quote_tickers,
) -> None:
    """The absent-quote fixture carries Delta's `"0"`, `""` and `null` spellings, so no
    strike quotes both sides. F1 and F2 have nothing to fit; they must say so rather
    than divide by zero, and F4 must still answer from spot."""
    chain = build_chain(
        "BTC", "04-09-2026", absent_quote_tickers, fetched_at=CAPTURE_TAKEN
    )

    results = {(r.method, r.width): r for r in compare_forwards(chain, runs=2)}

    assert results[("F1", 3)].forward is None
    assert results[("F1", 3)].trusted is False
    assert results[("F2", None)].forward is None
    assert results[("F4", None)].forward == chain.spot


def test_the_sweep_includes_an_unwindowed_fit_over_every_paired_strike(
    chain_tickers,
) -> None:
    """The widest fit is the honest one on this data, so it belongs in the sweep.

    Every narrow window on the captured chain implies a rate the gate rejects. Over all
    63 paired strikes — a 38.7% span rather than 1.3% — the fit passes, and the rate it
    recovers agrees with the basis computed independently from spot.
    """
    results = sweep_widths(captured_chain(chain_tickers))

    unwindowed = results[-1]
    assert unwindowed.width is None
    assert unwindowed.n_pairs == 63
    assert unwindowed.trusted is True
    assert unwindowed.implied_rate == pytest.approx(0.0282, abs=5e-4)
