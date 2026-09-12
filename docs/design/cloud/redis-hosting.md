# Where Redis is hosted, and what it is allowed to keep

The message bus document's hosting section, and its acknowledgement, trimming and
persistence policies. Why each rule is what it is — the four options, the prices, the
measurements — is
[../research/0002-redis-hosting.md](../research/0002-redis-hosting.md); the decision is
[../decisions/0002-redis-hosting.md](../decisions/0002-redis-hosting.md); the names on the
wire are [nomenclature.md](nomenclature.md).

Landed by #69 as a standard; I4 (#63) adds the store's recovery rule below.

---

## 1. Where it runs

**Redis runs as a container beside the services, one per environment**, on the compute R4
(#68) chooses. It is the official image, the same one `dev` runs under Compose.

```
redis-server --save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

| | `dev` | `prod` |
|---|---|---|
| Where | Compose on a laptop | the same image beside `feed`, `store` and `api` |
| Reachable from | the Compose network only | the VPC only; never a public address |
| Sizing | whatever the laptop has | `maxmemory 2gb` — 2× the `derived` 1,051.5 MiB |

Stream names carry no environment: there is one Redis on a laptop and one in prod, and they never share one (I11, #74; decision record 0006).

**The named fallback is ElastiCache for Valkey**, one `cache.t4g.medium` node, no replica,
backup retention 0 — `derived` $47.30/month in ap-south-1, 2026-09-09. Moving there is one
endpoint string, because nothing below uses a feature a managed tier withholds. What would
make us move is listed in the decision record.

**Latency to a managed endpoint is unmeasured** and stays that way until AWS access
arrives. Every latency figure here is loopback, through Docker Desktop's WSL2 port forward,
and is therefore an upper bound for a container and a floor for anything with a network hop.

## 2. Persistence: none

| Setting | Value | Why |
|---|---|---|
| `appendonly` | `no` | The archive is the Parquet store. An AOF of 1,850 writes a second buys a restart we do not need and a disk that grows |
| `save` | `""` | **`redis:7-alpine` ships `save 3600 1 300 100 60 10000`** (`measured`, 2026-09-09). At our rate that is a fork and a snapshot every minute, forever, chosen by nobody |
| Volumes | none | A container with no volume cannot leave a file behind |
| Backups | none | There is nothing in Redis that Parquet does not also hold |

**Redis here is a pipe, not an archive.** Losing it costs a restart: `store` replays from
its last flushed ID, and `api` refills its cache from the events that arrive next, inside one
book refresh — `measured` 508 ms a contract.

**The disk growth the senior fights never starts here** — and, separately, is diagnosed in
[../research/0002a-disk-bloat-diagnosis.md](../research/0002a-disk-bloat-diagnosis.md),
because the most likely cause of his is not Redis at all.

## 3. The memory ceiling, and what happens at it

| Setting | Value | Why |
|---|---|---|
| `maxmemory` | `2gb` in `prod` | 2× the `derived` 1,051.5 MiB at thirty minutes (#58) |
| `maxmemory-policy` | `noeviction` | **The whole point.** At the ceiling Redis returns an error to the writer |

**`noeviction` is a data-loss decision, not a tuning one.** Under `allkeys-lru` Redis evicts
whole *keys*, and one of our keys is one stream: `md.option_quote:DELTA:BTC` would stop
existing with nothing raised. `noeviction` turns the same condition into an `OOM` error at
`feed`, which is where the invariant wants it — loud, at the publisher.

A managed node must have this pinned in its parameter group: ElastiCache's default is
`volatile-lru`, which behaves as `noeviction` only because we set no TTLs, and that is an
accident to depend on. MemoryDB's default is already `noeviction`.

## 4. Retention and trimming

| Rule | Value |
|---|---|
| Retention | **thirty minutes**, by age, never by count |
| Command | `XTRIM <stream> MINID ~ <ms-since-epoch of now − 1800s>` |
| When | **on every batch write**, in the same pipeline as that batch's `XADD`s |
| Which streams | every stream the publisher writes in that batch |

`MINID` trims by id and `~` trims approximately, removing whole macro nodes rather than
counting exactly ([XADD](https://redis.io/docs/latest/commands/xadd/)). Approximate is
correct here: the floor is a clock, so trimming a little late costs a little memory and
never costs an entry a reader still wants.

**Trimming on the write path is free at our shape.** `measured`, 2026-09-09,
`tools/measure_redis_hosting.py`, 100 ms batches of 185 entries: **31.8 µs an entry with the
trim and 31.8 µs without**, p50 batch 5.89 ms against 5.88 ms. There is no case for a
separate trim timer, and a timer would be one more thing that can stop.

**Why age and not `MAXLEN`.** A count is a guess about rate; thirty minutes is the promise
made to a restarting `store`. When the market goes quiet, `MAXLEN` would hold hours; when it
goes loud, it would hold four minutes. The promise has to be the unit.

## 5. Acknowledgement

| Thing | `store` | `api` |
|---|---|---|
| Consumer group | `store` | `api` |
| Group start id | checkpoint id in `<root>/_store-checkpoint.json` for that stream; on first start the head (`$`, taken once as a concrete id); never `0` | `$` — never replay |
| Ack | **on receipt**, before the work | **on receipt** |
| After a restart | reads forward from each stream's recorded `Position` (id plus index) in `<root>/_store-checkpoint.json` | joins at `$`; the cache refills from the events that arrive next, inside one `measured` 508 ms book refresh |
| Trimmed position | replays the retained suffix; counts, alerts and logs the loss with both bounds; never refuses start-up | not applicable |
| Pending list | never used for recovery | never used |

Starting at `0` for the cutover would re-record up to the `derived` thirty minutes the old
writer already put in Parquet, creating duplicates. The accepted cutover leaves the few
seconds between stopping that writer and starting `store` unrecorded. `store.state` also
crosses the pipe as the venue-scoped `store.state:DELTA` key: one extra key and one event every
ten seconds (`derived`), negligible against the `measured` 1,849.8 events/s bus rate.

**One consumer group per service, never one per instance** ([nomenclature.md](nomenclature.md)
§5). Two groups on one stream each receive every entry, which is what makes the store and
the screen independent readers rather than competitors for one message.

**Acking on receipt and replaying from a recorded id is deliberate, and it is not the
textbook pattern.** The textbook has a consumer ack after the work and recover from the
pending list with `XAUTOCLAIM`. Ours records the id it last *flushed* and reads forward from
there, because the flush is the durability boundary — five minutes of bars are in memory
until a Parquet file lands, so a per-message ack tells us nothing about what survived. This
closes the five-minute loss window #57 names. **A Redis restart is the only way to lose that
replay**, and that is the accepted cost of a pipe with no persistence.

**`XACK` frees no memory** — `measured`, same run: 5,000 entries acked, `XLEN` still 5,000,
`MEMORY USAGE` unchanged at 1.81 MB; only `XTRIM` moved it, to 730 KB. Acknowledging says
"this group is done with it", not "delete it". Retention is §4's job and nothing else's.

## 6. What each consumer costs, and the numbers behind it

| Number | Tag | Source |
|---|---|---|
| 1,051.5 MiB at thirty minutes | `derived` | #58, encoding B, from `measured` per-entry sizes and 1,693.6 frames/s |
| 1,849.8 events/s | `derived` | #58 |
| 598.2 KiB/s written | `derived` | #58 |
| `XADD` unpipelined: p50 699.0 µs, 129.3% of a core | `measured` | `tools/measure_redis_hosting.py`, 2026-09-09, loopback |
| Pipelined 100 ms: 31.8 µs/entry, 5.88% of a core | `measured` | same run |
| Pipelined 100 ms **with the trim**: 31.8 µs/entry | `measured` | same run |
| `redis:7-alpine` ships `save '3600 1 300 100 60 10000'`, `appendonly no`, `noeviction` | `measured` | same run, §1 |
| `XACK` frees no stream memory; `XTRIM` does | `measured` | same run, §5 |
| Container marginal cost $0–16.35/mo; ElastiCache Valkey $47.30/mo | `derived` | AWS Price List Bulk API, ap-south-1, read 2026-09-09 |
| Latency of a managed endpoint | **unmeasured** | no AWS access; the plan is in the findings §5 |

**The batch interval is not fixed here.** I2 (#61) measures 10, 50 and 100 ms against the
live feed and chooses one. This section fixes only what rides *with* the batch: the trim,
and that the trim is free.
