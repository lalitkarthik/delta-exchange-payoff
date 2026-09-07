"""One number for "what the market thinks volatility will be over the next N days".

There is almost never an option with exactly `N` days to run, and the forward almost
never sits exactly on a listed strike. So a constant-maturity ATM implied volatility has
to be **constructed**, by interpolating twice: across strikes to find the money, and
across expiries to find the tenor.

Pure, like `forward.py` — rows in, one number out. No store, no clock, no socket.

**ATM only, and never to be called a DVOL equivalent.** DVOL and VIX integrate option
prices across the whole strike range weighted by `1/K^2`, capturing the distribution
rather than its centre, so they rise when skew steepens even with ATM flat. This one
tracks the level and is blind to shape. That is a deliberate trade for an instrument
whose subject is the level, and it carries the obligation not to label the output as
something it will predictably disagree with.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

#: Crypto trades weekends and this venue lists weekend expiries, so a year is 365 days
#: everywhere in this project. `forward.py` spells the same constant for the same reason;
#: a 1/252 year was measured overstating theta by 1.456x (`docs/greeks.md`).
DAYS_PER_YEAR = 365.0

#: No expiry under a week enters the index, under any circumstance.
#:
#: Near expiry an option's implied volatility becomes extremely noisy — vega collapses, so
#: a penny of price moves sigma a long way, and the forward itself stops being reliable.
#: This repo has already measured the front expiry's own forward fit implying a **43.1%**
#: rate at 1.139 days, outside its own 0-30% gate (`docs/greeks.md` section 6). VIX
#: excludes options under a week for exactly this reason.
#:
#: **The exclusion is reported, not silent.** With a tenor comfortably above the floor the
#: rule should never reach into the bracket at all, so `AtmIv.excluded_under_floor` says
#: how many expiries it removed. If that number ever starts mattering to the answer we
#: find out from the payload rather than from a chart that looks fine.
MIN_EXPIRY_DAYS = 7.0


@dataclass(frozen=True, slots=True)
class ContractIv:
    """One contract's stored implied volatility, with its chain's forward beside it.

    `forward` and `years_to_expiry` are per **chain** rather than per contract and are
    repeated down every row of an expiry — that is how `COMPUTED_SCHEMA` stores them, and
    it is what lets a row be checked on its own without a join to a table that does not
    exist.

    **The forward is read, never recomputed and never approximated by spot.** It is what
    decides which two strikes are at the money, and `docs/implied-vol.md` §2.1 measures
    the forward rather than the discount as the axis IV disagreement actually turns on.
    """

    expiry: str
    strike: float
    iv: float
    forward: float
    years_to_expiry: float


class AtmIv(BaseModel):
    """The index at one tenor, with the two expiries that produced it beside it.

    `near_days`, `far_days` and `near_weight` are carried so the roll is inspectable. As
    time passes the far expiry shrinks toward the tenor, `near_weight` walks to zero, and
    at the crossing the far expiry becomes the near one with the whole weight — a
    continuous hand-over rather than a step. A weight that jumped would say the roll was
    wrong, and without these fields nothing on the outside could tell.
    """

    tenor_days: float
    iv: float
    near_days: float
    far_days: float
    #: Weight on the nearer expiry's total variance. 1.0 when the tenor sits on it.
    near_weight: float
    #: Expiries the seven-day floor removed before any of this ran.
    excluded_under_floor: int


def _atm_for_one_expiry(rows: list[ContractIv]) -> float | None:
    """The IV at the forward, interpolated between its two bracketing strikes.

    **Not the nearest strike.** Delta lists strikes on a fixed grid and the forward
    almost never sits on one. Snapping to the nearest makes the series step discretely
    every time the forward crosses a strike midpoint — not because volatility moved but
    because a different contract started being read. On a smile with curvature that is a
    visible sawtooth of pure measurement artifact. Reading both neighbours costs one
    extra contract and removes it.
    """
    solved = sorted(
        {row.strike: row for row in rows if row.iv is not None}.values(),
        key=lambda row: row.strike,
    )
    if not solved:
        return None

    forward = solved[0].forward
    below = [row for row in solved if row.strike <= forward]
    above = [row for row in solved if row.strike >= forward]
    if not below or not above:
        # The forward sits outside the listed strikes. Extrapolating a smile past its
        # own wing is not an interpolation and is refused rather than guessed.
        return None

    lower, upper = below[-1], above[0]
    if lower.strike == upper.strike:
        return lower.iv
    weight = (forward - lower.strike) / (upper.strike - lower.strike)
    return lower.iv + weight * (upper.iv - lower.iv)


def _term_structure(
    rows: list[ContractIv],
) -> tuple[list[tuple[float, float]], int]:
    """`(days, atm_iv)` per expiry ascending, and how many the floor removed.

    Expiries that solved no ATM level are dropped silently — that is an ordinary absence,
    already explained by `iv_reason` on the row. Expiries under the floor are counted,
    because that removal is a decision of ours rather than a gap in the data.
    """
    by_expiry: dict[str, list[ContractIv]] = {}
    for row in rows:
        by_expiry.setdefault(row.expiry, []).append(row)

    points: list[tuple[float, float]] = []
    excluded = 0
    for expiry_rows in by_expiry.values():
        days = expiry_rows[0].years_to_expiry * DAYS_PER_YEAR
        if days < MIN_EXPIRY_DAYS:
            excluded += 1
            continue
        level = _atm_for_one_expiry(expiry_rows)
        if level is None:
            continue
        points.append((days, level))
    return sorted(points), excluded


def atm_iv(rows: list[ContractIv], *, tenor_days: float) -> AtmIv | None:
    """The ATM implied volatility at exactly `tenor_days`, or nothing.

    **Blended in total variance, never in volatility.** Variance is additive across time
    and volatility is not, so a straight line between two volatilities is a curve in the
    quantity that accumulates — and between two expiries it can imply *negative forward
    variance*, a term structure that can be arbitraged against itself. VIX and DVOL both
    interpolate in total variance for this reason.

    **`tenor_days` outside the listed term structure is refused rather than
    extrapolated.** Past the longest listed expiry there is no market opinion to read;
    returning a number there would be inventing one.

    **A minute whose bracketing data is absent returns `None`, never a fallback.** The
    same discipline as a leg with no volatility carrying no Greeks: five plausible numbers
    describing nothing is the failure this project keeps refusing.
    """
    points, excluded = _term_structure(rows)
    if not points:
        return None

    below = [point for point in points if point[0] <= tenor_days + 1e-9]
    above = [point for point in points if point[0] >= tenor_days - 1e-9]
    if not below or not above:
        return None

    (near_days, near_iv) = below[-1]
    (far_days, far_iv) = above[0]
    if abs(far_days - near_days) < 1e-9:
        return AtmIv(
            tenor_days=tenor_days, iv=near_iv, near_days=near_days,
            far_days=far_days, near_weight=1.0, excluded_under_floor=excluded,
        )

    near_variance = near_iv * near_iv * near_days
    far_variance = far_iv * far_iv * far_days
    weight = (tenor_days - near_days) / (far_days - near_days)
    total_variance = near_variance + weight * (far_variance - near_variance)
    if total_variance <= 0.0 or tenor_days <= 0.0:
        return None
    return AtmIv(
        tenor_days=tenor_days,
        iv=(total_variance / tenor_days) ** 0.5,
        near_days=near_days,
        far_days=far_days,
        near_weight=1.0 - weight,
        excluded_under_floor=excluded,
    )
