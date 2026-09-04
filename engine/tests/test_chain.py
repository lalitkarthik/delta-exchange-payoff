"""The pivot, against captured Delta responses. No network anywhere in here."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from deltapayoff.chain import (
    ValidationError,
    build_chain,
    build_expiries,
    expiry_from_symbol,
    nearest_strike,
    normalise_underlying,
    spot_from_tickers,
    validate_expiry,
)

EXPIRY = "04-09-2026"

# Measured from tests/fixtures/tickers-btc-04-09-2026.json, captured 2026-09-01.
FIXTURE_SPOT = 77568.2
FIXTURE_STRIKE_COUNT = 65
FIXTURE_ATM = 77600.0


def chain(tickers: list[dict[str, Any]]):
    return build_chain("BTC", EXPIRY, tickers)


# --- the pivot ------------------------------------------------------------------


def test_calls_and_puts_sharing_a_strike_land_on_one_row(chain_tickers) -> None:
    result = chain(chain_tickers)
    assert len(chain_tickers) == 128
    assert len(result.rows) == FIXTURE_STRIKE_COUNT

    row = next(r for r in result.rows if r.strike == 77500.0)
    assert row.call is not None and row.put is not None
    assert row.call.symbol == "C-BTC-77500-040926"
    assert row.put.symbol == "P-BTC-77500-040926"


def test_rows_are_ascending_by_strike(chain_tickers) -> None:
    strikes = [row.strike for row in chain(chain_tickers).rows]
    assert strikes == sorted(strikes)
    assert strikes[0] == 58000.0
    assert strikes[-1] == 90000.0


def test_a_strike_listed_on_one_side_only_gets_a_null_leg(chain_tickers) -> None:
    """Delta lists puts at 89000 and 90000 with no matching call."""
    result = chain(chain_tickers)
    for strike in (89000.0, 90000.0):
        row = next(r for r in result.rows if r.strike == strike)
        assert row.call is None
        assert row.put is not None


def test_a_row_carries_every_contract_field(chain_tickers) -> None:
    row = next(r for r in chain(chain_tickers).rows if r.strike == 77500.0)
    call = row.call
    assert call is not None
    assert call.product_id == 148238
    assert call.bid == 926.0
    assert call.ask == 943.0
    assert call.mark == 940.22653943
    assert call.bid_iv == 0.33126258
    assert call.ask_iv == 0.33754417
    assert call.mark_iv == 0.33451656
    assert call.delta == 0.51597771
    assert call.gamma == 0.00017558
    assert call.theta == -161.78721439
    assert call.vega == 27.06304078
    assert call.rho == 2.99484353
    # `oi` is **contracts**, from REST's `oi_contracts`. It used to read REST's `oi`,
    # which is the notional in BTC - 41.134 BTC against 41,134 contracts, a factor of
    # 1,000 that is exactly `contract_value`. The websocket has always sent contracts,
    # so the two transports disagreed by that factor on the field the ladder renders.
    assert call.oi == 41134.0
    assert call.oi_value_usd == 3190258.4318
    assert call.oi_change_usd_6h == 538728.51
    assert call.tick_size == 0.1


def test_fetched_at_is_utc_with_a_trailing_z(chain_tickers) -> None:
    stamp = datetime(2026, 9, 1, 9, 21, 4, tzinfo=timezone.utc)
    result = build_chain("BTC", EXPIRY, chain_tickers, fetched_at=stamp)
    assert result.fetched_at == "2026-09-01T09:21:04Z"


# --- string to number -----------------------------------------------------------


def test_no_decimal_leaves_the_engine_as_a_string(chain_tickers) -> None:
    payload = chain(chain_tickers).model_dump()
    numeric = {
        "strike", "bid", "ask", "mark", "bid_iv", "ask_iv", "mark_iv",
        "delta", "gamma", "theta", "vega", "rho", "oi", "oi_value_usd",
        "oi_change_usd_6h", "tick_size",
    }
    for key in ("spot", "atm_strike"):
        assert isinstance(payload[key], float), key
    for row in payload["rows"]:
        assert isinstance(row["strike"], float)
        for leg in (row["call"], row["put"]):
            if leg is None:
                continue
            assert isinstance(leg["symbol"], str)
            assert isinstance(leg["product_id"], int)
            assert not isinstance(leg["product_id"], bool)
            for key in numeric & leg.keys():
                assert leg[key] is None or isinstance(leg[key], float), (
                    f"{leg['symbol']}.{key} is {type(leg[key]).__name__}"
                )


def test_iv_stays_a_decimal_fraction(chain_tickers) -> None:
    for row in chain(chain_tickers).rows:
        for leg in (row.call, row.put):
            if leg is None or leg.mark_iv is None:
                continue
            assert 0.0 < leg.mark_iv < 5.0, leg.symbol


# --- absent is null, and null is not zero ---------------------------------------


def test_zero_and_empty_quotes_become_null(absent_quote_tickers) -> None:
    row = next(r for r in chain(absent_quote_tickers).rows if r.strike == 59000.0)
    call = row.call
    assert call is not None
    assert call.bid is None, "best_bid \"0\" means nobody is bidding"
    assert call.bid_iv is None, "bid_iv \"0\" is an absent vol, not a zero vol"
    assert call.ask is None, "best_ask \"\" is absent"
    assert call.ask_iv is None, "ask_iv null is absent"
    assert call.mark is not None, "mark_price is still a real number"


def test_zero_open_interest_stays_zero_not_null(absent_quote_tickers) -> None:
    """`oi` of zero means no contracts are open. That is a value, not an absence."""
    row = next(r for r in chain(absent_quote_tickers).rows if r.strike == 59000.0)
    assert row.call is not None
    assert row.call.oi == 0.0
    assert row.call.oi_value_usd == 0.0


def test_a_zero_greek_stays_zero(absent_quote_tickers) -> None:
    """Greeks are Delta's numbers, passed through. A gamma of 0 is a gamma of 0."""
    row = next(r for r in chain(absent_quote_tickers).rows if r.strike == 89000.0)
    assert row.put is not None
    assert row.put.gamma == 0.0
    assert row.put.mark_iv is None, "a mark_iv of exactly 0 is an absent vol"


def test_impact_mid_price_is_not_in_the_contract(chain_tickers) -> None:
    leg = chain(chain_tickers).rows[0].put
    assert leg is not None
    assert "impact_mid_price" not in leg.model_dump()


# --- spot and the ATM strike ----------------------------------------------------


def test_spot_is_the_top_level_spot_price_not_greeks_spot(chain_tickers) -> None:
    result = chain(chain_tickers)
    assert result.spot == FIXTURE_SPOT

    # In this capture every ticker agrees on spot_price, while greeks.spot takes 15
    # different values across the 128 contracts. They are not the same measurement.
    assert {float(t["spot_price"]) for t in chain_tickers} == {FIXTURE_SPOT}
    greek_spots = {float(t["greeks"]["spot"]) for t in chain_tickers}
    assert len(greek_spots) > 1
    assert greek_spots - {FIXTURE_SPOT}, "greeks.spot disagrees with spot_price"


def test_greeks_spot_is_never_exposed(chain_tickers) -> None:
    payload = chain(chain_tickers).model_dump()
    for row in payload["rows"]:
        for leg in (row["call"], row["put"]):
            if leg is not None:
                assert "spot" not in leg


def test_atm_strike_is_the_listed_strike_closest_to_spot(chain_tickers) -> None:
    result = chain(chain_tickers)
    assert result.atm_strike == FIXTURE_ATM
    listed = {row.strike for row in result.rows}
    assert result.atm_strike in listed
    assert abs(FIXTURE_ATM - FIXTURE_SPOT) < abs(77500.0 - FIXTURE_SPOT)


@pytest.mark.parametrize(
    ("spot", "expected"),
    [
        (77568.2, 77600.0),
        (77500.0, 77500.0),
        (0.0, 58000.0),
        (10_000_000.0, 90000.0),
        (77550.0, 77500.0),  # a tie between 77500 and 77600 resolves to the lower
        (None, None),
    ],
)
def test_nearest_strike(
    chain_tickers, spot: float | None, expected: float | None
) -> None:
    strikes = [row.strike for row in chain(chain_tickers).rows]
    assert nearest_strike(strikes, spot) == expected


def test_atm_is_null_when_spot_is_missing() -> None:
    tickers = [
        {
            "symbol": "C-BTC-77000-040926",
            "contract_type": "call_options",
            "strike_price": "77000",
            "spot_price": "",
        }
    ]
    result = chain(tickers)
    assert result.spot is None
    assert result.atm_strike is None


def test_spot_from_tickers_skips_unparseable_rows() -> None:
    assert spot_from_tickers([{"spot_price": None}, {"spot_price": "77568.2"}]) == 77568.2
    assert spot_from_tickers([]) is None


# --- expiries -------------------------------------------------------------------


def test_expiries_are_ascending_by_date_not_by_string(all_expiry_tickers) -> None:
    result = build_expiries("BTC", all_expiry_tickers)
    assert result.underlying == "BTC"
    assert result.expiries == [
        "02-09-2026",
        "03-09-2026",
        "04-09-2026",
        "11-09-2026",
        "18-09-2026",
        "25-09-2026",
        "30-10-2026",
        "27-11-2026",
    ]
    # Sorted as text, 30-10-2026 would sort after 27-11-2026. It does not.
    assert result.expiries != sorted(result.expiries)


def test_expiries_are_deduplicated_across_calls_and_puts(all_expiry_tickers) -> None:
    result = build_expiries("BTC", all_expiry_tickers)
    assert len(all_expiry_tickers) == 2 * len(result.expiries)
    assert len(set(result.expiries)) == len(result.expiries)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("C-BTC-77000-040926", "04-09-2026"),
        ("P-ETH-3000-271126", "27-11-2026"),
        ("MARK:C-BTC-77000-040926", "04-09-2026"),
        ("BTCUSD", None),
        ("C-BTC-77000-999999", None),
        ("", None),
    ],
)
def test_expiry_from_symbol(symbol: str, expected: str | None) -> None:
    assert expiry_from_symbol(symbol) == expected


# --- parameter validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"), [("BTC", "BTC"), ("eth", "ETH"), (" btc ", "BTC")]
)
def test_normalise_underlying(raw: str, expected: str) -> None:
    assert normalise_underlying(raw) == expected


@pytest.mark.parametrize("raw", ["SOL", "", "BTCUSD", "BT C"])
def test_normalise_underlying_rejects(raw: str) -> None:
    with pytest.raises(ValidationError):
        normalise_underlying(raw)


def test_validate_expiry_accepts_delta_format() -> None:
    assert validate_expiry("04-09-2026") == "04-09-2026"


@pytest.mark.parametrize(
    "raw",
    ["2026-09-04", "4-9-2026", "04/09/2026", "32-09-2026", "04-13-2026", "", "040926"],
)
def test_validate_expiry_rejects(raw: str) -> None:
    with pytest.raises(ValidationError):
        validate_expiry(raw)
