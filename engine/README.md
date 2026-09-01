# engine

FastAPI. Fetches Delta's tickers, pivots them into an option chain, computes nothing else.

The shape it serves is fixed by [`../docs/chain-contract.md`](../docs/chain-contract.md).
That file is the contract; this is one implementation of it.

## Install

Python 3.13. From `engine/`:

```sh
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt   # POSIX
```

`requirements.txt` is the runtime — FastAPI, httpx, uvicorn. `requirements-dev.txt`
pulls that in and adds pytest and ruff. No API key, no `.env`: Delta's `/v2/tickers` is
public market data.

## Run

```sh
.venv/Scripts/python.exe -m uvicorn --app-dir src deltapayoff.main:app --port 8000 --reload
```

Port **8000**. The web app calls it there, and CORS is open to `http://localhost:3000`
and `http://127.0.0.1:3000` — the Next.js dev server — and to nothing else.

`--app-dir src` is what puts the package on the path; there is no install step for the
package itself.

| Route | |
|---|---|
| `GET /expiries?underlying=BTC` | Every listed expiry, ascending. Source of the dropdown. |
| `GET /chain?underlying=BTC&expiry=04-09-2026` | The pivoted ladder for one expiry. |
| `GET /health` | Liveness. Says nothing about Delta. |
| `GET /docs` | FastAPI's generated reference. |

## Test

```sh
.venv/Scripts/python.exe -m pytest
.venv/Scripts/python.exe -m ruff check .
```

**No test touches the network.** An autouse fixture in `tests/conftest.py` replaces the
async client factory with one that raises, so a test that tries to dial out fails rather
than quietly succeeding against live data. The endpoint tests stub the Delta dependency;
the pivot tests run over captured JSON.

Three fixtures under `tests/fixtures/`, all from production on 2026-09-01:

| File | |
|---|---|
| `tickers-btc-04-09-2026.json` | A verbatim tickers response for one expiry. 128 tickers, 65 strikes, spot 77568.2. |
| `tickers-btc-all-expiries.json` | The same call without `expiry_date`, subsetted to one call and one put per expiry. Rows untouched. |
| `tickers-absent-quotes.json` | Three rows from the first file, hand-edited to carry Delta's absent-value spellings. Delta quotes every strike in practice, so these have to be constructed. |

## Layout

```
src/deltapayoff/
  main.py           FastAPI app, routes, CORS, error mapping.
  delta_client.py   The only code that talks to Delta. Envelope unwrapping lives here.
  chain.py          The pivot, atm_strike, expiry parsing. Pure — no network, no I/O.
  convert.py        Delta's decimal strings become JSON numbers, exactly once.
  models.py         The contract's response shapes.
```

`chain.py` takes a decoded list of ticker dicts, so the pivot is testable without a
network and without a fake HTTP layer.

## Things about Delta worth knowing before you edit this

**There is no option-chain endpoint.** A chain is `/v2/tickers` filtered by
`contract_types`, `underlying_asset_symbols` and `expiry_date`, pivoted here.

**A ticker carries no `expiry_date` field.** The expiry appears only as the `DDMMYY`
suffix of the symbol — `C-BTC-77600-040926`. `/expiries` parses it back out, and sorts by
the parsed date: sorted as text, `30-10-2026` would land after `27-11-2026`.

**Every request needs a `User-Agent`.** Without one Delta's edge answers `403` with an
HTML body, not JSON. Timeout is 10 seconds.

**`spot_price` and `greeks.spot` are different measurements.** In the captured chain
every one of the 128 tickers reports `spot_price` 77568.2, while `greeks.spot` takes 15
distinct values between 77557.7 and 77569.5. This project uses `spot_price` and never
exposes `greeks.spot`.

**`"0"` means different things in different places.** In a quote field it means nobody is
quoting, so it becomes `null`. In `oi`, `oi_value_usd` or a greek it is a real zero and
stays `0.0`. That split is the difference between `to_quote_number` and `to_number`.
