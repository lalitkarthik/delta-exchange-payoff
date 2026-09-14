"""`docs/payoff-contract.md`, in tests.

The contract document is the authority and these pin the parts of it a type can hold:
what a leg is allowed to say, what a curve is allowed to be, and that nothing which
cannot cross a JSON wire can be built at all.

Every expected value below is a literal read off the contract document or the spec —
never a figure recomputed the way the models compute one, because the models compute
nothing. The arithmetic that fills these shapes is P2's.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from deltapayoff.events.instrument import Instrument, InstrumentParseError
from deltapayoff.payoff_models import (
    AnalysedLeg,
    AnalyseRequest,
    AnalyseResponse,
    Curve,
    Greeks,
    LegRequest,
    Metrics,
    PayoffPoint,
    Window,
)

CALL = "DELTA-BTC-20260904-77000-C-USD"


def test_a_leg_is_bought_or_sold_and_nothing_else() -> None:
    """`direction` is `+1` (bought) or `-1` (sold). Not `0`, which is neither, and not
    `2`, which is a quantity wearing the sign field's clothes."""
    assert LegRequest(instrument=CALL, direction=1, quantity=1).direction == 1
    assert LegRequest(instrument=CALL, direction=-1, quantity=1).direction == -1
    for refused in (0, 2, -2):
        with pytest.raises(ValidationError):
            LegRequest(instrument=CALL, direction=refused, quantity=1)


def test_a_strategy_needs_at_least_one_leg() -> None:
    """An empty `legs` is a refusal, not an empty chart. `docs/payoff-contract.md`
    makes it a 422; the rule lives on the type so no route can forget it."""
    with pytest.raises(ValidationError):
        AnalyseRequest(legs=[])


def test_no_as_of_means_live() -> None:
    """The one optional parameter, and the whole of the live-versus-stored switch."""
    assert AnalyseRequest(legs=[LegRequest(instrument=CALL, direction=1)]).as_of is None


# The long call from `docs/payoff-contract.md`'s worked response: 77000 strike, paid
# 1240, so it loses the premium below the strike and gains one dollar per dollar above.
LONG_CALL_CORNERS = [
    PayoffPoint(price=74000.0, pnl=-1240.0),
    PayoffPoint(price=77000.0, pnl=-1240.0),
    PayoffPoint(price=81000.0, pnl=2760.0),
]


def test_a_curve_ascends_strictly_by_price() -> None:
    """Corners out of order draw a curve that folds back on itself, and corners at one
    price draw a vertical segment; neither raises on a chart, so neither can be built.
    """
    curve = Curve(
        corners=LONG_CALL_CORNERS,
        slope_left=0.0,
        slope_right=1.0,
        window=Window(low=74000.0, high=81000.0),
    )
    assert [corner.price for corner in curve.corners] == [74000.0, 77000.0, 81000.0]

    for broken in (
        [LONG_CALL_CORNERS[0], LONG_CALL_CORNERS[2], LONG_CALL_CORNERS[1]],
        [LONG_CALL_CORNERS[1], LONG_CALL_CORNERS[1]],
    ):
        with pytest.raises(ValidationError):
            Curve(
                corners=broken,
                slope_left=0.0,
                slope_right=1.0,
                window=Window(low=74000.0, high=81000.0),
            )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_no_non_finite_number_can_be_built(bad: float) -> None:
    """`nan` and the infinities are not JSON. Python's own encoder emits them as the
    bare tokens `NaN` and `Infinity`, which `JSON.parse` rejects — so a chart would
    fail to load rather than draw wrong, but only after the engine had already claimed
    a 200. Refused on the type, where no route can route around it.

    An unbounded maximum is `null`, which is a different thing and is tested below.
    """
    window = Window(low=74000.0, high=81000.0)
    with pytest.raises(ValidationError):
        PayoffPoint(price=77000.0, pnl=bad)
    with pytest.raises(ValidationError):
        PayoffPoint(price=bad, pnl=0.0)
    with pytest.raises(ValidationError):
        Curve(
            corners=LONG_CALL_CORNERS,
            slope_left=bad,
            slope_right=1.0,
            window=window,
        )


def test_a_window_opens_left_to_right() -> None:
    """`low < high`. A window whose ends are swapped or equal has no width to draw in,
    and an axis scaled from it divides by zero or runs backwards."""
    assert Window(low=74000.0, high=81000.0).high == 81000.0
    with pytest.raises(ValidationError):
        Window(low=81000.0, high=74000.0)
    with pytest.raises(ValidationError):
        Window(low=77000.0, high=77000.0)


def test_unbounded_is_null_and_never_an_infinity() -> None:
    """A long call's max profit has no finite value. `docs/payoff-contract.md` spells
    that `null` — not `1e18`, not `Infinity`, not `"Unlimited"`.

    `reward_risk` goes with it: a ratio against unlimited has no meaning, and a large
    number in its place would read as a good trade.
    """
    metrics = Metrics(
        max_profit=None,
        max_loss=-1240.0,
        breakevens=[78240.0],
        net_premium=1240.0,
        reward_risk=None,
    )
    assert metrics.max_profit is None
    assert metrics.reward_risk is None
    # ...and the infinity that would otherwise have been reached for is refused.
    with pytest.raises(ValidationError):
        Metrics(
            max_profit=float("inf"),
            max_loss=-1240.0,
            breakevens=[78240.0],
            net_premium=1240.0,
            reward_risk=None,
        )


def test_breakevens_ascend() -> None:
    """A short straddle crosses zero twice, and the two are read left to right off the
    chart. Out of order they still render — as a list of two plausible prices in the
    wrong places."""
    straddle = Metrics(
        max_profit=2480.0,
        max_loss=None,
        breakevens=[74520.0, 79480.0],
        net_premium=-2480.0,
        reward_risk=None,
    )
    assert straddle.breakevens == [74520.0, 79480.0]
    with pytest.raises(ValidationError):
        Metrics(
            max_profit=2480.0,
            max_loss=None,
            breakevens=[79480.0, 74520.0],
            net_premium=-2480.0,
            reward_risk=None,
        )


GREEKS = Greeks(delta=0.5231, gamma=0.0000312, vega=0.4118, theta=-66.58, rho=0.129)


def test_a_leg_with_no_volatility_carries_no_greeks() -> None:
    """The project's standing rule, `docs/chain-contract.md`: reporting Greeks at some
    default sigma puts five plausible numbers on screen that describe nothing. So `iv`
    and `greeks` are present together or absent together, and neither half can be
    published without the other."""
    solved = AnalysedLeg(
        instrument=CALL, direction=1, quantity=2, entry_price=1240.0,
        iv=0.3712, greeks=GREEKS,
    )
    assert solved.greeks is not None
    unsolved = AnalysedLeg(
        instrument=CALL, direction=1, quantity=2, entry_price=1240.0,
        iv=None, greeks=None,
    )
    assert unsolved.greeks is None

    with pytest.raises(ValidationError):
        AnalysedLeg(
            instrument=CALL, direction=1, quantity=2, entry_price=1240.0,
            iv=None, greeks=GREEKS,
        )
    with pytest.raises(ValidationError):
        AnalysedLeg(
            instrument=CALL, direction=1, quantity=2, entry_price=1240.0,
            iv=0.3712, greeks=None,
        )


# `docs/payoff-contract.md`'s worked response, copied out of the document by hand. It is
# the source of truth for this test: nothing below recomputes any figure in it.
WORKED_RESPONSE = """
{
  "underlying": "BTC",
  "expiry": "04-09-2026",
  "as_of": "2026-09-04T09:21:00Z",
  "spot": 77543.0,
  "forward": 77609.4,
  "discount": 0.99961,
  "contract_value": 0.001,
  "legs": [
    {
      "instrument": "DELTA-BTC-20260904-77000-C-USD",
      "direction": 1,
      "quantity": 1,
      "entry_price": 1240.0,
      "iv": 0.3712,
      "greeks": { "delta": 0.5231, "gamma": 0.0000312, "vega": 0.4118,
                  "theta": -66.58, "rho": 0.129 }
    }
  ],
  "total_greeks": { "delta": 0.5231, "gamma": 0.0000312, "vega": 0.4118,
                    "theta": -66.58, "rho": 0.129 },
  "curve": {
    "corners": [ { "price": 74000.0, "pnl": -1240.0 },
                 { "price": 77000.0, "pnl": -1240.0 },
                 { "price": 81000.0, "pnl": 2760.0 } ],
    "slope_left": 0.0,
    "slope_right": 1.0,
    "window": { "low": 74000.0, "high": 81000.0 }
  },
  "metrics": {
    "max_profit": null,
    "max_loss": -1240.0,
    "breakevens": [78240.0],
    "net_premium": 1240.0,
    "reward_risk": null
  },
  "table": [ { "price": 74000.0, "pnl": -1240.0 } ]
}
"""

#: Every key in the contract whose value is a decimal or `null`, never a string.
NUMERIC_KEYS = frozenset(
    {
        "spot", "forward", "discount", "contract_value", "entry_price", "iv",
        "delta", "gamma", "vega", "theta", "rho", "price", "pnl",
        "slope_left", "slope_right", "low", "high",
        "max_profit", "max_loss", "net_premium", "reward_risk",
    }
)


def _strings_where_numbers_belong(value: object, path: str = "") -> list[str]:
    if isinstance(value, dict):
        return [
            offence
            for key, item in value.items()
            for offence in (
                [f"{path}.{key}={item!r}"]
                if key in NUMERIC_KEYS and isinstance(item, str)
                else _strings_where_numbers_belong(item, f"{path}.{key}")
            )
        ]
    if isinstance(value, list):
        return [
            offence
            for index, item in enumerate(value)
            for offence in _strings_where_numbers_belong(item, f"{path}[{index}]")
        ]
    return []


def test_the_worked_response_round_trips_with_no_field_lost() -> None:
    """The document's own example, parsed by the models and written back out.

    Compared as parsed JSON rather than as text, so whitespace does not matter but a
    dropped field, an invented default or a decimal turned into a string does.
    """
    sent = json.loads(WORKED_RESPONSE)
    parsed = AnalyseResponse.model_validate_json(WORKED_RESPONSE)
    assert json.loads(parsed.model_dump_json()) == sent


def test_no_decimal_arrives_as_a_string() -> None:
    """Delta sends decimals as strings and the engine converts once, at the boundary.
    Nothing downstream calls `parseFloat`, so a string here would sort and scale as text
    and draw a plausible, wrong chart rather than fail."""
    parsed = AnalyseResponse.model_validate_json(WORKED_RESPONSE)
    dumped = json.loads(parsed.model_dump_json())
    assert _strings_where_numbers_belong(dumped) == []
    # And the guard itself bites, rather than passing because it looked at nothing.
    assert _strings_where_numbers_belong({"legs": [{"entry_price": "1240.0"}]}) == [
        ".legs[0].entry_price='1240.0'"
    ]


def test_total_greeks_are_published_only_when_every_leg_solved() -> None:
    """A sum over the legs that happened to solve describes a different position from
    the one on screen. So the total is all or nothing."""
    unsolved = AnalysedLeg(
        instrument=CALL, direction=1, quantity=1, entry_price=1240.0,
        iv=None, greeks=None,
    )
    body = json.loads(WORKED_RESPONSE)
    body["legs"].append(json.loads(unsolved.model_dump_json()))
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)


def test_the_payoff_table_ascends_like_the_curve() -> None:
    """The table and the corners are one quantity sampled twice, so they obey one rule.
    A table read top to bottom out of order is a column of plausible prices against the
    wrong P&Ls."""
    body = json.loads(WORKED_RESPONSE)
    body["table"] = [
        {"price": 81000.0, "pnl": 2760.0},
        {"price": 74000.0, "pnl": -1240.0},
    ]
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)


def test_a_field_the_contract_does_not_name_is_refused_not_ignored() -> None:
    """Both directions. On the way in, a client's typo must not be silently discarded
    into a default. On the way out, the pure core builds these objects directly, so a
    mistyped keyword there would drop a whole section of the response and answer 200
    without it — `entry_premium` is the sibling project's spelling of `entry_price` and
    is exactly the typo to expect."""
    with pytest.raises(ValidationError):
        LegRequest(instrument=CALL, direction=1, entry_premium=1240.0)
    body = json.loads(WORKED_RESPONSE)
    body["max_pain"] = 77000.0
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)


def test_a_quantity_is_a_positive_whole_number() -> None:
    """The size, never the sign — that lives in `direction`. One by default, because a
    click on B beside a strike is one contract until the trader says otherwise."""
    assert LegRequest(instrument=CALL, direction=1).quantity == 1
    for refused in (0, -1, 1.5):
        with pytest.raises(ValidationError):
            LegRequest(instrument=CALL, direction=1, quantity=refused)


def test_the_leg_does_not_second_guess_the_instrument_parser() -> None:
    """**Deliberately** no field validator on `instrument`.

    `Instrument.from_canonical` is the single validator and it names which of the six
    parts was wrong. A validator here would bury that naming inside FastAPI's 422
    envelope, where `docs/payoff-contract.md` promises a 400 carrying the part — so the
    string travels verbatim and the route parses it. This test exists so that adding one
    later is a deliberate act rather than a tidy-up.
    """
    assert LegRequest(instrument="nonsense", direction=1).instrument == "nonsense"

    # ...and the one validator does refuse it, naming what it could not read.
    with pytest.raises(InstrumentParseError, match="nonsense"):
        Instrument.from_canonical("nonsense")
    # The pre-I1 five-part shape is refused too: the currency is required, not optional.
    with pytest.raises(InstrumentParseError):
        Instrument.from_canonical("DELTA-BTC-20260904-77000-C")
    assert Instrument.from_canonical(CALL).canonical() == CALL


def test_the_forward_and_the_discount_are_null_together() -> None:
    """`docs/payoff-contract.md`: they come out of one fit, so one without the other is
    half a fit reported as a whole one. The third exactly-when on this contract, beside
    `iv`/`greeks` and `total_greeks`."""
    body = json.loads(WORKED_RESPONSE)
    body["forward"] = None
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)
    body = json.loads(WORKED_RESPONSE)
    body["discount"] = None
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)


def test_an_unfitted_chain_carries_no_volatility_anywhere() -> None:
    """The other half of the same sentence: with no forward there is nothing to invert a
    volatility against, so no leg may carry an `iv` or any Greeks.

    The curve and the metrics survive it — a P&L at expiry is intrinsic value and a
    subtraction — which is why this is a nullable pair rather than a refusal.
    """
    body = json.loads(WORKED_RESPONSE)
    body["forward"] = None
    body["discount"] = None
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)

    # ...and stripped of its volatility, the same response is legitimate.
    body["legs"][0]["iv"] = None
    body["legs"][0]["greeks"] = None
    body["total_greeks"] = None
    unfitted = AnalyseResponse.model_validate(body)
    assert unfitted.metrics.breakevens == [78240.0]
    prices = [corner.price for corner in unfitted.curve.corners]
    assert prices == [74000.0, 77000.0, 81000.0]


def test_a_response_with_a_curve_has_a_table_too() -> None:
    """The corners and the table are one quantity sampled twice, so a response holding
    three corners and an empty table is exactly the drift a shared type exists to
    prevent."""
    body = json.loads(WORKED_RESPONSE)
    body["table"] = []
    with pytest.raises(ValidationError):
        AnalyseResponse.model_validate(body)
