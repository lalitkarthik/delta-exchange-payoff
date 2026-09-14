# Architecture

**One socket, one cache, many browsers.** The system holds exactly one connection to the venue,
turns its frames into canonical events, and lets independent consumers read those events under
their own queue policies. Nothing downstream ever sees venue JSON, and no service calls another
over HTTP.

## Design philosophy

**Absence is data.** A minute with no arrivals produces no row. A quote nobody is making is
`null`, not `0`. A leg with no volatility carries no Greeks. Every rule here exists because the
alternative -- a plausible number where there was no observation -- is the failure this project
is about.

**Fail loudly at the boundary.** An unregistered event type raises. An undeclared field raises.
A `schema_version` this build does not know raises at parse, naming both versions. A canonical
symbol in the pre-currency form raises rather than defaulting a currency onto it. A stale cache
key should be a crash, not a silent guess.

**Measure rather than assert.** Every number in these documents is tagged `measured`, `derived` or
`assumed` and names the run behind it. `tools/` regenerates them.

**Keep the core pure.** Anything that can take data in and return data out does. Only
`delta_client.py` talks to the venue and only `store.py` touches a file.

**Documents and code are pinned to each other.** Tests parse the event catalogue, the log-record
catalogue and every Markdown table under `docs/`, so prose and code cannot drift apart quietly.

## Environment contexts

Two compositions, selected by one environment variable.

| | Monolith (`DELTA_BUS` unset) | Split (`DELTA_BUS=redis`) |
|---|---|---|
| Processes | one: `deltapayoff.main:app` | four apps plus Redis |
| Bus | `fanout.py`, in process | `redis_bus.py`, Redis Streams |
| Venue socket | in the same process | `feed` only |
| Parquet writer | in the same process | `store` only |
| Command path | a direct call | a `control.command` event |

The split composition is the production shape and what the Docker stack runs. The monolith remains
the default and is unchanged by any of it -- it is the fastest way to work on the pricing core.

## Core components

| Component | Module | Owns |
|---|---|---|
| `DeltaAdapter` / `DeltaFeed` | `adapters/` | The venue socket, symbol spelling, channel names, wire layout |
| `FeedController` | `controller.py` | The five connection states and every transition between them |
| `FeedSupervisor` | `supervisor.py` | The reconnect budget, the watchdog, the health report |
| `FanOut` / `RedisBus` | `fanout.py`, `redis_bus.py` | `publish` and `subscribe`, and nothing else |
| `ChainStream` | `stream.py` | Newest event per contract; rebuilds a ladder on demand |
| The pricing core | `forward.py`, `solvers.py`, `greeks.py`, ... | Forward, implied volatility, Greeks. Pure |
| `BarWriter` / `BarStore` | `bars.py`, `store.py` | Sealing minutes, flushing Parquet, the checkpoint |
| `BarBuffer` | `bar_buffer.py` | The api's copy of sealed bars not yet on its disk |
| `FeedConnectionCache` | `feed_runtime.py` | The api's projection of remote feed state |
| `StoreStateCache` | `store_main.py` side | The api's projection of the store's own state |
| Alert consumer | `alert_consumer.py` | The `alert` stream, and a Discord webhook |

### The processes, and what each one refuses to do

- **`feed_main:app`** owns the venue. It publishes canonical events and reads no stream but
  `control.command`. It performs no Parquet write and holds no ladder.
- **`store_main:app`** is the **only writer** of the bar tables. It consumes losslessly, keeps a
  per-stream checkpoint, replays from it, and publishes `store.state` and `md.option_bar`.
- **`main:app`** (the api) opens no venue socket in split mode and owns no writer. It attaches
  `ChainStream`, `FeedConnectionCache`, `StoreStateCache` and a lossless `BarBuffer`, so the four
  historical read paths can answer from the newest sealed minute with no local writer.
- **`alert_main:app`** subscribes to `alert` and nothing else, so it pays no market-data decode.

## Data flow: the life of a quote

1. Delta sends an `ob_l2` frame. `DeltaFeed` reads it and publishes a `VenueMessage` -- the frame
   verbatim, with its channel and arrival stamp -- **and returns**. The socket handler never runs
   inside a consumer: if it blocked, the OS receive buffer would fill and the venue would close us.
2. `DeltaAdapter.events_from_frame` decodes it into one `md.option_quote` carrying a canonical
   `Instrument`. Absent quotes become `null` here, not `0`. A non-finite number becomes `None` and
   is counted.
3. The bus carries it. `ChainStream` takes it drop-oldest; `BarWriter` takes it losslessly.
4. `ChainStream` replaces the newest frame for that `(type, symbol)`. Its recompute pass solves the
   forward, then the out-of-the-money leg's implied volatility, then the Greeks, and publishes
   `computed.chain`.
5. A browser's `/ws/chain` socket receives the rebuilt ladder -- the identical object `/chain`
   returns.
6. `BarWriter` buckets the tick on `ts_venue` alone, seals the minute once its grace elapses, and
   the flush thread writes a uniquely named Parquet file.

**The two consumers are deliberately not one.** One wants the latest state and drops on overflow,
because a four-second-old quote is worthless to a screen. The other wants every state, because a
dropped message there is a permanent hole in the record. Sharing one structure would make them
fight.

## Component state management

A connection is in exactly one of five states at every moment: `connecting`, `connected`,
`degraded`, `reconnecting`, `stopped`. Every transition emits **one** `feed.connection` event and
**one** log record, and nothing else changes state.

| From | To | Trigger |
|---|---|---|
| -- | `connecting` | Adapter start, or a `resume` command |
| `connecting` | `connected` | Socket open and every subscription replayed |
| `connected` | `degraded` | No message for the staleness interval |
| `degraded` | `connected` | The next message arrives |
| `degraded` | `reconnecting` | Silence exceeds the second, longer bound |
| any | `reconnecting` | Socket close, or a `reconnect` command |
| `reconnecting` | `connecting` | Backoff elapses and an attempt is made |
| `reconnecting` | `stopped` | Reconnect budget exhausted -- plus an `alert` and an error log |
| any | `stopped` | A `pause` command; no budget is spent |

`degraded` does not alert and nor does a `pause`: fifteen quiet seconds is already a badge and a
heartbeat, a pause is something a person just did, and an alert on either is the flood an alert
exists to stand out from.

## Threading model

One asyncio event loop per process. The socket reader, the bus readers, the recompute pass and the
supervisor watchdog are all tasks on it. **The one thread is the store's disk write**: `BarStore`
flushes on a worker thread so a multi-megabyte Parquet write never stalls the loop that is reading
the socket. Nothing else is threaded, and nothing shares mutable state across the boundary except
the batch handed to the writer.

## Health, and what it is about

`/health` on the api is authoritative about **remote** feed state, projected from `feed.connection`
and `heartbeat`; the websocket badge is derived from the same projection, heartbeat-silence rule
included. `/health` on `feed` is **503** when stopped, paused, out of budget, silent past 135 s, or
when its bus reader, flusher or control consumer has died. `/health` on `store` is **readiness, not
liveness**: 503 once its reader stops, its group is trimmed past, or its lag passes the threshold.

## Where it runs on AWS

**ECS on EC2: one `c7g.xlarge` in a public subnet of `ap-south-1` (Mumbai), one task definition
holding all seven containers, `host` network mode.** `derived` **$78.07 a month** of compute,
on-demand, 2026-09-12; **$79.99 all-in** with S3 for BTC and ETH.

| Concern | Choice | Why |
|---|---|---|
| Orchestrator | Amazon ECS, EC2 launch type | A restart is an alarmable platform event, and EC2 compute adds $0 over plain Compose |
| Instance | one `c7g.xlarge` -- Graviton3, **4 vCPU, 8 GiB**, 30 GB gp3 root | Reservations total 3.55 vCPU and 8.25 GB across seven containers |
| Architecture | `linux/arm64` | `m7i.large`, the x86 twin, costs **82% more** in ap-south-1 for the same shape |
| Region | `ap-south-1` | Cheapest of four priced, in-country for the NSE adapter named next |
| Network mode | `host` | Keeps the publisher on `127.0.0.1`; `awsvpc` would put a VPC hop between `feed` and Redis |
| Subnet | public, one in-use IPv4 | **A NAT gateway would cost `derived` $165/month** -- more than the compute it fronts |
| Inbound | none public | The dashboard is reached over a tunnel or Tailscale |
| Bus | Redis in a container, not ElastiCache | The bus is a pipe; losing it costs a restart, not history |
| Store | **Amazon S3 Standard**, one bucket per environment | `s3://<env>-deltapayoff-bars/`, the same hive layout the engine already writes |
| Images | Amazon ECR | One `Dockerfile` per service; the same tag runs in dev and prod |
| Alarms | Amazon CloudWatch | A stopped task, and the box |

### Container reservations

| Container | vCPU | Memory | Basis |
|---|---|---|---|
| `feed` | **1.0** | 1 GB | `measured` 0.3592 core at 1x; `derived` 0.52-0.71 with the publisher at a 50 ms batch |
| `store` | 0.5 | 1 GB | `measured` 0.3048 core; `derived` 201.5 MB/day written for BTC+ETH |
| `api` | 1.0 | 2 GB | `measured` 0.3045 core, +0.0466 for the first viewer; a 175 ms full solve pass |
| `web` | 0.25 | 0.5 GB | `measured` 110.6 MiB idle, no CPU with no viewer |
| `discord-alerts` | 0.05 | 0.25 GB | Subscribes to `alert` only, so it pays no market-data decode |
| `proxy` | 0.25 | 0.5 GB | `assumed` |
| `redis` | 0.5 | **3 GB** | The `maxmemory 2gb` ceiling plus overhead |

**These reservations under-count CPU and over-count memory**: the `derived` need at 1x is 1.21-1.95
cores and 5.05 GiB, which is why the instance is 4 vCPU and not 2. At ten times the rate it is
`c7g.4xlarge`-`c7g.8xlarge`, `derived` $295.72-582.39.

Full sizing arithmetic, the four-platform comparison, the region table and the month of operations
are in [Deployment](deployment.md).

## Related guides

- [Adapters](adapters.md), [Events](events.md), [Message bus](message-bus.md)
- [Data store](data-store.md), [Logging](logging.md), [Deployment](deployment.md)
