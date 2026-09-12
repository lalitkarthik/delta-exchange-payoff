# The data feed engine

**Read this to learn what runs where, on which AWS service, and what the feed does when the venue
goes quiet.** It is assembled from the closed research and its decision records, and it links to each
one where it uses it. Every term resolves to [../../../CONTEXT.md](../../../CONTEXT.md). The bus it
publishes to is [message-bus.md](message-bus.md).

Nothing in `prod` is built. The engine and its controller run today, in the local stack and against
the live venue.

---

## 1. The services, and the comparison of each

**Check [services.md](services.md) before you name a service here.** In five lines:

1. **Amazon ECS on EC2** runs one task definition holding every container.
2. **Amazon EC2**, one `c7g.xlarge` in a public subnet of `ap-south-1`, is the box.
3. **Amazon S3 Standard** holds the four bar tables, in the hive layout the engine already writes.
4. **Amazon VPC** gives one public subnet and one in-use public IPv4; there is no NAT gateway.
5. **Amazon ECR and CloudWatch** carry the images and the alarms.

The full list, the fallbacks and the bill are [services.md](services.md).

### 1.1 The platform, compared

#57's five criteria, in order. Dollars are `derived` from `measured` Price List Bulk API unit prices,
ap-south-1, on-demand, 730 hours; the arithmetic is
[../research/0005b-prices-and-sources.md](../research/0005b-prices-and-sources.md) §3.

| | (1) Invariants | (2) Ops for 2–3 people | (3) $/mo, 1x → 10x | (4) Path to the right half | (5) Latency |
|---|---|---|---|---|---|
| **ECS on EC2** ← chosen | a dead container restarts, **and the restart is an alarmable event** | one AMI to patch; restarts and deploys belong to the platform | **$48.94 → $179.43** | a new consumer is a container in the task | Redis on `127.0.0.1` under `host` mode |
| EC2 with Compose | identical, but **nothing records that it restarted** | the same AMI, plus every deploy is an `ssh` | **$48.94 → $179.43** | one YAML file on one box | the same |
| ECS on Fargate | identical; no host to misconfigure | the lightest: no AMI, no agent, no disk | $89.33 → $242.41 | the best per-service isolation | `awsvpc` is mandatory, so a VPC hop sits between `feed` and Redis |
| Amazon EKS | identical | the heaviest: a Kubernetes minor upgrade at least yearly | $121.94 → $252.43 one node | the most, and the most unused | the same as ECS on EC2 |

**Two rows tie exactly on cost**, because ECS on the EC2 launch type charges $0 for compute. When
criterion 3 ties, the decision moves up to criterion 2, and that is where Compose loses. The full
table, the region comparison and the operations list are [compute.md](compute.md);
the decision is [0005](../decisions/0005-compute-and-region.md).

### 1.2 One box, or several

| Topology | 1x upper | 10x upper | Why it is not chosen |
|---|---|---|---|
| **T1, one box** ← chosen | **$78.07** | **$582.39** | — |
| T2, `feed`+Redis ∣ rest | $84.46 | $588.70 | `store` crosses the hop to Redis |
| T3, one box per service | $128.07 | $670.86 | three boxes and two hops on the lossless path |
| T4, `feed`+`store`+Redis ∣ `api`+`web`+proxy ← **next** | $73.22 | $732.08 | it is the growth path, not today's shape |

All `derived`, [0008](../decisions/0008-topology.md) §3, Bulk API published 2026-09-10, read
2026-09-12. **We move to T4 when the box passes 2.8 cores, or when `oms` places its first order** —
whichever comes first. The cell-by-cell comparison is [compute-topology.md](compute-topology.md).

## 2. The system design on those services

**Follow one quote through the system: this is the path, and every hop on it is loopback.**

1. **`feed` holds the venue websocket** and turns each venue frame into a canonical event.
2. **`feed` publishes** the event to its outbox, which never blocks the socket reader.
3. **The flusher writes one batch every 50 ms**, with the trim riding in the same pipeline.
4. **`store` reads losslessly** through its consumer group, folds ticks into one-minute bars, and
   flushes sealed bars to Parquet every 300 s.
5. **`api` reads drop-oldest**, keeps the newest event per contract, and serves the ladder.

**Only `feed` contacts Delta, and no service calls another over HTTP.** That is #57's rule and
[hld.md](../hld.md) §2 is the shape it produces.

### 2.1 What each container reserves, and what it actually costs

| Container | Reserved | `derived` at 1x | Bound by |
|---|---|---|---|
| `feed` | 1,024 CPU units, 1 GB | 0.52–0.71 core | CPU, one event loop |
| `store` | 0.5 vCPU, 1 GB | 0.28–0.51 core; 2.33 KB/s to disk | CPU, decode and fold |
| `api` | 1.0 vCPU, 2 GB | 0.25–0.49 core, + 0.05–0.14 a watched expiry | CPU, set by viewers |
| `web` and proxy | 0.5 vCPU, 1 GB | ~0 core | memory |
| `redis` | 0.5 vCPU, 3 GB | — | memory, `maxmemory 2gb` plus overhead |

The `derived` column is [0007](../decisions/0007-load-profile.md); the reservations are
[compute.md](compute.md) §4. **`feed` reserves a whole vCPU because a starved `feed` leaves holes**:
it falls behind the socket, the receive buffer fills, and Delta closes the connection.

**Every Python service is capped at one core by its one event loop.** Nothing is disk-bound or
network-bound at 1x or at 10x: `store` writes `derived` 2.33 KB/s against a gp3 baseline of 125 MiB/s,
and `feed` reads `derived` 6.9 Mbit/s against a 0.937 Gbps baseline.

### 2.2 The durable store, tailored

**Write one whole object per table per partition per flush, and never append.** That is already how
`flush` behaves, so the move to S3 changes no code path.

| Rule | Value | Tag | Run behind it |
|---|---|---|---|
| Layout | four dataset prefixes, `date=` and `underlying=`, nothing else | — | [0004](../decisions/0004-durable-store.md) |
| Storage class | S3 Standard | — | Standard-IA bills a 128 KB minimum, and `measured` 100% of `spot-bars` objects are under it |
| Objects a day, per underlying | 1,152 — 288 flushes x 4 tables | `derived` | [durable-store.md](durable-store.md) §3 |
| Bytes retained a day | 143 MB, BTC alone | `measured` | `docs/storage.md` §10 run F |
| Compaction | nightly, every partition strictly before today | — | [0009](../decisions/0009-compaction-cadence.md) |
| Compaction result | 2,040 objects → 8, 16.9% smaller, 4.6 s | `measured` | one real closed day, BTC+ETH, 2026-09-08 |
| `/chain/at`, uncompacted → compacted | 117.8 ms → 21.3 ms | `measured` | 2026-09-09, 2,217,941 rows, **local disk** |

**Compaction is part of the deployment and not housekeeping.** On object storage the file count is a
bill: uncompacted, one `/chain/at` read touches 1,152 objects; compacted it touches 4. It reads every
input, writes a tmp, reads the tmp back in full, writes a manifest, deletes the inputs, and only then
publishes — so an interruption reads short and never doubled. The rules are
[durable-store.md](durable-store.md).

**No latency figure above touched AWS.** Local file system numbers are a floor for S3, and the ratio
between the two columns is the part that transfers.

## 3. The names on the wire

**Build a key from this grammar, and never from the keyspace.** In five lines:

1. **A stream is named `{event_type}:{VENUE}[:{UNDERLYING}]`** — `md.option_quote:DELTA:BTC`.
2. **No environment section** ([0006](../decisions/0006-stream-names-without-environment.md)).
3. **A contract is `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY`**, six parts, joined on `-`.
4. **A consumer group is named for the service**, one per service and never one per instance.
5. **A reader builds its key list from configuration.** No `KEYS`, no `SCAN`, no pattern.

The grammar, the stream list, the envelope fields and the group rules are
[nomenclature.md](nomenclature.md). What each event means is [../events.md](../events.md), which wins
on names and directions.

## 4. The connection controller's policies and rules

**Check the state and the counters at `GET /health` before you touch anything else.** The controller
holds one connection per adapter in exactly one of five states, and every transition publishes one
`feed.connection` event and one log record.

| State | What it means |
|---|---|
| `connecting` | An attempt is open. Nothing has arrived yet. |
| `connected` | Data is arriving. |
| `degraded` | The socket is up and nothing has arrived for `degraded_after`. |
| `reconnecting` | The socket is gone or unusable, and a backoff is running. |
| `stopped` | Not running: paused by an operator, or the budget is spent. |

**The failure all of it refuses.** A websocket that is open and silent is indistinguishable, at the
TCP layer, from one that is open and busy in a quiet market. Every rule below turns *nothing has
arrived* into something a person or a machine can see.

| Rule | Policy | Value and tag |
|---|---|---|
| C1 | The staleness clock is reset by a market-data event and by no other frame. | — |
| C2 | A quiet connection becomes `degraded`, and that is a badge and not an alarm. | `degraded_after` 15 s, `assumed` |
| C3 | A silent connection becomes `reconnecting`, and that raises an `alert` at `error`. | `reconnect_after` 45 s, `assumed` |
| C4 | Grace is given to a new socket and to an attempt, so a reopened socket is not demoted for the gap before it. | — |
| C5 | Backoff doubles from 1 s to a 60 s ceiling, with no jitter. | all `assumed` |
| C6 | The budget counts **consecutive** failures, and only a delivered message restores it. | 10, `assumed` |
| C7 | Silence past `reconnect_after` **ends the socket itself**, then backs off and redials. | #59's one change |
| C8 | Every subscription is replayed on every open, and a socket is not open until its subscribe is on the wire. | — |
| C9 | A spent budget stops the adapter loudly, and nothing restarts it. | 303 s across 11 dials, `derived` |
| C10 | An operator has three verbs: `pause` spends nothing, `resume` restores the budget, `reconnect` spends one. | — |
| C11 | The feed sends a websocket ping every 30 s and publishes a `heartbeat` event every 10 s. | both `assumed` |

**C3's bound is supported by measurement, and the value is still chosen.** The longest quiet gap on
an unbroken connection is `measured` **6.792 s** over a six-hour tagged run, 2026-09-09 13:03–19:03Z
([../quiet-gap.md](../quiet-gap.md)) — 6.6x inside the bound. 45 s also sits under Delta's documented
60 s inactivity limit and above its own 35 s heartbeat window.

**C6 is why the budget is not restored by a socket opening.** Delta can accept a connection and close
it at once: `measured` **21 attempts in 0.3 s** with a budget of 3, still going. A delivered frame
proves the endpoint, the subscription and the decode together.

The reasoning behind every rule, the Nautilus Trader comparison and the rejected options are
[controller-policies.md](controller-policies.md) and
[0003](../decisions/0003-controller-policy.md). The mechanism is
[../lld/controller.md](../lld/controller.md), which wins where it disagrees with the prose here.
