# Configuration

**There is no `.env` and no secret in this repository.** Delta's market data is public: no API key,
no signed request, no credential to rotate. The one secret the system can hold -- a Discord webhook
-- lives in a git-ignored overlay file and is never committed.

Everything below is an environment variable read at start-up. `stack.env` is the one committed
environment file and holds non-secret defaults only.

## Selecting a topology

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `DELTA_BUS` | unset | all | Unset is the in-process monolith. `redis` selects the four-process split |
| `DELTA_REDIS_URL` | -- | all | e.g. `redis://redis:6379`. **Mandatory** when `DELTA_BUS=redis` |
| `DELTA_LIVE_FEED` | `1` | api, feed-in-monolith | `0` serves every route and opens no venue socket. What the tests set |
| `DELTA_LIVE_UNDERLYINGS` | `BTC,ETH` | feed | The underlyings to list and subscribe. The Docker stack narrows this to `BTC` |
| `DELTA_FEED_ADAPTER` | the real venue adapter | feed | `module:attribute`. How the smoke test mounts a scripted fake |

**An unreachable Redis in split mode fails start-up with `BusUnavailable`, before the feed opens the
venue socket.** A feed that came up and published into nothing would be a silent data loss.

**`feed_main` does not consult `DELTA_LIVE_FEED`.** A plain `docker compose up` without the smoke
override therefore creates a second real venue subscriber beside any live engine.

## The bus

| Variable | Default | Meaning |
|---|---|---|
| `DELTA_BUS_BATCH_MS` | `50` | Publisher batching interval. **The system is sized at 50 ms**; the 100 ms figures in the record belong to a run, not a deployment |
| `DELTA_BUS_RETENTION_SECONDS` | `1800` | The `XTRIM MINID` window. Thirty minutes is what the bus promises a restarting store |
| `DELTA_BUS_INSTANCE` | `1` | The instance number in the consumer identity, `{service}-{instance}` |

Redis itself is configured on its command line, not through these:

```sh
redis-server --save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

All four matter, and `noeviction` is a data-loss decision rather than a tuning one. See
[Message bus](message-bus.md).

## The store

| Variable | Default | Meaning |
|---|---|---|
| `DELTA_STORE_ROOT` | `data/` | The dataset root. The Docker stack uses `/data`, bound to `./.stack-data/` |
| `FLUSH_SECONDS` | `300` | The flush cadence, and **the crash-loss budget** |
| `STORE_BUS_MONITOR_INTERVAL_SECONDS` | `10` | How often the store asks Redis where its group stands. Shares the `store.state` loop |
| `STORE_BUS_MONITOR_STALE_INTERVALS` | -- | How many intervals of silence before a reader is called stopped |
| `STORE_LAG_ALERT_ENTRIES` | -- | Consumer lag past which `store.consumer_lag` is raised |
| `STORE_STATE_INTERVAL_SECONDS` | `10` | The `store.state` publish cadence |
| `STORE_STATE_STALE_SECONDS` | -- | How old a `store.state` may be before the api stops trusting it |
| `STORE_QUEUE_SIZE` | -- | The lossless subscription's watermark |
| `STORE_CONTROL_QUEUE_SIZE` | -- | The control subscription's bound |
| `STORE_COMMAND_ACK_TIMEOUT_SECONDS` | -- | How long a command waits for the store to acknowledge |

**`data/` is read-only in this repository** -- it holds the live Parquet store, and a second writer
there would corrupt it. The Docker stack mounts `/data` read-write in `store` and **read-only in the
api**, which keeps "the store is the only writer" true at the filesystem boundary rather than on
trust.

## Alerts

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | empty | The webhook the alert consumer posts to. Empty disables posting |

**The real value belongs in `stack.local.env`**, a git-ignored overlay Compose loads after
`stack.env` for the `discord-alerts` service. No secret is ever committed.

## The web app

| Variable | Default | Meaning |
|---|---|---|
| `NEXT_PUBLIC_ENGINE_URL` | `http://localhost:8000` | Where the browser reaches the engine. The stack builds it as `http://localhost:8080/api` |

**`NEXT_PUBLIC_` values are inlined by `next build`.** It is a build argument, not a run-time
variable: changing the proxy mount or origin requires a **rebuild**, not a restart.

## Ports and origins

| Port | What |
|---|---|
| 8000 | The engine, in development |
| 3000 | `next dev` |
| 8080 | The Docker stack's only host listener |

**CORS allows only port 3000**, `localhost` and `127.0.0.1`. Serving the web side from another port
fails in a way that looks exactly like the engine being down. The stack sidesteps this entirely: the
proxy makes the browser same-origin, so no cross-origin request is made and the allow-list is never
consulted. `/ws/chain` is a websocket handshake and is not subject to CORS in either case.

## Compose overrides

| Variable | Default | Meaning |
|---|---|---|
| `DXP_CONTAINER_PREFIX` | `dxp` | The container-name prefix |
| `DXP_PROXY_PORT` | `8080` | The one published host port |

Both exist so a **second** Compose project can run the same file beside a live one. `container_name`
overrides Compose's own project-service-index naming outright, so before these were interpolated a
second project collided on `/dxp-redis` and never started -- and two tickets deferred their
end-to-end runs on the strength of a flag that could not isolate anything.

## Related guides

[Getting started](getting-started.md) | [Message bus](message-bus.md) | [Data store](data-store.md)
