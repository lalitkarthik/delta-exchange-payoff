"""The one place this service talks to Delta.

Production market data only, no API key — `/v2/tickers` is public. Testnet is never
used: its strike ladders no longer span spot, so nothing is read from it.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import __version__

BASE_URL = "https://api.india.delta.exchange"
TICKERS_PATH = "/v2/tickers"
TIMEOUT_SECONDS = 10.0

#: Delta answers 4XX to a request with no User-Agent, so every request carries one.
USER_AGENT = f"delta-exchange-payoff/{__version__} (+chain engine)"

OPTION_CONTRACT_TYPES = "call_options,put_options"


class DeltaUnavailable(RuntimeError):
    """Delta was unreachable, timed out, or answered `success: false`. Maps to 502."""


class DeltaClient:
    """A thin wrapper over `GET /v2/tickers`, the only source of a chain."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owned = client is None

    async def __aenter__(self) -> DeltaClient:
        if self._client is None:
            self._client = _new_async_client()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def tickers(
        self, underlying: str, expiry: str | None = None
    ) -> list[dict[str, Any]]:
        """Option tickers for one underlying, optionally narrowed to one expiry.

        Omitting `expiry` returns every listed expiry, which is how `/expiries` is built.
        """
        params: dict[str, str] = {
            "contract_types": OPTION_CONTRACT_TYPES,
            "underlying_asset_symbols": underlying,
        }
        if expiry is not None:
            params["expiry_date"] = expiry

        if self._client is None:
            self._client = _new_async_client()

        try:
            response = await self._client.get(TICKERS_PATH, params=params)
        except httpx.TimeoutException as exc:
            raise DeltaUnavailable(f"Delta timed out after {TIMEOUT_SECONDS:g}s") from exc
        except httpx.HTTPError as exc:
            raise DeltaUnavailable(f"Delta was unreachable: {exc}") from exc

        return parse_envelope(response)


def parse_envelope(response: httpx.Response) -> list[dict[str, Any]]:
    """Unwrap `{"success": true, "result": [...]}`, or raise :class:`DeltaUnavailable`.

    On failure Delta answers `{"success": false, ...}`. The `error` member is sometimes
    an object and sometimes a bare string, so it is stringified rather than indexed.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise DeltaUnavailable(
            f"Delta returned HTTP {response.status_code} with a non-JSON body"
        ) from exc

    if not isinstance(payload, dict) or not payload.get("success"):
        detail = ""
        if isinstance(payload, dict) and payload.get("error") is not None:
            detail = f": {payload['error']}"
        raise DeltaUnavailable(
            f"Delta answered HTTP {response.status_code} without success{detail}"
        )

    result = payload.get("result")
    if not isinstance(result, list):
        raise DeltaUnavailable("Delta returned a success envelope with no result list")
    return [row for row in result if isinstance(row, dict)]


def _new_async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
