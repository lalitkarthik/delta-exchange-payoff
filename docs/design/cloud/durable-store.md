# Where the durable store lives, and how it is written and read

The data feed engine document's durable-store section. Why each rule is what it is — the
six options, the prices, the measurements — is
[../research/0004-durable-store.md](../research/0004-durable-store.md), with the per-option
detail in
[../research/0004b-absence-reads-and-oms.md](../research/0004b-absence-reads-and-oms.md)
and every unit price in
[../research/0004a-prices-and-sources.md](../research/0004a-prices-and-sources.md); the
decision is [../decisions/0004-durable-store.md](../decisions/0004-durable-store.md); the
names are [nomenclature.md](nomenclature.md); what the four tables mean is
[../../storage-start-here.md](../../storage-start-here.md).

Landed by #67 as a standard, not as code. Nothing here is built yet.

---

## 1. Where it runs

**The four bar tables are Parquet files on S3 Standard, in the hive layout the engine
already writes**, one bucket per environment, in the region R4 (#68) chooses.

| | `dev` | `prod` |
|---|---|---|
| Root | `data/` on the laptop | `s3://<env>-deltapayoff-bars/` |
| Written by | the `store` service | the `store` service, unchanged but for the root |
| Reachable from | the file system | the VPC, via a gateway endpoint; the bucket blocks public access |
| Storage class | — | **Standard**; not Intelligent-Tiering, not Standard-IA — see §5 |
| Compaction | `tools/compact_store.py`, by hand | the same tool, nightly, once a day per closed partition — not on the go, see §4 |

**The named fallback, if criterion 4 moves, is RDS for PostgreSQL beside the bars, not
instead of them** — `db.t4g.medium`, `derived` $61.32/month in ap-south-1, for OMS state
when an order path arrives. The bars stay on S3 in that world too.

**No latency here has touched AWS.** Every figure in §6 is a local file system. It is a
floor for S3 and not a substitute for it; the plan to replace it is in the decision record.

## 2. The layout on it

```
s3://prod-deltapayoff-bars/quote-bars/underlying=BTC/date=2026-09-08/20260908T131500Z-000287.parquet
                           reference-bars/…
                           computed-bars/…
                           spot-bars/…
```

**Four dataset prefixes, not one with a `table=` key.** A shared root forces every scan to
carry a filter a prefix should have answered and puts four schemas in one dataset for
Parquet's metadata to reconcile on every read. They share the same two partition keys, so a
reader joins spot to quotes to our volatility on `date` and `underlying` with no
translation.

**`date=` and `underlying=`, and nothing else.** The filter is in the key name, so a query
for BTC on 8 September skips every other prefix without a `GET`. Expiry, strike and option
type stay columns: as partition levels they explode into thousands of prefixes holding a
handful of rows each, and object storage is worse at that than a disk is.

**Bucket settings.** Versioning **off** — compaction's whole safety argument is that it
deletes inputs only after a verified read-back, and versioning would keep the deleted
inputs alive and let a naive `list` read the day doubled. Block Public Access **on**.
Default encryption SSE-S3. One lifecycle rule, and it only aborts incomplete multipart
uploads after 7 days; nothing transitions and nothing expires — bars are kept indefinitely,
which at `derived` 143 MB/day retained for **BTC alone** is `derived` $1.23 a month of storage in
month 12, and $1.44 at `derived` 167.4 MB/day for BTC+ETH (§3).

## 3. How the store service writes to it

Unchanged from `docs/design/lld/store.md` except for the root. Sealed bars accumulate in
memory and are written **every five minutes**, one object per table per partition per
flush. The flush interval *is* the crash-loss budget, and after #65 (I6) `store` also
replays from its last flushed message ID, which closes it.

**These rows are sized on BTC alone.** Run F subscribed 688 listed **BTC** options and nothing else (`docs/storage.md` §10), so every cell in the BTC column is one underlying; the stack runs BTC **and** ETH, and the column beside it is that.

| Number | Tag | BTC alone | BTC+ETH | Run |
|---|---|---|---|---|
| Objects written per day | `derived` | **1,152** — 288 flushes × 4 tables; 34,560 a month | **2,304** — the flush writes **8 files** for two underlyings, `measured` (`../lld/store-numbers.md`); 69,120 a month | one `underlying=` partition per table per flush |
| Bytes written per day, before compaction | `derived` | **172.1 MB** — 143 ÷ (1 − 0.169) | **201.5 MB** — 699,591 B a scheduled flush `measured` × 288 ([../research/0007-load-profile.md](../research/0007-load-profile.md) D1) | D1's own cross-check from a part day lands within 2.4% |
| Bytes retained per day, after compaction | `derived` | **143 MB** — from run F's `measured` bytes/row and rows/minute (`docs/storage.md` §10) | **167.4 MB** — 201.5 × (1 − 0.169) | cross-checked `measured` below: §4's real BTC+ETH day retained 144.81 MiB over 255 of 288 flushes, a full day of which is 171.5 MB, 2.4% above |
| Storage charge, month 12 | `derived` | **$1.233** — 49.34 GB × $0.025 | **$1.444** — 57.75 GB × $0.025 | 11.5 months' average of a store growing at the row above |
| `PUT` charge at that rate | `derived` | **$0.173/month** | **$0.346/month** | ap-south-1, 2026-09-09; $0.005/1,000 |
| Compaction requests, and `assumed` 1,000 ladder reads a day | `derived` | **$0.016 + $0.096** | **$0.033 + $0.096** | [../research/0004a-prices-and-sources.md](../research/0004a-prices-and-sources.md) §3. A ladder read is one underlying, so it does not double |
| **S3 Standard, compacted, a month** | `derived` | **$1.52** | **$1.92** | the four charge rows added; $15.18 and $19.19 at ten times the rate |
| Median object size, `quote-bars` / `reference-bars` / `computed-bars` / `spot-bars` | `measured` | 73,442 / 230,112 / 90,240 / 2,334 B | unmeasured | `tools/measure_store_cloud.py`, 2026-09-09 |

**Do not put 201.5 MB/day beside 143 MB/day and call the gap 1.4×.** They are different quantities: 201.5 is bytes **written** before compaction, 143 is bytes **retained** after it. Like for like the second underlying costs `derived` **1.17× the bytes** and **2× the objects**, and the bill moves from $1.52 to $1.92 — a **1.26×** that is mostly the object count.

**A write is one whole object; there is no append.** That is already how `flush` behaves —
each flush writes its own uniquely named file and never reopens an earlier one — so the
move to object storage changes no code path. Polars is still not allowed to lay out the
tree: `write_parquet(partition_by=…)` names its output `00000000.parquet` in every
partition on every call, which on S3 would be a silent overwrite rather than a noisy one.

**A minute with no arrivals produces no object and no row.** Nothing about S3 changes that,
and that is the point: absence here is the absence of bytes, not a null to defend.

## 4. Compaction is part of the deployment, not housekeeping

**Nightly, one process, every partition strictly before today.** `tools/compact_store.py`
against the bucket. It reads every input in a partition, writes a tmp, **reads the tmp back
in full** to verify, writes a manifest, deletes the inputs and only then publishes — a gap
is visible and recoverable, a doubling is invention.

`measured` 2026-09-08, one real closed day of five-minute flush files, BTC and ETH:

    2,040 objects -> 8,  3,061,741 rows,  174.22 MiB -> 144.81 MiB  (16.9% smaller),  4.6 s

| Number | Tag | Note |
|---|---|---|
| Bytes saved by compacting a real five-minute day | `measured` | **16.9%** — against the ~1.5% `derived` in `docs/storage.md` §10 from *hourly* files. The bound was right arithmetic on the wrong cadence |
| Objects saved | `measured` | 2,040 → 8, **99.6%** |
| Requests it costs, per day | `derived` | 1,156 `GET` and 16 Tier-1 writes — **$0.016/month** |
| `GET` charge it saves, at `assumed` 1,000 ladder reads a day | `derived` | **$27.55/month** — $27.65 uncompacted against $0.10 compacted |

**On object storage the file count is a bill.** Uncompacted, one `/chain/at` read touches
1,152 objects and each object costs at least a footer request and a data request
(`assumed` 2, not measured). Compacted it touches 4. Same dashboard, same data, 288x the
requests.

**`os.replace` is not atomic on S3.** The publish step becomes a `COPY` plus a `DELETE`,
and the window between them is the same *gap, never a doubling* window the local code
already accepts. The manifest is what makes it recoverable, and it is a sidecar in the
prefix it describes so a partition is recoverable on its own.

**Nightly, not on the go** — [../decisions/0009-compaction-cadence.md](../decisions/0009-compaction-cadence.md).
Parquet cannot append, so "on the go" means folding today's partition while `store` flushes
into it. `measured` 2026-09-11 on a real day cut at 15:00Z, 193 files a table: `/chain/at`
93.5 ms median, 115.1 ms p95; folded into hour files, 34.1 / 45.5 ms. `/smile`, ~410 ms, is
row-bound and does not move. A `derived` ~60 ms does not pay for an hourly job, a fold
and a compactor beside the live writer. **It flips when a route reads today from S3**: a
`PUT` is atomic there, and `derived` at `assumed` 1,000 today-reads a day the bill is $14.69
a month nightly against $2.73 two-tier. Re-measure `/chain/at` against a ~180-object S3
partition first; past 250 ms p95, build the two-tier fold the record describes.

## 5. The storage class, and the 128 KB cliff

**S3 Standard.** Both cheaper classes have a 128 KB rule and `measured` we sit on the wrong
side of it before compaction:

| table | objects | median object | under 128 KB |
|---|---|---|---|
| `quote-bars` | 821 | 73,442 B | **98.4%** |
| `reference-bars` | 821 | 230,112 B | 0.6% |
| `computed-bars` | 821 | 90,240 B | **98.4%** |
| `spot-bars` | 821 | 2,334 B | **100%** |

**Standard-IA bills a 128 KB minimum per object**, so `spot-bars` at 2.3 KB would be billed
at 55 times its own size. **Intelligent-Tiering does not monitor or tier an object under
128 KB** — "smaller objects are always stored in the Frequent Access tier" — so before
compaction it would tier most of the *bytes* (`reference-bars` is 62% of the store) and
almost none of the *objects*. After compaction every daily object is megabytes and
Intelligent-Tiering would work, `derived` $0.70/month against Standard's $1.52 — **and its
saving is conditional on nobody reading a day older than thirty days**, because a `GET`
promotes an object back to Frequent Access and the product is a backtester. Revisit with a
measured access pattern, not before.

## 6. How it is read

**The same call, a different root.** `pl.scan_parquet("s3://…/**/*.parquet",
hive_partitioning=True, hive_schema=HIVE_SCHEMA, storage_options=…)`. Polars documents that
`scan_*` over cloud storage "can benefit from predicate and projection pushdowns, where the
query optimizer will apply them before the file is downloaded", so a filter on `date` or
`underlying` is still answered by the key before an object is opened. The glob stays
`**/*.parquet` for the reason it was added: a bare prefix refuses the whole dataset the
moment it holds one non-Parquet file, and compaction puts two there while it runs.

Three readers, three shapes. `measured` 2026-09-09 on 2026-09-08, BTC, 2,217,941 rows,
local disk, warm cache, minimum of five — a floor for S3, and the ratio is the part that
transfers:

| Reader | What it reads | uncompacted | compacted |
|---|---|---|---|
| `/chain/at`, `/chain/minutes` | one minute, four tables | 117.8 ms | **21.3 ms** |
| `/bars` | one contract's day, two tables | 137.2 ms | **41.2 ms** |
| compaction, a backtest | a whole day, four tables | 206.4 ms | **82.0 ms** |

**This is the first whole-day read measured against the five-minute layout.**
`docs/storage-start-here.md` *Still open* item 2 had it `derived` at roughly 88 ms.

**A backtest need not be written in our language.** The same tree answers DuckDB —
`read_parquet('…/*/*/*.parquet', hive_partitioning = true)`, with "filters on the partition
keys … automatically pushed down" — with no export step and no service to keep running.
Athena reads it too, `$5.00/TB` scanned with a 10 MB minimum per query; it is not bought
now and adding it changes no file, only a Glue catalogue.

## 7. What is not decided here

- **The region.** R4 (#68). Prices above are ap-south-1 with us-east-1 beside them in
  [../research/0004a-prices-and-sources.md](../research/0004a-prices-and-sources.md).
- **OMS state.** Row-shaped, mutable and transactional; an object store has no row-level
  update and no transaction. It goes in a Postgres beside the bars when it arrives, and the
  bars do not move.
- **Anything measured over a network.** No AWS account exists on this machine, so no S3
  latency, no request count per Parquet read, and no cross-AZ figure is stated. The
  `assumed` 2 requests per object is a floor and is labelled as one.
- **Re-costing after #65 (I6).** `store`'s replay can re-write a partial interval; if the
  object count or per-object size moves, §3 and §5 both need re-running with
  `tools/measure_store_cloud.py`.
