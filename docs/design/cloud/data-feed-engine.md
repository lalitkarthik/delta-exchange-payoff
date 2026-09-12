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
2. **Amazon EC2**, one `c7g.xlarge` in a public subnet of `ap-south-1`, is the instance.
3. **Amazon S3 Standard** holds the four bar tables, in the `date=`/`underlying=` partition layout
   the engine already writes.
4. **Amazon VPC** gives one public subnet and one in-use public IPv4; there is no NAT gateway.
5. **Amazon ECR and CloudWatch** carry the images and the alarms.

The full list, the fallbacks and the bill are [services.md](services.md).

### 1.1 The platform, compared

**Read the chosen row first, then the criterion the tie moved to.** #57's five criteria, in order.
Dollars are `derived` from `measured` Price List Bulk API unit prices,
ap-south-1, on-demand, 730 hours; the arithmetic is
[../research/0005b-prices-and-sources.md](../research/0005b-prices-and-sources.md) §3.

| | (1) Invariants | (2) Ops for 2–3 people | (3) $/mo, 1x → 10x | (4) Path to the right half | (5) Latency |
|---|---|---|---|---|---|
| **ECS on EC2** ← chosen | a dead container restarts, **and the restart is an alarmable event** | one AMI to patch; restarts and deploys belong to the platform | **$48.94 → $179.43** | a new consumer is a container in the task | Redis on `127.0.0.1` under `host` mode |
| EC2 with Compose | identical, but **nothing records that it restarted** | the same AMI, plus every deploy is an `ssh` | **$48.94 → $179.43** | one YAML file on one instance | the same |
| ECS on Fargate | identical; no host to misconfigure | the lightest: no AMI, no agent, no disk | $89.33 → $242.41 | the best per-service isolation | `awsvpc` is mandatory, so a VPC hop sits between `feed` and Redis |
| Amazon EKS | identical | the heaviest: a Kubernetes minor upgrade at least yearly | $121.94 → $252.43, one instance | the most, and the most unused | the same as ECS on EC2 |

**Two rows tie exactly on cost**, because ECS on the EC2 launch type charges $0 for compute. When
criterion 3 ties, the decision moves up to criterion 2, and that is where Compose loses. The full
table, the region comparison and the operations list are [compute.md](compute.md);
the decision is [0005](../decisions/0005-compute-and-region.md).

### 1.2 One instance, or several

**Check the load against 2.8 cores before you read this table: it is what moves us off T1.**

| Topology | 1x upper | 10x upper | Why it is not chosen |
|---|---|---|---|
| **T1, one instance** ← chosen | **$78.07** | **$582.39** | — |
| T2, `feed`+Redis ∣ rest | $84.46 | $588.70 | `store` crosses the hop to Redis |
| T3, one instance per service | $128.07 | $670.86 | three instances and two hops on the lossless path |
| T4, `feed`+`store`+Redis ∣ `api`+`web`+proxy ← **next** | $73.22 | $732.08 | it is the growth path, not today's shape |

All `derived`, [0008](../decisions/0008-topology.md) §3, Bulk API published 2026-09-10, read
2026-09-12. **We move to T4 when the instance passes `derived` 2.8 cores — 70% of the
`c7g.xlarge`'s 4 vCPU ([0008](../decisions/0008-topology.md)) — or when `oms` places its first
order**, whichever comes first. The cell-by-cell comparison is
[compute-topology.md](compute-topology.md).

## 2. The system design on those services

**Follow one quote through the system: this is the path, and every hop on it is loopback.**

1. **`feed` holds the venue websocket** and turns each venue frame into a canonical event.
2. **`feed` publishes** the event to its outbox, which never blocks the socket reader.
3. **The flusher performs one bus flush every 50 ms**, writing one batch with the trim in the
   same pipeline.
4. **`store` reads losslessly** through its consumer group, folds ticks into one-minute bars, and
   performs a **store flush** to Parquet every 300 s (`FLUSH_SECONDS`, `assumed`,
   `deltapayoff.store`). It reads forward from its checkpoint, and the store flush is the
   durability boundary ([0010](../decisions/0010-store-replay.md) R1 to R3).
5. **`api` reads drop-oldest**, keeps the newest event per contract, and serves the ladder.

**Only `feed` contacts Delta, and no service calls another over HTTP.** That is #57's rule and
[hld.md](../hld.md) §2 is the shape it produces.

### 2.1 What each container reserves, and what it actually costs

**Compare the reservation against the cost beside it: `feed` is the one that is deliberately
over-reserved, and I13 (#79) has now `measured` how deliberately.**

| Container | Reserved | `measured` at 1x | `derived` at 1x | Bound by |
|---|---|---|---|---|
| `feed` | 1.0 vCPU, 1 GB | **0.3592 core**, 97.1 MiB | 0.52–0.71 core | CPU, one event loop |
| `store` | 0.5 vCPU, 1 GB | **0.3048 core**; 1.49 KB/s to disk | 0.28–0.51 core; 2.33 KB/s | CPU, decode and fold |
| `api` | 1.0 vCPU, 2 GB | **0.3045 core**; +0.0466 the first viewer, +0.0168 each of the next two | 0.25–0.49 core, + 0.05–0.14 a watched expiry | CPU, set by viewers |
| `web` and proxy | 0.5 vCPU, 1 GB | **0.0388 / 0.0109 core**, 110.6 / 17.1 MiB | ~0 core | memory |
| `redis` | 0.5 vCPU, 3 GB | **0.0706 core**, 761.8 MiB | — | memory, `maxmemory 2gb` plus overhead |

The `measured` column is [../research/0007a-container-measurement.md](../research/0007a-container-measurement.md),
`measured` over **5h12m and not one day**, at `derived` 0.86x of 1x; the `derived` column is
[0007](../decisions/0007-load-profile.md); the reservations are [compute.md](compute.md) §4, and
ECS spells `feed`'s whole vCPU as **1,024 CPU units** ([0008](../decisions/0008-topology.md)).
**`feed` reserves a whole vCPU, because a starved `feed` leaves holes.** It falls behind the
socket. The receive buffer fills. Delta then closes the connection. **Every reservation still
holds at the `measured` figures, `feed`'s with 0.64 core to spare.**

**One event loop caps every Python service at one core.** Nothing is disk-bound or network-bound
at 1x or at 10x, and the measurement confirms it with room: `store` writes `measured` 1.49 KB/s
against gp3's published baseline of 125 MiB/s, and `feed` reads `measured` 5.80 Mbit/s against
the `.large` classes' published baseline of 0.937 Gbps — 0.62% of a figure the chosen
`c7g.xlarge` doubles. **`store`'s disk cell is measured at the source**, 248 flush files into
`.stack-data/`: `docker stats` BlockIO does not observe a bind mount and reported 25.4 B/s.

### 2.2 The durable store, tailored

**Write one whole object per table per partition per store flush, and never append.** That is
already how the store flush behaves, so the move to S3 changes no code path. The replay, the
checkpoint and the flush intent that make it exactly-once are
[0010](../decisions/0010-store-replay.md) R1 to R3.

**Every rule, figure, tag and run is
[data-feed-engine-numbers.md](data-feed-engine-numbers.md)** — the layout, S3 Standard against the
128 KB cliff, 1,152 objects a day, 143 MB retained, and what compaction returned on a real day.
Nightly compaction is [0009](../decisions/0009-compaction-cadence.md). **Quote a number from
there, never from a sentence here.**

**Compaction is part of the deployment and not housekeeping.** On object storage the file count is a
bill: uncompacted, one `/chain/at` read touches 1,152 objects; compacted it touches `derived` 4.
**Compaction runs these six steps in this order**, so an interruption reads short and never doubled:

1. Read every input object in the partition.
2. Write one tmp object per table.
3. Read the tmp back in full.
4. Write the manifest.
5. Delete the inputs.
6. Publish, and only now.

The rules are [durable-store.md](durable-store.md). **No latency figure in either file touched
AWS**: a local file system number is a floor for S3, and the ratio between the two columns is the
part that transfers.

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

**These five rules decide what the feed does when the venue goes quiet.** The other six — grace,
backoff, resubscription, giving up, the operator's three verbs and the two heartbeats — are one
section each in [controller-policies.md](controller-policies.md) §2, with their values and their
reasons.

| Rule | Policy | Value and tag |
|---|---|---|
| C1 | A market-data event resets the staleness clock. No other traffic does. | — |
| C2 | A quiet connection becomes `degraded`, and that is a badge and not an alarm. | `degraded_after` 15 s, `assumed` |
| C3 | A silent connection becomes `reconnecting`, and that raises an `alert` at `error`. | `reconnect_after` 45 s, `assumed` |
| C6 | The budget counts **consecutive** failures, and only a delivered message restores it. | 10, `assumed` |
| C7 | Silence past `reconnect_after` **ends the socket itself**, then backs off and redials. | #59's one change |

**Measurement supports C3's bound, and a person still chose the value.** The longest quiet gap on
an unbroken connection is `measured` **6.792 s** over a six-hour tagged run, 2026-09-09 13:03–19:03Z
(`tools/measure_quiet_gap.py`, run `20260909T130306Z`, [../quiet-gap.md](../quiet-gap.md)) —
`derived` 6.6x inside the bound. 45 s also sits under Delta's documented 60 s inactivity limit and
above its own documented 35 s heartbeat window
([0003](../decisions/0003-controller-policy.md)).

**C6 is why an opening socket restores nothing.** Delta can accept a connection and close
it at once: `measured` **21 attempts in 0.3 s** with a budget of 3, still going
(2026-09, recorded in `engine/tests/test_feed.py`; [../lld/reconnect.md](../lld/reconnect.md)).
A delivered venue frame proves the endpoint, the subscription and the decode together.

**C9 gives up after `derived` 303 s across 11 dials** — 1+2+4+8+16+32+60+60+60+60 s of backoff
from C5's ladder — and nothing restarts the adapter after that
([controller-policies.md](controller-policies.md) §2).

The reasoning behind every rule, the Nautilus Trader comparison and the rejected options are
[controller-policies.md](controller-policies.md) and
[0003](../decisions/0003-controller-policy.md). The mechanism is
[../lld/controller.md](../lld/controller.md), which wins where it disagrees with the prose here.
