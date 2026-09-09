# 0002 — Where Redis is hosted

**Status** decided, #69, 2026-09-09. **Supersedes** nothing. **Fills**
[../cloud/redis-hosting.md](../cloud/redis-hosting.md). **Evidence**
[../research/0002-redis-hosting.md](../research/0002-redis-hosting.md), prices and sources
[../research/0002b-prices-and-sources.md](../research/0002b-prices-and-sources.md), the
senior's disk question
[../research/0002a-disk-bloat-diagnosis.md](../research/0002a-disk-bloat-diagnosis.md).

## The question

Where should Redis run — a container beside the services, ElastiCache, MemoryDB, or
self-managed on EC2 — for a pipe with thirty minutes of retention and no persistence?

## The options

| | Considered |
|---|---|
| Hosting | **container beside the services**; ElastiCache node (Redis OSS or Valkey); ElastiCache Serverless; MemoryDB; self-managed on a small EC2 |
| Engine | Redis OSS; **Valkey** where a managed tier is used |
| Sizing at 1.027 GiB | `cache.t4g.small`; **`cache.t4g.medium`**; `db.t4g.medium`; `t4g.medium` |

## The decision

**Redis runs as a container beside the services**, one per environment, on the compute R4
(#68) chooses, started with persistence off and a memory ceiling:

```
redis-server --save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

`dev` is that container under Compose on a laptop; `prod` is the same image in the same task
or host definition as `feed`, `store` and `api`. **The named fallback, if any of the five
criteria moves, is ElastiCache for Valkey, one node, `cache.t4g.medium`, no replica, backup
retention 0** — $47.30 a month in ap-south-1 and a change of one endpoint string, because
nothing in the design touches a Redis feature a managed tier withholds.

The retention, acknowledgement and trimming policies this implies are in
[../cloud/redis-hosting.md](../cloud/redis-hosting.md); they confirm #57 and add the
numbers.

## Why, in the criteria's order

**1. Invariants.** Every option can be made safe, and they fail differently when they are
not.

- *The ceiling must exist and must be `noeviction`.* Under `allkeys-lru` Redis evicts whole
  keys, and one of our keys is one stream: `prod:md.option_quote:DELTA:BTC` would simply
  stop existing, with nothing raised. `noeviction` turns the same condition into an error at
  the publisher, which is where our first invariant wants it — loud, at the writer, not
  silent, at the reader. The container ships `noeviction`; ElastiCache defaults to
  `volatile-lru`, which behaves the same only because we set no TTLs.
- *Against serverless.* There is no `maxmemory` to hit, so a trim that stops working is not
  an error at all — it is a bill. Putting the ceiling back means a cache usage limit that is
  not there by default. A failure mode that arrives at the end of the month is the exact
  shape criterion 1 exists to refuse.
- *For all four on data loss.* Redis is a pipe: `store` replays from its last flushed ID and
  the Parquet store is the archive, so losing Redis costs a restart, not history.

**2. Operations burden.** The pipe holds nothing that cannot be rebuilt, so the operational
consequence of running it ourselves is a restart — which is why the usual argument for
managed does not apply. Against that, a container is one more entry in the Compose file and
the task definition I5 (#65) is already writing; a managed cache is a subnet group, a
parameter group, a security group and a second thing to keep in Terraform, for a component
whose worst failure is `feed` exiting loudly. Self-managed EC2 is the loser on this
criterion, not the winner: it is the container's operations plus an AMI to patch and a
process to watch at 02:00.

**3. Cost.** `derived`, ap-south-1, 2026-09-09, at `derived` 1,051.5 MiB (#58) and at ten
times it:

| | now | ten times |
|---|---|---|
| **Container beside the services** | **$0–16.35** | **$32.70–54.82** |
| Self-managed EC2 | $17.08 | $55.55 |
| ElastiCache Valkey node | $47.30 | $262.22 |
| MemoryDB Valkey | $61.83 | $567.38 |
| ElastiCache Serverless Valkey | $65.34 | $653.39 |
| MemoryDB Redis OSS | $410.29 | $3,681.69 |

The container's memory is bought inside R4's compute, so its marginal cost is one
instance-size step or nothing at all. The gap is 3–4x now and 5–12x at ten times the rate.

**4. Path to the right half.** A co-located container dies with its host, and that is the
real cost of this decision. It buys nothing back except the OMS case: the day an order path
puts state on the bus that the venue cannot rebuild, the pipe stops being a pipe and
durability is worth its price. #57 puts OMS, execution and risk out of scope, and every
consumer we can name — `store`, `api`, the Discord alert consumer (#66), an NSE feed —
reads events that Delta or Parquet can produce again. Adding consumers is free on any of the
four: a consumer group costs nothing at the publisher.

**5. Latency to the venue.** Loopback has no network hop; every managed option adds one, and
MemoryDB's synchronous durability adds a documented *single-digit milliseconds* to every
write on top of it. `measured` on loopback, R1's encoding, 100 ms batches: **31.8 µs an
entry, 5.88% of a core**, unchanged when `XTRIM MINID ~` rides on every batch. Unpipelined
it is 699 µs and 129.3% of a core, which is why the publisher pipelines.

**The network hop is unmeasured, and stays unmeasured until AWS access arrives.** There is
no account on this machine. `tools/measure_redis_hosting.py` is written to be re-run from an
EC2 instance against an ElastiCache endpoint in the same AZ and in a second AZ; until then
the managed options' latency column says *unmeasured*, and no figure has been invented for
it. I2's batch interval was also chosen on loopback, so if we ever move to a managed
endpoint that choice is re-measured, not carried over.

## Rejected, and why

| Rejected | Why |
|---|---|
| **MemoryDB** | Criterion 1 and 5, then 3. Durability is the product and cannot be turned off: every write goes to a Multi-AZ transactional log, synchronous writes cost single-digit milliseconds each, and the Redis OSS engine bills `derived` $321.96/month of data-written charges on top of the node — five times the node — to guarantee the one property thirty minutes of rebuildable frames does not need |
| **ElastiCache Serverless** | Criterion 1: no `maxmemory` means a trim bug is a bill, not an error. Criterion 3: the most expensive option that is not MemoryDB, and the only one whose price scales with our read fan-out — every extra consumer group is another 1,849.8 ECPU/s |
| **ElastiCache node** | Nothing is wrong with it; it loses on 2 and 3 against a component whose failure is a restart. **Kept as the named fallback**, `cache.t4g.medium` Valkey, $47.30 |
| **`cache.t4g.small`** | It fits by 0.06% — 692,060 bytes of headroom on a `derived` figure that excludes `computed.chain`. That is a coincidence, not a fit |
| **Self-managed on EC2** | Criterion 2. It is the container's operations plus an operating system to patch, for $0.73/month more than the container and $30 less than managed. It buys separation from the compute host and nothing else |
| **Redis OSS over Valkey**, if a managed tier is ever used | Criterion 3 only: identical API, 20% cheaper node hours on ElastiCache, and free data-written to 10 TB/month on MemoryDB |

## What would change this decision

- **An order path on the bus.** The first event whose truth the venue cannot re-tell —
  an intent, a local fill record — ends the pipe argument. Re-open at MemoryDB, and expect
  the data-written charge to dominate.
- **The network hop, once measured.** If a same-AZ ElastiCache hop turns out to cost less
  than the batch interval already costs, criterion 5 stops defending the container and this
  becomes a criterion 2-and-3 decision, which ElastiCache narrows.
- **R4's compute.** If #68 lands on Fargate or a platform where a sidecar container's memory
  is priced steeply, the container's "$0–16.35" stops being true and the gap to $47.30
  closes. This decision assumes a host we choose the size of.
- **Ten times the rate, sustained.** 10.269 GiB is still one container, but it is `r7g`-class
  memory on the compute host and a jump to `cache.r7g.xlarge` if managed. At that size the
  encoding lever R1 left open — `payload` in MessagePack, one function — is worth pulling
  before the hosting lever.
- **A second team, or an on-call rota we do not have.** "We can restart it" is doing real
  work in criterion 2. If nobody is awake to restart it, buy managed.
- **A measurement we did not take.** `computed.chain` is `derived` under 1% and never
  measured (#58). If it is large, 1,051.5 MiB is low, and `cache.t4g.medium` — not the
  container — is the first thing that stops fitting.
