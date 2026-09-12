# 0004 — Where the durable store lives

**Status** decided, #67, 2026-09-09. **Supersedes** nothing. **Fills**
[../cloud/durable-store.md](../cloud/durable-store.md). **Evidence**
[../research/0004-durable-store.md](../research/0004-durable-store.md), prices and sources
[../research/0004a-prices-and-sources.md](../research/0004a-prices-and-sources.md),
absence, read paths and OMS per option
[../research/0004b-absence-reads-and-oms.md](../research/0004b-absence-reads-and-oms.md).

## The question

Where should the four bar tables live — S3 as Parquet, S3 plus Athena, RDS Postgres, RDS
with Timescale, Timestream, or an EBS volume on the compute host?

## The options

| | Considered |
|---|---|
| Object storage | **S3 Parquet, hive `date=/underlying=` layout unchanged**; S3 plus Athena for ad hoc SQL |
| Storage class | **S3 Standard**; S3 Intelligent-Tiering; S3 Standard-IA |
| Relational | RDS for PostgreSQL, Single-AZ; RDS with `timescaledb` |
| Time series | Timestream for LiveAnalytics; Timestream for InfluxDB |
| Block | EBS gp3 on the compute host, with a daily snapshot policy |

## The decision

**The four tables stay Parquet in the hive `date=/underlying=` layout, on S3 Standard, in
one bucket per environment, compacted nightly to one object per table per partition.**
`dev` is the local `data/` directory the engine writes today; `prod` is the same tree under
an `s3://` prefix, reached by `pl.scan_parquet` with `storage_options` — the same call
`store.py` makes now with a different root. No schema changes, no column changes, no change
to the never-forward-fill rule, because none of them is what moved.

**Compaction becomes load-bearing rather than housekeeping**, and the nightly job is part
of the deployment rather than an optional tidy-up: it is what keeps the read bill and the
read latency where §3 says they are.

**S3 Standard, not Intelligent-Tiering, for now.** Intelligent-Tiering is `derived` $0.82 a
month cheaper at our rate and its saving depends on nobody reading a day older than thirty
days — which is the one thing a backtesting product does. Revisit when there is a measured
access pattern to tier against; the switch is a bucket setting and a lifecycle rule.

**Athena is not bought.** It reads the same files and can be added later with one Glue
catalogue and no change to the store. Nothing in the engine's read path needs it, and it
would be paid for in queries somebody has yet to want to write.

**The named fallback, if criterion 4 moves, is RDS for PostgreSQL beside the bars, not
instead of them** — a `db.t4g.medium` at $61.32 a month for OMS state, with the bars left
on S3.

## Why, in the criteria's order

**1. Invariants.** Two of the six options never reached this test: `timescaledb` is not in
RDS for PostgreSQL's supported-extension list, and Timestream for LiveAnalytics has been
closed to new customers since **2025-06-20** — and with no AWS account yet, we are a new
customer. Of the four that remain:

- *Absence is free on files and has to be defended everywhere else.* A minute with no
  arrivals produces no bar, so nothing is appended and no row exists. On Postgres the same
  absence has to survive a schema with no defaults, an insert path that never coalesces and
  a hundred columns of `NOT NULL` decisions; on a time-series store it has to survive
  `fill(previous)` and `interpolate_locf`, functions whose entire purpose is to put the
  minute back. Our rule would stop being structural and become a convention — the exact
  shape of Delta's own `/v2/history/candles`, 801 bars of which 797 are invented.
- *Against EBS, on durability.* gp3 is documented at **99.8–99.9% volume durability with an
  annual failure rate no higher than 0.2 percent**, in **one** Availability Zone. S3 is
  designed for **99.999999999% across at least three**. For the only copy of a year of bars
  that no venue will re-serve, that is nine orders of magnitude, and snapshots turn a
  volume loss into a restore of up to a day's loss rather than none.
- *For all four on partial writes.* Compaction is the only part of the store that deletes,
  and its ordering — verify by full read-back, write the manifest, delete, then publish —
  is what makes an interruption read *short* rather than *doubled*. That property lives in
  `store.py` and is unchanged by where the bytes sit.

**2. Operations burden.** A bucket and a lifecycle rule against an instance with a version,
a maintenance window, a parameter group, a backup retention and a size to grow by hand.
There is no failover to design because there is no server. The nightly compaction job is
the only moving part this decision adds, and it already exists and is already
crash-tested at every one of its six stages.

**3. Cost.** `derived`, ap-south-1, month 12 of year one, at `measured` 143 MB/day and at
ten times it:

| | now | ten times |
|---|---|---|
| **S3 Standard, compacted** | **$1.52** | **$15.18** |
| S3 Standard + Athena | $3.66 | $36.63 |
| S3 Standard, never compacted | $29.31 | $293.05 |
| EBS gp3 + 7 daily snapshots | $16.20 | $120.93 |
| RDS PostgreSQL, Single-AZ | $94.07 | $499.86 |
| Timestream for InfluxDB | $120.28 | $413.67 |
| Timestream for LiveAnalytics | $1,752.06 | $2,041.73 |

The gap is 10x to RDS now and 33x at ten times the rate. Two shapes matter more than the
ordering. **A database is priced for being awake**: `db.t4g.medium` costs $61.32 a month to
exist, and this store is idle almost all of the time. **A row store is not paying for our
bytes**: `measured`, the same 2,217,941 rows are 107.8 MB of Parquet and 412.5 MB
uncompressed — 3.83x — and `derived` 4.4x once Postgres's per-row overhead is added, at
$0.131/GB-month against S3's $0.025.

**4. Path to the right half.** Every consumer we can name reads the same files with no
server between: the dashboard's three historical routes, the compaction job, a backtest in
Polars, a backtest in DuckDB — `read_parquet('…/*/*/*.parquet', hive_partitioning = true)`
with partition filters "automatically pushed down". An NSE adapter adds a column and a
partition value, not a migration. **OMS is the one thing this decision does not serve, and
it should not**: order intents and fills are row-shaped, mutable and transactional, and an
object store has neither row-level update nor a transaction. The answer is a small Postgres
beside the bars, which is a cheaper change than moving 52 GB a year of immutable
append-only history into a database that does not want it.

**5. Latency.** `measured` 2026-09-09 on the real 2026-09-08 day, 2,217,941 rows, local
disk, minimum of five:

| Read | uncompacted | compacted | ratio |
|---|---|---|---|
| whole day, four tables | 206.4 ms | **82.0 ms** | 2.5x |
| one minute, four tables (`/chain/at`) | 117.8 ms | **21.3 ms** | 5.5x |
| one contract's day, two tables (`/bars`) | 137.2 ms | **41.2 ms** | 3.3x |

**These are local file system numbers and S3 will be slower**; they are a floor, and the
ratio between the two columns is the part that transfers. **No S3 latency is measured
here** — there is no AWS account on this machine — and none has been invented. The plan
when access arrives is in *Still open* below.

## Rejected, and why

| Rejected | Why |
|---|---|
| **RDS with Timescale** | **Not available.** `timescaledb` appears in no supported-extension table for RDS for PostgreSQL 15 through 19. AWS's own guidance for time series on RDS Postgres is declarative partitioning and `pg_partman` — which is not Timescale, and is what our directory layout already does |
| **Timestream for LiveAnalytics** | **Not available.** Closed to new customers since 2025-06-20, and we have no account. Even if it were open, ap-south-1's price list carries no per-GB-scanned SKU — only provisioned TCU, minimum 4, `derived` **$1,719.88 a month whether or not a query runs** |
| **Timestream for InfluxDB** | Criterion 2 and 3, then 4. $120.28 a month, a managed instance to run, and a second query language beside Polars for a store whose readers are all ours. It holds no OMS state either, so a second store is still required |
| **RDS PostgreSQL for the bars** | Criteria 2 and 3. Nothing is wrong with it, and it is the right home for the thing that comes *next* — which is why it is **kept as the named fallback for OMS state**, beside the bars rather than holding them. Putting 143 MB a day of append-only history on the same instance makes the noisy table the expensive one |
| **EBS gp3 on the compute host** | Criterion 1. It is the cheapest option that keeps the code unchanged and it puts the only copy of the archive in one Availability Zone at up to a 0.2% annual failure rate. It also dies with its host, which criterion 4 refuses for a store three services will read |
| **S3 Intelligent-Tiering** | Not on cost — it is the cheapest row. On criterion 1's spirit: its saving is conditional on old days not being read, a `GET` promotes an object back to Frequent Access, and the product is a backtester. **Revisit with a measured access pattern** |
| **S3 Standard-IA** | The 128 KB minimum billable object size. `measured`, 98.4% of `quote-bars` objects, 98.4% of `computed-bars` and **100% of `spot-bars`** are under it — `spot-bars` at 2.3 KB would be billed at 55 times its own size |
| **Athena, now** | Nothing is wrong with it and it changes no file. It is $2.14 a month for SQL nobody has asked to write, and it can be added later with one catalogue |
| **Leaving the store uncompacted** | `derived` **$27.65 a month of `GET` charges** at 1,000 ladder reads a day against $0.10 compacted, plus 2.5x to 5.5x on every read. Compaction stopped being optional the moment the store was on object storage |

## What would change this decision

- **An OMS on the bus.** The first row-shaped, mutable, transactional record ends the
  files-only argument for *that data*. Re-open at RDS Postgres beside the bars, and expect
  the bars to stay where they are.
- **A read pattern that is not one operator and a nightly job.** These numbers rest on
  `assumed` 1,000 ladder reads a day and `assumed` 2 requests per Parquet object. Ten
  analysts scrubbing the ladder all day is a different bill, and the lever is the read
  path — one object per partition, a warm cache in `api`, or a materialised day — before it
  is the hosting.
- **A measured S3 latency that the dashboard cannot live with.** If a same-region `GET`
  puts `/chain/at` past what a scrubber can wait on, the answer is a cache in front of S3,
  not a database behind it — but that is a decision this ticket cannot take without a
  bucket.
- **R4's compute (#68).** If the compute lands somewhere with no cheap same-region path to
  S3, or where an attached volume is effectively free, the EBS row's $16.20 and S3's $1.52
  stop being the whole comparison.
- **#65 (I6) landing.** The `store` service replays from its last flushed minute, which can
  re-write a partial interval. If that changes the object count or the per-object size, §3
  and the 128 KB argument both need re-running — which is an acceptance criterion on #67.
- **A second venue with a different retention obligation.** NSE data we are required to
  keep for years, or to delete on a schedule, makes lifecycle rules a requirement rather
  than an option, and Intelligent-Tiering's conditional saving becomes a Glacier decision.

## Still open

**No number here has touched AWS.** The plan for when access arrives, in one paragraph:
create one bucket in the region R4 (#68) chooses, sync one compacted day into it, and run
`tools/measure_store_cloud.py` against `s3://` instead of `data/` to get the same three
read times over the network; capture the actual request count per Parquet read from S3
server access logs, which replaces the `assumed` 2; and re-run the §3 table with the
`measured` request count. Budget one hour and under a dollar.

## #95 correction — which underlyings §3 is sized on

**Appended, not rewritten.** The cost ordering and the decision are unchanged; what is added is
the set of underlyings behind the numbers, which §3 did not state.

**§3's `measured` 143 MB/day is BTC alone.** It comes from run F, which subscribed 688 listed
**BTC** options and nothing else (`docs/storage.md` §10) — and it is `derived` from that run's
`measured` bytes per row and rows per minute rather than `measured` directly. The stack runs BTC
**and** ETH. Beside it:

| | BTC alone | BTC+ETH |
|---|---|---|
| Bytes written a day, before compaction | `derived` 172.1 MB | `derived` **201.5 MB** (`../research/0007-load-profile.md` D1, from a `measured` 699,591 B flush × 288) |
| Bytes retained a day, after compaction | `derived` 143 MB | `derived` **167.4 MB** — 201.5 × (1 − 0.169) |
| Objects a day | `derived` 1,152 | `measured` 8 files a flush → **2,304** |
| **S3 Standard, compacted** | **$1.52** | `derived` **$1.92**; $19.19 at ten times |

The arithmetic is in `../cloud/durable-store.md` §3 and the bill is restated in
`../cloud/services.md` §4. **The gap is 1.26×, not the 1.4× a reader gets from putting 201.5
beside 143** — those are bytes *written* against bytes *retained*, and like for like the second
underlying costs 1.17× the bytes and 2× the objects. A `measured` cross-check agrees: §4's real
compaction run on 2026-09-08 BTC+ETH retained 144.81 MiB over 255 of 288 flushes, a full day of
which is 171.5 MB, 2.4% above the `derived` 167.4.

**No threshold in *What would change this decision* is crossed.** None of them is a dollar figure
on this row: they are an OMS on the bus, a different read pattern, a measured S3 latency, R4's
compute, #65's object count, and a second venue's retention obligation. A $0.40 a month move on a
$79.99 bill reverses no ordering, and the re-run against a real bucket is already *Still open*.
