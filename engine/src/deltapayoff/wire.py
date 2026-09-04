"""Delta's websocket payloads, decoded into the shapes the engine already uses.

Delta abbreviates hard on the wire. There are no field names for quotes, Greeks or
implied vols — there are single letters, and **arrays where position carries the
meaning**. Measured from live frames on 2026-09-03:

    ticker   {sy, sp, ts, d: [{g, i, m, m24hc, ohlc, oi, pb, q, qiv, s, to}]}
    ob_l2    {sy, ts, lts, a: [[price, size], ...], b: [[price, size], ...]}

    q    = [best_ask, ask_size, best_bid, bid_size, impact_mid]
    qiv  = [ask_iv, bid_iv, mark_iv]
    g    = [delta, gamma, rho, theta, vega]
    oi   = [oi_contracts, oi_change_usd_6h]
    ohlc = [open, high, low, close]          -- Delta's rolling 24-HOUR trade candle
    to   = [turnover, turnover_usd]
    sp   = spot          ts = microseconds        m = mark

**`oi[1]` is not open interest in USD**, whatever this module has called it since #3.
Checked against the REST snapshot captured alongside the frames on 2026-09-03, it equals
`oi_change_usd_6h` on **all 136** symbols and REST's `oi_value_usd` on ten; it also goes
negative, which a notional cannot. The ticker channel carries no USD open interest at
all, so `Leg.oi_value_usd` is **`None`** on this path — absent rather than derived — and
the number travels as `oi_change_usd_6h`, which is what it is. `tests/test_wire.py` pins
both the finding and the agreement between the two transports, so neither can regress.

The abbreviation is not gratuitous. At about 500 bytes a message and 320 messages a
second, writing `"best_bid"` instead of position 2 would roughly double the byte rate for
no information. The cost is that **a transposed index produces numbers that are entirely
plausible and entirely wrong, with nothing crashing** — which is why `tests/test_wire.py`
tests the array orders against invariants a swap would break rather than against a
restatement of the constants below.

Two fields Delta sends that #3 does not document: `pb`, the price band, and `to`,
turnover. Neither is decoded.

**A frame carries no strike and no expiry.** Both live only in the symbol, so
`chain.build_chain` cannot be reused directly — hence `chain_from_frames`, which rebuilds
the ticker dicts the REST path expects and then hands off to the same pivot. One pivot,
two transports.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple

from .chain import build_chain
from .convert import to_int, to_number, to_quote_number
from .models import ChainResponse, Leg

#: Positions in `q`. Named here once so the wire format appears in exactly one place.
_ASK, _ASK_SIZE, _BID, _BID_SIZE = 0, 1, 2, 3

#: Positions in `qiv` and `g`.
_ASK_IV, _BID_IV, _MARK_IV = 0, 1, 2
_DELTA, _GAMMA, _RHO, _THETA, _VEGA = 0, 1, 2, 3, 4

#: Positions in `oi`. The second is a **six-hour change**, not a notional — see the
#: module docstring. The constant is named for what the number is, not for the `Leg`
#: field it currently lands in.
_OI_CONTRACTS, _OI_CHANGE_USD_6H = 0, 1

#: Positions in `ohlc`, Delta's rolling **24-hour** trade candle. Only the close is ever
#: read: it is the last traded price and worth keeping, while the high, low and open are
#: a 24-hour window that would be re-stored identically 1,440 times a day.
_OHLC_OPEN, _OHLC_HIGH, _OHLC_LOW, _OHLC_CLOSE = 0, 1, 2, 3

#: Positions in `to`. The two are equal on every captured symbol — this chain is quoted
#: in USD, so `turnover` and `turnover_usd` are the same number — and only the first is
#: read, because storing a number twice invites the two copies to disagree.
_TURNOVER, _TURNOVER_USD = 0, 1


def _at(values: list[Any] | None, index: int) -> Any:
    if not values or index >= len(values):
        return None
    return values[index]


def decode_ticker(frame: dict[str, Any]) -> tuple[str, Leg]:
    """One `ticker` frame to `(symbol, Leg)`.

    The payload nests one contract inside `d`, a list. Delta has only ever sent a single
    entry there in every frame captured, and the first is taken.
    """
    symbol = frame.get("sy") or ""
    body = (frame.get("d") or [{}])[0]
    quotes = body.get("q") or []
    ivs = body.get("qiv") or []
    greeks = body.get("g") or []
    interest = body.get("oi") or []

    return symbol, Leg(
        symbol=symbol or body.get("s", ""),
        product_id=to_int(body.get("i")),
        # `"0"` and `""` mean nobody is quoting, which is absent rather than zero — the
        # same rule the REST path applies, and the reason `to_quote_number` exists.
        bid=to_quote_number(_at(quotes, _BID)),
        ask=to_quote_number(_at(quotes, _ASK)),
        bid_iv=to_quote_number(_at(ivs, _BID_IV)),
        ask_iv=to_quote_number(_at(ivs, _ASK_IV)),
        mark_iv=to_quote_number(_at(ivs, _MARK_IV)),
        mark=to_number(body.get("m")),
        delta=to_number(_at(greeks, _DELTA)),
        gamma=to_number(_at(greeks, _GAMMA)),
        rho=to_number(_at(greeks, _RHO)),
        theta=to_number(_at(greeks, _THETA)),
        vega=to_number(_at(greeks, _VEGA)),
        oi=to_number(_at(interest, _OI_CONTRACTS)),
        # The websocket carries no USD notional. Absent, never a stand-in: deriving
        # one from contracts x contract size x spot is a calculation, and this field
        # reports an observation. See the module docstring.
        oi_value_usd=None,
        oi_change_usd_6h=to_number(_at(interest, _OI_CHANGE_USD_6H)),
    )


class TickerExtras(NamedTuple):
    """The three ticker fields `Leg` has no place for, named rather than positional.

    Returned as a `NamedTuple` and not a bare triple on purpose: this module exists
    because position-as-meaning is how Delta's payloads go wrong, and handing the caller
    three anonymous floats would rebuild the same hazard one layer up.
    """

    last_traded_price: float | None
    turnover: float | None
    spot: float | None


def decode_ticker_extras(frame: dict[str, Any]) -> TickerExtras:
    """One `ticker` frame to the fields the reference and spot bars need.

    **Only `ohlc`'s close is taken.** That array is a rolling 24-hour trade candle, so
    its close is the last traded price — a real observation that moves — while its open,
    high and low are a 24-hour window that would be re-stored identically 1,440 times a
    day. Delta's own field names in the REST snapshot captured beside these frames name
    all four, and `tests/test_wire.py` checks the ordering against them rather than
    against the constants above.

    **A contract that never traded sends `ohlc: [null, null, null, null]`** and
    `to: [null, null]` — sixteen of the 136 captured symbols. Absent stays absent: a
    zero here would read as "it last traded at zero", a price nobody paid.

    **`sp` is a property of the underlying, not of the contract.** Measured, all 136
    frames captured inside a 0.06 s window carried an identical `sp` of 77651.9, which
    is why spot gets a table of its own at per-underlying grain rather than a column on
    588 contract rows.
    """
    body = (frame.get("d") or [{}])[0]
    candle = body.get("ohlc") or []
    turnover = body.get("to") or []
    return TickerExtras(
        last_traded_price=to_number(_at(candle, _OHLC_CLOSE)),
        turnover=to_number(_at(turnover, _TURNOVER)),
        spot=to_number(frame.get("sp")),
    )


def decode_ob_l2(frame: dict[str, Any]) -> tuple[str, float | None, float | None]:
    """One `ob_l2` frame to `(symbol, best_bid, best_ask)`.

    The book arrives sorted — `a` cheapest ask first, `b` highest bid first — so the best
    quote is row zero of each. **Only row zero is used.** The other fourteen levels are
    what a large order would pay, which is a liquidity question and not a pricing one;
    every implied vol in this project inverts the top-of-book midpoint.
    """
    asks, bids = frame.get("a") or [], frame.get("b") or []
    best_bid = to_quote_number(bids[0][0]) if bids and bids[0] else None
    best_ask = to_quote_number(asks[0][0]) if asks and asks[0] else None
    return frame.get("sy") or "", best_bid, best_ask


def _as_rest_ticker(symbol: str, leg: Leg, spot: float | None) -> dict[str, Any]:
    """A decoded leg, shaped as the REST ticker dict `build_chain` already understands.

    Round-tripping back through the REST shape rather than duplicating the pivot means
    the strike parsing, the call/put fold and the ATM lookup have exactly one
    implementation, and a websocket chain cannot drift from a REST one.
    """
    parts = symbol.split("-")
    return {
        "symbol": symbol,
        "product_id": leg.product_id,
        "contract_type": "call_options" if parts[0] == "C" else "put_options",
        "strike_price": parts[2] if len(parts) > 2 else None,
        "spot_price": spot,
        "mark_price": leg.mark,
        "quotes": {
            "best_bid": leg.bid,
            "best_ask": leg.ask,
            "bid_iv": leg.bid_iv,
            "ask_iv": leg.ask_iv,
            "mark_iv": leg.mark_iv,
        },
        "greeks": {
            "delta": leg.delta,
            "gamma": leg.gamma,
            "rho": leg.rho,
            "theta": leg.theta,
            "vega": leg.vega,
        },
        # `oi_contracts`, because that is the key `chain.build_leg` reads. Both
        # transports report open interest in contracts; REST's own `oi` is the
        # notional in BTC and is not what this is.
        "oi_contracts": leg.oi,
        "oi_value_usd": leg.oi_value_usd,
        "oi_change_usd_6h": leg.oi_change_usd_6h,
    }


def chain_from_frames(
    underlying: str,
    expiry: str,
    ticker_frames: dict[str, dict[str, Any]],
    book_frames: dict[str, dict[str, Any]] | None = None,
    fetched_at: datetime | None = None,
) -> ChainResponse:
    """A `ChainResponse` from websocket frames — the same type the REST path returns.

    `book_frames` **overrides** the ticker's quotes where present, and that override is
    the whole reason to subscribe both channels. Measured by `tools/measure_feed.py` on a
    live 136-symbol chain: `ticker` republishes every **5001 ms** while `ob_l2` moves
    every **508 ms**, and they carry the same top-of-book numbers. Taking the book's copy
    makes every price here **9.8x fresher**, and every implied volatility with it.

    `ticker` is still needed, but for nothing in the calculation — it carries spot, open
    interest, and Delta's own Greeks and implied vols, which travel as reference columns
    and are never consumed as inputs (`tests/test_no_delta_inputs.py`).
    """
    spot: float | None = None
    tickers = []
    for symbol, frame in ticker_frames.items():
        _, leg = decode_ticker(frame)
        if spot is None:
            spot = to_number(frame.get("sp"))

        book = (book_frames or {}).get(symbol)
        if book is not None:
            _, best_bid, best_ask = decode_ob_l2(book)
            if best_bid is not None or best_ask is not None:
                leg = leg.model_copy(update={"bid": best_bid, "ask": best_ask})

        tickers.append(_as_rest_ticker(symbol, leg, spot))

    return build_chain(
        underlying,
        expiry,
        tickers,
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )
