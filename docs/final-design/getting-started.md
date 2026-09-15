# Getting started

This page takes a fresh checkout to a live option ladder in the browser. Two processes in the
simple case: the **engine** on port 8000 and the **web app** on port 3000. A seven-container
Docker stack on port 8080 is the third option, and the closest thing to production.

## Requirements

| Thing | Version | Note |
|---|---|---|
| Python | 3.13 | `measured` 3.13.13 on 2026-09-14. 3.12 runs, with one known solver failure |
| Bun | current | The web toolchain. Not npm, not pnpm |
| Docker | 29.x | Only for the local stack and the Redis-backed tests |
| An API key | none | Delta's market data is public. There is no `.env` and no secret |

Every outbound request needs a `User-Agent` header or Delta's edge answers `403` with HTML.
The client sets one; a hand-rolled `curl` must too.

## Install

From `engine/`:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt         # POSIX
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt # Windows
```

`requirements.txt` is the runtime (FastAPI, httpx, uvicorn, polars, redis). `requirements-dev.txt`
pulls that in and adds pytest and ruff. There is **no install step for the package itself** --
`--app-dir src` is what puts `deltapayoff` on the path.

From `web/`:

```sh
bun install
```

## Run the monolith

Engine first, from `engine/`:

```sh
.venv/bin/python -m uvicorn --app-dir src deltapayoff.main:app --port 8000 --reload
```

Then the web app, from `web/`:

```sh
bun run dev        # http://localhost:3000
```

**CORS allows only port 3000** (`localhost` and `127.0.0.1`). Serving the web side from any
other port fails in a way that looks exactly like the engine being down. `/ws/chain` is a
websocket handshake and is not subject to CORS.

With no environment set, `deltapayoff.main:app` is the in-process monolith: it opens the single
venue websocket, fans out to the chain cache and the bar writer, and serves every route.

### Running without the venue

```sh
DELTA_LIVE_FEED=0 .venv/bin/python -m uvicorn --app-dir src deltapayoff.main:app --port 8000
```

The REST routes and the websocket serve; no socket to Delta is opened. This is what the test
suite sets, and it is the right setting for working on the read paths offline.

## Run the split stack

Three apps and a Redis replace the monolith when `DELTA_BUS=redis` is set. Each is an ordinary
uvicorn app:

```sh
DELTA_BUS=redis DELTA_REDIS_URL=redis://127.0.0.1:6379 \
  .venv/bin/python -m uvicorn --app-dir src deltapayoff.feed_main:app --port 8001
# and likewise deltapayoff.store_main:app, deltapayoff.main:app, deltapayoff.alert_main:app
```

Redis is **mandatory** in that mode: an unreachable Redis fails startup with `BusUnavailable`
before the feed opens the venue socket. See [Configuration](configuration.md) for the full
variable list and [Architecture](architecture.md) for what each process owns.

## Run the Docker stack

Seven containers -- `feed`, `store`, `api`, `web`, `discord-alerts`, `redis` and an nginx
proxy -- behind one host port. From the repository root:

```sh
docker compose --project-name dxp --env-file stack.env up -d --wait --wait-timeout 120
```

Open [http://localhost:8080/](http://localhost:8080/). Tear it down by project name, never by a
prune:

```sh
docker compose --project-name dxp --env-file stack.env down --remove-orphans
```

Port 8080 is the stack's only host listener: a dev engine owns 8000 and a dev web server owns
3000, and the stack binds neither. The proxy makes the browser same-origin, so CORS does not
apply -- `/api/` is stripped and forwarded to the api, and everything else goes to `web`.

The stack's store root is `./.stack-data/`, **never** `data/`, which holds the live store and is
read-only. Start-to-healthy is `measured` **14.11 s** for the whole stack.

### Smoke test

A scripted fake adapter, no venue contact, and a real Parquet file as the assertion:

```sh
engine/.venv/bin/python tools/smoke_stack.py --project-name smoke65 --proxy-port 8099
```

Use that form whenever a stack is already running. The script tears down whatever project it was
given, so a bare run with the default project name is the one that would stop yours.

## Verify

From `engine/`:

```sh
.venv/bin/python -m pytest -q      # the whole suite
.venv/bin/python -m ruff check .
```

The suite is **1,610** tests with Docker available (`measured` 2026-09-14 at `7783a4b`).
Without Docker the Redis-backed parametrisations skip: the collected count is the same and the
passed count is lower. A run that collects far fewer has failed to collect, whatever it printed.

From `web/`:

```sh
bun run typecheck
bun run test
bun run build
```

**No test may touch the network.** `tests/conftest.py` sets `DELTA_LIVE_FEED=0` and replaces the
async client factory with one that raises, so a test that tries fails loudly.

## Probes

`tools/` holds probes, not engine code. They answer questions about the venue and the store without
touching either service:

```sh
python tools/measure_feed.py          # channel refresh rates
python tools/measure_arrival_lag.py   # ts_received - ts_venue, per channel
python tools/measure_store.py         # what the store writes, and how big
python tools/probe_api.py             # the REST surface
```

## Related guides

- [Overview](overview.md) -- what the system does and why
- [Architecture](architecture.md) -- what each process owns
- [Configuration](configuration.md) -- every environment variable
- [API reference](api-reference.md) -- the routes the web app calls
