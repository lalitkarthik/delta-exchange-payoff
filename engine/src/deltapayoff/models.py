"""Response shapes. These are the contracts in `docs/chain-contract.md`,
`docs/smile-contract.md` and `docs/recording-contract.md`, in code. Those files are the
authority and change first."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, StrictBool

from .events import ConnectionState


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
    #: Open interest as a **USD notional**. REST publishes it; `md.option_reference`
    #: does not, so it is `None` on the live path. Absent rather than derived:
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
    #: Delta's top-level `spot_price`, always present on the live path. Nullable because
    #: `HistoricalChain` below is a `ChainResponse` too, and a stored minute with no
    #: `spot-bars` row legitimately has neither this nor `atm_strike`.
    #: `docs/chain-contract.md`, #47.
    spot: float | None = None
    atm_strike: float | None = None
    fetched_at: str
    #: ISO 4217, upper case — `"USD"`. What every price in `rows` is quoted in, #60
    #: (I1). At the response level and not per leg: one chain is one underlying on
    #: one venue, so every row on it shares one quote currency, and a field repeated
    #: on every leg would be the same fact copied dozens of times for no reason. Not
    #: nullable — unlike `spot`, a chain's currency is a fact about the contract, not
    #: an observation that can be missing, so even a stored minute with nothing in
    #: `spot-bars` still knows what it would have been quoted in.
    quote_currency: str
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


class HistoricalMinutes(BaseModel):
    """`GET /chain/minutes` — `docs/historical-chain-contract.md`.

    Every minute the store holds quotes for, on one underlying, expiry and date. The
    slider's domain and, by what is missing from it, its gaps.
    """

    underlying: str
    expiry: str
    #: `YYYY-MM-DD` — the store's own partition spelling, not Delta's `DD-MM-YYYY`.
    date: str
    #: Ascending. ISO 8601 UTC, second precision, `Z`-suffixed — `smile.MINUTE_FORMAT`'s
    #: spelling, so a stamp round-trips into the ladder route with no reformatting.
    minutes: list[str]


class HistoricalChain(ChainResponse):
    """`GET /chain/at` — `docs/historical-chain-contract.md`.

    Every field `/chain` and `/ws/chain` carry, unchanged, plus `minute`: the ladder
    component renders either shape without knowing which one it was handed, and the
    header reads `minute` to say which one it is showing.
    """

    #: ISO 8601 UTC, second precision, `Z`-suffixed — the exact minute this ladder was
    #: rebuilt for, and the same stamp `fetched_at` carries here (see `historical.py`).
    minute: str


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
    #: Age of the cached split-mode store state; `null` for the local writer.
    state_age_seconds: float | None = None


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


class AdapterHealth(BaseModel):
    """One adapter's line of `GET /health`. **Facts, never a verdict.**

    Whether a state is acceptable is the reader's judgement — #40 draws a badge from it,
    #42 alerts on it, an operator reads it — and a field called `healthy` here would fix
    one of those readings for all three. Every field is either a fact the controller
    holds or `null` because it is genuinely unknown; none is a default standing in for
    one.
    """

    #: The venue's short name, `DELTA`. The same string every `Instrument` from this
    #: adapter carries, so a log line and this report agree without a lookup.
    adapter: str
    #: One of the five. `stopped` for a controller that was never started, because a
    #: connection nobody started is not running and `null` would be a sixth state.
    state: ConnectionState
    #: Why it is in that state — the `reason` of its last transition, one of the short
    #: stable names in `controller.py`. `null` before a controller has ever moved.
    #: **Added by #41**, because `stopped` alone cannot tell an operator's own `pause`
    #: from a spent reconnect budget, and those are the two ends of the range: one is
    #: what somebody just asked for, the other the loudest failure this engine has.
    reason: str | None = None
    #: When the last message arrived, wall clock, or `null` if none ever has. **Derived
    #: from the age at request time**, because the controller measures on a monotonic
    #: clock — the only kind a staleness bound can be measured on.
    last_message_at: datetime | None = None
    #: Seconds since that message, or `null`. An unknown age, not an age of zero.
    last_message_age_seconds: float | None = None
    #: Times this connection has **dropped** since the process started — the count the
    #: controller spends its budget against, not the number of entries into
    #: `reconnecting`, which the staleness watchdog can reach without a drop.
    reconnects: int | None = None
    #: Reconnects left before the connection gives up for good. Restored in full by any
    #: message arriving, so a healthy feed sits at the configured budget.
    budget_remaining: int | None = None
    #: Every state change since the process started, including the ones that came back.
    #: A connection flapping between `connected` and `degraded` shows up here and in no
    #: other field of this report.
    transitions: int | None = None
    #: Sockets that opened with **nothing subscribed** — a connection guaranteed to
    #: deliver nothing, which is the failure with no error. `null` when the adapter has
    #: no socket owner to ask, because an absent count is not a count of zero.
    empty_opens: int | None = None
    #: Frames that parsed as JSON and then made no sense to the decoder. `delta.py` logs
    #: the first and counts the rest, and its comment says why this belongs here: a
    #: systematic decode bug zeroes the event stream while the message counter keeps
    #: climbing, which is a silent failure with a counter nobody reads. `null` when the
    #: adapter does not decode anything of its own.
    undecodable: int | None = None
    #: When the API observed the event that currently supplies `state`, or `null` before
    #: the first state event reaches a remote consumer.
    state_learned_at: datetime | None = None
    #: Seconds since `state_learned_at`, measured on a monotonic clock.
    state_age_seconds: float | None = None
    #: When the API observed the newest `feed.connection`, or `null` if none arrived.
    last_connection_at: datetime | None = None
    #: Seconds since `last_connection_at`, measured on a monotonic clock.
    last_connection_age_seconds: float | None = None
    #: When the API observed the newest `heartbeat`, or `null` if none arrived.
    last_heartbeat_at: datetime | None = None
    #: Seconds since `last_heartbeat_at`, measured on a monotonic clock.
    last_heartbeat_age_seconds: float | None = None


class WatchedPair(BaseModel):
    """One `(underlying, expiry)` the live solve is running for — #44.

    **What the engine is solving right now, and why.** The 100 ms pass no longer covers
    the venue's listing; it covers this list. A pair is here because a browser is on it
    (`viewers` above zero) or because one just left and the grace has not elapsed
    (`viewers` zero and `grace_remaining_seconds` set). Exactly one of the two is true,
    which is why the grace field is `null` rather than `0` while anyone is watching: no
    countdown is running, and `0` would read as one that had just finished.

    An expiry absent from this list is **not** unsolved. It is solved once a minute by
    the pass that fills the store, which runs whether or not any browser exists.
    """

    underlying: str
    expiry: str
    #: How many open `/ws/chain` connections are on this pair. Zero only in grace.
    viewers: int
    #: Seconds of grace left after the last viewer left, or `null` if it is being
    #: watched. Rounded to the millisecond — the field is for a person reading a
    #: report, and a full float of seconds is noise.
    grace_remaining_seconds: float | None = None


class BarBufferReport(BaseModel):
    """The split api's in-memory sealed-bar buffer, as reported by `/health`."""

    bars: int
    per_table: dict[str, int]
    minutes: int
    oldest_minute: str | None
    newest_minute: str | None
    skipped: int
    malformed: int
    evicted: int
    horizon_seconds: float


class HealthReport(BaseModel):
    """`GET /health`. **Liveness and readiness, side by side and not confused.**

    `status` is what this route has always answered and means the same thing it always
    did: the process is up and served you. It is kept because things read it — two tests
    in this repo, and any script or probe outside it — and because a route that removes a
    field to add three is a breaking change dressed as an improvement.

    `feed` is the question people were actually asking when they read `status`: is market
    data flowing? It is the **worst** state among the adapters, by the order in
    `supervisor.SEVERITY`, so a process with one venue connected and one stopped does not
    report green about the half of itself that works.

    `adapters` is a **list and not a map keyed by name**, so that a later ticket adding a
    field — #44's watched set — extends a record rather than changing what a key means,
    and so the order is the configured one rather than whatever a dictionary gives back.
    """

    #: Liveness. Always `"ok"`: a route that answered is a process that is alive.
    status: str = "ok"
    #: Readiness. The worst state among the adapters; `stopped` when there are none.
    feed: ConnectionState
    adapters: list[AdapterHealth] = []
    #: The pairs the live solve is running for — #44. Empty when nothing is watched,
    #: which is the ordinary state of an engine recording with no browser open, and is
    #: **not** a fault: the minute pass still covers every listed expiry. Empty by
    #: default so a process with no chain cache still answers the same shape.
    watched: list[WatchedPair] = []
    #: The split api's sealed-bar buffer, or `null` when this process has no one.
    bar_buffer: BarBufferReport | None = None
