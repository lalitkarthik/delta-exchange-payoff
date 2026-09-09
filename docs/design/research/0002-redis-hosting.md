# R5 — Where Redis lives, for a pipe that holds thirty minutes

Findings for #69, under epic #57. The decision is
[../decisions/0002-redis-hosting.md](../decisions/0002-redis-hosting.md), the spec section
[../cloud/redis-hosting.md](../cloud/redis-hosting.md), the senior's disk question
[0002a-disk-bloat-diagnosis.md](0002a-disk-bloat-diagnosis.md). Sizing comes from R1
([0001-stream-naming-and-payload-format.md](0001-stream-naming-and-payload-format.md)).

## The question

Where should Redis run — a container beside the services, ElastiCache, MemoryDB, or
self-managed on EC2 — for a pipe with thirty minutes of retention and no persistence?

Decided against #57's criteria, in order: **(1)** invariants — no silent data loss, never
forward-fill, `null` is not `0`; **(2)** ops burden for two or three people; **(3)** cost at
our rate and ten times it; **(4)** path to OMS, NSE and more consumers; **(5)** latency to the
venue.

**What is being sized.** `derived` **1,051.5 MiB** at thirty minutes — R1's decided encoding
B, from `measured` per-entry sizes and the `measured` 1,693.6 venue frames/s (#58). That is
1,102,577,664 bytes, **1.027 GiB**; ten times the rate is **10.269 GiB**. Write volume is
`derived` 598.2 KiB/s, **1,609.8 GB a month**, 16.1 TB at ten times.

## 1. The four options against the five criteria

| | (1) Invariants | (2) Ops for 2–3 people | (3) Cost/mo, ap-south-1 | (4) Path to the right half | (5) Latency |
|---|---|---|---|---|---|
| **Container beside the services** | `maxmemory` + `noeviction` makes a full pipe a loud `OOM` error, not an eviction. `measured`: the image ships `noeviction` | one more container in the Compose file and the task definition I5 already writes; no VPC object, no parameter group | `derived` **$0–16.35** — one instance-size step on R4's host | dies with its host; more consumers are free (groups) | **no network hop** — loopback or a unix socket |
| **ElastiCache, single node** | same, but `maxmemory-policy` defaults to `volatile-lru`; with no TTLs that behaves as `noeviction`. `allkeys-lru` would evict a **whole stream key** | AWS patches and replaces the node; we own a subnet group, a parameter group, a security group and one more Terraform surface | **$47.30** Valkey / $59.13 Redis OSS, `cache.t4g.medium` | survives host replacement; NSE and an OMS attach the same way | one VPC hop, **unmeasured** |
| **ElastiCache Serverless** | **no `maxmemory` to hit.** A trim bug stops being a loud error and becomes an unbounded bill; a cache usage limit has to be set to get the error back | least to run; nothing to size | `derived` **$65.34** Valkey / $97.28 Redis OSS | same as above; always Multi-AZ, cannot be single-AZ | one VPC hop, **unmeasured** |
| **MemoryDB** | `noeviction` by default. Durability is not optional: every write goes to a Multi-AZ transactional log | as above; nothing extra | **$61.83** Valkey node, **+$0** data written (under 10 TB); **$88.33 + $321.96 = $410.29** Redis OSS | the only option that could hold an OMS's non-rebuildable state | one VPC hop **plus** the log: synchronous writes are documented at *single-digit milliseconds* |
| **Self-managed on EC2** | ours to configure, same as the container | we patch the AMI, we watch the process, we own the failure at 02:00 — for data that is rebuildable | `derived` **$17.08** (`t4g.medium` + 8 GB gp3) | a second single point of failure to keep alive | one VPC hop, **unmeasured** |

## 2. Sizing: which node, and why that one

ElastiCache and MemoryDB publish a `maxmemory` per node type, and ElastiCache reserves a
share of it: `reserved-memory-percent`, **default 25**. So usable memory is
`maxmemory × 0.75`. MemoryDB's parameter list documents no `reserved-memory-percent`; the
same 25% is applied to it as an `assumed` operating headroom, so the two are compared on
one rule.

| Node | `maxmemory`, B | usable at 25% | 1.027 GiB | 10.269 GiB |
|---|---|---|---|---|
| `cache.t4g.small` | 1,471,026,299 | 1.027 GiB | **misses by 0.66 MB** | no |
| `cache.t4g.medium` / `db.t4g.medium` | 3,317,862,236 | 2.32 GiB | **yes** | no |
| `cache.r7g.large` | 14,037,181,030 | 9.80 GiB | yes | **no — short by 0.47 GiB** |
| `cache.r7g.xlarge` / `db.r7g.xlarge` | 28,261,849,702 | 19.74 GiB | yes | **yes** |

Two facts worth keeping. **`cache.t4g.small` fits our pipe by 0.06%** — 1,103,269,724 usable
bytes against 1,102,577,664 needed — which is not a fit, it is a coincidence, so the
one-times row buys `t4g.medium`. And **`cache.r7g.large` misses ten times the rate by half a
gigabyte**, so the ten-times row jumps two sizes to `r7g.xlarge` and the bill roughly
doubles. Lowering `reserved-memory-percent` to 10 would let `r7g.large` hold it
(11.77 GiB) at $130.82 — a tuning decision, not a fact, and it is not assumed here.

## 3. Can persistence be turned off? — the question #69 says to notice

| Option | AOF | RDB | Verdict |
|---|---|---|---|
| Container / EC2 | `appendonly no` is the shipped default | `save ""` disables snapshotting | **Yes.** `measured`: `redis:7-alpine` ships `save '3600 1 300 100 60 10000'` and `appendonly 'no'`, so **snapshots are on unless you say otherwise** |
| ElastiCache node | **not available at all** — "Redis OSS configuration variables `appendonly` and `appendfsync` are not supported"; the parameter table lists both as `Modifiable: No`, default off | backups off by setting the retention limit to 0 | **Yes**, and AOF is not even offered. Multi-AZ durability is a separate opt-in, priced at +18% of the node hour for synchronous writes |
| ElastiCache Serverless | not exposed | automatic backups optional, retention 0 disables | **Yes for disk**, but the cache is Multi-AZ by construction |
| **MemoryDB** | not exposed | snapshots free at 1-day retention | **No.** "MemoryDB also stores data durably across multiple Availability Zones using a Multi-AZ transactional log", and you are billed for "the volume of data (in GB) you write" |

**This is the finding the ticket predicted.** MemoryDB's durability is the product, not a
setting, and for a pipe carrying thirty minutes of rebuildable frames it is a cost with no
buyer: at our rate the Redis OSS engine adds `derived` **$321.96 a month of data-written
charges alone**, five times the node. Valkey's free 10 TB/month hides that at our rate and
stops hiding it at ten times, where 6.1 TB overflows at $0.04/GB — `derived` $243.92.

Redis's own page states the third option plainly: "**No persistence**: You can disable
persistence completely."

## 4. Cost, at 1,051.5 MiB and at ten times it

All prices read **2026-09-09** from the AWS Price List Bulk API — ElastiCache and EC2 offer
files published 2026-09-08, MemoryDB 2026-08-31. Region **ap-south-1 (Mumbai)** primary,
**us-east-1 (N. Virginia)** second. One month is 730 hours. Every figure is `derived`:
`measured` unit prices times our `derived` sizing.

| Option | Choice at 1.027 GiB | ap-south-1 | us-east-1 | Choice at 10.269 GiB | ap-south-1 | us-east-1 |
|---|---|---|---|---|---|---|
| Container beside the services | +2 GiB on R4's host | **$8.18–16.35** | $12.26–24.53 | +12 GiB on R4's host | **$32.70–54.82** | $49.06–78.18 |
| ElastiCache Valkey, single node | `cache.t4g.medium` | **$47.30** | $37.96 | `cache.r7g.xlarge` | **$262.22** | $255.21 |
| ElastiCache Redis OSS, single node | `cache.t4g.medium` | $59.13 | $47.45 | `cache.r7g.xlarge` | $327.77 | $319.01 |
| ElastiCache Serverless, Valkey | 1.10 GB stored | **$65.34** | — | 11.03 GB stored | **$653.39** | — |
| MemoryDB Valkey | `db.t4g.medium` | **$61.83** | $49.57 | `db.r7g.xlarge` | **$567.38** | $559.21 |
| MemoryDB Redis OSS | `db.t4g.medium` | $410.29 | $392.77 | `db.r7g.xlarge` | $3,681.69 | $3,670.01 |
| Self-managed EC2 | `t4g.medium` + 8 GB gp3 | **$17.08** | $25.17 | `r7g.large` + 8 GB gp3 | **$55.55** | $78.82 |

Every unit price, the arithmetic behind each cell, and every source URL used anywhere in
this file are in [0002b-prices-and-sources.md](0002b-prices-and-sources.md). Data transfer
is $0 for either managed option in the same AZ as the compute, and $0.01/GiB each way
across AZs, charged on the EC2 side.

## 5. `XADD` on loopback, `measured`; the network hop, not measured

`tools/measure_redis_hosting.py`, 2026-09-09, Redis 7.4.11 in `redis:7-alpine` on
`127.0.0.1:6399`, R1's encoding B, real decoded frames in the `measured` type mix — 1,849
entries a second, 340.5 B mean.

| | p50 | p95 | p99 | per entry | at 1,849.8 events/s |
|---|---|---|---|---|---|
| `XADD`, one per event | 699.0 µs | 973.3 µs | 2,371.3 µs | 699.0 µs | **129.3% of a core** |
| Pipelined, 10 ms batch (18) | 1.38 ms | — | 5.34 ms | 76.9 µs | 14.23% |
| Pipelined, 50 ms batch (92) | 3.52 ms | — | 7.00 ms | 38.3 µs | 7.09% |
| Pipelined, 100 ms batch (185) | 5.88 ms | — | 15.06 ms | 31.8 µs | **5.88%** |
| 100 ms batch **with `XTRIM MINID ~`** | 5.89 ms | — | 13.28 ms | 31.8 µs | 5.88% |

**Trimming on every batch write is free at this shape** — 31.8 µs an entry either way, inside
run-to-run noise. That is the epic's trim policy paid for.

**The caveat, quoted from the vault's spike write-up, still governs every figure above:**
"Docker Desktop on Windows routes localhost through a WSL2 VM, so every Redis call pays a
port-forward hop. Native Linux Redis over loopback or a unix socket is typically several
times faster. The ratios and the batching lesson transfer; the microsecond figures do not."
So these are **upper bounds** for a co-located container and a **floor** no managed Redis can
beat: the container's real number is smaller, and the hop is not on it at all.

**The network hop to a managed Redis cannot be measured here.** There is no AWS account on
this machine; #69 was unblocked on #65 (I6), and the cloud tickets run after access arrives.
Stating a number for it would be inventing one. The plan for when access arrives, in one
paragraph: bring up `cache.t4g.medium` in the same AZ as an EC2 instance in the same subnet,
run this same tool from that instance against (a) a `redis:7-alpine` container on loopback,
(b) the same container over a unix socket, (c) the ElastiCache endpoint, and (d) the same
endpoint from a second AZ; report p50/p99 of the unpipelined `XADD` and of the 100 ms batch,
and re-run I2's batch-interval choice against the (c) number, because the interval was
chosen on loopback and a hop changes the arithmetic. Budget one hour and one node-hour.

### What `XACK` does not do

`measured`, same run, 5,000 entries in one stream:

| | `MEMORY USAGE` | `XLEN` |
|---|---|---|
| written | 1,808,964 B | 5,000 |
| after `XREADGROUP`, 5,000 pending | 6,980,267 B | 5,000 |
| after `XACK` of all 5,000 | 1,809,531 B | **5,000** |
| after `XTRIM MINID 4000-0` | 730,203 B | 2,000 |

**Acknowledging returns the pending list's memory and deletes nothing.** The stream is the
same length. Only trimming frees a stream — which is why the trim is on the write path and
not on a timer, and why cause 1 in
[0002a-disk-bloat-diagnosis.md](0002a-disk-bloat-diagnosis.md) is worth ruling out on the
senior's machine before anyone changes broker.

## What I learned

| Term | What it means here |
|---|---|
| **Pipe, not archive** | Redis holds thirty minutes of frames that the Parquet store also holds. Nothing in it is unique for longer than one flush. |
| **Persistence** | Writing memory to disk so a restart can reload it. Two kinds: **AOF** logs every write; **RDB** takes periodic snapshots. |
| **`maxmemory` / eviction policy** | The ceiling, and what Redis does at it. `noeviction` refuses new writes with an error; `allkeys-lru` deletes keys — whole streams — to make room. |
| **`reserved-memory-percent`** | ElastiCache's share of the node kept back from data. Default 25, so a 3.09 GiB node holds 2.32 GiB. |
| **ECPU** | ElastiCache Serverless's billing unit: one per kilobyte moved, per request. Our events are 340 B, so one operation is one ECPU. |
| **Multi-AZ transactional log** | MemoryDB's durability: every write copied to two more Availability Zones before, or just after, it is acknowledged. |
| **Managed** | AWS patches it, replaces it and pages itself. You get an endpoint and a bill; you lose the config file. |

**A pipe with nothing unique in it does not need durability, and durability is what managed
Redis mostly sells.** MemoryDB's whole pitch is that a write survives losing an Availability
Zone. For thirty minutes of quotes we already have on disk in Parquet, that is a guarantee
with no buyer — and it is not a checkbox we can clear. Buying MemoryDB here would be paying
for the one property the design deliberately does not need.

**"Turn persistence off" is three different sentences on four products.** On a container it
is two flags. On ElastiCache, AOF is not offered at all and snapshots are a retention
number you set to zero. On serverless there is nothing to turn off and nothing to size. On
MemoryDB it cannot be done. The question `#69` told us to ask — *does this tier let you turn
persistence off* — has four different answers and one of them decides the ticket.

**The default is not off.** `redis:7-alpine` starts with `save 3600 1 300 100 60 10000`:
snapshot if 10,000 keys changed in a minute, which at 1,850 writes a second is **every
minute, forever**. Nobody chose that; it is what happens if nobody chooses. That is exactly
the shape of the senior's disk problem, and it is one flag.

**Eviction is not trimming, and one of them is silent.** Trimming deletes the oldest entries
of a stream and is ours to command. Eviction deletes *whole keys* when memory runs out and
is Redis's to choose — under `allkeys-lru` a stream nobody read recently simply stops
existing, with nothing raised. `noeviction` converts that into an error on the writer, which
is the failure our first invariant wants: loud, at the publisher, not silent, at the reader.

**`XACK` frees no memory.** We measured it: 5,000 entries acknowledged, `XLEN` still 5,000.
Acknowledging says "this consumer group is done with it", not "delete it". A system that
acks diligently and never trims grows exactly as fast as one that does neither — which is
candidate 1 in the senior's diagnosis, and the reason our trim rides on every batch write.

**Serverless changes the shape of being wrong.** With a node you buy a ceiling, and a bug
that stops trimming hits it and raises an error. With serverless there is no ceiling; the
same bug scales the cache and arrives as a bill at the end of the month. A cache usage
limit puts the ceiling back, and it is not there by default.

**A hop you have not measured is not a number.** I2 chose its batch interval on loopback.
Every managed option adds a VPC hop that this machine cannot measure, and the honest entry
in the table is *unmeasured*, with a plan attached — not a plausible-looking microsecond
figure. The one thing we can say is directional and comes free: loopback has no hop, so the
container's latency is a floor the others are measured against.
