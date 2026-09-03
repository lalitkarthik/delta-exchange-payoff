"""Decoding Delta's websocket payloads. No network — captured frames only.

Delta abbreviates everything on the wire to save bandwidth. There are no field names for
the quotes, the Greeks or the implied vols; there are single letters and **arrays where
position is meaning**:

    q   = [best_ask, ask_size, best_bid, bid_size, impact_mid]
    qiv = [ask_iv, bid_iv, mark_iv]
    g   = [delta, gamma, rho, theta, vega]

Read position 2 as the bid instead of position 0 and every number downstream stays
entirely plausible and is wrong. Nothing crashes. So the tests here are built to catch a
transposed index specifically, using invariants a swap would break:

* a quote is never crossed — the ask is never below the bid
* the top of the `ob_l2` book **is** the `ticker` quote, on the same connection
* a call's delta is positive and a put's is negative; gamma and vega are positive and
  theta is negative, and all five differ in magnitude by orders

Fixtures captured 2026-09-03 by `tools/capture_ws.py`: one frame per symbol on each
channel for the 04-09-2026 BTC chain, 136 symbols, plus the REST snapshot taken
alongside so the same contracts can be read two ways.
"""

from __future__ import annotations

import pytest

from deltapayoff.wire import (
    chain_from_frames,
    decode_ob_l2,
    decode_ticker,
    decode_ticker_extras,
)

EXPIRY = "04-09-2026"


# --- the quote array -------------------------------------------------------------


def test_no_decoded_quote_is_crossed(ws_ticker_frames) -> None:
    """The ask is never below the bid. True on all 136 captured symbols.

    This is the test that catches `q` being read in the wrong order: swap positions 0
    and 2 and every two-sided quote in the file inverts at once.
    """
    crossed = []
    for frame in ws_ticker_frames.values():
        _, leg = decode_ticker(frame)
        if leg.bid is not None and leg.ask is not None and leg.ask < leg.bid:
            crossed.append(leg.symbol)

    assert crossed == []


def test_quote_sizes_are_not_read_as_prices(ws_ticker_frames) -> None:
    """`q` interleaves prices and sizes — [ask, ask_size, bid, bid_size]. The sizes run
    to five figures where the deep-wing prices are 0.1, so reading one as the other is
    both easy and immediately absurd. No option on this chain is worth 5,000."""
    for frame in ws_ticker_frames.values():
        _, leg = decode_ticker(frame)
        if leg.ask is not None:
            assert leg.ask < 25_000.0, leg.symbol


def test_the_top_of_the_book_is_the_ticker_quote(
    ws_ticker_frames, ws_book_frames
) -> None:
    """Two channels, one connection, the same numbers. The strongest ordering check here.

    `ob_l2` sends the ladder and `ticker` sends a summary of it, so `a[0]` must be the
    ask in `q[0]` and `b[0]` the bid in `q[2]`. They are not captured at the same instant
    — `ticker` publishes about every 6 s and `ob_l2` about every 940 ms — so a tolerance
    is needed for genuine movement. **Measured**: 42 of 136 agree exactly and 121 agree
    within 2%. Under a transposed index, none would.
    """
    close = 0
    compared = 0
    for symbol, frame in ws_ticker_frames.items():
        _, leg = decode_ticker(frame)
        book = ws_book_frames.get(symbol)
        if book is None or leg.bid is None or leg.ask is None:
            continue
        _, book_bid, book_ask = decode_ob_l2(book)
        if book_bid is None or book_ask is None:
            continue
        compared += 1
        if (
            abs(leg.ask - book_ask) / book_ask < 0.02
            and abs(leg.bid - book_bid) / max(book_bid, 1e-9) < 0.02
        ):
            close += 1

    assert compared == 136
    assert close >= 100, f"only {close}/{compared} agreed; the array order is suspect"


# --- the greeks array ------------------------------------------------------------


def test_greeks_have_the_signs_their_side_requires(ws_ticker_frames) -> None:
    """`g = [delta, gamma, rho, theta, vega]`, and each has a sign it cannot violate.

    A call's delta is between 0 and 1, a put's between -1 and 0. Gamma and vega are
    positive for both. Theta is negative for both. Any rotation of this array breaks
    several of those at once — the captured values are delta 0.55, gamma 0.0003,
    rho 1.23, theta -234, vega 16.6, which are four different orders of magnitude.

    These are **reference columns**. `tests/test_no_delta_inputs.py` asserts they never
    reach a calculation; they are decoded so our own Greeks can be compared against them.
    """
    for frame in ws_ticker_frames.values():
        symbol, leg = decode_ticker(frame)
        if leg.delta is None:
            continue
        if symbol.startswith("C-"):
            assert 0.0 <= leg.delta <= 1.0, symbol
        else:
            assert -1.0 <= leg.delta <= 0.0, symbol
        assert leg.gamma >= 0.0, symbol
        assert leg.vega >= 0.0, symbol
        assert leg.theta <= 0.0, symbol


# --- what the symbol carries -----------------------------------------------------


def test_the_strike_and_side_are_parsed_from_the_symbol(ws_ticker_frames) -> None:
    """A websocket frame has no `strike_price` and no `contract_type` field. Both live
    only in the symbol, so a chain built from frames depends entirely on parsing it."""
    symbol, leg = decode_ticker(ws_ticker_frames["C-BTC-77500-040926"])

    assert symbol == "C-BTC-77500-040926"
    assert leg.symbol == "C-BTC-77500-040926"


# --- a whole chain ---------------------------------------------------------------


def test_a_chain_built_from_frames_has_the_same_shape_as_a_rest_chain(
    ws_ticker_frames, ws_book_frames, rest_snapshot
) -> None:
    """Same strikes, same pairing, same row count. The websocket and REST paths must
    produce the identical `ChainResponse` shape or nothing downstream is transferable."""
    from datetime import datetime, timezone

    from deltapayoff.chain import build_chain

    stamp = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    over_rest = build_chain("BTC", EXPIRY, rest_snapshot, fetched_at=stamp)
    over_wire = chain_from_frames(
        "BTC", EXPIRY, ws_ticker_frames, ws_book_frames, fetched_at=stamp
    )

    assert [r.strike for r in over_wire.rows] == [r.strike for r in over_rest.rows]
    # 136 symbols over 69 strikes, so two strikes list only one side.
    assert len(over_wire.rows) == 69
    for wire_row, rest_row in zip(over_wire.rows, over_rest.rows, strict=True):
        assert (wire_row.call is None) == (rest_row.call is None)
        assert (wire_row.put is None) == (rest_row.put is None)


def test_the_spot_comes_from_the_frame_not_from_the_greeks(ws_ticker_frames) -> None:
    """`sp` at the top of the frame, never `greeks.spot`.

    The REST path already refuses `greeks.spot` because the two disagree. The websocket
    frame has no `greeks.spot` at all, so this asserts the value is the one Delta puts
    at the top level rather than something reconstructed.
    """
    chain = chain_from_frames("BTC", EXPIRY, ws_ticker_frames, None)

    assert chain.spot == pytest.approx(77_651.9, abs=200.0)


def test_a_websocket_chain_solves_with_the_untouched_forward_and_solver_code(
    ws_ticker_frames, ws_book_frames
) -> None:
    """The payoff for #2's insistence on a transport-agnostic signature.

    `f1_parity_fit` and `implied_vol_householder` were written against REST snapshots and
    are not modified here. If the decoder produces a real `ChainResponse`, they work as
    they are — that requirement was written into #2 for exactly this moment.
    """
    from deltapayoff.forward import f1_parity_fit
    from deltapayoff.solvers import implied_vol_householder, solve_chain

    chain = chain_from_frames("BTC", EXPIRY, ws_ticker_frames, ws_book_frames)
    forward = f1_parity_fit(chain)
    solved = solve_chain(chain, forward, solver=implied_vol_householder)

    assert forward.forward == pytest.approx(77_650.0, abs=400.0)
    assert forward.trusted is True
    assert sum(1 for r in solved.values() if r.converged) >= 55


# --- the fields `decode_ticker` has no `Leg` field for ---------------------------


def _rest_by_symbol(rest_snapshot) -> dict[str, dict]:
    return {row["symbol"]: row for row in rest_snapshot}


def test_the_24_hour_ohlc_is_read_in_the_order_the_rest_snapshot_names(
    ws_ticker_frames, rest_snapshot
) -> None:
    """`ohlc` is four bare numbers with no names. The REST snapshot captured **at the
    same moment** names all four, so the ordering is checked against an independent
    source rather than against a restatement of the constant in `wire.py`.

    119 of the 120 symbols REST reports a candle for agree element for element, and the
    one that does not traded between the two captures. On 81 of them the close differs
    from the open, the high *and* the low, so a transposed index cannot pass this by
    coincidence.
    """
    rest = _rest_by_symbol(rest_snapshot)
    agreeing = 0
    discriminating = 0

    for symbol, frame in ws_ticker_frames.items():
        row = rest.get(symbol)
        if row is None or "close" not in row:
            continue
        candle = frame["d"][0].get("ohlc")
        if candle != [row["open"], row["high"], row["low"], row["close"]]:
            continue  # it traded between the two captures
        agreeing += 1
        extras = decode_ticker_extras(frame)
        assert extras.last_traded_price == row["close"], symbol
        if row["close"] not in (row["open"], row["high"], row["low"]):
            discriminating += 1

    assert agreeing >= 100, "the fixtures stopped overlapping; the check is vacuous"
    assert discriminating >= 50, "no symbol distinguishes the close from open/high/low"


def test_a_contract_that_never_traded_has_no_last_traded_price(
    ws_ticker_frames,
) -> None:
    """Delta sends `ohlc: [null, null, null, null]` for a contract with no trades, and
    `to: [null, null]` with it. Sixteen of the 136 captured symbols are in that state.

    Absent must stay absent. A zero here would read as "it last traded at zero", which
    is a price nobody paid — the same lie as a forward-filled bar.
    """
    never_traded = [
        symbol
        for symbol, frame in ws_ticker_frames.items()
        if frame["d"][0].get("ohlc") == [None, None, None, None]
    ]

    assert len(never_traded) >= 10, "the fixture no longer covers a never-traded contract"
    for symbol in never_traded:
        extras = decode_ticker_extras(ws_ticker_frames[symbol])
        assert extras.last_traded_price is None, symbol
        assert extras.turnover is None, symbol
        # ...while spot, which belongs to the underlying and not to the contract, is
        # still there. A contract nobody traded is still quoted against a live BTC.
        assert extras.spot is not None and extras.spot > 0.0, symbol


def test_turnover_is_the_first_element_and_the_second_is_its_usd_twin(
    ws_ticker_frames, rest_snapshot
) -> None:
    """`to` is two numbers. REST names them `turnover` and `turnover_usd`, and on this
    chain — quoted in USD — they are equal on all 120 symbols that traded. Only the
    first is decoded; storing the second would store the same number twice."""
    rest = _rest_by_symbol(rest_snapshot)
    checked = 0

    for symbol, frame in ws_ticker_frames.items():
        row = rest.get(symbol)
        if row is None or row.get("turnover") is None:
            continue
        extras = decode_ticker_extras(frame)
        assert extras.turnover == row["turnover"], symbol
        assert frame["d"][0]["to"][1] == row["turnover_usd"], symbol
        checked += 1

    assert checked >= 100


def test_the_open_interest_second_element_is_a_six_hour_change_not_a_usd_notional(
    ws_ticker_frames, rest_snapshot
) -> None:
    """**The mislabel this ticket found.** `wire.decode_ticker` calls `oi[1]`
    `oi_value_usd`. It is not: on **all 136** captured symbols it equals REST's
    `oi_change_usd_6h`, and it is *not* REST's `oi_value_usd` on 126 of them — it even
    goes negative, which a notional cannot.

    The value is left where it is because renaming `Leg.oi_value_usd` changes the chain
    contract the web app reads, which is not this ticket's to change. What this test does
    is stop the mistake being made twice: the reference bars store this number under the
    name it actually has, and store no USD open interest at all, because the ticker
    channel does not carry one.
    """
    rest = _rest_by_symbol(rest_snapshot)
    matches_change = 0
    matches_notional = 0
    negative = 0

    for symbol, frame in ws_ticker_frames.items():
        row = rest.get(symbol)
        interest = frame["d"][0].get("oi") or []
        if row is None or len(interest) < 2:
            continue
        value = float(interest[1])
        assert float(interest[0]) == float(row["oi_contracts"]), symbol
        if value == float(row["oi_change_usd_6h"]):
            matches_change += 1
        if value == float(row["oi_value_usd"]):
            matches_notional += 1
        if value < 0.0:
            negative += 1

    assert matches_change == 136, "oi[1] stopped being the six-hour change"
    assert matches_notional <= 10, "oi[1] is the USD notional after all"
    assert negative > 0, "a USD notional cannot be negative; this one is"
