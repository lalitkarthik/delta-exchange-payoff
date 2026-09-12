# Local stack: the numbers

Evidence for [local-stack.md](local-stack.md) §9. The design note stays there and the
measurements live here, which is what `hld-evidence.md` (#62), `logging-catalogue.md` (#63)
and `store-numbers.md` (#81) each did when their note reached the 200-line bound.

## The runs

Every figure here is `measured` on Docker 29.7.2. Image sizes come from `docker images`
2026-09-12T16:45Z. The timings come from the isolated smoke run of 2026-09-12T16:43:32Z --
`tools/smoke_stack.py --project-name smoke65 --proxy-port 8099`, exit 0 -- with a poller
sampling `docker inspect` at 0.5 s started **before** `up`, which is what makes these
transitions rather than upper bounds.

| Measurement | Value | Command | Date |
|---|---|---|---|
| `feed` image size | 761 MB | `docker images` | 2026-09-12 |
| `store` image size | 761 MB | `docker images` | 2026-09-12 |
| `api` image size | 761 MB | `docker images` | 2026-09-12 |
| `discord-alerts` image size | 761 MB | `docker images` | 2026-09-12 |
| `web` image size | 1.08 GB | `docker images` | 2026-09-12 |
| Redis image size | 57.8 MB | `docker images` | 2026-09-12 |
| `nginx:alpine` proxy image size | 103 MB | `docker images` | 2026-09-12 |

The four Python images share one base stage, so what they occupy on disk is **not** 3,044 MB
(`assumed` -- the shared layer was never measured on its own).

### Start-to-healthy, per container

Each row is that container's own `StartedAt` to the first `docker inspect` sample reporting
`healthy`, so the rows are comparable with each other and not with a whole-stack clock.

| Container | Start-to-healthy | Started, relative to the first container | Healthy, relative to the first container |
|---|---|---|---|
| `redis` | 2.61 s | +0.00 s | +2.61 s |
| `web` | 6.43 s | +0.00 s | +6.43 s |
| `feed` | 5.97 s | +2.81 s | +8.78 s |
| `store` | 6.15 s | +2.78 s | +8.93 s |
| `api` | 6.30 s | +2.80 s | +9.10 s |
| `discord-alerts` | 6.64 s | +2.81 s | +9.45 s |
| `proxy` | 5.49 s | +8.62 s | +14.11 s |

**Whole stack, first container start to last container healthy: 14.11 s** (`derived` from the
two rows above). The operator-facing number is larger and measures something else: the smoke
script's own clock, which starts before `docker compose up` and so includes image resolution,
network and container creation, reported **30.7 s** (`measured`, same run).

Three caveats travel with this table, and it should not be quoted without them.

1. **`feed`'s 5.97 s is the scripted fake adapter**, which `compose.smoke.yml` selects. It is
   not the venue adapter's figure. The other six rows are the production images unchanged.
2. **Every row is quantised by its own healthcheck.** `compose.yml` gives six services
   `interval: 3s` with `start_period: 10s` and Redis `interval: 2s`, so a container can only
   be *observed* healthy on a probe boundary. These are "healthy by", not "ready at".
3. **`proxy` starts last by design**, on `depends_on: service_healthy` for `api` and `web`, so
   its +8.62 s start offset is the dependency graph and not slowness.

### What a restart of `feed` actually costs

#65 asks how long `feed` takes from container start to its first frame on the bus, because
that is what every restart costs and R4 needs it. Taken from the **live** `dxp` stack, where
the venue adapter is real, on the `feed` restart of 2026-09-12T16:17:24.373Z (`docker inspect
.State.StartedAt`) against the first stream entry id at or after it (`XRANGE <stream>
1789229844373 + COUNT 1`), all `measured`:

| Milestone | Stream | After container start |
|---|---|---|
| first control event | `feed.connection:DELTA` | 1.380 s |
| socket open | `.stack-logs/feed/2026-09-12.log`, `connecting -> connected` | 2.478 s |
| first heartbeat | `heartbeat:DELTA` | 2.407 s |
| **first market-data event** | `md.option_quote:DELTA:BTC` | **2.961 s** |
| first index quote | `md.index_quote:DELTA:BTC` | 6.510 s |

**2.961 s is the number to carry forward**, per [CONTEXT.md](../../../CONTEXT.md) §2, where a
market-data event is one of the three `md.*` types and control traffic is not one. It is
`derived` by subtraction from two `measured` timestamps.

It is also roughly a quarter of that container's 11.19 s start-to-healthy recorded elsewhere
for the same stack. The two do not disagree: the healthcheck cannot report before its
`start_period` and `interval` allow, so start-to-healthy bounds a restart's cost from above
and this number measures it.

