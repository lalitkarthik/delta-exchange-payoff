"""Shared fixtures. Nothing in this suite touches the network.

The three JSON fixtures under `tests/fixtures/`:

* `tickers-btc-04-09-2026.json` — a verbatim `GET /v2/tickers` response for
  `contract_types=call_options,put_options&underlying_asset_symbols=BTC&expiry_date=04-09-2026`,
  captured from production on 2026-09-01. 128 tickers, spot 77568.2.
* `tickers-btc-all-expiries.json` — the same call without `expiry_date`, subsetted to
  one call and one put per listed expiry so the file stays small. Real rows, untouched.
* `tickers-absent-quotes.json` — three rows lifted from the chain capture and then
  hand-edited to carry the absent-value spellings Delta uses: `"0"`, `""` and `null`.
  Delta's live snapshots quote every strike, so the edge cases have to be constructed.
"""

from __future__ import annotations

import json
import sys
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
