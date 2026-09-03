"""The agreement matrix: how far apart two methods land, and where.

Implied volatility is not observable. It is inverted out of a price under assumptions,
so there is no ground truth to grade against, and **agreement between independent
methods is the only evidence available**. Where two methods agree, the assumption
separating them is not carrying risk and the cheaper one wins. Where they diverge, the
divergence names the assumption that is.

`dIV` here is always method against method, **never distance from Delta's published
figures** — see `tests/test_no_delta_inputs.py`, which corrupts every number Delta
publishes and asserts nothing downstream moves.

A whole-chain median hides the interesting part, because disagreement is not uniform. It
concentrates where vega is small, which is the wings and the front expiry. So everything
here is sliced two ways: by moneyness, and by time to expiry.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from .chain import EXPIRY_FORMAT, build_chain, expiry_from_symbol
from .forward import year_fraction

#: Moneyness bands on `K/F`. The boundaries are not tuned — they are the natural
#: divisions of an option chain, and the study reports how each behaves rather than
#: choosing between them.
MONEYNESS_BANDS = (
    ("deep ITM put side", 0.00, 0.90),
    ("OTM put side", 0.90, 0.98),
    ("at the money", 0.98, 1.02),
    ("OTM call side", 1.02, 1.10),
    ("deep OTM call side", 1.10, 99.0),
)

#: Time bands in days. The front expiry is separated because `sqrt(T)` multiplies every
#: vega in the book, so a half-day contract has an order of magnitude less of it than a
#: monthly one at the same moneyness.
TIME_BANDS = (
    ("under a day", 0.0, 1.0),
    ("1 to 7 days", 1.0, 7.0),
    ("7 to 30 days", 7.0, 30.0),
    ("over 30 days", 30.0, 9_999.0),
)


class Agreement(BaseModel):
    """How far apart two methods landed, in vol points, over one slice."""

    left: str
    right: str
    slice_name: str
    n: int
    median: float
    p95: float
    worst: float

    @property
    def within_target(self) -> bool:
        """T2's target is 0.1 vol points between methods, read at p95."""
        return self.p95 <= 0.1


def band_for(value: float, bands) -> str:
    for name, low, high in bands:
        if low <= value < high:
            return name
    return bands[-1][0]


def compare(left_name: str, left: dict[float, float],
            right_name: str, right: dict[float, float],
            slice_name: str = "all") -> Agreement | None:
    """Pairwise `dIV` in **vol points**, over the strikes both methods solved.

    Restricting to the common set is deliberate. A method that declines a strike has not
    disagreed about it — it has declined, which is a separate fact reported separately.
    Scoring a refusal as a disagreement would make the most careful solver look worst.
    """
    common = sorted(set(left) & set(right))
    if not common:
        return None
    gaps = sorted(abs(left[k] - right[k]) * 100.0 for k in common)
    index = max(int(0.95 * len(gaps)) - 1, 0)
    return Agreement(
        left=left_name,
        right=right_name,
        slice_name=slice_name,
        n=len(gaps),
        median=statistics.median(gaps),
        p95=gaps[index],
        worst=gaps[-1],
    )


def chains_by_expiry(
    underlying: str, tickers: list[dict[str, Any]], fetched_at: datetime
) -> dict[str, Any]:
    """Split a multi-expiry ticker dump into one chain per expiry, ascending by date.

    `/v2/tickers` returns every listed contract in one response with no grouping; the
    expiry lives only in the symbol suffix. This is the same parse `/expiries` does.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ticker in tickers:
        expiry = expiry_from_symbol(ticker.get("symbol", ""))
        if expiry is not None:
            grouped.setdefault(expiry, []).append(ticker)

    ordered = sorted(grouped, key=lambda e: datetime.strptime(e, EXPIRY_FORMAT))
    return {
        expiry: build_chain(underlying, expiry, rows, fetched_at=fetched_at)
        for expiry, rows in ((e, grouped[e]) for e in ordered)
    }


def days_to_expiry(chain) -> float:
    """Calendar days from snapshot to settlement — `year_fraction` in readable units."""
    return year_fraction(chain) * 365.0


def utc(text: str) -> datetime:
    """`2026-09-02T08:40:14Z` to an aware datetime. Snapshot times, not the wall clock."""
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
