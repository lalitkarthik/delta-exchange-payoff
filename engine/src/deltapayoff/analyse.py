"""`POST /analyse`: legs against a ladder, and the whole analysis out. Composition only.

Nothing here prices anything. `compute.enrich` has already fitted the forward, inverted
one implied volatility per strike off the out-of-the-money leg's midpoint and reported
five Greeks; `payoff.py` already turns weighted legs into corners, metrics and a table.
This module is what stands between them and the wire: it resolves each requested leg
against a ladder, decides what it was entered at, and assembles
`payoff_models.AnalyseResponse`.

**It does not know which ladder it was handed.** The live chain cache and
`historical.read_ladder_at` both answer with a `ChainResponse`, so choosing between them
is `main.py`'s business and nothing below reads a clock, a socket or a file. That is also
what lets the fitted case be tested against rows a test wrote rather than against a solve
that depends on today's date.

**Five of the contract's eight refusals are raised here** — the malformed instrument, the
two legs that disagree about the underlying, the two that disagree about the expiry, the
leg that is not listed and the leg nobody is quoting. Two of the others say that there is
no ladder at all, which is a fact about the read path rather than about the legs, so they
belong to the route that chose it; the last is an empty `legs`, which `AnalyseRequest`
refuses on the type before this module is reached.

`AnalyseRefusal` carries the status code rather than the route re-deriving one from an
exception type, because `docs/payoff-contract.md` fixes the code alongside the message and
the two should not be able to drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence

from .chain import CONTRACT_VALUES, EXPIRY_FORMAT, nearest_strike
from .events.instrument import Instrument, InstrumentParseError, Right
from .models import ChainResponse, ChainRow, ComputedLeg, Leg
from .payoff import (
    PayoffLeg,
    payoff_curve,
    payoff_table,
    position_greeks,
    scale_greeks,
    strategy_metrics,
    suggested_window,
)
from .payoff_models import AnalysedLeg, AnalyseResponse, Greeks, LegRequest


class AnalyseRefusal(ValueError):
    """A request `/analyse` will not answer, and the status code that says why.

    A `ValueError` for the reason `chain.ValidationError` is one: this module knows
    nothing about HTTP and must not import a web framework to say that a caller asked
    for something impossible. The code travels with the message because
    `docs/payoff-contract.md` fixes the two together — a 404 that arrived as a 422 would
    be a different promise to the client's error path.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


def strategy_series(legs: Sequence[LegRequest]) -> tuple[list[Instrument], str, str]:
    """The legs' instruments, and the one underlying and expiry they name.

    Parsed before any ladder is read, because the underlying and the expiry are what
    decide *which* ladder to read — and refused before any is read, because one ladder
    cannot answer for two series.

    **Two expiries** leave no date on which every leg has finished: the surviving leg has
    a price rather than a payoff, and the line could only be drawn by assuming a
    volatility. Calendar and diagonal spreads are #2.

    **Two underlyings** are refused for a harder reason than that. The ladder is fetched
    for the first leg's underlying, so the others are looked up on a chain that does not
    list them — and the only thing standing between that and a silently wrong answer is
    that BTC strikes are around 77,000 and ETH's around 4,000. On a collision the response
    would echo one `underlying` and one `contract_value` for legs whose lot sizes differ
    by a factor of ten, and the screen would multiply some of them by the wrong number
    with nothing on the page saying so. Multi-underlying strategies are out of scope by
    the spec; this is what makes that a refusal rather than an assumption.

    `Instrument.from_canonical` is the single validator, and its message already names
    which of the six parts was wrong; it is passed through rather than rewritten.
    """
    if not legs:
        # Unreachable through the route — `AnalyseRequest.legs` is `min_length=1` and the
        # type refuses an empty strategy before this is called. Here so that the function
        # is total for the `Sequence` it declares: a direct caller would otherwise get an
        # `IndexError` where every other malformed strategy gets a refusal.
        raise AnalyseRefusal(422, "a strategy must hold at least one leg")

    instruments = []
    for leg in legs:
        try:
            instruments.append(Instrument.from_canonical(leg.instrument))
        except InstrumentParseError as error:
            # Caught by its own type and not as a bare `ValueError`: the parser raises
            # exactly this for a bad string, and a wider catch here would answer 400 —
            # "you sent a malformed instrument" — for something that was not one.
            raise AnalyseRefusal(400, str(error)) from error
    underlyings = sorted({instrument.underlying for instrument in instruments})
    if len(underlyings) > 1:
        raise AnalyseRefusal(
            422,
            "one underlying per strategy; these legs span "
            + " and ".join(underlyings),
        )

    expiries = sorted({instrument.expiry for instrument in instruments})
    if len(expiries) > 1:
        spelled = " and ".join(day.strftime(EXPIRY_FORMAT) for day in expiries)
        raise AnalyseRefusal(422, f"one expiry per strategy; these legs span {spelled}")
    return instruments, underlyings[0], expiries[0].strftime(EXPIRY_FORMAT)


def analysed(
    chain: ChainResponse,
    requested: Sequence[LegRequest],
    instruments: Sequence[Instrument],
) -> AnalyseResponse:
    """Everything about one strategy, priced against one ladder.

    `as_of` is the ladder's own `fetched_at` and not a second clock reading: on the
    stored path that is the minute the rows were sealed in, and on the live path it is
    when the cache last rebuilt — so a client that named no minute is still told exactly
    which ladder answered.
    """
    payoff_legs: list[PayoffLeg] = []
    rows: list[AnalysedLeg] = []

    for request, instrument in zip(requested, instruments, strict=True):
        listed = _listed(chain, request, instrument)
        greeks = _greeks(listed.computed)
        leg = PayoffLeg(
            strike=float(instrument.strike),
            is_call=instrument.right is Right.CALL,
            direction=request.direction,
            quantity=request.quantity,
            entry_price=_entry_price(request, listed),
            greeks=greeks,
        )
        payoff_legs.append(leg)
        rows.append(
            AnalysedLeg(
                instrument=request.instrument,
                direction=request.direction,
                quantity=request.quantity,
                entry_price=leg.entry_price,
                # The volatility is the strike's and travels unweighted; the Greeks are
                # this leg's exposure and are signed and scaled — `scale_greeks` once
                # and only here, since `position_greeks` below weights the unweighted
                # copies held on `payoff_legs` itself, and doing it twice would square
                # the quantity with no type in the system able to catch it.
                iv=None if greeks is None else listed.computed.iv,
                greeks=None if greeks is None else scale_greeks(greeks, leg.weight),
            )
        )

    anchor = _anchor(chain.forward, chain.spot)
    window = suggested_window(
        payoff_legs,
        anchor=anchor,
        atm_iv=_atm_iv(chain, anchor),
        years=chain.years_to_expiry,
    )
    return AnalyseResponse(
        underlying=chain.underlying,
        expiry=chain.expiry,
        as_of=chain.fetched_at,
        spot=chain.spot,
        forward=chain.forward,
        discount=chain.discount,
        contract_value=CONTRACT_VALUES[chain.underlying],
        legs=rows,
        total_greeks=position_greeks(payoff_legs),
        curve=payoff_curve(payoff_legs, window),
        metrics=strategy_metrics(payoff_legs),
        table=payoff_table(payoff_legs, window),
    )


def _anchor(forward: float | None, spot: float | None) -> float | None:
    """What the window is centred on: the fitted forward, then spot, then nothing.

    The forward first because that is what the terminal distribution is centred on. A
    non-positive price is treated as no anchor at all rather than passed on —
    `suggested_window` refuses one, correctly, and a chart that failed to open because
    an upstream number came out at zero would report a fitting problem as a broken route.
    """
    return next(
        (price for price in (forward, spot) if price is not None and price > 0.0), None
    )


def _atm_iv(chain: ChainResponse, anchor: float | None) -> float | None:
    """The volatility of the strike nearest the anchor, which is what sets the window's
    width. `None` on an unfitted chain, where `suggested_window` falls back to a nominal
    width rather than refusing — a strategy still has a curve without a model.

    Read at the strike nearest the **anchor** rather than at `chain.atm_strike`, which is
    nearest **spot**: the window is drawn around the anchor, so its width should be the
    volatility of the strike it is centred on, and the two differ by a strike whenever
    the basis is wider than half a gap.
    """
    strike = nearest_strike([row.strike for row in chain.rows], anchor)
    row = None if strike is None else _row_at(chain, strike)
    if row is None:
        return None
    for leg in (row.call, row.put):
        iv = leg.computed.iv if leg is not None and leg.computed else None
        if iv is not None:
            # Either side answers: parity gives the strike one volatility and `compute`
            # writes it to both legs, with `iv_leg` naming where it came from. Reading
            # the call first is a coin toss, not a preference.
            return iv
    return None


def _row_at(chain: ChainResponse, strike: float) -> ChainRow | None:
    """The ladder's row at one strike, or `None` if it lists none.

    **Exact float equality, deliberately.** Both sides of this comparison are the
    correctly-rounded double of the *same decimal literal* — the ladder's `strike` is
    `float()` of the venue's own text (through `Instrument.strike` on the live path and
    the symbol's digits on the stored one), and the value passed in is `float()` of the
    `Decimal` that `from_canonical` read out of the request's copy of that same text.
    Python rounds a decimal string to the nearest double deterministically, so `77600`
    and `77600.0` and `Decimal("77600")` are one bit pattern and there is nothing for a
    tolerance to absorb. No strike anywhere in this engine is ever *computed*, which is
    the only way the two could drift.

    And it **fails safe**: a mismatch finds no row and answers the 404 that names the
    instrument, rather than silently returning a neighbouring strike's leg.
    """
    return next((row for row in chain.rows if row.strike == strike), None)


def _listed(chain: ChainResponse, request: LegRequest, instrument: Instrument) -> Leg:
    """The ladder's row for this leg, or a 404 naming the string that was not on it.

    A strike and a side do not name a contract — 77000 C trades in every series at once —
    so the whole canonical string is what is looked up and what the refusal quotes back.
    """
    row = _row_at(chain, float(instrument.strike))
    listed = None
    if row is not None:
        listed = row.call if instrument.right is Right.CALL else row.put
    if listed is not None:
        return listed
    raise AnalyseRefusal(
        404,
        f"{request.instrument} is not listed on the {chain.underlying} "
        f"{chain.expiry} chain",
    )


def _entry_price(request: LegRequest, listed: Leg) -> float:
    """What one unit of this leg cost: **the ask when bought, the bid when sold**.

    Never the mid, which is a trade nobody can make — the spread is a median 1.81% of it
    and 46.15% at p90. Never `ltp`, which on this venue is the close of a rolling 24-hour
    candle republished on every frame. Never the mark, which is Delta's own model output.
    A supplied price overrides the book, because "what if I were filled at 900" is a
    question the trader is entitled to ask of a quoted leg as well as an unquoted one.

    **A leg with nothing on its side refuses the whole analysis**, naming the side that
    was empty. Not dropped — a strategy missing a wing is a different strategy, drawn
    with no sign that a leg is missing — and never taken from the other side of the
    strike, which would be inventing the one number the trader is being asked for.
    """
    if request.entry_price is not None:
        return request.entry_price
    bought = request.direction == 1
    price = listed.ask if bought else listed.bid
    if price is None:
        side = "ask" if bought else "bid"
        raise AnalyseRefusal(
            422,
            f"nothing is quoted on the {side} of {request.instrument}; "
            "supply an entry_price for it",
        )
    return price


def _greeks(computed: ComputedLeg | None) -> Greeks | None:
    """This strike's five exposures **per one unit and unsigned**, as `compute` solved
    them. `None` unless all five and the volatility are there.

    A leg with no volatility carries no Greeks: five figures at some default sigma would
    describe nothing. **And a leg with four Greeks carries none either** — table C's five
    columns are nullable independently of `iv`, so a partly-solved row is a shape the
    store can hold, and the caller drops the `iv` with them so the pair stays exact. That
    leg is then published as unfitted rather than answered with a 500: it is a condition
    that arose in the data, and a real condition leaving as a server error is the
    inversion this project is organised against. Dropping a number is not fabricating
    one — `docs/payoff-contract.md`'s Refusals section carries the same ruling.
    """
    if computed is None or computed.iv is None:
        return None
    values = (
        computed.delta,
        computed.gamma,
        computed.vega,
        computed.theta,
        computed.rho,
    )
    if any(value is None for value in values):
        return None
    delta, gamma, vega, theta, rho = values
    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)
