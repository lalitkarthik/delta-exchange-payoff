"""How long something took, reported the same way everywhere.

#4, #6 and #8 all state latency targets, so they all report through this. Two rules
worth stating once rather than four times:

**Median and p95, never mean.** A mean is dragged around by one slow run — a garbage
collection, a scheduler hiccup — and hides the shape underneath. The median says what
usually happens; the p95 says what happens when it does not.

**p95 by nearest rank.** The reported figure is a run that actually occurred, not an
interpolation between two runs that did.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Timing(BaseModel):
    """A latency summary, in milliseconds."""

    median_ms: float
    p95_ms: float
    runs: int


def summarise(samples: list[float]) -> Timing:
    """Median and p95 over sample durations in milliseconds."""
    if not samples:
        raise ValueError("summarise needs at least one sample; got none")

    ordered = sorted(samples)
    n = len(ordered)
    middle = n // 2
    median = ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    p95 = ordered[math.ceil(0.95 * n) - 1]
    return Timing(median_ms=median, p95_ms=p95, runs=n)


def time_it(fn: Callable[[], T], runs: int = 100) -> tuple[T, Timing]:
    """Call `fn` `runs` times, return its last value and the timing over all of them.

    `perf_counter` rather than `time()`: it is monotonic and has the resolution these
    functions need, where the wall clock has neither.
    """
    if runs < 1:
        raise ValueError(f"runs must be at least 1; got {runs}")

    samples: list[float] = []
    result: T | None = None
    for _ in range(runs):
        started = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - started) * 1000)
    return result, summarise(samples)  # type: ignore[return-value]
