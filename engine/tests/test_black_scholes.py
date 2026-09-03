"""M2, Black-Scholes on spot — and the identity that makes the model axis collapse.

The ticket calls M1 and M2 "the same model wearing different clothes". That is exactly
true and it is checkable: Black-Scholes with spot `S` and rate `r` is Black-76 with
`F = S·e^(rT)` and `D = e^(-rT)`. The first test asserts it to machine precision.

What follows from that is the real finding of the model axis. **M1-versus-M2 is not a
second axis at all — it is the forward axis in different clothes**, because the only
thing M2 can disagree with M1 about is the forward it implies from its rate. And that
comparison already has a name in #2: F3.
"""

from __future__ import annotations

import math

import pytest

from deltapayoff.black76 import call_price, put_price
from deltapayoff.black_scholes import bs_call_price, bs_put_price, bs_vega

#: Absolute price agreement demanded of the identity, in USD per unit of underlying.
#: Two decades above the worst observed 7.9e-12 gap, and still a ten-billionth of a cent.
PRICE_ROUNDING = 1e-10

SPOT = 77_568.20
YEARS = 0.010408517250126838
RATE = 0.065


@pytest.mark.parametrize("strike", [60_000.0, 70_000.0, 77_600.0, 85_000.0, 95_000.0])
@pytest.mark.parametrize("sigma", [0.10, 0.30, 0.45, 0.90])
def test_black_scholes_is_black_76_on_the_carry_forward(
    strike: float, sigma: float
) -> None:
    """`BS(S, r) == B76(F = S·e^(rT), D = e^(-rT))`, to machine precision.

    Twenty combinations. If this held only approximately the two would be genuinely
    different models and the agreement matrix would need a model axis. It holds exactly,
    so it does not.

    The tolerance is absolute, and that choice is itself measured. Across these cases
    the relative gap swings a thousandfold - 2.1e-16 at a $17,608 price, 2.3e-13 at a
    $33 one - while the absolute gap barely moves, staying between 3.5e-12 and 7.9e-12.
    The rounding is set by the scale of the intermediate terms, where `S*Phi(d1)` is of
    order 77,568, and not by the size of the answer. A relative tolerance would
    therefore be far too loose on the large prices and too tight on the small ones.

    So this is floating point, not mathematics. Agreeing to a hundred-billionth of a
    cent is not two models being close; it is one model written twice.
    """
    forward = SPOT * math.exp(RATE * YEARS)
    discount = math.exp(-RATE * YEARS)

    scholes = bs_call_price(
        spot=SPOT, strike=strike, years=YEARS, sigma=sigma, rate=RATE
    )
    seventy_six = call_price(
        forward=forward, strike=strike, years=YEARS, sigma=sigma, discount=discount
    )

    assert scholes == pytest.approx(seventy_six, abs=PRICE_ROUNDING)


def test_the_same_identity_holds_for_puts() -> None:
    """Puts too, or the identity is a coincidence of one branch."""
    forward = SPOT * math.exp(RATE * YEARS)
    discount = math.exp(-RATE * YEARS)

    for strike in (60_000.0, 77_600.0, 95_000.0):
        scholes = bs_put_price(
            spot=SPOT, strike=strike, years=YEARS, sigma=0.45, rate=RATE
        )
        seventy_six = put_price(
            forward=forward, strike=strike, years=YEARS, sigma=0.45, discount=discount
        )
        assert scholes == pytest.approx(seventy_six, abs=PRICE_ROUNDING)


def test_black_scholes_vega_matches_too() -> None:
    """Vega decides where every solver fails, so if it differed between the two models
    the failure regions would differ and the identity would not be worth much."""
    forward = SPOT * math.exp(RATE * YEARS)
    discount = math.exp(-RATE * YEARS)

    from deltapayoff.black76 import vega

    for strike in (70_000.0, 77_600.0, 85_000.0):
        assert bs_vega(
            spot=SPOT, strike=strike, years=YEARS, sigma=0.45, rate=RATE
        ) == pytest.approx(
            vega(
                forward=forward,
                strike=strike,
                years=YEARS,
                sigma=0.45,
                discount=discount,
            ),
            abs=PRICE_ROUNDING,
        )


def test_a_different_rate_is_a_different_forward_and_nothing_else() -> None:
    """The consequence worth stating: changing M2's rate moves only its forward.

    At r = 0 Black-Scholes must equal Black-76 with `F = S`, which is F4. So the model
    axis and the forward axis are the same axis, and the agreement matrix needs only one
    of them.
    """
    for strike in (70_000.0, 77_600.0, 85_000.0):
        assert bs_call_price(
            spot=SPOT, strike=strike, years=YEARS, sigma=0.45, rate=0.0
        ) == pytest.approx(
            call_price(
                forward=SPOT,
                strike=strike,
                years=YEARS,
                sigma=0.45,
                discount=1.0,
            ),
            abs=PRICE_ROUNDING,
        )
