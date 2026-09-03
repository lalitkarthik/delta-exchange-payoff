"""Delta's websocket payloads, decoded into the shapes the engine already uses.

Delta abbreviates hard on the wire. There are no field names for quotes, Greeks or
implied vols — there are single letters, and **arrays where position carries the
meaning**. Measured from live frames on 2026-09-03:

    ticker   {sy, sp, ts, d: [{g, i, m, m24hc, ohlc, oi, pb, q, qiv, s, to}]}
    ob_l2    {sy, ts, lts, a: [[price, size], ...], b: [[price, size], ...]}

    q    = [best_ask, ask_size, best_bid, bid_size, impact_mid]
    qiv  = [ask_iv, bid_iv, mark_iv]
    g    = [delta, gamma, rho, theta, vega]
    oi   = [oi_contracts, oi_value_usd]
    sp   = spot          ts = microseconds        m = mark

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
from typing import Any

from .chain import build_chain
from .convert import to_int, to_number, to_quote_number
from .models import ChainResponse, Leg

#: Positions in `q`. Named here once so the wire format appears in exactly one place.
_ASK, _ASK_SIZE, _BID, _BID_SIZE = 0, 1, 2, 3

#: Positions in `qiv` and `g`.
_ASK_IV, _BID_IV, _MARK_IV = 0, 1, 2
_DELTA, _GAMMA, _RHO, _THETA, _VEGA = 0, 1, 2, 3, 4

#: Positions in `oi`.
_OI_CONTRACTS, _OI_VALUE_USD = 0, 1


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
        oi_value_usd=to_number(_at(interest, _OI_VALUE_USD)),
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
        "oi": leg.oi,
        "oi_value_usd": leg.oi_value_usd,
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
    the whole reason to subscribe both channels. Measured by `tools/probe_ws.py`:
    `ticker` republishes about every 6 s while `ob_l2` moves about every 940 ms, and they
    carry the same top-of-book numbers. Taking the book's copy makes every price here
    roughly six times fresher, and every implied volatility with it.

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
