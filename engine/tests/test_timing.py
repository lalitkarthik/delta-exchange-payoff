"""The timing harness. #4, #6 and #8 all report through this, so it is tested on
hand-checked sample lists rather than on the clock — a percentile is arithmetic, and
arithmetic is what can be wrong.
"""

from __future__ import annotations

import pytest

from deltapayoff.timing import Timing, summarise, time_it


def test_summarise_reports_a_hand_checked_median_and_p95() -> None:
    """Ten samples, ascending. Median is the mean of the 5th and 6th = 55.0.

    p95 by nearest rank is ceil(0.95 * 10) = the 10th sample = 100.0. Nearest rank is
    chosen over interpolation because a p95 should be a value that actually happened.
    """
    samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]

    result = summarise(samples)

    assert result.median_ms == 55.0
    assert result.p95_ms == 100.0
    assert result.runs == 10


def test_summarise_is_order_independent() -> None:
    """The same ten samples shuffled must summarise identically — a percentile that
    reads the list in arrival order is the classic bug here."""
    shuffled = [70.0, 10.0, 100.0, 40.0, 90.0, 20.0, 60.0, 30.0, 80.0, 50.0]

    result = summarise(shuffled)

    assert result.median_ms == 55.0
    assert result.p95_ms == 100.0


def test_p95_on_a_hundred_samples_is_the_ninety_fifth() -> None:
    """1..100 ms. ceil(0.95 * 100) = 95, so p95 is 95.0 and not 95.5 or 96.0."""
    result = summarise([float(i) for i in range(1, 101)])

    assert result.median_ms == 50.5
    assert result.p95_ms == 95.0


def test_time_it_runs_the_callable_once_per_run_and_returns_its_value() -> None:
    """A harness that timed one call and reported it as ten would look identical in the
    output. Counting the calls is the only way to catch that."""
    calls = []

    def work() -> str:
        calls.append(1)
        return "answer"

    result, timing = time_it(work, runs=7)

    assert len(calls) == 7
    assert result == "answer"
    assert timing.runs == 7


def test_summarise_refuses_an_empty_sample_list() -> None:
    """No samples is a caller bug, not a zero. Reporting 0.0 ms would read as 'fast'."""
    with pytest.raises(ValueError):
        summarise([])


def test_timing_is_serialisable_alongside_a_result() -> None:
    """#4, #6 and #8 report timings on the wire next to the numbers they timed."""
    dumped = Timing(median_ms=1.5, p95_ms=2.5, runs=3).model_dump()

    assert dumped == {"median_ms": 1.5, "p95_ms": 2.5, "runs": 3}
