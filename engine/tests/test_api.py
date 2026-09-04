"""The two endpoints and the error table, with Delta stubbed out entirely."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from deltapayoff.delta_client import DeltaUnavailable, parse_envelope
from deltapayoff.main import app, get_delta_client


class StubDelta:
    """Stands in for DeltaClient. Records what was asked for; never opens a socket."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.raises = raises
        self.calls: list[tuple[str, str | None]] = []

    async def tickers(
        self, underlying: str, expiry: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((underlying, expiry))
        if self.raises is not None:
            raise self.raises
        return self.rows


@pytest.fixture
def make_client() -> Iterator[Callable[[StubDelta], TestClient]]:
    """A TestClient whose Delta dependency is the given stub.

    TestClient is not entered as a context manager, so the app lifespan never runs and
    no real httpx client is ever constructed.
    """

    def factory(stub: StubDelta) -> TestClient:
        app.dependency_overrides[get_delta_client] = lambda: stub
        return TestClient(app)

    yield factory
    app.dependency_overrides.clear()


# --- happy paths ----------------------------------------------------------------


def test_expiries_endpoint(make_client, all_expiry_tickers) -> None:
    stub = StubDelta(all_expiry_tickers)
    response = make_client(stub).get("/expiries", params={"underlying": "BTC"})
    assert response.status_code == 200
    assert response.json() == {
        "underlying": "BTC",
        "expiries": [
            "02-09-2026",
            "03-09-2026",
            "04-09-2026",
            "11-09-2026",
            "18-09-2026",
            "25-09-2026",
            "30-10-2026",
            "27-11-2026",
        ],
    }
    assert stub.calls == [("BTC", None)], "no expiry_date filter when listing expiries"


def test_chain_endpoint(make_client, chain_tickers) -> None:
    stub = StubDelta(chain_tickers)
    response = make_client(stub).get(
        "/chain", params={"underlying": "BTC", "expiry": "04-09-2026"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["underlying"] == "BTC"
    assert body["expiry"] == "04-09-2026"
    assert body["spot"] == 77568.2
    assert body["atm_strike"] == 77600.0
    assert body["fetched_at"].endswith("Z")
    assert len(body["rows"]) == 65
    assert stub.calls == [("BTC", "04-09-2026")]

    row = body["rows"][0]
    assert set(row) == {"strike", "call", "put"}
    assert set(row["put"]) == {
        "symbol", "product_id", "bid", "ask", "mark", "bid_iv", "ask_iv", "mark_iv",
        "delta", "gamma", "theta", "vega", "rho", "oi", "oi_value_usd",
        "oi_change_usd_6h", "tick_size",
        # Ours, added beside Delta's rather than replacing any of them. Every name
        # above is still the venue's own figure.
        "computed",
    }
    assert set(row["put"]["computed"]) == {
        "iv", "iv_leg", "iv_reason", "delta", "gamma", "vega", "theta", "rho",
    }


def test_underlying_is_case_insensitive(make_client, all_expiry_tickers) -> None:
    stub = StubDelta(all_expiry_tickers)
    response = make_client(stub).get("/expiries", params={"underlying": "btc"})
    assert response.status_code == 200
    assert response.json()["underlying"] == "BTC"
    assert stub.calls == [("BTC", None)], "Delta is queried with the normalised symbol"


def test_cors_allows_the_next_dev_server(make_client, all_expiry_tickers) -> None:
    response = make_client(StubDelta(all_expiry_tickers)).get(
        "/expiries",
        params={"underlying": "BTC"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


# --- the error table ------------------------------------------------------------


@pytest.mark.parametrize("underlying", ["SOL", "BTCUSD", "xyz"])
def test_bad_underlying_is_400(make_client, underlying: str) -> None:
    stub = StubDelta([])
    response = make_client(stub).get("/expiries", params={"underlying": underlying})
    assert response.status_code == 400
    assert "detail" in response.json()
    assert stub.calls == [], "a bad parameter never reaches Delta"


@pytest.mark.parametrize("expiry", ["2026-09-04", "4-9-2026", "32-09-2026", "nonsense"])
def test_malformed_expiry_is_400(make_client, expiry: str) -> None:
    stub = StubDelta([])
    response = make_client(stub).get(
        "/chain", params={"underlying": "BTC", "expiry": expiry}
    )
    assert response.status_code == 400
    assert stub.calls == []


def test_missing_parameter_is_422_from_fastapi(make_client) -> None:
    """A parameter that is absent altogether is FastAPI's own validation, not ours."""
    response = make_client(StubDelta([])).get("/chain", params={"underlying": "BTC"})
    assert response.status_code == 422


def test_no_contracts_for_that_pair_is_404(make_client) -> None:
    response = make_client(StubDelta([])).get(
        "/chain", params={"underlying": "BTC", "expiry": "01-01-2030"}
    )
    assert response.status_code == 404
    assert "01-01-2030" in response.json()["detail"]


def test_no_contracts_for_that_underlying_is_404(make_client) -> None:
    response = make_client(StubDelta([])).get("/expiries", params={"underlying": "ETH"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "failure",
    [
        DeltaUnavailable("Delta timed out after 10s"),
        DeltaUnavailable("Delta was unreachable: connection refused"),
        DeltaUnavailable("Delta answered HTTP 400 without success"),
    ],
)
def test_upstream_failure_is_502(make_client, failure: Exception) -> None:
    for path, params in (
        ("/expiries", {"underlying": "BTC"}),
        ("/chain", {"underlying": "BTC", "expiry": "04-09-2026"}),
    ):
        response = make_client(StubDelta(raises=failure)).get(path, params=params)
        assert response.status_code == 502
        assert response.json()["detail"] == str(failure)


# --- the Delta envelope ---------------------------------------------------------


def _response(status: int, body: Any) -> httpx.Response:
    request = httpx.Request("GET", "https://api.india.delta.exchange/v2/tickers")
    if isinstance(body, (dict, list)):
        return httpx.Response(status, json=body, request=request)
    return httpx.Response(status, text=body, request=request)


def test_parse_envelope_unwraps_result() -> None:
    rows = parse_envelope(_response(200, {"success": True, "result": [{"symbol": "x"}]}))
    assert rows == [{"symbol": "x"}]


def test_parse_envelope_rejects_success_false() -> None:
    """Delta's failure envelope carries `error` as a bare string here, not an object."""
    body = {"success": False, "error": "Invalid date format. Expected format: DD-MM-YYYY"}
    with pytest.raises(DeltaUnavailable, match="Invalid date format"):
        parse_envelope(_response(400, body))


def test_parse_envelope_rejects_success_false_with_an_error_object() -> None:
    body = {"success": False, "error": {"code": "unavailable"}}
    with pytest.raises(DeltaUnavailable, match="unavailable"):
        parse_envelope(_response(500, body))


def test_parse_envelope_rejects_a_non_json_body() -> None:
    """A request with no User-Agent gets an HTML 403 from Delta's edge, not JSON."""
    with pytest.raises(DeltaUnavailable, match="non-JSON"):
        parse_envelope(_response(403, "<HTML><HEAD><TITLE>ERROR</TITLE>"))


def test_parse_envelope_rejects_a_missing_result_list() -> None:
    with pytest.raises(DeltaUnavailable, match="no result list"):
        parse_envelope(_response(200, {"success": True}))


def test_parse_envelope_drops_non_object_rows() -> None:
    rows = parse_envelope(_response(200, {"success": True, "result": [{"a": 1}, "junk"]}))
    assert rows == [{"a": 1}]
