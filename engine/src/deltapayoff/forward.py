"""Four independent answers to "what is the forward for this expiry?".

Pure functions over a :class:`~deltapayoff.models.ChainResponse`. Nothing here knows
whether that snapshot arrived over REST or the websocket, and nothing here dials out.

Delta's options are vanilla, USD-quoted and USD-settled — see `docs/settlement.md`, which
measures it rather than assuming it. So textbook put-call parity applies unmodified, and
`contract_value` never enters: every price below is USD per unit of underlying, exactly as
Delta quotes it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel

from .chain import EXPIRY_FORMAT
from .models import ChainResponse, Leg
from .timing import Timing, time_it

#: Delta settles every option at 12:00 UTC on its expiry date. Measured, not assumed.
SETTLEMENT_HOUR_UTC = 12

#: ACT/365, matching `payoff-project`.
DAYS_PER_YEAR = 365.0

#: The gate, carried over from `payoff-project/docs/calculations.md` §1. OLS returns a
#: number whether the input deserves one or not; these two conditions decide whether it
#: does. Both are really *discount* gates — see `docs/forward.md` on why the forward
#: survives a fit the discount does not.
MIN_PAIRS = 5
MAX_PLAUSIBLE_RATE = 0.30

#: The rate F2 and F3 assume. There is no risk-free rate for BTC, so this is a borrowed
#: constant, not a measurement — `payoff-project` uses the same 6.5%. F1 assumes nothing
#: and exists precisely so this number can be checked rather than trusted.
ASSUMED_RATE = 0.065


class ForwardResult(BaseModel):
    """One method's answer, with the verdict on whether to believe it."""

    method: str
    forward: float | None = None
    discount: float | None = None
    implied_rate: float | None = None
    trusted: bool = False
    n_pairs: int = 0
    strike_range: tuple[float, float] | None = None
    width: int | None = None
    timing: Timing | None = None


def mid(leg: Leg | None) -> float | None:
    """The market's price for one leg: the midpoint of its two-sided quote.

    A one-sided quote has no midpoint, so it is absent rather than half-guessed.
    """
    if leg is None or leg.bid is None or leg.ask is None:
        return None
    return (leg.bid + leg.ask) / 2


def parity_pairs(chain: ChainResponse) -> list[tuple[float, float]]:
    """`(strike, C - P)` for every strike quoting **both** sides.

    Parity needs a pair. A strike quoting only one side carries no information about
    the forward and must not enter the fit.
    """
    pairs = []
    for row in chain.rows:
        call, put = mid(row.call), mid(row.put)
        if call is not None and put is not None:
            pairs.append((row.strike, call - put))
    return pairs


def window(pairs: list[tuple[float, float]], atm: float | None, width: int | None):
    """The `width` strikes either side of the money, so `2·width + 1` at most.

    The wings are where quotes go stale and spreads blow out, and the slope — hence the
    discount — is what wide wings corrupt. `width=None` keeps every pair.
    """
    if width is None or atm is None:
        return pairs
    ordered = sorted(pairs, key=lambda pair: pair[0])
    strikes = [strike for strike, _ in ordered]
    if atm not in strikes:
        return ordered
    centre = strikes.index(atm)
    return ordered[max(0, centre - width) : centre + width + 1]


def assumed_discount(chain: ChainResponse) -> float:
    """`D = e^(-rT)` at the assumed rate. Used where a slope cannot be fitted."""
    return math.exp(-ASSUMED_RATE * year_fraction(chain))


def f1_parity_fit(chain: ChainResponse, width: int | None = None) -> ForwardResult:
    """Ordinary least squares through `C - P` against `K`.

    Parity says that line is `y = D·F - D·K`, so its slope is `-D` and it crosses zero
    at `K = F`. Fitting it recovers both, assuming neither.
    """
    pairs = window(parity_pairs(chain), chain.atm_strike, width)
    n = len(pairs)
    if n < 2:
        # One point defines no line and zero points define nothing. There is no number
        # to report here, and reporting one anyway is the failure this ticket is about.
        return ForwardResult(method="F1", n_pairs=n, width=width)

    sum_x = sum(x for x, _ in pairs)
    sum_y = sum(y for _, y in pairs)
    sum_xy = sum(x * y for x, y in pairs)
    sum_xx = sum(x * x for x, _ in pairs)

    denominator = n * sum_xx - sum_x * sum_x
    if denominator == 0:
        # Every pair sits on one strike, so the line is vertical and has no slope.
        return ForwardResult(method="F1", n_pairs=n, width=width)

    slope = (n * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / n

    if slope >= 0:
        # An upward-sloping C - P line implies a negative discount factor. Parity
        # cannot produce one, so the input is not a parity line.
        return ForwardResult(method="F1", n_pairs=n, width=width)

    discount = -slope
    forward = -intercept / slope
    rate = -math.log(discount) / year_fraction(chain) if discount > 0 else None

    trusted = n >= MIN_PAIRS and rate is not None and 0 < rate < MAX_PLAUSIBLE_RATE
    strikes = [strike for strike, _ in pairs]
    return ForwardResult(
        method="F1",
        forward=forward,
        discount=discount,
        implied_rate=rate,
        trusted=trusted,
        n_pairs=n,
        strike_range=(min(strikes), max(strikes)),
        width=width,
    )


def year_fraction(chain: ChainResponse) -> float:
    """Years from the snapshot to settlement, ACT/365.

    Delta settles options at 12:00 UTC on the expiry date. **Measured**:
    `/v2/products/P-BTC-90000-040926` reports `settlement_time` `2026-09-04T12:00:00Z`.
    So the snapshot carries everything needed and no clock is read here.
    """
    settles = datetime.strptime(chain.expiry, EXPIRY_FORMAT).replace(
        hour=SETTLEMENT_HOUR_UTC, tzinfo=timezone.utc
    )
    taken = datetime.strptime(chain.fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (settles - taken).total_seconds() / (DAYS_PER_YEAR * 86_400)


def f2_single_strike(chain: ChainResponse) -> ForwardResult:
    """Parity inverted at one strike: `F = K* + (C(K*) - P(K*)) / D`.

    One strike cannot give a slope, so `D` has to be assumed rather than fitted. That
    is the entire difference between this and F1, and it is why F2 inherits F1's
    robustness in the forward while giving up any claim to the discount.

    `K*` is the money strike, and it must quote both sides — there is nothing to invert
    otherwise.
    """
    discount = assumed_discount(chain)
    row = next(
        (r for r in chain.rows if r.strike == chain.atm_strike),
        None,
    )
    call, put = (mid(row.call), mid(row.put)) if row else (None, None)
    if call is None or put is None:
        return ForwardResult(method="F2", discount=discount, trusted=False)

    forward = row.strike + (call - put) / discount
    return ForwardResult(
        method="F2",
        forward=forward,
        discount=discount,
        implied_rate=ASSUMED_RATE,
        trusted=True,
        n_pairs=1,
        strike_range=(row.strike, row.strike),
    )


def f3_carry(chain: ChainResponse) -> ForwardResult:
    """`F = S · e^(rT)` at an assumed r. Never reads an option price.

    This is the textbook forward, and on an equity index it is close to right. On a
    crypto venue there is no risk-free rate for BTC, so `r` is a guess wearing a
    formula. Included as the number the parity fit has to beat.
    """
    if chain.spot is None:
        return ForwardResult(method="F3", trusted=False)
    years = year_fraction(chain)
    return ForwardResult(
        method="F3",
        forward=chain.spot * math.exp(ASSUMED_RATE * years),
        discount=math.exp(-ASSUMED_RATE * years),
        implied_rate=ASSUMED_RATE,
        trusted=True,
    )


def f4_spot(chain: ChainResponse) -> ForwardResult:
    """`F = S`. Asserts the basis is zero, which it is not.

    Kept because it is the honest null hypothesis. `payoff-project` measured the NIFTY
    basis at ~120 points and forcing it to zero corrupted every Greek downstream; on
    crypto the answer may differ, and #4 exists to measure it rather than assume it.
    """
    if chain.spot is None:
        return ForwardResult(method="F4", trusted=False)
    return ForwardResult(
        method="F4", forward=chain.spot, discount=1.0, implied_rate=0.0, trusted=True
    )


#: The widths #2 asks F1 to be swept over, plus `None` — every paired strike, no window.
#:
#: `None` was added after the sweep measured why it had to be. Each narrow window on the
#: captured chain implies a rate the gate rejects, and the reason is that a slope needs a
#: wide base: ATM+/-3 spans 1.3% of spot, the full chain spans 38.7%. Trimming the wings
#: is the usual instinct for noisy data and it is the wrong one here.
SWEEP_WIDTHS: tuple[int | None, ...] = (3, 5, 7, 9, None)


def sweep_widths(
    chain: ChainResponse, widths: tuple[int | None, ...] = SWEEP_WIDTHS
) -> list[ForwardResult]:
    """F1 at each window width, so the two can be compared side by side.

    Sweeping is not tuning. There is no "best" x to pick here — the point is to watch
    which quantity moves when x does, and the answer is that the discount moves and the
    forward does not.
    """
    return [f1_parity_fit(chain, width=width) for width in widths]


def compare_forwards(chain: ChainResponse, runs: int = 100) -> list[ForwardResult]:
    """Every method's answer to the same snapshot, each with its own timing.

    Deliberately *not* a fallback ladder. `payoff-project` wires these three tiers in
    sequence and takes the first that passes its gate; here they stay side by side,
    because #4 exists to measure whether the parity fit was worth the trouble at all
    and a ladder would have already decided that.
    """
    methods: list[tuple[Callable[[ChainResponse], ForwardResult], int | None]] = [
        *((lambda c, w=w: f1_parity_fit(c, width=w), w) for w in SWEEP_WIDTHS),
        (f2_single_strike, None),
        (f3_carry, None),
        (f4_spot, None),
    ]

    results = []
    for method, width in methods:
        result, timing = time_it(lambda m=method: m(chain), runs=runs)
        results.append(result.model_copy(update={"timing": timing, "width": width}))
    return results
