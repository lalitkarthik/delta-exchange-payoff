"""Shared fixtures. Nothing in this suite touches the network.

The three JSON fixtures under `tests/fixtures/`:

* `tickers-btc-04-09-2026.json` — a verbatim `GET /v2/tickers` response for
  `contract_types=call_options,put_options&underlying_asset_symbols=BTC&expiry_date=04-09-2026`,
  captured from production on 2026-09-01. 128 tickers, spot 77568.2.
* `tickers-btc-all-expiries.json` — the same call without `expiry_date`, subsetted to
  one call and one put per listed expiry so the file stays small. Real rows, untouched.
* `tickers-btc-multi-expiry.json` — a verbatim `GET /v2/tickers` for BTC options with
  no `expiry_date` filter, captured from production on 2026-09-02T08:40:14Z. 588
  contracts across eight expiries, half a day to 85 days out, spot 77874.2. This is the
  fixture the agreement matrix slices by time to expiry; the chain capture above is one
  expiry and cannot.
* `ws-ticker-04-09-2026.json`, `ws-ob-l2-04-09-2026.json`, `rest-04-09-2026.json` —
  captured together on 2026-09-03 by `tools/capture_ws.py`. One verbatim websocket frame
  per symbol on each channel for the 04-09-2026 BTC chain, 136 symbols, plus the REST
  response for the same expiry taken alongside. The pairing is the point: it lets the
  same contracts be read two ways so the wire decoder can be checked rather than assumed.
* `tickers-absent-quotes.json` — three rows lifted from the chain capture and then
  hand-edited to carry the absent-value spellings Delta uses: `"0"`, `""` and `null`.
  Delta's live snapshots quote every strike, so the edge cases have to be constructed.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(autouse=True)
def no_live_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's start-up subscribes every live BTC option over a websocket. No test may.

    Set before any `TestClient(app)` runs the lifespan, so the REST endpoints and
    `/ws/chain` are exercised against a hand-fed `ChainStream` instead of Delta.
    """
    monkeypatch.setenv("DELTA_LIVE_FEED", "0")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard stop: a test that opens a real client fails instead of dialling out."""
    from deltapayoff import delta_client

    def refuse() -> None:
        raise AssertionError("a test tried to open a live Delta client")

    monkeypatch.setattr(delta_client, "_new_async_client", refuse)


@pytest.fixture
def chain_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-btc-04-09-2026.json")["result"]


@pytest.fixture
def all_expiry_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-btc-all-expiries.json")["result"]


@pytest.fixture
def absent_quote_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-absent-quotes.json")["result"]


@pytest.fixture
def multi_expiry_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-btc-multi-expiry.json")["result"]


@pytest.fixture
def ws_ticker_frames() -> dict[str, Any]:
    return load_fixture("ws-ticker-04-09-2026.json")["frames"]


@pytest.fixture
def ws_book_frames() -> dict[str, Any]:
    return load_fixture("ws-ob-l2-04-09-2026.json")["frames"]


@pytest.fixture
def rest_snapshot() -> list[dict[str, Any]]:
    return load_fixture("rest-04-09-2026.json")["result"]

@pytest.fixture
def ws_captured_at(ws_ticker_frames) -> datetime:
    """The instant the websocket fixtures were captured, read off the frames.

    A snapshot has to be priced as of when it was taken. `chain_from_frames` defaults
    `fetched_at` to the current clock, so a test that omits it silently re-dates a
    2026-09-03 capture to today — and `year_fraction` measures from `fetched_at` to
    settlement. The 04-09-2026 fixture then walks its own time to expiry down to nothing.

    That is not hypothetical. It fired on 2026-09-04: the fitted discount's implied rate
    reached 38.8% and `f1_parity_fit` refused the chain, failing a test that had asserted
    a trusted forward since T4 without anyone touching the code.

    Taken from the frames' own `ts` rather than the fixture's `captured` date string,
    because `ts` is microsecond-exact and cannot drift from the data beside it.
    """
    stamps = [frame["ts"] for frame in ws_ticker_frames.values() if frame.get("ts")]
    return datetime.fromtimestamp(max(stamps) / 1e6, tz=timezone.utc)
