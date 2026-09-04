"""`GET /smile` — a day of stored implied volatility for one expiry.

Driven over HTTP through `TestClient`, the same seam the rest of the endpoint suite uses.
The store under it is built here, in `tmp_path`: nothing in this file reads `data/`.

**Every minute in this file is a literal.** Two tests in this suite have already detonated
on a calendar date with nobody touching the code, by deriving a year fraction from
`datetime.now()` against a hardcoded expiry. `/smile` reads stored numbers and computes no
clock of its own, so no assertion here depends on the wall clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltapayoff.bars import ComputedBar
from deltapayoff.compute import MODEL_VERSION
from deltapayoff.main import app, get_computed_store
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    BarStore,
    BarWriter,
)

MINUTE = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)


def computed_bar(
    *,
    minute: datetime = MINUTE,
    strike: float = 77600.0,
    option_type: str = "C",
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    iv: float | None = 0.43212345,
    iv_leg: str | None = "call",
    iv_reason: str | None = None,
    forward: float | None = 77590.43210987,
    model_version: str = MODEL_VERSION,
) -> ComputedBar:
    """One row of table C. Greeks travel with the volatility or not at all."""
    greeks: dict[str, float | None] = dict(
        delta=0.51234567, gamma=0.00012345, vega=31.41592653, theta=-8.2, rho=1.9
    )
    if iv is None:
        greeks = dict.fromkeys(greeks)
    return ComputedBar(
        symbol=f"{option_type}-{underlying}-{strike:.0f}-{expiry.replace('-', '')[:6]}",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute,
        iv=iv,
        iv_leg=iv_leg,
        iv_reason=iv_reason,
        forward=forward,
        discount=0.99997892,
        years_to_expiry=0.00114155,
        forward_method="F1+assumed-rate",
        model_version=model_version,
        **greeks,
    )


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[Callable[[BarStore], TestClient]]:
    """A TestClient whose smile endpoint reads the given store and nothing else.

    The lifespan never runs — `TestClient` is not entered as a context manager — so the
    app has no `BarWriter` and the override is the only thing that can answer.
    """

    def factory(store: BarStore) -> TestClient:
        app.dependency_overrides[get_computed_store] = lambda: store
        return TestClient(app)

    yield factory
    app.dependency_overrides.clear()


@pytest.fixture
def store(tmp_path: Path) -> BarStore:
    return BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)


def smile(client: TestClient, underlying: str = "BTC", expiry: str = "04-09-2026"):
    response = client.get("/smile", params={"underlying": underlying, "expiry": expiry})
    assert response.status_code == 200, response.text
    return response.json()


# --- the day, not the minute ----------------------------------------------------


def test_one_request_returns_every_stored_minute_for_that_expiry(
    make_client, store
) -> None:
    """The whole point of the endpoint. A per-minute read would put a network round trip
    inside every scrubber drag and, measured, would save 2.3 ms of the 6.8 ms it costs to
    read the lot."""
    minutes = [
        datetime(2026, 9, 4, 9, minute, tzinfo=timezone.utc) for minute in range(5)
    ]
    store.add(
        computed_bar(minute=when, strike=strike)
        for when in minutes
        for strike in (77000.0, 77600.0, 78000.0)
    )
    store.flush()

    body = smile(make_client(store))

    assert body["underlying"] == "BTC"
    assert body["expiry"] == "04-09-2026"
    assert [entry["minute"] for entry in body["minutes"]] == [
        "2026-09-04T09:00:00Z",
        "2026-09-04T09:01:00Z",
        "2026-09-04T09:02:00Z",
        "2026-09-04T09:03:00Z",
        "2026-09-04T09:04:00Z",
    ]
    assert [point["strike"] for point in body["minutes"][0]["points"]] == [
        77000.0,
        77600.0,
        78000.0,
    ]


# --- the guard that matters most: parquet AND the buffer ------------------------


def test_a_minute_still_in_the_buffer_reaches_the_wire_unflushed(
    make_client, store
) -> None:
    """Nothing is on disk at all. A parquet-only read answers with an empty series.

    This is the regression that would be invisible in every other test here: the shape
    would be right, the fields would be right, and only the newest minutes — the ones the
    screen's right edge is made of — would be missing.
    """
    store.add([computed_bar(minute=MINUTE, strike=77600.0, iv=0.4321)])
    assert store.buffered == 1, "the bar must be unflushed for this test to mean anything"

    body = smile(make_client(store))

    assert [entry["minute"] for entry in body["minutes"]] == ["2026-09-04T09:00:00Z"]
    assert body["minutes"][0]["points"][0]["iv"] == 0.4321


def test_the_series_is_the_union_of_disk_and_buffer_in_one_ascending_run(
    make_client, store
) -> None:
    """The flush boundary must not be visible in the response. The store flushes every
    five minutes, so the last five minutes of any live read are buffer-only — a seam
    there would put a hole at the right edge of the curve up to a full interval wide."""
    flushed = [datetime(2026, 9, 4, 9, m, tzinfo=timezone.utc) for m in (0, 1, 2)]
    store.add(computed_bar(minute=when) for when in flushed)
    assert store.flush() == 3

    buffered = [datetime(2026, 9, 4, 9, m, tzinfo=timezone.utc) for m in (3, 4)]
    store.add(computed_bar(minute=when) for when in buffered)

    body = smile(make_client(store))

    assert [entry["minute"] for entry in body["minutes"]] == [
        "2026-09-04T09:00:00Z",
        "2026-09-04T09:01:00Z",
        "2026-09-04T09:02:00Z",
        "2026-09-04T09:03:00Z",
        "2026-09-04T09:04:00Z",
    ]


def test_a_buffered_row_carries_the_same_fields_as_a_flushed_one(
    make_client, store
) -> None:
    """The two halves of the union are one contract, not two. A buffered point with a
    null volatility must carry its reason exactly as a flushed one does."""
    store.add(
        [
            computed_bar(
                minute=MINUTE, strike=78000.0, iv=None, iv_leg=None, iv_reason="NO_QUOTE"
            )
        ]
    )

    point = smile(make_client(store))["minutes"][0]["points"][0]

    assert point == {
        "strike": 78000.0,
        "iv": None,
        "iv_leg": None,
        "iv_reason": "NO_QUOTE",
    }


# --- what a point carries -------------------------------------------------------


def test_an_unsolved_strike_travels_as_a_null_carrying_its_reason(
    make_client, store
) -> None:
    """The screen has to tell a strike that was not solved from a strike that does not
    exist, and the only way it can is if the null arrives. Dropping the point would draw
    a line straight through the gap and let a reader take a number off it."""
    store.add(
        [
            computed_bar(strike=77000.0, iv=0.51, iv_leg="put"),
            computed_bar(
                strike=77500.0, iv=None, iv_leg=None, iv_reason="no two-sided quote"
            ),
            computed_bar(strike=78000.0, iv=0.49, iv_leg="call"),
        ]
    )
    store.flush()

    points = smile(make_client(store))["minutes"][0]["points"]

    assert [point["strike"] for point in points] == [77000.0, 77500.0, 78000.0]
    assert [point["iv"] for point in points] == [0.51, None, 0.49]
    assert points[1]["iv_reason"] == "no two-sided quote"
    assert points[1]["iv_leg"] is None, "no side produced a number there"


def test_a_solved_point_carries_the_leg_it_came_from_and_no_reason(
    make_client, store
) -> None:
    """`iv_leg` flips from put to call across the forward, and a reader who does not know
    that reads the change in the curve's character as a break in our arithmetic."""
    store.add(
        [
            computed_bar(strike=77000.0, iv=0.51, iv_leg="put"),
            computed_bar(strike=78000.0, iv=0.49, iv_leg="call"),
        ]
    )
    store.flush()

    points = smile(make_client(store))["minutes"][0]["points"]

    assert [point["iv_leg"] for point in points] == ["put", "call"]
    assert [point["iv_reason"] for point in points] == [None, None]


def test_a_paired_strike_is_one_point_and_not_two(make_client, store) -> None:
    """Table C's grain is the contract, so a paired strike stores two rows carrying the
    same volatility. The smile plots volatility against strike, so they are one point —
    and parity means the de-duplication is not choosing between two numbers."""
    store.add(
        [
            computed_bar(strike=77000.0, option_type="C", iv=0.51, iv_leg="put"),
            computed_bar(strike=77000.0, option_type="P", iv=0.51, iv_leg="put"),
        ]
    )
    store.flush()

    points = smile(make_client(store))["minutes"][0]["points"]

    assert [point["strike"] for point in points] == [77000.0]
    assert points[0]["iv_leg"] == "put"


def test_no_greeks_travel_with_the_curve(make_client, store) -> None:
    """They are stored beside these rows and are deliberately not served: the smile plots
    volatility, and five figures nothing on the screen reads would be five more chances
    for the client and the store to drift."""
    store.add([computed_bar()])
    store.flush()

    point = smile(make_client(store))["minutes"][0]["points"][0]

    assert set(point) == {"strike", "iv", "iv_leg", "iv_reason"}


# --- what a minute carries ------------------------------------------------------


def test_the_forward_travels_once_per_minute_and_moves_with_it(
    make_client, store
) -> None:
    """The offset axis and the reference line are both read off this number, so it has to
    arrive per curve rather than be inferred from spot or carried once for the day."""
    store.add(
        [
            computed_bar(minute=MINUTE, forward=77590.43),
            computed_bar(
                minute=datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc), forward=77612.11
            ),
        ]
    )
    store.flush()

    minutes = smile(make_client(store))["minutes"]

    assert [entry["forward"] for entry in minutes] == [77590.43, 77612.11]
    assert minutes[0]["discount"] == 0.99997892
    assert minutes[0]["years_to_expiry"] == 0.00114155
    assert minutes[0]["forward_method"] == "F1+assumed-rate"


def test_the_model_stamp_is_read_from_the_data(make_client, store) -> None:
    """Read off the rows, never hardcoded. The forward convention alone is worth up to
    3.9 vol points and this screen plots nothing but vol points."""
    store.add([computed_bar()])
    store.flush()

    body = smile(make_client(store))

    assert body["model_versions"] == [MODEL_VERSION]
    assert body["minutes"][0]["model_version"] == MODEL_VERSION


def test_a_response_spanning_two_stamps_reports_both(make_client, store) -> None:
    """A model change mid-day puts two differently computed curves on one axis. Reporting
    one of them silently is how a reader compares numbers that are not comparable."""
    later = datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc)
    store.add(
        [
            computed_bar(minute=MINUTE, model_version="F1 / S1 / ACT365 / mid-OTM"),
            computed_bar(minute=later, model_version="F2 / S1 / ACT365 / mid-OTM"),
        ]
    )
    store.flush()

    body = smile(make_client(store))

    assert body["model_versions"] == [
        "F1 / S1 / ACT365 / mid-OTM",
        "F2 / S1 / ACT365 / mid-OTM",
    ]
    assert body["minutes"][0]["model_version"] == "F1 / S1 / ACT365 / mid-OTM"
    assert body["minutes"][1]["model_version"] == "F2 / S1 / ACT365 / mid-OTM"


# --- absence is 200 and empty ---------------------------------------------------


def test_an_underlying_with_no_stored_partitions_is_an_empty_series(
    make_client, store
) -> None:
    """ETH is in the contract and is not being collected. A 404 would make the ordinary
    case of "we have not started storing that yet" render as a broken engine."""
    store.add([computed_bar(underlying="BTC")])
    store.flush()

    body = smile(make_client(store), underlying="ETH")

    assert body == {
        "underlying": "ETH",
        "expiry": "04-09-2026",
        "model_versions": [],
        "minutes": [],
    }


def test_an_expiry_that_was_never_stored_is_an_empty_series(make_client, store) -> None:
    """Same partition, different expiry — so this one is answered by a column rather than
    by a directory name, and it still must not be an error."""
    store.add([computed_bar(expiry="04-09-2026")])
    store.flush()

    body = smile(make_client(store), expiry="11-09-2026")

    assert body["minutes"] == []
    assert body["model_versions"] == []


def test_a_store_with_no_files_at_all_is_an_empty_series(make_client, store) -> None:
    """Before the first flush and with an empty buffer, both halves of the union are
    empty. "Nothing yet" is a legitimate answer to a legitimate question."""
    body = smile(make_client(store))

    assert body["minutes"] == []


def test_rows_from_a_neighbouring_expiry_do_not_reach_the_curve(
    make_client, store
) -> None:
    """One partition holds every expiry, so the expiry filter is the only thing keeping
    two boards off one axis."""
    store.add(
        [
            computed_bar(expiry="04-09-2026", strike=77000.0),
            computed_bar(expiry="11-09-2026", strike=77000.0),
        ]
    )
    store.flush()

    points = smile(make_client(store))["minutes"][0]["points"]

    assert len(points) == 1


# --- the error table ------------------------------------------------------------


@pytest.mark.parametrize("underlying", ["SOL", "BTCUSD", "xyz"])
def test_a_bad_underlying_is_400(make_client, store, underlying: str) -> None:
    response = make_client(store).get(
        "/smile", params={"underlying": underlying, "expiry": "04-09-2026"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("expiry", ["2026-09-04", "4-9-2026", "32-09-2026", "nonsense"])
def test_a_malformed_expiry_is_400(make_client, store, expiry: str) -> None:
    response = make_client(store).get(
        "/smile", params={"underlying": "BTC", "expiry": expiry}
    )
    assert response.status_code == 400


def test_a_missing_parameter_is_422_from_fastapi(make_client, store) -> None:
    response = make_client(store).get("/smile", params={"underlying": "BTC"})
    assert response.status_code == 422


def test_the_underlying_is_normalised_before_the_store_is_asked(
    make_client, store
) -> None:
    """`/chain` accepts `btc`; so does this, and the response echoes the normalised
    symbol so a client never has to remember which spelling it sent."""
    store.add([computed_bar(underlying="BTC")])
    store.flush()

    body = smile(make_client(store), underlying="btc")

    assert body["underlying"] == "BTC"
    assert len(body["minutes"]) == 1


# --- the wiring, with nothing overridden ----------------------------------------


def test_the_endpoint_reads_the_running_writers_own_store(
    monkeypatch, tmp_path: Path
) -> None:
    """No dependency override here — this is the one test that checks the seam itself.

    Every other test in this file injects a store, so all of them would keep passing if
    the endpoint were wired to a **fresh** `BarStore` over the same directory. That store
    would have an empty buffer and the response would silently lose the last five minutes
    on the live engine while the suite stayed green. The buffered bar below is the proof:
    it exists only in this writer's memory and in no file anywhere.
    """
    writer = BarWriter(BarStore(tmp_path))
    writer.computed_store.add([computed_bar(minute=MINUTE, strike=77600.0, iv=0.4321)])
    assert writer.computed_store.buffered == 1
    monkeypatch.setattr(app.state, "writer", writer, raising=False)

    body = smile(TestClient(app))

    assert [entry["minute"] for entry in body["minutes"]] == ["2026-09-04T09:00:00Z"]
    assert body["minutes"][0]["points"][0]["iv"] == 0.4321
