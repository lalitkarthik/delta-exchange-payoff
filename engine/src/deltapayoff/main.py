"""The FastAPI app. Two endpoints, both fixed by `docs/chain-contract.md`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .chain import (
    ValidationError,
    build_chain,
    build_expiries,
    normalise_underlying,
    validate_expiry,
)
from .delta_client import DeltaClient, DeltaUnavailable
from .models import ChainResponse, ExpiriesResponse

#: The Next.js dev server. Development only; production origins are a deploy concern.
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One HTTP client for the process, so connections are reused across requests."""
    client = DeltaClient()
    await client.__aenter__()
    app.state.delta = client
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="delta-exchange-payoff engine",
    version="0.1.0",
    summary="Delta Exchange option chain, pivoted. Computes nothing else.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_delta_client() -> DeltaClient:
    """Overridden in tests so nothing here ever reaches the network."""
    return app.state.delta


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Says nothing about Delta."""
    return {"status": "ok"}


@app.get("/expiries", response_model=ExpiriesResponse)
async def expiries(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    delta: Annotated[DeltaClient, Depends(get_delta_client)],
) -> ExpiriesResponse:
    """Every listed expiry for one underlying, ascending. Source of the dropdown."""
    symbol = _validated(normalise_underlying, underlying)
    tickers = await _fetch(delta, symbol, None)
    if not tickers:
        raise HTTPException(
            status_code=404, detail=f"Delta lists no option contracts for {symbol}"
        )
    return build_expiries(symbol, tickers)


@app.get("/chain", response_model=ChainResponse)
async def chain(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    expiry: Annotated[str, Query(description="DD-MM-YYYY, as Delta spells it")],
    delta: Annotated[DeltaClient, Depends(get_delta_client)],
) -> ChainResponse:
    """The pivoted ladder for one underlying and one expiry."""
    symbol = _validated(normalise_underlying, underlying)
    date = _validated(validate_expiry, expiry)
    tickers = await _fetch(delta, symbol, date)
    if not tickers:
        raise HTTPException(
            status_code=404,
            detail=f"Delta lists no option contracts for {symbol} expiring {date}",
        )
    return build_chain(symbol, date, tickers)


def _validated(check: Callable[[str], str], value: str) -> str:
    try:
        return check(value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _fetch(
    delta: DeltaClient, underlying: str, expiry: str | None
) -> list[dict[str, Any]]:
    try:
        return await delta.tickers(underlying, expiry)
    except DeltaUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
