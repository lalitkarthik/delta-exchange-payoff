"""The pivot. Pure functions over a decoded Delta `result` list — no network here.

Delta has no option-chain endpoint. A chain is `/v2/tickers` filtered by underlying and
expiry, with calls and puts sharing a strike folded onto one row. That fold is the only
thing this engine computes, and `atm_strike` is a lookup over the listed strikes rather
than a model.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .convert import to_int, to_number, to_quote_number
from .models import ChainResponse, ChainRow, ExpiriesResponse, Leg

UNDERLYINGS = ("BTC", "ETH")

EXPIRY_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
EXPIRY_FORMAT = "%d-%m-%Y"

#: `C-BTC-77000-040926` — the trailing group is the expiry as DDMMYY.
_SYMBOL_EXPIRY_RE = re.compile(r"-(\d{6})$")


class ValidationError(ValueError):
    """A caller-supplied parameter is not one this engine accepts. Maps to 400."""


def normalise_underlying(underlying: str) -> str:
    """`BTC` or `ETH`, case-insensitively. Anything else is a 400."""
    candidate = (underlying or "").strip().upper()
    if candidate not in UNDERLYINGS:
        raise ValidationError(
            f"underlying must be one of {', '.join(UNDERLYINGS)}; got {underlying!r}"
        )
    return candidate


def validate_expiry(expiry: str) -> str:
    """`DD-MM-YYYY`, the format Delta's `expiry_date` filter expects. No reformatting."""
    candidate = (expiry or "").strip()
    if not EXPIRY_RE.match(candidate):
        raise ValidationError(f"expiry must be DD-MM-YYYY; got {expiry!r}")
    try:
        datetime.strptime(candidate, EXPIRY_FORMAT)
    except ValueError as exc:
        raise ValidationError(f"expiry is not a real date: {expiry!r}") from exc
    return candidate


def expiry_from_symbol(symbol: str) -> str | None:
    """Recover `DD-MM-YYYY` from a Delta option symbol's `DDMMYY` suffix.

    A ticker carries no `expiry_date` field of its own — the symbol is the only place
    the expiry appears — so `/expiries` is built by parsing it back out.
    """
    match = _SYMBOL_EXPIRY_RE.search(symbol or "")
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%d%m%y")
    except ValueError:
        return None
    return parsed.strftime(EXPIRY_FORMAT)


def build_expiries(underlying: str, tickers: list[dict[str, Any]]) -> ExpiriesResponse:
    """Every expiry listed for this underlying, ascending by date."""
    dated: dict[str, datetime] = {}
    for ticker in tickers:
        expiry = expiry_from_symbol(ticker.get("symbol", ""))
        if expiry is not None:
            dated[expiry] = datetime.strptime(expiry, EXPIRY_FORMAT)
    ordered = sorted(dated, key=lambda text: dated[text])
    return ExpiriesResponse(underlying=underlying, expiries=ordered)


def build_leg(ticker: dict[str, Any]) -> Leg:
    """One ticker becomes one leg, with every decimal converted exactly once.

    `greeks.spot` is deliberately not read here — see :func:`spot_from_tickers`.
    """
    quotes = ticker.get("quotes") or {}
    greeks = ticker.get("greeks") or {}
    return Leg(
        symbol=ticker.get("symbol", ""),
        product_id=to_int(ticker.get("product_id")),
        # Quote fields: `"0"` and `""` mean nobody is quoting, which is null, not zero.
        bid=to_quote_number(quotes.get("best_bid")),
        ask=to_quote_number(quotes.get("best_ask")),
        bid_iv=to_quote_number(quotes.get("bid_iv")),
        ask_iv=to_quote_number(quotes.get("ask_iv")),
        mark_iv=to_quote_number(quotes.get("mark_iv")),
        # Delta's own numbers, passed through. Zero is a real value for these.
        mark=to_number(ticker.get("mark_price")),
        delta=to_number(greeks.get("delta")),
        gamma=to_number(greeks.get("gamma")),
        theta=to_number(greeks.get("theta")),
        vega=to_number(greeks.get("vega")),
        rho=to_number(greeks.get("rho")),
        oi=to_number(ticker.get("oi")),
        oi_value_usd=to_number(ticker.get("oi_value_usd")),
        tick_size=to_number(ticker.get("tick_size")),
    )


def spot_from_tickers(tickers: list[dict[str, Any]]) -> float | None:
    """Delta's top-level `spot_price`, never `greeks.spot`.

    The two disagree — 77568.2 against 77558.2 in the fixture captured for these tests —
    and this project uses `spot_price` everywhere.
    """
    for ticker in tickers:
        spot = to_number(ticker.get("spot_price"))
        if spot is not None:
            return spot
    return None


def nearest_strike(strikes: list[float], spot: float | None) -> float | None:
    """The listed strike closest to spot. A lookup, not a model. Ties go to the lower."""
    if spot is None or not strikes:
        return None
    return min(sorted(strikes), key=lambda strike: abs(strike - spot))


def build_chain(
    underlying: str,
    expiry: str,
    tickers: list[dict[str, Any]],
    fetched_at: datetime | None = None,
) -> ChainResponse:
    """Pivot a list of tickers into the ladder, ascending by strike."""
    legs: dict[float, dict[str, Leg]] = {}
    for ticker in tickers:
        strike = to_number(ticker.get("strike_price"))
        if strike is None:
            continue
        contract_type = ticker.get("contract_type")
        if contract_type == "call_options":
            side = "call"
        elif contract_type == "put_options":
            side = "put"
        else:
            continue
        legs.setdefault(strike, {})[side] = build_leg(ticker)

    rows = [
        ChainRow(strike=strike, call=sides.get("call"), put=sides.get("put"))
        for strike, sides in sorted(legs.items())
    ]
    spot = spot_from_tickers(tickers)
    stamp = fetched_at or datetime.now(timezone.utc)
    return ChainResponse(
        underlying=underlying,
        expiry=expiry,
        spot=spot,
        atm_strike=nearest_strike(list(legs), spot),
        fetched_at=stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        rows=rows,
    )
