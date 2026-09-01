"""Response shapes. These are the contract in `docs/chain-contract.md`, in code."""

from __future__ import annotations

from pydantic import BaseModel


class Leg(BaseModel):
    """One side of a strike — the call or the put."""

    symbol: str
    product_id: int | None = None
    bid: float | None = None
    ask: float | None = None
    mark: float | None = None
    bid_iv: float | None = None
    ask_iv: float | None = None
    mark_iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    oi: float | None = None
    oi_value_usd: float | None = None
    tick_size: float | None = None


class ChainRow(BaseModel):
    """A strike, with whichever of its two legs Delta lists."""

    strike: float
    call: Leg | None = None
    put: Leg | None = None


class ChainResponse(BaseModel):
    underlying: str
    expiry: str
    spot: float | None = None
    atm_strike: float | None = None
    fetched_at: str
    rows: list[ChainRow]


class ExpiriesResponse(BaseModel):
    underlying: str
    expiries: list[str]
