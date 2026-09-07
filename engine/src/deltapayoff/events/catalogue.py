"""The nine events. Eight outbound from a producer to the bus, one inbound.

**This module is `docs/design/events.md` in code, field for field.** That document is the
authority on the names and the directions; `tests/test_events.py` parses its section
headings and asserts this registry matches, so neither can move without the other.

Field spellings are **the browser contract's**, `docs/chain-contract.md` and `models.Leg`
— `mark`, `oi`, `oi_value_usd`, `oi_change_usd_6h`, `tick_size`,
`bid_iv`/`ask_iv`/`mark_iv`, `iv`/`iv_leg`/`iv_reason` — so that a producer wires to the
browser without renaming anything. **The store spells several of the same quantities
differently** and is not followed here: `oi_contracts`, `ltp_*` and `venue_*` in
`store.py`'s `REFERENCE_SCHEMA`. That translation is `bars.samples_from_reference`, one
place and not four, and `docs/design/lld/store.md` tables it column by column.

**`null` is not `0`, and that is the adapter's boundary to hold, not this module's.**
Every price and size here is nullable so the distinction *can* be carried: an absent quote
is `null` even where the venue spells absence `"0"`, and a real zero in open interest or a
greek stays `0`. These types do not enforce it — a string `"0"` handed to a `float` field
is still coerced to `0.0` by pydantic, so #36's adapter is where `"0"` must become `None`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from .envelope import Event, register
from .instrument import strike_as_json_number


class ConnectionState(str, Enum):
    """The five states of `hld.md` §3. A connection is in exactly one of them."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


class BarTable(str, Enum):
    """Which of the store's four tables a sealed bar belongs to.

    A discriminator on one event rather than four event types, because the envelope,
    the timing and the seal rule are identical across them and only the columns differ.
    """

    QUOTE = "quote"
    REFERENCE = "reference"
    SPOT = "spot"
    COMPUTED = "computed"


@register
class OptionQuote(Event):
    """`md.option_quote` — top of book, from the venue's book channel.

    The mid is computed per tick by the consumer and **never derived from separately
    aggregated bid and ask**, which is why no mid travels here.
    """

    type: Literal["md.option_quote"] = "md.option_quote"
    bid: float | None = None
    bid_size: float | None = None
    ask: float | None = None
    ask_size: float | None = None
    #: The venue's own last-trade stamp on the book frame, as the venue gave it. **Carried
    #: and never bucketed on**: `ts_venue` alone decides which minute a tick belongs to,
    #: and this travels to the quote bars' `last_lts` column deciding nothing. Added by
    #: #37 with `schema_version` left at 1, which `docs/design/events.md` calls a
    #: compatible change.
    lts: datetime | None = None


@register
class OptionReference(Event):
    """`md.option_reference` — the venue's own view of a contract.

    **Reference columns, never inputs.** Nothing computes on them;
    `tests/test_no_delta_inputs.py` pins that and must keep passing with this event in
    place. `oi` is contracts and `oi_value_usd` is a notional: different quantities, and
    this event carries only the first.
    """

    type: Literal["md.option_reference"] = "md.option_reference"
    #: The venue's numeric id for this contract. Not an opinion and not a price — it is
    #: carried because it reaches the browser as `models.Leg.product_id`.
    product_id: int | None = None
    mark: float | None = None
    last_price: float | None = None
    #: Traded value over the venue's rolling window, for the reference bars' `turnover`
    #: column. `null` for a contract that has never traded, never `0`.
    turnover: float | None = None
    #: **The venue's own top of book, as its slower channel reports it.** The same
    #: quantity `md.option_quote` carries, observed on the channel that carries
    #: everything else here — `measured` 5,001 ms against the book's 508 ms. It is the
    #: **fallback quote** for a contract whose book stays silent for a whole minute, and
    #: a consumer holding both takes the book's **wholesale** rather than merging them.
    #:
    #: This is a quote and not a reference column: `test_no_delta_inputs.py`'s rule is
    #: about the venue's *opinions* — its IV, its greeks, its mark — and a bid is an
    #: observed price, which is what every implied volatility in this project inverts.
    bid: float | None = None
    ask: float | None = None
    #: Open interest in **contracts**.
    oi: float | None = None
    #: Open interest as a **USD notional**. Absent rather than derived on the live path.
    oi_value_usd: float | None = None
    #: How the USD notional moved over six hours. Can be negative, which is what proves
    #: it is not the notional above.
    oi_change_usd_6h: float | None = None
    tick_size: float | None = None
    #: The venue's own implied volatilities — decimal fractions, never percentages.
    bid_iv: float | None = None
    ask_iv: float | None = None
    mark_iv: float | None = None
    #: The venue's own greeks. Ours are on `computed.chain`, under the same bare names,
    #: and the two never meet in one record.
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None


@register
class IndexQuote(Event):
    """`md.index_quote` — the underlying's spot, off the same frames as the reference.

    `instrument` is `null` and the payload names the underlying instead: spot is a
    property of BTC, not of the contract whose frame happened to carry it. Storing the
    messenger would invite a reader to join on it.
    """

    type: Literal["md.index_quote"] = "md.index_quote"
    instrument: None = None
    underlying: str
    spot: float | None = None


@register
class OptionBar(Event):
    """`md.option_bar` — a sealed minute bar, emitted by the bar writer at seal.

    **A minute with no arrivals produces no event**, as it produces no row. Nothing is
    forward-filled and no bar is invented.

    `columns` carries that table's own columns as the store already shapes them. The four
    schemas are **not** re-declared here: `store.py`'s `SCHEMA`, `REFERENCE_SCHEMA`,
    `SPOT_SCHEMA` and `COMPUTED_SCHEMA` are their authority, and a second copy is exactly
    the drift this catalogue exists to prevent. Values must be JSON scalars for the event
    to survive a wire round trip, which is the producer's side of the bargain.
    """

    type: Literal["md.option_bar"] = "md.option_bar"
    table: BarTable
    #: The minute the bar covers, at its open.
    minute: datetime
    columns: dict[str, Any]


class ChainStrike(BaseModel):
    """Our numbers for one strike of one expiry.

    `iv` is a property of the **strike** and not of a leg — put-call parity gives both
    sides one volatility — so `iv_leg` names the side it was solved on. **`iv` is `null`
    and never `0`**, and the greeks are null with it, because greeks at some default
    volatility would be five plausible numbers describing nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strike: Decimal
    #: **`null` and never `0`.** A zero here would be a solved volatility of zero, which
    #: is not a thing; unsolved is `None` with `iv_reason` saying why. Enforced rather
    #: than asserted, because this is the catalogue's most repeated sentence.
    iv: float | None = None
    #: `"call"` or `"put"` — the out-of-the-money side this strike's `iv` came from.
    iv_leg: str | None = None
    #: Empty when solved. Otherwise the solver's own account of why it stopped.
    iv_reason: str = ""
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    rho: float | None = None

    @field_validator("iv")
    @classmethod
    def _iv_is_null_and_never_zero(cls, value: float | None) -> float | None:
        if value == 0:
            raise ValueError("iv is null and never 0; an unsolved strike carries None")
        return value

    @field_serializer("strike", when_used="json")
    def _serialise_strike(self, strike: Decimal) -> int | float:
        return strike_as_json_number(strike)


@register
class ComputedChain(Event):
    """`computed.chain` — our IV and greeks for one expiry, from the recompute pass.

    `instrument` is `null`: the event is about an expiry, not a contract. `strikes` is a
    tuple so the event stays genuinely immutable rather than frozen around a mutable list.
    """

    type: Literal["computed.chain"] = "computed.chain"
    instrument: None = None
    underlying: str
    expiry: date
    forward: float | None = None
    discount: float | None = None
    #: How the forward was arrived at — fitted, or a borrowed constant saying so.
    forward_method: str
    years_to_expiry: float
    #: The whole model stamp, as `compute.MODEL_VERSION` writes it into table C.
    model_version: str
    #: The solver inside that stamp, named on its own so a consumer need not split it.
    solver: str
    strikes: tuple[ChainStrike, ...] = ()


@register
class FeedConnection(Event):
    """`feed.connection` — one state transition, and nothing else changes state.

    `instrument` is `null`. `from_state` is `null` on the first transition into
    `connecting`, which is the `—` row of `hld.md` §3's transition table.
    """

    type: Literal["feed.connection"] = "feed.connection"
    instrument: None = None
    adapter: str
    from_state: ConnectionState | None = None
    to_state: ConnectionState
    reason: str = ""


@register
class Heartbeat(Event):
    """`heartbeat` — the controller is running, whatever the state.

    **Not a message from the venue.** It carries the age of the last real message so
    that a quiet market and a dead socket are distinguishable, which is the failure this
    whole project keeps refusing.
    """

    type: Literal["heartbeat"] = "heartbeat"
    adapter: str
    state: ConnectionState
    #: `null` before the first message ever arrives — an unknown age, not an age of zero.
    last_message_age_seconds: float | None = None


@register
class Alert(Event):
    """`alert` — something a person should see.

    A lossless queue dropping a message should be impossible and is logged at error;
    that is the case this event exists for as much as a spent reconnect budget.
    """

    type: Literal["alert"] = "alert"
    severity: str
    #: A short stable name, greppable across days of logs.
    code: str
    detail: str = ""
    #: `null` for an alert no single adapter owns.
    adapter: str | None = None


@register
class ControlCommand(Event):
    """`control.command` — the one inbound event, from an operator over a route.

    `pause` reaches `stopped` **without spending reconnect budget**; `resume` reaches
    `connecting`; `reconnect` closes the socket and enters `reconnecting`.
    """

    type: Literal["control.command"] = "control.command"
    adapter: str
    command: Literal["pause", "resume", "reconnect"]
