# The split, start here

**Read this before you read any other document about the services.** It tells you what runs,
what was chosen, and where the reasoning is. It repeats no reasoning of its own.

Every word in this file resolves to [../CONTEXT.md](../CONTEXT.md). Look a word up there before
you write it anywhere else.

## 1. What runs

The engine was one process. It is now **seven containers** on a Redis Streams bus.

| Service | What it does | Where it lives |
|---|---|---|
| `feed` | Holds the one websocket to the venue. It publishes market-data events. | `feed_main.py` |
| `store` | Reads the bus. It folds bars and writes Parquet. It is the only writer of the four tables. | `store_main.py` |
| `api` | Serves the ladder and the REST routes. It opens no venue socket. | `main.py` |
| `web` | Renders the page. It computes nothing. | `web/` |
| proxy | One `nginx:alpine`. It puts the stack on one host port. | `proxy/nginx.conf` |
| Redis | The bus. One container, no persistence. | [0002](design/decisions/0002-redis-hosting.md) |
| `discord-alerts` | Reads the `alert` stream only. It posts to a webhook. | `alert_main.py` |

**There are two compositions, not one.**

1. The **monolith** is the default. `DELTA_BUS` is unset. One process holds an in-process bus.
2. **Split mode** is `DELTA_BUS=redis`. The seven services above run over Redis.

Both are supported. The monolith serves port 8000 today.

## 2. What to run

```bash
docker compose -p dxp up -d
```

That starts all seven. The stack is on `http://localhost:8080`. The proxy puts `api` under
`/api/` and `web` at the root, so the browser sees one origin.

```bash
python tools/smoke_stack.py --project-name dxp-smoke
```

That builds its own stack, drives one scripted frame through it, checks a bar reaches disk, and
removes itself. **Give it a project name.** Its default is `dxp-smoke`, and its tear-down runs
`down` on whatever project it is given.

Read [design/cloud/local-stack.md](design/cloud/local-stack.md) for the rest.

## 3. What was chosen

One line each. The reasoning is in the record. **Do not re-argue a decision here.**

| # | Question | What was chosen |
|---|---|---|
| [0001](design/decisions/0001-stream-naming-and-payload-format.md) | How streams are named | `{event_type}:{VENUE}[:{UNDERLYING}]`. The envelope is flat, one Redis field per key. The payload is one JSON object. |
| [0002](design/decisions/0002-redis-hosting.md) | Where Redis runs | A container beside the services. Persistence off. A 2 GiB ceiling and `noeviction`. |
| [0003](design/decisions/0003-controller-policy.md) | The controller's policy | Five states. A budget of **ten consecutive** failures. Backoff 1 s doubling to 60 s. Silence past `reconnect_after` cuts the socket. |
| [0004](design/decisions/0004-durable-store.md) | Where the bars live | Parquet on S3 Standard, one bucket per environment, the hive `underlying=`/`date=` layout. |
| [0005](design/decisions/0005-compute-and-region.md) | The platform and region | ECS on EC2, `ap-south-1`, a public subnet, `host` network mode, every container in one task definition. |
| [0006](design/decisions/0006-stream-names-without-environment.md) | The environment prefix | Removed. There is one local stack and one prod, never a staging. |
| [0007](design/decisions/0007-load-profile.md) | The shape of the load | Every Python service is compute-bound and capped at one core by its event loop. Redis and `web` are memory-bound. |
| [0008](design/decisions/0008-topology.md) | One instance or several | **One `c7g.xlarge`** (Graviton3, 4 vCPU, 8 GiB). It supersedes 0005's `m7g.large`. |
| [0009](design/decisions/0009-compaction-cadence.md) | When to compact | Nightly only, while the store is on local disk. |
| [0010](design/decisions/0010-store-replay.md) | The store's crash protocol | A watermark per stream, a checkpoint at the root, a flush intent, and a seal clock. |

**The instance is the answer most people ask for.** It is **one `c7g.xlarge`**, `derived`
$78.07 a month. At ten times the rate it is one `c7g.4xlarge` or `c7g.8xlarge`, `derived`
$295.72–582.39. The named fallback for Redis, if any criterion moves, is ElastiCache for
Valkey on `cache.t4g.medium`, `derived` $47.30 a month.

**The split threshold is written down.** We split into two boxes when the box's load passes
**2.8 cores** — 70% of 4 vCPU — or when `oms` places its first order through the bus, whichever
comes first.

## 4. The numbers that govern the running system

Every one of these is in the code, not only in a document.

| Constant | Value | Where |
|---|---|---|
| Bus retention | **30 minutes**, by age and never by count | `redis_bus.py` |
| Store flush interval | **300 s** | `store.py` |
| Reconnect budget | **10 consecutive** failures | `controller.py` |
| `degraded_after` | **15 s** | `controller.py` |
| `reconnect_after` | **45 s** | `controller.py` |
| Store lag alert | **100,000** entries | `store_main.py` |

The measured figures are in [split-numbers.md](split-numbers.md).

## 5. What the split promises, and what it does not

**It promises three things.**

1. **A new consumer costs the producers nothing.** `discord-alerts` was added and no other
   service was opened. That is the reason the bus exists.
2. **A restarted `store` loses nothing inside the retention window.** It replays from the last
   entry it flushed, not from the last entry it acked. An ack means delivered. A store flush
   means safe. They are different facts.
3. **A minute with no arrivals produces no row.** The store never forward-fills. An empty
   minute is empty.

**It does not promise these.**

1. **A seal is final.** If a minute is sealed while its data is unread, that data is lost. A
   restart cannot recover it.
2. **Retention is thirty minutes.** A `store` that stops for longer than that loses the
   difference, permanently.
3. **An alert is not durable.** `discord-alerts` acks on receipt and never replays. A post that
   fails is gone. It is now logged.

## 6. Where to read next

1. [../CONTEXT.md](../CONTEXT.md) — every term. Read this first if you read nothing else.
2. [design/hld.md](design/hld.md) — the high-level design.
3. [design/cloud/message-bus.md](design/cloud/message-bus.md) and
   [design/cloud/data-feed-engine.md](design/cloud/data-feed-engine.md) — the two assembled
   cloud documents. They are written to be read by someone who did not build this.
4. [split-numbers.md](split-numbers.md) — every measured figure, with the run behind it.
5. [split-what-to-do-next.md](split-what-to-do-next.md) — what is not finished.

**Study one ticket with [study-with-chatgpt.md](study-with-chatgpt.md).** It carries a prompt
that teaches the decision in this repository's own words.
