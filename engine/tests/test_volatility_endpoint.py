"""`/volatility`, against a hand-fed source. Nothing here opens a socket or a file.

The route's job is validation, units and the contract — the arithmetic has its own suites
in `test_realised_vol.py`, `test_iv_index.py` and `test_volatility.py`. So the source is
stubbed and the assertions are about what a browser receives.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from deltapayoff.iv_index import ContractIv
from deltapayoff.main import app, get_volatility_source
from deltapayoff.realised_vol import Bar

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
START = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)


class StubSource:
    """Twenty days of minute bars and a flat 8/60-day term structure. No store."""

    def __init__(self, days: int = 20, *, skip: set[int] | None = None) -> None:
        self.days = days
        self.skip = skip or set()
        self.calls: list[str] = []

    def spot_bars(self, underlying: str, **_: object) -> list[Bar]:
        self.calls.append(underlying)
        return [
            Bar(
                at=START + n * MINUTE,
                open=80_000.0 + (10.0 if n % 2 else 0.0),
                high=80_015.0,
                low=79_995.0,
                close=80_000.0 + (10.0 if n % 2 else 0.0),
            )
            for n in range(self.days * 1440)
            if n not in self.skip
        ]

    def contract_ivs(
        self, underlying: str, **_: object
    ) -> dict[datetime, list[ContractIv]]:
        rows = [
            ContractIv(
                expiry=expiry,
                strike=strike,
                iv=level,
                forward=80_005.0,
                years_to_expiry=days / 365.0,
            )
            for expiry, days, level in (("near", 8.0, 0.45), ("far", 60.0, 0.40))
            for strike in (80_000.0, 80_500.0)
        ]
        # Hourly, which is enough for any step the tests below ask for.
        return {START + n * HOUR: rows for n in range(self.days * 24)}


@pytest.fixture
def client_factory() -> Iterator[object]:
    """A client over whichever source the test supplies. Overrides cleared after.

    #54 needs two sources — one holding a backfill and one without — where every test
    before it needed only the default, so the fixture became a factory rather than
    growing a parameter every existing test would have to pass.
    """

    def build(source: object) -> TestClient:
        app.dependency_overrides[get_volatility_source] = lambda: source
        return TestClient(app)

    yield build
    app.dependency_overrides.clear()


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory(StubSource())


def test_both_series_arrive_for_one_lookback(client: TestClient) -> None:
    response = client.get(
        "/volatility",
        params={
            "underlying": "BTC",
            "lookback_days": 10,
            "interval": "1h",
            "estimators": "log,parkinson",
            "alignment": "contemporaneous",
            "max_points": 20,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "BTC"
    assert body["lookback_days"] == 10
    assert body["alignment"] == "contemporaneous"
    assert body["estimators"] == ["log", "parkinson"]
    assert body["points"], "a lookback inside the bounds must produce points"

    point = body["points"][0]
    assert set(point["rv"]) == {"log", "parkinson"}
    assert set(point["returns"]) == {"log", "parkinson"}
    assert point["iv"] is not None


def test_the_response_names_the_bounds_and_the_constraint_that_binds(
    client: TestClient,
) -> None:
    """Twenty days of bars against a term structure reaching sixty: history binds."""
    body = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 10, "interval": "1h"},
    ).json()

    bounds = body["bounds"]
    assert bounds["binding"] == "history"
    assert "history" in bounds["detail"]
    assert bounds["min_days"] >= 8.0
    assert bounds["max_days"] < 20.0


def test_the_sampling_interval_and_the_return_count_are_both_reported(
    client: TestClient,
) -> None:
    """Both are decisions the reader must be able to see, so both are in the payload.

    Ten days at hourly sampling is 240 returns. Without the count on the wire the screen
    cannot show it, and an estimate from 240 returns and one from 12 look identical.
    """
    body = client.get(
        "/volatility",
        params={
            "underlying": "BTC", "lookback_days": 10,
            "interval": "1h", "estimators": "log",
        },
    ).json()

    assert body["interval_seconds"] == 3600
    assert body["step_seconds"] >= 3600
    assert body["valid_intervals"]
    assert body["points"][0]["returns"]["log"] == 240


def test_a_lookback_above_the_upper_bound_is_a_400_that_says_which_bound(
    client: TestClient,
) -> None:
    """Not an empty chart, and not a 500. A refusal that explains itself."""
    response = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 45, "interval": "1h"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "above the upper bound" in detail
    assert "history" in detail


def test_a_lookback_below_the_lower_bound_is_a_400_that_says_which_bound(
    client: TestClient,
) -> None:
    """Under the shortest listed expiry there is no implied volatility to compare with."""
    response = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 2, "interval": "1h"},
    )

    assert response.status_code == 400
    assert "below the lower bound" in response.json()["detail"]


def test_an_unknown_estimator_or_interval_is_refused_by_name(
    client: TestClient,
) -> None:
    """Including the one deliberately absent, so its absence is a stated answer."""
    for params, expected in (
        ({"estimators": "yang_zhang"}, "estimators must be drawn from"),
        ({"estimators": "garch"}, "estimators must be drawn from"),
        ({"interval": "7s"}, "interval must be one of"),
        ({"alignment": "sideways"}, "alignment must be one of"),
    ):
        response = client.get(
            "/volatility",
            params={
                "underlying": "BTC", "lookback_days": 10, "interval": "1h", **params
            },
        )
        assert response.status_code == 400, params
        assert expected in response.json()["detail"]


def test_lag_alignment_leaves_the_trailing_window_null_rather_than_absent(
    client: TestClient,
) -> None:
    """The key is there, the value is not, and it is never a zero."""
    body = client.get(
        "/volatility",
        params={
            "underlying": "BTC", "lookback_days": 10,
            "interval": "1h", "estimators": "log", "alignment": "lag",
        },
    ).json()

    trailing = body["points"][-1]
    assert trailing["iv"] is not None
    assert "log" in trailing["rv"]
    assert trailing["rv"]["log"] is None


def test_every_decimal_on_the_wire_is_a_json_number_or_null(
    client: TestClient,
) -> None:
    """The rule the chain contract is built on, applied to the new payload.

    The engine converts at the boundary and the web app never calls `parseFloat`. A
    number arriving as a string would either be parsed by the client — putting arithmetic
    on the wrong side of the contract — or rendered verbatim and quietly wrong.
    """
    import json

    body = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 10, "interval": "1h"},
    ).text

    numeric_fields = {
        "iv", "min_days", "max_days", "lookback_days", "interval_seconds",
        "step_seconds", "coverage", "returns", "rv",
    }

    def walk(node: object, key: str | None = None) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                walk(value, name)
        elif isinstance(node, list):
            for value in node:
                walk(value, key)
        elif key in numeric_fields:
            assert node is None or isinstance(node, (int, float)), (
                f"{key} arrived as {type(node).__name__}: {node!r}"
            )
            assert not isinstance(node, str)

    walk(json.loads(body))


def test_a_gap_in_the_bars_is_visible_in_the_payload_rather_than_inferred() -> None:
    """Coverage below one is the only thing distinguishing a holed window from a full one.

    Never-forward-fill means a gap is a missing row, so the two produce numbers that look
    the same. If the payload did not carry the coverage the client would have nothing to
    grey out and nothing to break the line on.
    """
    source = StubSource(skip=set(range(5_000, 5_600)))  # ten hours with no arrivals
    app.dependency_overrides[get_volatility_source] = lambda: source
    try:
        body = TestClient(app).get(
            "/volatility",
            params={
                "underlying": "BTC", "lookback_days": 10,
                "interval": "1h", "estimators": "log,parkinson",
            },
        ).json()
    finally:
        app.dependency_overrides.clear()

    holed = [point for point in body["points"] if point["coverage"]["log"] < 1.0]
    assert holed, "a ten-hour hole must show up as coverage below one somewhere"
    for point in holed:
        assert point["rv"]["log"] is not None, "computed from what exists"
        assert point["returns"]["log"] < 240


def test_the_bounds_can_be_asked_for_without_asking_for_a_series(
    client: TestClient,
) -> None:
    """The slider has to be bounded before a lookback can be chosen inside those bounds.

    Without this the screen would have to guess a lookback, be refused, and read the
    bounds out of the 400's prose — which would make an error message load-bearing.
    """
    response = client.get(
        "/volatility/bounds", params={"underlying": "BTC", "interval": "1h"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["binding"] == "history"
    assert body["min_days"] >= 8.0
    assert body["max_days"] < 20.0
    assert body["usable"] is True
    assert "1h" in body["intervals"]


def test_the_bounds_endpoint_says_so_when_nothing_is_usable() -> None:
    """Half a day of bars against a term structure starting at eight days.

    A 200 carrying `usable: false` rather than an error: the screen has a real answer to
    print, and "no lookback works yet" is information rather than a failure.
    """
    source = StubSource(days=1)
    app.dependency_overrides[get_volatility_source] = lambda: source
    try:
        body = TestClient(app).get(
            "/volatility/bounds", params={"underlying": "BTC", "interval": "1m"}
        ).json()
    finally:
        app.dependency_overrides.clear()

    assert body["usable"] is False
    assert body["binding"] == "history"
    assert "history" in body["detail"]


# ---------------------------------------------------------------------------
# #54: the realised series moves to the venue's own index candles.
# ---------------------------------------------------------------------------


class IndexSource(StubSource):
    """A store that holds backfilled `index-bars` as well as recorded `spot-bars`.

    The two series are given visibly different prices so a test cannot pass by reading
    the wrong one: the index bars sit 1,000 higher and move twice as far.
    """

    def index_bars(self, underlying: str, **_: object) -> list[Bar]:
        self.calls.append(f"index:{underlying}")
        return [
            Bar(
                at=START + n * MINUTE,
                open=81_000.0 + (20.0 if n % 2 else 0.0),
                high=81_030.0,
                low=80_990.0,
                close=81_000.0 + (20.0 if n % 2 else 0.0),
            )
            for n in range(self.days * 1440)
        ]


def test_the_realised_series_prefers_the_index_and_says_so(client_factory) -> None:
    """Both tables populated: the index wins, and the payload names it.

    R1 measured the venue's per-minute range wider than ours on 16 of 16 overlapping
    minutes (`docs/index-history.md` §5), consistent with our ~7,600 ticker frames a
    minute carrying only a few dozen *distinct* observations. The venue's bars are the
    less biased source for a range estimator, so they are preferred — and a reader must
    be able to tell which produced the line, because the two give measurably different
    answers to Parkinson and Garman-Klass.
    """
    client = client_factory(IndexSource())
    response = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 8.0, "interval": "1h"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["realised_source"] == "index-bars"


def test_without_a_backfill_the_recorded_series_still_answers(client_factory) -> None:
    """An empty `index-bars` falls back to `spot-bars` **whole**, and says so.

    Falling back is not mixing: the series is one source or the other end to end. A
    window straddling a seam between them would return an answer that depended on where
    it fell, and nothing on the point would attribute it.
    """
    client = client_factory(StubSource())
    response = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 8.0, "interval": "1h"},
    )

    assert response.status_code == 200
    assert response.json()["realised_source"] == "spot-bars"


def test_the_payload_states_how_thin_the_implied_side_is(client_factory) -> None:
    """Months of realised against minutes of implied, in figures, on the response.

    After the backfill the chart draws a long realised line against an implied line that
    is null nearly everywhere — `_implied_at`'s tolerance is `max(interval, 1 minute)`
    and refuses anything staler, deliberately, so that it cannot become a forward-fill.
    One confident line on a screen whose title promises a comparison is the exact
    misreading the screen exists to prevent, so the asymmetry is part of the contract
    rather than something the client is left to infer from counting nulls.
    """
    client = client_factory(IndexSource())
    body = client.get(
        "/volatility",
        params={"underlying": "BTC", "lookback_days": 8.0, "interval": "1h"},
    ).json()

    assert body["implied_points"] + body["realised_points"] > 0
    assert body["implied_points"] <= len(body["points"])
    assert body["realised_points"] <= len(body["points"])
