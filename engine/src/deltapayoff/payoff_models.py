"""`docs/payoff-contract.md`, in code. That file is the authority and changes first.

Types and nothing else — no maths, no I/O, no lookups. The pure payoff core builds
these directly rather than returning private dataclasses something later maps, so this
module must stay importable from a module that touches no socket, no clock and no file:
it depends on Pydantic and on nothing else in this package.

`models.py` holds the ladder's shapes. This is a second contract with a second document
behind it, so it gets a second module for the reason `/smile`, `/bars` and the two
historical routes each got a document of their own rather than an appendix to `/chain`'s.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Finite = Annotated[float, Field(allow_inf_nan=False)]
"""A real number and nothing else.

`nan` and the infinities are not JSON. Python's encoder writes them as the bare tokens
`NaN`, `Infinity` and `-Infinity`, which `JSON.parse` refuses — so one reaching the wire
fails the chart *after* the engine has already answered 200. Enforcing it on the type
means it cannot get there however the layers above are rewritten.

An **unbounded** maximum is `None`, which is a different thing entirely: see `Unbounded`.
And so is an **absent observation** — `spot` on a stored minute with no `spot-bars` row is
spelled `Finite | None` rather than `Unbounded`, because nothing about it is unlimited.
"""

Unbounded = Finite | None
"""A number that may have no finite value at all. `None` serialises as JSON `null` and
reads on screen as "Unlimited". Never an infinity, never a large sentinel, never a
string — `docs/payoff-contract.md`."""

Direction = Literal[1, -1]
"""Bought or sold. Separate from `Quantity`, never one signed quantity: the two spellings
produce the same curve today and disagree the first time anything sums or renders a
quantity."""

Quantity = Annotated[int, Field(ge=1)]
"""How many contracts of a leg. At least one; the sign lives in `Direction`."""


class _Contract(BaseModel):
    """Every shape on this contract, with one rule shared.

    **A field the contract does not name is refused, not ignored.** Inbound, a client's
    typo must not be quietly discarded into a default. Outbound matters just as much
    here: the pure payoff core builds these objects directly rather than returning
    private records something later maps, so a mistyped keyword there would drop a whole
    section of the response and still answer 200 — `entry_premium` is the sibling
    project's spelling of `entry_price`, and is exactly the typo to expect.
    """

    model_config = ConfigDict(extra="forbid")


def _refuse_unless_ascending(values: list[float], what: str) -> None:
    """Strictly increasing, or not built at all.

    Every ordered sequence on this contract is read left to right off a chart or down a
    column, and an out-of-order one does not raise there — it draws, slightly wrong, and
    nobody notices. Equal neighbours are refused with the reversed ones: two points at
    one price are a vertical segment, and two breakevens at one price are one breakeven
    counted twice.
    """
    if any(later <= earlier for earlier, later in zip(values, values[1:], strict=False)):
        raise ValueError(f"{what} must ascend strictly; got {values}")


class LegRequest(_Contract):
    """A leg as a client is allowed to describe it.

    `instrument` is **not** validated here. `events.instrument.Instrument.from_canonical`
    is the single validator, and it names which of the six parts was wrong — a field
    validator would bury that inside FastAPI's `422` envelope, where
    `docs/payoff-contract.md` promises a `400` carrying the part. So the string travels
    verbatim and the route parses it.
    """

    instrument: str
    direction: Direction
    quantity: Quantity = 1
    entry_price: Finite | None = None


class AnalyseRequest(_Contract):
    """Analyse one strategy, as of one moment.

    There is no strategy type: a strategy is an ordered list of legs and nothing more.
    The order is the order the trader built them in, and it survives into the response
    because the per-leg Greeks table is read alongside the legs on screen.

    The moment is asked for once here rather than per leg: the ladder, the entry prices
    and the volatilities all come from the same one.
    """

    #: Ordered, and never empty — an empty strategy is a refusal rather than an
    #: empty chart, and the rule lives on the type so no route can forget it.
    legs: list[LegRequest] = Field(min_length=1)
    #: ISO 8601 UTC, second precision, `Z`-suffixed — `/chain/minutes`'s own spelling.
    #: `None` means **live**: price against the chain cache as it stands.
    as_of: str | None = None


class PayoffPoint(_Contract):
    """One `(price, pnl)` pair: what the strategy is worth if the underlying finishes
    there.

    `price` is the underlying's price **at expiry**, USD per one unit — the axis the
    curve is drawn on. `pnl` is the strategy's profit or loss there, same units.

    One type for both the curve's corners and the payoff table, deliberately: it is one
    quantity sampled twice rather than two quantities, and a second type would invite
    the two to drift.
    """

    price: Finite
    pnl: Finite


class Window(_Contract):
    """The range the engine suggests opening the chart on.

    A **suggestion, not a clamp**: the corners and the two end slopes describe the curve
    everywhere, so a reader may zoom past this in either direction and still be reading
    exact values.
    """

    low: Finite
    high: Finite

    @model_validator(mode="after")
    def _opens_left_to_right(self) -> Window:
        """A window with its ends swapped, or with no width at all, gives an axis that
        runs backwards or divides by zero."""
        if self.low >= self.high:
            raise ValueError(
                f"window low must be below high; got {self.low}, {self.high}"
            )
        return self


class Curve(_Contract):
    """The chart's line: P&L at expiry, as its **corner points** rather than samples.

    A single-expiry payoff is piecewise linear — straight everywhere, kinked only at a
    strike — so the kinks plus the two end slopes are exact, smaller on the wire, and
    zoomable without limit. Drawing a straight segment between two given points is
    rendering rather than arithmetic, which is what keeps the web app's rule that it
    computes nothing intact.
    """

    corners: list[PayoffPoint] = Field(min_length=1)
    #: P&L per unit of underlying **outside** the first and the last corner, so the two
    #: rays are exact however far out the reader zooms. Zero on both for a capped
    #: structure.
    slope_left: Finite
    slope_right: Finite
    window: Window

    @model_validator(mode="after")
    def _strictly_ascending(self) -> Curve:
        """Corners out of order fold the line back on itself and two corners at one
        price draw a vertical segment. Neither raises on a chart — both just draw,
        slightly wrong, and nobody notices — so neither can be built."""
        _refuse_unless_ascending([corner.price for corner in self.corners], "corners")
        return self


class Metrics(_Contract):
    """The four numbers under the chart, and the ratio between two of them.

    Every one is a property of the legs **at expiry**, so a live tab recomputes them
    only because the entry prices it is holding may have changed — the curve itself does
    not move.
    """

    #: The most the strategy can make. `None` when unbounded.
    max_profit: Unbounded
    #: The **worst outcome, as a P&L** on the same axis as `PayoffPoint.pnl`, so it is
    #: read straight off the chart rather than sign-flipped in the reader's head.
    #: Negative on almost everything, and zero or positive on a structure that cannot
    #: lose — which is why it is not constrained to be negative. `None` when unbounded.
    max_loss: Unbounded
    #: Every price at which P&L crosses zero, ascending. Empty when it never does.
    breakevens: list[Finite]
    #: Positive is **paid out** (a debit), negative is **received** (a credit).
    net_premium: Finite
    #: `max_profit` over the magnitude of `max_loss`. `None` when either side is
    #: unbounded or when there is no loss to divide by: a ratio against unlimited has
    #: no meaning, and a large number in its place would read as a good trade.
    reward_risk: Unbounded

    @model_validator(mode="after")
    def _breakevens_ascend(self) -> Metrics:
        """Read left to right off the chart. Out of order they still render, as
        plausible prices in the wrong places."""
        _refuse_unless_ascending(self.breakevens, "breakevens")
        return self


class Greeks(_Contract):
    """One leg's exposures, or a whole strategy's — the shape is the same either way.

    **Per one unit of the underlying**: no `contract_value`, no lot count. Those are
    presentation multipliers applied at the very end, and a Greek carrying one could not
    be compared against the ladder's.

    The conventions are `greeks.py`'s and `docs/chain-contract.md`'s, and they are not
    all textbook: `delta` and `gamma` **undiscounted** and with respect to the
    **forward**; `vega` and `rho` discounted and **per one percent**; `theta` a
    **one-calendar-day** repricing on ACT/365, never a 1/252 trading-day step, which
    overstates it by 1.456x here because crypto trades weekends.
    """

    delta: Finite
    gamma: Finite
    vega: Finite
    theta: Finite
    rho: Finite


class AnalysedLeg(_Contract):
    """One requested leg, echoed with the price it was actually entered at and what it
    is exposed to.

    The first four fields are the request back, so the screen can show what a leg was
    priced at without holding on to what it asked for. `entry_price` is never `None`
    here: a leg with no quote on its side and none supplied refuses the whole analysis
    rather than arriving priceless.

    `delta` and the rest are **signed by `direction` and scaled by `quantity`**, because
    that is what a per-leg exposure means to whoever reads it beside the legs.
    `entry_price` is **not**: it is what one unit cost rather than what the leg cost, so
    it stays comparable with the `bid` and `ask` on the ladder it was taken from.
    """

    instrument: str
    direction: Direction
    quantity: Quantity
    entry_price: Finite
    #: A decimal fraction — `0.3712` is 37.12%, and the engine never multiplies by 100.
    #: A property of the **strike**, not the leg: recovered by inverting the
    #: out-of-the-money leg's midpoint and written to both legs of the pair.
    iv: Finite | None = None
    greeks: Greeks | None = None

    @model_validator(mode="after")
    def _greeks_travel_with_a_volatility(self) -> AnalysedLeg:
        """Present together or absent together. Greeks at some default sigma would be
        five plausible numbers describing nothing; a volatility with no Greeks beside it
        would be a solve nobody used."""
        if (self.iv is None) != (self.greeks is None):
            raise ValueError(
                "iv and greeks are present together or absent together; got "
                f"iv={self.iv!r}, greeks={'set' if self.greeks else None}"
            )
        return self


class AnalyseResponse(_Contract):
    """Everything about one strategy, in one response.

    **Deliberately fat.** Splitting it would mean several round trips carrying the same
    legs and recomputing the same curve, and the trader would watch the numbers arrive
    after the chart they belong to.

    Every number here is **per one unit of the underlying**. `contract_value` is echoed
    so the screen can multiply; the engine never does — `docs/settlement.md` is explicit
    that the multiplier is a lot size applied at the very end and never inside a pricing
    calculation.
    """

    underlying: str
    #: `DD-MM-YYYY`, `/chain`'s own spelling. One expiry per strategy: legs spanning two
    #: are refused before this is built.
    expiry: str
    #: ISO 8601 UTC, second precision, `Z`-suffixed. Always populated — on a live
    #: request it is the minute the ladder that answered was stamped with, so a client
    #: that named none is still told which one it got.
    as_of: str
    #: Delta's top-level `spot_price`. `greeks.spot` is never exposed.
    spot: Finite | None = None
    #: What the Greeks were priced against, and the discount fitted alongside it. `None`
    #: together exactly when the chain could not be fitted, and then no leg carries an
    #: `iv` or any Greeks either. The curve and the metrics survive that: a P&L at
    #: expiry is intrinsic value and a subtraction, and needs no model at all.
    forward: Finite | None = None
    discount: Finite | None = None
    #: The lot-size multiplier, `0.001` on this venue. Echoed, never applied here.
    contract_value: Finite
    #: One row per requested leg, in the order they were sent.
    legs: list[AnalysedLeg] = Field(min_length=1)
    #: The strategy's exposure, the same shape summed across the legs.
    total_greeks: Greeks | None = None
    curve: Curve
    metrics: Metrics
    #: The same quantity as `curve.corners`, on the readable grid a trader takes exact
    #: figures off rather than inferring them from the picture. Strictly ascending, and
    #: the same type deliberately: one quantity sampled twice, not two quantities.
    table: list[PayoffPoint] = Field(min_length=1)

    @model_validator(mode="after")
    def _total_greeks_are_all_or_nothing(self) -> AnalyseResponse:
        """Published only when **every** leg carries Greeks. A sum over the legs that
        happened to solve describes a different position from the one on screen, and
        nothing on that screen would say so."""
        every_leg_solved = all(leg.greeks is not None for leg in self.legs)
        if every_leg_solved != (self.total_greeks is not None):
            raise ValueError(
                "total_greeks is published exactly when every leg carries greeks; "
                f"{sum(leg.greeks is not None for leg in self.legs)} of "
                f"{len(self.legs)} legs solved"
            )
        return self

    @model_validator(mode="after")
    def _the_table_ascends_too(self) -> AnalyseResponse:
        """The same rule the corners obey, because it is the same quantity sampled
        twice rather than a second one."""
        _refuse_unless_ascending([row.price for row in self.table], "table")
        return self

    @model_validator(mode="after")
    def _a_fit_is_whole_or_absent(self) -> AnalyseResponse:
        """`forward` and `discount` come out of **one** fit, so one without the other is
        half a fit reported as a whole one — and with no forward there is nothing to
        invert a volatility against, so no leg may carry an `iv` or any Greeks either.

        The third exactly-when on this contract, beside `iv`/`greeks` and `total_greeks`.
        The curve and the metrics survive an unfitted chain, which is why this is a
        nullable pair rather than a refusal.
        """
        if (self.forward is None) != (self.discount is None):
            raise ValueError(
                "forward and discount are published together or not at all; got "
                f"forward={self.forward!r}, discount={self.discount!r}"
            )
        if self.forward is None and any(leg.iv is not None for leg in self.legs):
            raise ValueError(
                "a chain with no forward has nothing to invert a volatility against, "
                "so no leg may carry an iv or greeks"
            )
        return self
