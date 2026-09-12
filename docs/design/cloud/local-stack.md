# Local stack

## 1. What it is

The local stack is one Compose project named `dxp`: seven containers, `feed`, `store`,
`discord-alerts`, `api`, `web`, Redis, and an `nginx:alpine` reverse proxy. One command
starts the stack, and one host port serves the dashboard and API. Every service defines a
health check, so Compose can wait for the whole stack instead of guessing from process
startup.

## 2. The one command

From the repository root:

```sh
docker compose --project-name dxp --env-file stack.env up -d --wait --wait-timeout 120
```

Open [http://localhost:8080/](http://localhost:8080/). Tear it down with the same explicit
project name:

```sh
docker compose --project-name dxp --env-file stack.env down --remove-orphans
```

The explicit `dxp` project keeps the containers prefixed `dxp-`; teardown is by project, never
by a prune. The command above is the live-data stack. The smoke workflow in §8 supplies the
fake-feed override for a safe local proof.

## 3. Why 8080 and not 3000 or 8000

Port 8080 is the stack's only host listener. A live `next dev` owns 3000 and a live engine
owns 8000 on this machine; the stack never binds either, because doing so would fail the run
or shadow the live system. The proxy makes 8080 same-origin for the browser, so CORS does not
apply.

## 4. Why there is a proxy at all

The browser sees one origin and makes no cross-origin request. `main.ALLOWED_ORIGINS` is
therefore untouched. The routing table lives in `proxy/nginx.conf`; it is derived from
`main.py`'s route declarations and enforced by `engine/tests/test_stack_proxy.py`.

| Path | Service |
|---|---|
| `/api/` (prefix stripped before forwarding) | `api` |
| `/` (catch-all) | `web` |

This prefix-and-catch-all shape replaced a per-route table: `main.py` declares `GET
/volatility` while the web app has a page at the same path, so no per-route table could
serve both.

For example, `/api/chain` reaches the API as `/chain`, and the websocket uses the same API
prefix. The `/volatility` collision is deliberate: the api's series wins the path, so the
web page at `web/app/volatility/page.tsx` is not reachable through the proxy today. `/docs`,
`/redoc`, and `/openapi.json` are not proxied.

## 5. The store root

The stack uses `./.stack-data/`, a git-ignored host directory bind-mounted at `/data`.
It is never the live `data/`: [AGENTS.md](../../../AGENTS.md) makes `data/` read-only, and a
second writer there would corrupt the live store. The `/data` mount is read-write in `store`
and read-only in `api`. `main.build_consumer_stack()` returns `writer=None` in split mode
since I4 (#63), so `store` is the only writer; the read-only API mount keeps that true at the
filesystem boundary rather than on trust.

## 6. Redis

Redis publishes no host port and has no persistence. Its command-line policy is:

```text
--save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

Parquet is the archive, so snapshots and AOF would add disk state without buying recovery.
The 2gb ceiling bounds the in-memory pipe; `noeviction` makes a full bus fail loudly at the
publisher instead of silently evicting market data. The ceiling must stay above the retention
window's own demand, or `noeviction` stalls the publisher rather than protecting it: thirty
minutes costs a `derived` 1,051.5 MiB for two underlyings, so the 1gb this file carried until
2026-09-12 was below it. See [redis-hosting.md](redis-hosting.md)
§1 and [0002-redis-hosting.md](../decisions/0002-redis-hosting.md).

## 7. Configuration

[`stack.env`](../../../stack.env) is the one committed environment file. It contains
non-secret defaults only. `DELTA_FEED_ADAPTER=module:attribute` selects the feed adapter;
the default is the real venue adapter.

| Variable | Read by |
|---|---|
| `DELTA_BUS` | `feed`, `store`, `api`, `discord-alerts` |
| `DELTA_REDIS_URL` | `feed`, `store`, `api`, `discord-alerts` |
| `DELTA_BUS_BATCH_MS` | `feed`, `store`, `api`, `discord-alerts` |
| `DELTA_BUS_RETENTION_SECONDS` | `feed`, `store`, `api`, `discord-alerts` |
| `DELTA_BUS_INSTANCE` | `feed`, `store`, `api`, `discord-alerts` |
| `DELTA_STORE_ROOT` | `store`, `api` |
| `FLUSH_SECONDS` | `store` |
| `DELTA_FEED_ADAPTER` | `feed` |
| `DISCORD_WEBHOOK_URL=` | `discord-alerts` (landed, #66); empty in the committed file |
| `NEXT_PUBLIC_ENGINE_URL=http://localhost:8080/api` | `web` image build |

The real webhook value belongs in `stack.local.env`, a git-ignored overlay Compose loads
after `stack.env` for the `discord-alerts` service; no secret is ever committed here.

`NEXT_PUBLIC_ENGINE_URL` is a build argument, not a run-time variable. `NEXT_PUBLIC_` values
are inlined by `next build`, so changing the proxy mount or origin requires a web rebuild,
not a restart.

## 8. The smoke test

Run from the repository root:

```sh
engine/.venv/Scripts/python.exe tools/smoke_stack.py
```

The smoke test starts Compose with the smoke-only `compose.smoke.yml` override, mounts the
scripted fake adapter, and sets `DELTA_FEED_ADAPTER` to that fake. The fake emits its script,
never contacts the venue, and the test waits for the services and drives the dashboard/API
through the proxy, then checks that the ladder updates and a Parquet file appears under
`.stack-data/`. It stops `store` so
`BarWriter.aclose()` performs its final flush; `FLUSH_SECONDS` is 300, so waiting for the
timer would be the wrong assertion. It tears the project down by name afterward.

The assertion is the Parquet file on disk, not an exit code. If the Docker daemon is absent,
the test skips loudly with exit code 3. The override mounts fakes only for smoke; production
images ship no test code.

`feed_main` does not consult `DELTA_LIVE_FEED`. A plain `docker compose up` without the smoke
override therefore creates a second real venue subscriber beside the live engine. That is
inside the venue's budget, but use the plain stack only when live data is wanted and bring it
straight down afterward.

## 9. Measurements

I6-11 fills this table after the orchestrator's run. No image size or start-to-healthy value
has been measured here.

| Measurement | Value | Command | Date |
|---|---|---|---|
| `feed` image size | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |
| `store` image size | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |
| `api` image size | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |
| `web` image size | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |
| Redis image size | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |
| `nginx:alpine` proxy image size | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |
| Start-to-healthy seconds | pending the orchestrator's run | pending the orchestrator's run | pending the orchestrator's run |

## 10. What this is not

This is not production: there is no restart policy, no TLS, no arm64 build, and the
containers run as root. See [compute.md](compute.md) for the production shape.
