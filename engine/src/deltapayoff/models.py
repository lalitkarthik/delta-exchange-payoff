"""Response shapes. These are the contract in `docs/chain-contract.md`, in code."""

from __future__ import annotations

from pydantic import BaseModel


class ComputedLeg(BaseModel):
    """What **we** recovered for one leg, as opposed to what Delta published.

    Kept in its own object rather than as prefixed fields on `Leg` so that the boundary
    between the venue's numbers and ours is visible in the payload itself. Every field
    Delta sends stays exactly where it was; nothing here replaces anything there.

    `iv` is a property of the **strike**, not of the leg — put-call parity gives both
    sides one volatility, and it is recovered from whichever side is out of the money.
    It is repeated on both legs for the screen's convenience, and `iv_leg` names the
    side it actually came from so that repetition cannot be mistaken for two
    independent solves.

    Absence is `None` and never zero. `iv_reason` says why, because a strike that could
    not be solved is a fact about the chain rather than a gap to be filled.
    """

    iv: float | None = None
    #: `"call"` or `"put"` — the out-of-the-money side this strike's `iv` was solved on.
    iv_leg: str | None = None
    #: Empty when solved. Otherwise the solver's own account of why it stopped.
    iv_reason: str = ""
    #: Conventions are documented in `greeks.py` and are not all textbook: delta and
    #: gamma are undiscounted, vega and rho are discounted and per one percent, and
    #: theta is a one-calendar-day repricing.
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    rho: float | None = None


class Leg(BaseModel):
    """One side of a strike — the call or the put.

    Every field except `computed` is Delta's, passed through untouched. They travel as
    **reference columns**: they are never inputs to anything we calculate, which
    `tests/test_no_delta_inputs.py` enforces.
    """

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
    #: Ours. `None` until the chain has been through `compute.enrich`.
    computed: ComputedLeg | None = None


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
    #: The forward the enrichment priced against, and the discount factor fitted
    #: alongside it. `None` on a chain that has not been enriched, and on one with
    #: nothing to fit. Reported because every `computed` figure below depends on
    #: them, and a volatility whose forward is unknown cannot be checked.
    forward: float | None = None
    discount: float | None = None
    years_to_expiry: float | None = None
    #: Which forward method produced it. `F1` is the parity regression.
    forward_method: str | None = None


class ExpiriesResponse(BaseModel):
    underlying: str
    expiries: list[str]
