"""Response shapes. These are the contracts in `docs/chain-contract.md`,
`docs/smile-contract.md` and `docs/recording-contract.md`, in code. Those files are the
authority and change first."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictBool


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
    #: Open interest in **contracts**, on both transports. REST's own `oi` field is
    #: the notional in BTC and is deliberately not read; `oi_contracts` is.
    oi: float | None = None
    #: Open interest as a **USD notional**. REST publishes it; the `ticker` websocket
    #: channel does not, so it is `None` on the live path. Absent rather than derived:
    #: contracts x contract size x spot is a calculation, and this field reports an
    #: observation.
    oi_value_usd: float | None = None
    #: How the USD notional moved over six hours. Both transports carry it. It can be
    #: negative, which is what proves it is not the notional above - `wire.py` read it
    #: into `oi_value_usd` from T4 until T5 measured the difference.
    oi_change_usd_6h: float | None = None
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


class SmilePoint(BaseModel):
    """One strike's volatility at one minute. `docs/smile-contract.md`.

    **One point per strike, not per leg.** Table C stores a row per contract, so a paired
    strike holds two rows carrying the same number; put-call parity gives the strike one
    volatility and `compute.enrich` writes it to both sides. `iv_leg` names the side it
    was solved on, which is why the pair is not two independent solves.

    **An unsolved strike is a point with a null `iv`, never a missing point.** The screen
    has to tell a strike that was not solved from a strike that does not exist, and the
    only way it can is if the null arrives. `iv_leg` is null exactly when `iv` is.

    No Greeks. They are stored beside these rows; the smile plots volatility, and five
    figures nothing on the screen reads would be five more chances to drift.
    """

    strike: float
    #: Decimal fraction, as everywhere else. `null` when the strike could not be solved.
    iv: float | None = None
    iv_leg: str | None = None
    #: `null` when solved — the store's spelling, not `/chain`'s empty string. A field
    #: holding both spellings for one fact is one every reader has to guess at.
    iv_reason: str | None = None


class SmileMinute(BaseModel):
    """One sealed minute of one expiry: the chain-level numbers, then the curve.

    `forward` and its three companions are per **chain** and are repeated down every
    stored row of the minute; they are lifted to this level rather than copied onto each
    point, because the offset axis and the reference line need one value per curve and a
    per-point copy is a per-point chance to disagree.
    """

    #: ISO 8601 UTC, second precision, `Z`-suffixed. Never a local time.
    minute: str
    forward: float | None = None
    discount: float | None = None
    #: ACT/365. The clock this minute's volatility is quoted on.
    years_to_expiry: float | None = None
    #: `F1`, `F1+assumed-rate` or `F2`. See `docs/chain-contract.md`.
    forward_method: str | None = None
    #: The stamp on this minute's rows. Read from the data, never hardcoded.
    model_version: str | None = None
    #: Ascending by strike.
    points: list[SmilePoint]


class SmileResponse(BaseModel):
    """`GET /smile?underlying=BTC&expiry=04-09-2026`.

    `model_versions` is a **list** because the model can change mid-day and every stored
    row says which one made it. A response spanning two stamps reports both rather than
    silently choosing one — the forward convention alone is worth up to 3.9 vol points,
    and this screen plots nothing but vol points.

    Absence is an empty `minutes`, not a 404. An underlying nobody has collected yet and
    a day nobody has lived through are both "nothing yet", which is the same answer the
    store gives a minute with no bar.
    """

    #: Pydantic reserves the `model_` prefix. The store's column is `model_version` and
    #: the contract carries that name unchanged rather than inventing a synonym that
    #: every reader would have to map back.
    model_config = ConfigDict(protected_namespaces=())

    underlying: str
    expiry: str
    #: Every distinct stamp in this response, ascending. Empty when there are no rows.
    model_versions: list[str]
    #: Ascending by minute.
    minutes: list[SmileMinute]


class RecordingState(BaseModel):
    """`GET /recording`, and the body `POST /recording` answers with.
    `docs/recording-contract.md`.

    One shape for both, so a client that switched the state needs no second request and
    cannot render a state that was never true: the POST answers with what is true
    **after** the change.

    The two counters are sums across the four tables rather than a per-table breakdown.
    They are here so a reader can see that recording is a fact rather than a label —
    `rows_written` climbing is the engine capturing, and `buffered_rows` falling to zero
    the instant recording is switched off is the flush the contract promises, observed.
    """

    #: Whether the writer is aggregating and writing right now. True at start-up.
    recording: bool
    #: Sealed bars held in memory and not yet on disk, across all four tables.
    buffered_rows: int
    #: Rows this process has written to Parquet, across all four tables.
    rows_written: int


class RecordingRequest(BaseModel):
    """The body of `POST /recording`. One field, required, and a **strict** boolean.

    A default would let a malformed body silently stop the day's capture; FastAPI's 422
    says what happened instead.

    `StrictBool` rather than `bool` because Pydantic's lax mode reads `"off"`, `"no"` and
    `"0"` as false. Guessing at a string is the wrong disposition for the one route in
    this engine that changes anything: this is the same rule as `null` is not `0` and the
    engine converting once at the boundary, applied to a request body.
    """

    recording: StrictBool
