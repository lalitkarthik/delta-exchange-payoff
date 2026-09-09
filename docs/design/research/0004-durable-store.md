# R3 — Where the durable store lives, for four bar tables that must never invent a minute

Findings for #67, under epic #57. The decision is
[../decisions/0004-durable-store.md](../decisions/0004-durable-store.md), the spec section
[../cloud/durable-store.md](../cloud/durable-store.md), every unit price, the arithmetic
behind each cell and every source URL
[0004a-prices-and-sources.md](0004a-prices-and-sources.md). Absence, the three read paths
and OMS, per option: [0004b-absence-reads-and-oms.md](0004b-absence-reads-and-oms.md). The
store this sizes is [../../storage-start-here.md](../../storage-start-here.md) and
[../lld/store.md](../lld/store.md).

## The question

Where should the four bar tables live — S3 as Parquet, S3 plus Athena, RDS Postgres, RDS
with Timescale, Timestream, or an EBS volume on the compute host — given how the store
actually writes, and #57's five criteria in order: **(1)** invariants — no silent data
loss, never forward-fill, `null` is not `0`; **(2)** ops burden for two or three people;
**(3)** cost at our rate and ten times it; **(4)** path to OMS, NSE and more consumers;
**(5)** latency.

**What is being sized.** `measured` **143 MB/day** retained and **288 flush files per table
per day** (`docs/storage-start-here.md`, run F). `derived` **172.1 MB/day written** before
compaction, from the 16.9% saving `measured` below. `derived` **1,152 objects a day** per
underlying — 288 × 4 tables — **34,560 a month**, **420,480 a year**. Ten times the rate is
modelled as **ten times the underlyings at the same five-minute cadence**, so bytes *and*
object count multiply: 1,430 MB/day and 11,520 objects a day. Stating it the other way —
ten times the rows inside the same partitions — leaves the request bill flat and only the
storage bill moves, and the choice below does not turn on which reading is right.

## 1. The six options against the five criteria

| | (1) Invariants | (2) Ops for 2–3 people | (3) Cost/mo, ap-south-1, month 12 | (4) Path to the right half | (5) Latency, `measured` here |
|---|---|---|---|---|---|
| **S3 Parquet**, hive layout unchanged | absence is the file layout: no row is written, so nothing has to represent one. 11 nines across ≥3 AZs | a bucket and a lifecycle rule; no instance, no version, no patch window | **$1.52** — $1.23 storage, $0.17 `PUT`, $0.10 read, $0.02 compaction | every consumer reads the same files; a second reader costs nothing | whole day, four tables, compacted **82.0 ms** |
| **S3 Parquet + Athena** | same files, so same answer | one more service, a Glue catalog and partitions to register | **+$2.14** at `assumed` 100 whole-day queries a day | SQL for people who will not write Polars | seconds per query; not a route's read path |
| **RDS Postgres** | a missing minute is an absent row, same as here — but `NOT NULL` is now a thing to get right on 100 columns, and a partial write is a transaction to roll back | an instance, a version, a maintenance window, a parameter group, backups | **$94.07** — `db.t4g.medium` $61.32 + 250 GB gp3 $32.75 | the one option that already holds OMS state properly | not measured; a network hop plus a query |
| **RDS + Timescale** | — | — | — | — | **not available.** `timescaledb` is not in RDS for PostgreSQL's supported-extension list |
| **Timestream** | absence is native to a time-series store | least to run | **$1,752.06** — of which **$1,719.88** is the 4-TCU provisioned minimum ap-south-1 offers | — | **closed to new customers since 2025-06-20** |
| *Timestream for InfluxDB*, the successor AWS names | absence is native | a managed instance | **$120.28** — `db.influx.medium` $98.55 + 189 GB $21.73 | a second query language beside Polars | not measured |
| **EBS gp3 on the compute host** | the files are ours, so absence works exactly as it does today — but the volume is **one AZ**, `99.8–99.9%` durable, AFR ≤ 0.2% | a volume to grow by hand and a snapshot policy to remember | **$16.20** — 150 GB $13.68 + snapshots $2.52 | dies with its host; a second consumer needs the host | fastest — a local file system |

Every figure is `derived` from `measured` unit prices read 2026-09-09; the working is in
[0004a-prices-and-sources.md](0004a-prices-and-sources.md).

## 2. The 128 KB cliff, and what compaction is actually buying — #67's *what to notice*

`measured` 2026-09-09, `tools/measure_store_cloud.py` over the live `data/`, 3,284 objects:

| table | objects | median object | **under 128 KB** |
|---|---|---|---|
| `quote-bars` | 821 | 73,442 B | **98.4%** |
| `reference-bars` | 821 | 230,112 B | 0.6% |
| `computed-bars` | 821 | 90,240 B | **98.4%** |
| `spot-bars` | 821 | 2,334 B | **100%** |

**128 KB is where two of S3's cost lines change behaviour, and three of our four tables sit
under it.** Intelligent-Tiering "is not monitored and is not eligible for automatic tiering"
below that size and such objects "are always stored in the Frequent Access tier";
Standard-IA bills a 128 KB minimum per object and a 30-day minimum duration. So on the
**uncompacted** five-minute layout, every storage class that is cheaper than Standard is
either inert or actively worse for `quote-bars`, `computed-bars` and `spot-bars` — and
`spot-bars`, at 2.3 KB an object, would be billed **55x its own size** under Standard-IA.

Compaction moves every table across the line. `measured` 2026-09-08, one real closed day of
five-minute flush files copied to a scratch root and compacted with `tools/compact_store.py`:

    2,040 files -> 8,  3,061,741 rows,  174.22 MiB -> 144.81 MiB  (16.9% smaller),  4.6 s

**This is the day storage.md §11 said had never been compacted, and it moves that section's
number.** Run G bounded the saving at *about 1.5%* by dividing hourly files by twelve. At
the cadence the engine actually writes it is **16.9%** — eleven times the bound — because a
five-minute file is 3,440 rows, not 41,280, and Parquet's fixed costs are a much larger
share of it. Compaction is no longer only a file-count decision.

**And on object storage the file count is a bill, not a tidiness score.** A `GET` is
$0.0004 per thousand and a Parquet reader issues at least a footer request and a data
request per object (`assumed` 2; not measured). At `assumed` 1,000 historical ladder reads
a day, one operator scrubbing:

| | objects touched per read | `GET`s a month | cost |
|---|---|---|---|
| compacted, 4 objects | 4 | 240,000 | **$0.10** |
| never compacted, 1,152 objects | 1,152 | 69,120,000 | **$27.65** |

**The same dashboard, the same data, and a 288x difference in the read line.** That is what
#67 asked to be noticed: the per-request cost of object storage is invisible at our write
rate — 34,560 `PUT`s a month is 17 cents — and it is the *read* side that the 288-file
cadence makes expensive. Compaction's own requests are noise: 34,680 `GET`s and 480 `PUT`s
a month, `derived` **$0.016**.

## 3. Cost, at 143 MB/day and at ten times it

`derived`, month 12 of year one, ap-south-1 primary and us-east-1 second, prices read
2026-09-09 from the AWS Price List Bulk API. One month is 730 hours and 30 days.

| Option | 1x aps3 | 1x use1 | 10x aps3 | 10x use1 |
|---|---|---|---|---|
| **S3 Intelligent-Tiering, compacted** | **$0.70** | $0.64 | **$6.97** | $6.41 |
| **S3 Standard, compacted** | **$1.52** | $1.42 | **$15.18** | $14.20 |
| S3 Standard + Athena | $3.66 | $3.56 | $36.63 | $35.65 |
| S3 Standard, **never compacted** | $29.31 | $29.19 | $293.05 | $291.86 |
| EBS gp3 + 7 daily snapshots | $16.20 | $14.52 | $120.93 | $109.17 |
| RDS PostgreSQL, Single-AZ | $94.07 | $76.20 | $499.86 | $438.97 |
| Timestream for InfluxDB | $120.28 | $106.50 | $413.67 | $363.42 |
| Timestream for LiveAnalytics | $1,752.06 | $44.71 | $2,041.73 | $447.08 |

Two rows need their shape explained rather than their number read. The per-option answers
to *can it hold a missing minute as absent*, the read path for each of the three readers,
and what changes when OMS state arrives are in
[0004b-absence-reads-and-oms.md](0004b-absence-reads-and-oms.md), with the `measured`
read times that §2's compaction argument rests on.

**RDS is not paying for bytes, it is paying for a row store.** `measured` on the compacted
2026-09-08 BTC day: 107,755,496 Parquet bytes against **412,547,927 bytes** of the same
2,217,941 rows held uncompressed — **3.83x**. Add Postgres's 23-byte tuple header and
4-byte line pointer and the floor is `derived` **4.4x**, before indexes, page slack and
`fillfactor`. A year that is 52 GB of Parquet is 230 GB of heap, and gp3 under RDS is
$0.131/GB-month against S3's $0.025.

**Timestream's ap-south-1 number is a floor, not a usage charge.** The Price List for
ap-south-1 carries **no per-GB-scanned SKU** — only `QueryTCU-Provisioned` at
$0.589/TCU-hour — and the minimum provision is **4 TCUs**. Four TCUs for a month is
`derived` **$1,719.88** whether or not a query runs. us-east-1 prices data scanned at
$0.010/GB and the same option costs $44.71. It is moot either way — the service is
closed to new customers.

## What I learned

| Term | What it means here |
|---|---|
| **Object storage** | A key and some bytes. No rows, no update-in-place; changing one row means rewriting the whole file. Priced per byte stored, per `PUT` and per `GET` |
| **`PUT` / `GET`** | The two request classes S3 bills. Tier 1 (`PUT`/`COPY`/`POST`/`LIST`) is $0.005 per 1,000; Tier 2 (`GET` and everything else) is $0.0004 per 1,000 — twelve times cheaper each, and we make far more of them |
| **The 128 KB cliff** | Below it, Intelligent-Tiering will not monitor or tier an object, and Standard-IA bills 128 KB anyway. Three of our four tables' flush files are under it |
| **Hive partitioning** | The filter is the directory name — `date=2026-09-08/underlying=BTC`. Polars and DuckDB both read the keys off the path and skip directories without opening a file |
| **Compaction** | Folding a closed day's 288 flush files per table into one. Here it saves 16.9% of bytes, 99.6% of objects, and most of the read latency |
| **Row store vs columnar** | Postgres stores a row's fields together and does not compress fixed-width columns; Parquet stores each column together and dictionary-encodes it. Same rows, `measured` 3.83x apart |
| **Provisioned TCU** | Timestream's ap-south-1 query billing: 4 vCPU and 16 GB per unit, minimum 4 units, charged whether or not a query runs |

**Our never-forward-fill rule is a property of the write path, and only one option keeps it
that way.** A minute with no arrivals produces no bar, so the file simply has no row for it
— the rule is enforced by `bars.py` and stored by doing nothing. On Postgres or on a
time-series store the same absence has to survive a schema with defaults, an insert path,
and query functions (`fill(previous)`, `interpolate_locf`) whose whole purpose is to put the
minute back. Nothing would break on day one. The rule would stop being structural and start
being a convention, which is exactly how `/v2/history/candles` came to return 801 bars of
which 797 are invented.

**`reference-bars` is why the 128 KB cliff has two answers.** It is 62% of the store's bytes
and the only table whose five-minute files clear 128 KB — 230 KB median against `quote-bars`'
73 KB. So on the uncompacted layout Intelligent-Tiering would tier most of the *bytes* and
almost none of the *objects*, and `spot-bars` would be billed at 55 times its own size under
Standard-IA. One store, four tables, and the cheap storage class is right for one of them.

**Compaction changed from a tidiness job to a cost decision, twice over.** storage.md called
it "a file-count decision, not a footprint decision" and bounded the bytes at 1.5%. Measured
on a real five-minute day it saves 16.9% of bytes — and on S3 it also saves $27.55 a month
of `GET` charges and 5.5x on the dashboard's slowest read. The bound was honest arithmetic on
the wrong cadence; the measurement is on the cadence #16 actually shipped.

**A backtest reading files is not a preference, it is a portability property.** The same
tree answers `pl.scan_parquet` and DuckDB's `read_parquet` with no export step, no
credentials beyond the bucket, and no service to keep running. Every database option here
would make a backtest ask a server that has to be alive, sized and paid for while nobody is
querying it.

**"Not available" beat every other reason twice.** Timescale is not an RDS extension, and
Timestream for LiveAnalytics has been closed to new customers since 2025-06-20 — and we have
no AWS account, so we are a new customer. Two of the six options were eliminated before a
single number was compared, which is why *check availability first* now precedes the cost
table rather than following it.

**The expensive thing about a database here is that it is awake.** S3 at our rate costs
$1.52 a month and nothing when nobody reads. `db.t4g.medium` costs $61.32 a month to exist.
The bars are read by one operator's dashboard and an occasional backtest; the store is idle
almost all of the time, and only one of these options is priced for idle.

## Still open

- **Re-cost after #65 (I6) lands.** The `store` service under Compose replays from its last
  flushed minute, which can re-write a partial interval; if that changes the object count or
  the per-object size, §2's cliff table and §3's `PUT` line both move. Re-run
  `tools/measure_store_cloud.py` and this file's §2 and §3 against the new pattern.
- **Requests per Parquet read is `assumed` 2, not measured.** A footer request and a data
  request per object is the floor; a projection over many row groups issues more. It cannot
  be measured without a bucket. §2's $27.65 is therefore a floor too.
- **No AWS account exists on this machine**, so no S3 latency, no RDS query time and no
  cross-AZ figure is measured here. Every latency in
  [0004b-absence-reads-and-oms.md](0004b-absence-reads-and-oms.md) §2 is a local file
  system, which is a floor for S3 and not a substitute for it.
- **The 16.9% compaction saving is one day**, 2026-09-08, BTC and ETH. It is one measurement
  where there were none; it is not a distribution.
