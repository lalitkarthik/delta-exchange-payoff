"""Where the two halves finally meet: implied against realised, at one lookback.

Pure. Bars and stored implied volatilities in, the payload the chart draws out. The store
is read by `main.py`, which is the only module allowed to touch a file.

**The web app does no arithmetic.** That is the rule the chain contract is built on, and
it is why `web/lib/contract.ts` mirrors `docs/chain-contract.md` field for field and the
page never calls `parseFloat`. So everything the chart needs arrives computed: both
series, already scaled to the window, already aligned or not according to the mode, and
with gaps already expressed as gaps rather than as missing keys a client has to infer.

**One lookback drives both sides.** `N` is the realised lookback *and* the implied
tenor. A ten-minute realised volatility against a thirty-day implied one is not a
volatility risk premium — it is two different questions on one axis, and the difference
between them would look like a signal. Binding them to one number makes that mistake
unavailable rather than merely discouraged.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

from pydantic import BaseModel

from .iv_index import MIN_EXPIRY_DAYS, ContractIv, atm_iv
from .realised_vol import (
    ESTIMATORS,
    Bar,
    RealisedVol,
    resample,
    rolling,
    scale_to_window,
)

#: Crypto trades weekends and this venue lists weekend expiries. Also in `forward.py`
#: and `iv_index.py`, for the same reason.
DAYS_PER_YEAR = 365.0

ALIGNMENTS = ("contemporaneous", "lag")

#: The two tables a realised series can come from, named on the response. `index-bars`
#: is the venue's own candles, backfilled; `spot-bars` is what our ticker channel
#: recorded. Never both in one series — see `VolatilitySeries.realised_source`.
INDEX_SOURCE = "index-bars"
SPOT_SOURCE = "spot-bars"

#: The fewest returns a window may rest on before the lower bound refuses it.
#:
#: A standard deviation from a handful of observations is mostly noise about itself, and
#: the chart's whole subject is a *difference* between two such numbers. Thirty is the
#: conventional floor and it is stated here rather than left implicit so that the lower
#: bound is a decision someone made rather than whatever fell out of the arithmetic.
MIN_OBSERVATIONS = 30

#: The sampling intervals a reader may choose between, by the name they go by on the wire.
#:
#: **The interval is offered, not imposed.** Realised volatility from one-minute returns
#: is biased upward by microstructure noise, and the classic result — Andersen et al.
#: (2001), Bandi-Russell (2005), Hansen-Lunde (2005) — puts the bias/variance optimum near
#: five minutes for equities. Our series is a *computed index* rather than a traded price,
#: so its noise profile may differ and the number should be measured here rather than
#: inherited. Until it has been, the reader picks and the screen says what was picked.
#:
#: `5s` is absent although the venue serves it: our own bars are minutes, so anything
#: below a minute would be a roll-up of one bar and a resolution we do not have.
INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


class VolPoint(BaseModel):
    """One timestamp, both series, and what each was computed from.

    **A point is emitted for every step in the range, whatever it could compute.** A
    minute with no answer carries `null`, not a missing key and not an omitted point:
    absence has to be visible in the payload or the client is left inferring a gap from
    a hole in an array, which reads identically to the series simply having ended.
    """

    at: datetime
    iv: float | None
    rv: dict[str, float | None]
    #: Returns (or bars, for the range estimators) behind each realised figure.
    returns: dict[str, int]
    #: `returns / expected` per estimator. Below one, the window had holes in it.
    coverage: dict[str, float]


class LookbackBounds(BaseModel):
    """What `N` is allowed to be, and which fact is stopping it being more.

    **The binding constraint is named because an empty chart otherwise reads as a bug.**
    In the first month of recording the history held is far shorter than the listed term
    structure, so the upper bound moves every day; a screen that cannot say *why* it
    refused looks broken rather than honest.
    """

    min_days: float
    max_days: float
    #: `"history"`, `"term_structure"`, `"observations"`, or `"none"` when nothing works.
    binding: str
    detail: str

    @property
    def usable(self) -> bool:
        return self.max_days >= self.min_days


class BoundsResponse(BaseModel):
    """`LookbackBounds` plus the intervals worth offering, for the controls to be built.

    Its own route because the slider has to be bounded *before* a lookback can be chosen
    inside those bounds. Without it the screen would guess, be refused, and have to read
    the bounds back out of a 400's prose — which makes an error message load-bearing.
    """

    min_days: float
    max_days: float
    binding: str
    detail: str
    usable: bool
    #: Every interval this store could serve. Which of them are legitimate at a given
    #: lookback is `valid_intervals`, and the screen narrows the list as `N` moves.
    intervals: list[str]


class VolatilitySeries(BaseModel):
    """The whole response: both series, the controls that produced them, the bounds."""

    underlying: str
    lookback_days: float
    interval_seconds: float
    step_seconds: float
    alignment: str
    estimators: list[str]
    #: The intervals legitimate at *this* lookback, for the picker to offer. Not one
    #: derived value: which interval trades bias against variance best on a computed
    #: index is an open question, so the choice belongs to the reader.
    valid_intervals: list[str]
    bounds: LookbackBounds
    #: Which table the realised series was computed from — `index-bars` when the venue's
    #: own candles have been backfilled, `spot-bars` otherwise. **One or the other, end
    #: to end.** R1 measured the two disagreeing on the per-minute range on 16 of 16
    #: overlapping minutes, so a window straddling a seam between them would give an
    #: answer that depended on where it fell, with nothing on the point to attribute it.
    #: Named on the response rather than inferred, because Parkinson and Garman-Klass
    #: give measurably different figures from the two and a reader has to know which.
    realised_source: str
    #: How many of `points` carry a realised figure for at least one estimator, and how
    #: many carry an implied one. **The asymmetry is the point.** Realised can be
    #: backfilled for years; implied cannot be backfilled at all — Delta's history
    #: carries no IV and no bid/ask — so after a backfill this reads as thousands
    #: against dozens. A screen promising a comparison must say that in figures rather
    #: than leave a reader to infer it from a line that is mostly not there.
    realised_points: int
    implied_points: int
    points: list[VolPoint]


def _listed_tenors(iv_rows: dict[datetime, list[ContractIv]]) -> list[float]:
    """Every expiry's days-to-run seen anywhere in the range, past the seven-day floor."""
    tenors: set[float] = set()
    for rows in iv_rows.values():
        for row in rows:
            days = row.years_to_expiry * DAYS_PER_YEAR
            if days >= MIN_EXPIRY_DAYS:
                tenors.add(days)
    return sorted(tenors)


def lookback_bounds(
    *,
    spot_bars: list[Bar],
    iv_rows: dict[datetime, list[ContractIv]],
    interval: timedelta,
) -> LookbackBounds:
    """What `N` may be at this sampling interval, and what is stopping it being more.

    Three constraints, and **only the engine knows the third**:

    * the **observation floor** — below `MIN_OBSERVATIONS` sampling intervals a window
      has too few returns to estimate anything;
    * the **term structure** — outside the listed expiries the index extrapolates, which
      R3 refuses, so `N` cannot go past the longest expiry nor under the shortest;
    * the **history held** — a thirty-day window needs thirty days of bars, and in the
      first month of recording this is much the tightest of the three.

    The binding one is named so the screen can print *"limited by available history"*
    rather than rendering an empty chart that reads as a bug.
    """
    tenors = _listed_tenors(iv_rows)
    ordered = sorted(spot_bars, key=lambda bar: bar.at)
    history_days = (
        (ordered[-1].at - ordered[0].at).total_seconds() / 86_400.0
        if len(ordered) >= 2
        else 0.0
    )
    observation_floor = MIN_OBSERVATIONS * interval.total_seconds() / 86_400.0

    if not tenors:
        return LookbackBounds(
            min_days=observation_floor,
            max_days=0.0,
            binding="term_structure",
            detail=(
                "no listed expiry survives the seven-day floor, so there is no implied "
                "volatility to compare against at any lookback"
            ),
        )

    min_days = max(observation_floor, tenors[0])
    if min_days == observation_floor and observation_floor > tenors[0]:
        lower_reason = f"{MIN_OBSERVATIONS} returns at this sampling interval"
    else:
        lower_reason = f"the shortest listed expiry, {tenors[0]:.2f} days"

    if history_days <= tenors[-1]:
        binding = "history"
        max_days = history_days
        detail = (
            f"limited by available history: {history_days:.2f} days of bars are held, "
            f"against a term structure reaching {tenors[-1]:.2f} days. "
            f"Lower bound set by {lower_reason}."
        )
    else:
        binding = "term_structure"
        max_days = tenors[-1]
        detail = (
            f"limited by the listed term structure: the longest expiry is "
            f"{tenors[-1]:.2f} days out, and past it the index would extrapolate. "
            f"Lower bound set by {lower_reason}."
        )

    return LookbackBounds(
        min_days=min_days, max_days=max_days, binding=binding, detail=detail
    )


def valid_intervals(lookback: timedelta) -> list[str]:
    """Intervals that leave at least `MIN_OBSERVATIONS` returns inside `lookback`."""
    return [
        name
        for name, interval in INTERVALS.items()
        if lookback / interval >= MIN_OBSERVATIONS
    ]


def implied_over_window(annualised: float, tenor_days: float) -> float:
    """A Black-76 sigma, expressed over the window instead of over a year.

    `sigma * sqrt(N / 365)`. Implied volatility is annualised **by construction** — the
    model only balances when sigma and `T` share a calendar — while the realised side is a
    window standard deviation with no scaling up. Serving the first raw beside the second
    would put a 40% line above a 7% line and invite the whole difference to be read as a
    risk premium, when all of it would be a unit mismatch.

    **Neither series is annualised, and that is the deliberate choice.** Annualising is
    the convention and it hides `N` inside a constant, so two charts at different
    lookbacks would carry identical-looking axes while answering different questions.
    Expressed over the window instead, the chart reads *"the market expected an 11.5%
    move over N days; it delivered 9%"*.

    **A year is 365 days**, as everywhere else in this project: crypto trades weekends and
    this venue lists weekend expiries. A 1/252 year would inflate every implied point by
    `sqrt(365/252)` = 1.204 — a 20% premium conjured out of a calendar.
    """
    return annualised * (tenor_days / DAYS_PER_YEAR) ** 0.5


def _implied_at(
    minutes: list[datetime],
    iv_rows: dict[datetime, list[ContractIv]],
    at: datetime,
    tolerance: timedelta,
) -> list[ContractIv] | None:
    """The most recent stored minute at or before `at`, if it is not stale.

    **Backwards, and bounded.** An output step lands where the arithmetic puts it —
    `start` is the first bar plus the lookback, so a lookback of 10.01 days puts every
    point 24 seconds off a minute boundary — and demanding an exact key match there would
    make the implied line vanish for a reason nothing on screen could explain. Reading
    backwards is also the honest direction: what was implied at 12:00:24 is what was
    implied at 12:00, not what will be implied at 12:01.

    **`tolerance` is what stops this becoming a forward-fill.** Past it there is no
    current opinion, and the answer is nothing — the same rule the bars follow.
    """
    index = bisect_right(minutes, at) - 1
    if index < 0:
        return None
    found = minutes[index]
    if at - found > tolerance:
        return None
    return iv_rows[found]


def _window(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    return [bar for bar in bars if start <= bar.at <= end]


def _realised(
    bars: list[Bar],
    *,
    estimators: list[str],
    interval: timedelta,
    lookback: timedelta,
    timestamps: list[datetime],
    alignment: str,
) -> dict[str, list[RealisedVol]]:
    """Every requested estimator at every timestamp, one pass per estimator.

    **The window the alignment names.** Contemporaneous looks back over `[at - N, at]`;
    lag-aligned looks *forward* over `[at, at + N]`, so the implied volatility at `at`
    sits against the movement that actually arrived after it. Both are windows ending at
    some instant, so lag is the same computation asked at `at + N` — which is also why
    the last `N` of the record has no answer in that mode: the window has not finished
    happening, and that is reported as `None` on a point that exists rather than as an
    omitted point or a zero.

    `rolling` rather than one call per point: recomputing a fourteen-day window of minute
    bars from scratch costs about 15 ms, so a six-hundred-point chart would take
    twenty-seven seconds and never draw.
    """
    ends = (
        [at + lookback for at in timestamps]
        if alignment == "lag"
        else list(timestamps)
    )
    last_bar = bars[-1].at if bars else None
    answered = [
        last_bar is not None and end <= last_bar if alignment == "lag" else True
        for end in ends
    ]

    out: dict[str, list[RealisedVol]] = {}
    for name in estimators:
        series = rolling(
            bars,
            estimator=name,
            interval=interval,
            lookback=lookback,
            timestamps=ends,
        )
        out[name] = [
            result
            if ok
            else RealisedVol(estimator=name, value=None, observations=0, expected=0)
            for result, ok in zip(series, answered, strict=True)
        ]
    return out


def volatility_series(
    *,
    spot_bars: list[Bar],
    iv_rows: dict[datetime, list[ContractIv]],
    lookback: timedelta,
    interval: timedelta,
    estimators: list[str],
    alignment: str,
    start: datetime,
    end: datetime,
    step: timedelta,
    underlying: str = "BTC",
    bounds: LookbackBounds | None = None,
    realised_source: str = SPOT_SOURCE,
) -> VolatilitySeries:
    """Both series over `[start, end]`, one point every `step`."""
    if alignment not in ALIGNMENTS:
        raise ValueError(f"alignment must be one of {ALIGNMENTS}, not {alignment!r}")
    for name in estimators:
        if name not in ESTIMATORS:
            raise ValueError(f"no such estimator: {name!r}")

    # Rolled up **once**, not per point: the estimators must see bars at the interval
    # asked for, and re-rolling the same minutes at every step would be the same answer
    # computed as many times as there are points on the chart.
    ordered = resample(spot_bars, interval)
    tenor_days = lookback.total_seconds() / 86_400.0

    minutes = sorted(iv_rows)
    # One sampling interval, or a minute, whichever is longer. At `1m` sampling that is a
    # minute — the resolution the store actually holds — and it grows with the interval
    # so a reader asking for four-hourly points is not refused an opinion three hours old
    # when three hours old is the best that interval could ever mean.
    tolerance = max(interval, timedelta(minutes=1))

    timestamps: list[datetime] = []
    at = start
    while at <= end:
        timestamps.append(at)
        at += step

    realised = _realised(
        ordered, estimators=estimators, interval=interval,
        lookback=lookback, timestamps=timestamps, alignment=alignment,
    )

    points: list[VolPoint] = []
    for index, when in enumerate(timestamps):
        rows = _implied_at(minutes, iv_rows, when, tolerance)
        implied = atm_iv(rows, tenor_days=tenor_days) if rows else None
        points.append(
            VolPoint(
                at=when,
                iv=(
                    implied_over_window(implied.iv, tenor_days)
                    if implied is not None
                    else None
                ),
                rv={
                    name: scale_to_window(
                        realised[name][index].value, interval=interval, window=lookback
                    )
                    for name in estimators
                },
                returns={
                    name: realised[name][index].observations for name in estimators
                },
                coverage={name: realised[name][index].coverage for name in estimators},
            )
        )

    return VolatilitySeries(
        underlying=underlying,
        lookback_days=tenor_days,
        interval_seconds=interval.total_seconds(),
        step_seconds=step.total_seconds(),
        alignment=alignment,
        estimators=list(estimators),
        valid_intervals=valid_intervals(lookback),
        bounds=bounds
        or LookbackBounds(
            min_days=tenor_days, max_days=tenor_days,
            binding="none", detail="bounds not computed",
        ),
        realised_source=realised_source,
        # Counted from the points themselves rather than tracked while building them:
        # one definition of "carries a figure", applied after the fact, cannot drift
        # from what the payload actually contains the way a running tally could.
        realised_points=sum(
            1 for point in points if any(v is not None for v in point.rv.values())
        ),
        implied_points=sum(1 for point in points if point.iv is not None),
        points=points,
    )
