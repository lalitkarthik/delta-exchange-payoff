# R3 — prices, arithmetic and sources

The working behind [0004-durable-store.md](0004-durable-store.md) and
[0004b-absence-reads-and-oms.md](0004b-absence-reads-and-oms.md). Nothing is decided here;
this file exists so every number in those two can be checked without re-reading them.

## 1. How the prices were read

All from the **AWS Price List Bulk API**, the machine-readable form of the public pricing
pages, fetched **2026-09-09** — the same method and the same day as R5's
[0002b-prices-and-sources.md](0002b-prices-and-sources.md):

```
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonAthena/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonTimestream/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSGlue/current/<region>/index.json
```

`publicationDate`: S3, Athena, Timestream and Glue **2026-08-31**; RDS
**2026-09-04T17:27:16Z**; EC2 (which carries EBS) **2026-09-09T00:46:05Z**. Regions
`ap-south-1` (Asia Pacific, Mumbai) and `us-east-1` (US East, N. Virginia). On-demand terms
only — `offerTermCode` `JRTCKXETXF`. One month is **730 hours** and **30 days**.

## 2. Unit prices, `measured` from the price list

**S3.** First 50 TB tier throughout; we are four orders of magnitude below it.

| | ap-south-1 | us-east-1 |
|---|---|---|
| Standard storage | $0.025 /GB-mo | $0.023 /GB-mo |
| Intelligent-Tiering, Frequent Access | $0.025 | $0.023 |
| Intelligent-Tiering, Infrequent Access | $0.0138 | $0.0125 |
| Intelligent-Tiering, Archive Instant Access | $0.005 | $0.004 |
| Intelligent-Tiering monitoring and automation | $0.0025 /1,000 objects-mo | $0.0025 |
| Standard-IA storage | $0.0138 | $0.0125 |
| Tier 1 — `PUT`/`COPY`/`POST`/`LIST` | $0.005 /1,000 | $0.005 |
| Tier 2 — `GET` and all other | $0.0004 /1,000 | $0.0004 |
| Lifecycle transition into Intelligent-Tiering | $0.01 /1,000 | $0.01 |

**Athena.** `$5.00 per Terabytes for DataScannedInTB`, identical in both regions. The
product record carries the two rules the pricing page states in prose: *"Charged for total
data scanned per query with a minimum 10MB for each successful or cancelled queries"* and
`freeQueryTypes`: *"Data definition queries (DDL), Failed Queries and statements used to
create or load partitions"*.

**AWS Glue Data Catalog**, needed by Athena. $0 for the first 1,000,000 objects stored and
the first 1,000,000 requests a month, then $1 per 100,000 objects-month and $1 per
1,000,000 requests, both regions. Four tables plus `derived` 2,920 partitions a year
(4 × 2 underlyings × 365) is three orders of magnitude inside the free tier.

**RDS for PostgreSQL**, on-demand, Single-AZ, no licence.

| | ap-south-1 | us-east-1 |
|---|---|---|
| `db.t4g.micro` | 0.021 /hr | 0.016 |
| `db.t4g.small` | 0.042 | 0.032 |
| `db.t4g.medium` | 0.084 | 0.065 |
| `db.t4g.large` | 0.167 | 0.129 |
| `db.m7g.large` | 0.240 | 0.168 |
| `db.r7g.large` | 0.272 | 0.239 |
| gp3 storage, Single-AZ | $0.131 /GB-mo | $0.115 |
| gp3 storage, Multi-AZ | $0.262 | $0.230 |
| gp3 provisioned IOPS beyond baseline | $0.023 /IOPS-mo | $0.020 |
| gp3 provisioned throughput beyond baseline | $0.091 /MiBps-mo | $0.080 |
| backup storage beyond the free allocation | $0.095 /GB-mo | $0.095 |

**Timestream.** For LiveAnalytics, and for InfluxDB — the successor AWS names on every page
of the LiveAnalytics guide.

| | ap-south-1 | us-east-1 |
|---|---|---|
| LiveAnalytics, data ingestion | $0.569 /GB | $0.500 |
| LiveAnalytics, memory store | $0.041 /GB-hr | $0.036 |
| LiveAnalytics, magnetic store | $0.0341 /GB-mo | $0.030 |
| LiveAnalytics, data scanned by queries | **no SKU in this region** | $0.010 /GB |
| LiveAnalytics, provisioned query TCU | $0.589 /TCU-hr | $0.518 |
| InfluxDB `db.influx.medium`, Single-AZ | 0.135 /hr | 0.120 |
| InfluxDB `db.influx.large`, Single-AZ | 0.269 | 0.239 |
| InfluxDB storage, `InfluxIOIncludedT1` | $0.115 /GB-mo | $0.100 |
| InfluxDB storage, Multi-AZ `T1` | $0.230 | $0.200 |

**EBS**, from the EC2 offer file.

| | ap-south-1 | us-east-1 |
|---|---|---|
| gp3 provisioned storage | $0.0912 /GB-mo | $0.08 |
| gp3 IOPS beyond the included 3,000 | $0.0057 /IOPS-mo | $0.005 |
| gp3 throughput beyond the included 125 MiB/s | $0.0456 /MiBps-mo | $0.04 |
| snapshot storage | $0.05 /GB-mo | $0.05 |
| snapshot archive storage | $0.0125 /GB-mo | $0.0125 |

## 3. The arithmetic, cell by cell

**What is being sized.** `measured` 143 MB/day retained (`docs/storage.md` §10, run F) —
4.29 GB a month, 52.2 GB a year. `derived` 172.1 MB/day written before compaction, from
143 ÷ (1 − 0.169) and the 16.9% saving `measured` on 2026-09-08. `derived` 1,152 objects a
day (288 flushes × 4 tables), 34,560 a month. **Month 12** is the reported month, so
average stored during it is 4.29 × 11.5 = **49.34 GB**. Ten times the rate multiplies both
bytes and objects by ten.

**S3 Standard, compacted, ap-south-1.** Storage 49.34 × $0.025 = **$1.233**. Writes
34,560 × $0.005/1,000 = **$0.173**. Compaction, per §4 of `store.py`'s sequence — one
`LIST`, one `GET` per input, one more `GET` for the full verifying read-back, and three
Tier-1 writes (tmp, manifest, and the `os.replace` publish, which is a `COPY` on object
storage): (1,152 + 4) × 30 × $0.0004/1,000 = **$0.0139** and (3 × 4 + 4) × 30 ×
$0.005/1,000 = **$0.0024**. Reads at `assumed` 1,000 ladder reads a day over 4 compacted
objects at `assumed` 2 requests each: 1,000 × 30 × 8 × $0.0004/1,000 = **$0.096**. Total
**$1.52**. us-east-1 is the same arithmetic at $0.023 storage: **$1.42**.

**S3, never compacted.** The same writes, storage grossed up by the 16.9% compaction never
took (49.34 ÷ 0.831 = 59.37 GB → $1.484), and reads over 1,152 objects instead of 4:
1,000 × 30 × 2,304 × $0.0004/1,000 = **$27.648**. Total **$29.31**.

**S3 Intelligent-Tiering, compacted.** In month 12, of 49.34 GB stored: the newest month
(4.29 GB) is in Frequent Access, the next two (8.58 GB) in Infrequent Access after 30 days
of no access, and the remaining 36.47 GB in Archive Instant Access after 90.
4.29 × 0.025 + 8.58 × 0.0138 + 36.47 × 0.005 = **$0.408**. Monitoring is charged on the
1,460 compacted objects a year: 1,460 × $0.0025/1,000 = **$0.0037**. Requests unchanged.
Total **$0.70**. **This number assumes nobody reads a day older than 30 days** — a `GET`
returns an object to Frequent Access, and a backtesting product reads old days.

**Athena.** `assumed` 100 whole-day queries a day, each scanning one stored day of 143 MB:
100 × 30 × 0.143 GB = 429 GB = 0.429 TB × $5 = **$2.145**. Glue is free at our object
count. Partition pruning is what keeps the scan to one day; without it every query scans
the whole bucket.

**RDS PostgreSQL.** `measured` on the compacted 2026-09-08 BTC day, four tables,
2,217,941 rows: 107,755,496 Parquet bytes against 412,547,927 bytes held uncompressed in
memory with categoricals widened to strings — **3.83x**. Postgres adds a 23-byte tuple
header and a 4-byte line pointer per row, 27 × 2,217,941 = 59.9 MB, which is another 0.56x
of the Parquet size, so **4.4x is a floor** — before any index, page slack or `fillfactor`.
Month 12 heap `derived` 49.34 × 4.4 = **217 GB**; a year is 230 GB, so 250 GB of gp3 is
provisioned. ap-south-1: $0.084 × 730 = **$61.32** plus 250 × $0.131 = **$32.75** →
**$94.07**. At ten times, `db.r7g.large` $0.272 × 730 = $198.56 plus 2,300 GB × $0.131 =
$301.30 → **$499.86**. Backup storage is free up to the provisioned storage size and is
therefore $0 here.

**Timestream for LiveAnalytics.** Ingestion is metered on record bytes, not on Parquet
bytes, so the `measured` 3.83x applies: `derived` 0.548 GB/day. ap-south-1:
0.548 × 30 × $0.569 = **$9.35** ingestion; one day of memory retention resident for the
month, 0.548 × 730 × $0.041 = **$16.39**; magnetic 49.34 × 3.83 × $0.0341 = **$6.44**; and
the query floor, 4 TCU × $0.589 × 730 = **$1,719.88**. Total **$1,752.06**. In us-east-1
the query line becomes data scanned, 100 × 30 × 0.548 × $0.010 = $16.43, and the total is
**$44.71**.

**Timestream for InfluxDB.** ap-south-1 `db.influx.medium` $0.135 × 730 = **$98.55** plus
49.34 × 3.83 = 189 GB × $0.115 = **$21.73** → **$120.28**. At ten times,
`db.influx.large` $0.269 × 730 = $196.37 plus 1,890 GB × $0.115 = $217.30 → **$413.67**.

**EBS gp3 with snapshots.** A year is 52.2 GB, so a 150 GB volume carries year one with
room to grow: 150 × $0.0912 = **$13.68**. Snapshots are incremental and billed on unique
blocks; seven dailies over a store growing 143 MB a day is 49.34 + 7 × 0.143 = 50.35 GB ×
$0.05 = **$2.52**. Total **$16.20**. The 3,000 IOPS and 125 MiB/s baseline is included with
the price of storage, and a 5-minute flush of about 600 KB does not approach it, so no
provisioned IOPS or throughput is bought.

**The `measured` object counts this rests on.** `tools/measure_store_cloud.py`, 2026-09-09,
against the live `data/`: 3,284 objects, 377,270,429 bytes across the four tables;
2026-09-08 alone, BTC and ETH, 2,040 objects and 182,684,477 bytes, which compact to 8
objects and 151,843,151 bytes. The engine was not up for the whole of 2026-09-08 — 301 of
288 possible BTC flush files and 209 ETH — so the day is `measured` and the 1,152-a-day
figure stays `derived` from the cadence rather than read off that listing.

## 4. Sources

Primary only. Every claim in the two findings files traces here.

| Claim | Source |
|---|---|
| "If the size of an object is less than 128 KB, it is not monitored and is not eligible for automatic tiering. Smaller objects are always stored in the Frequent Access tier"; the 30/90-day automatic tiers; "designed for 99.9% availability and 99.999999999% durability" | [How S3 Intelligent-Tiering works](https://docs.aws.amazon.com/AmazonS3/latest/userguide/intelligent-tiering-overview.html), read 2026-09-09 |
| The storage-class comparison table: S3 Standard durability 99.999999999% across ≥3 AZs, no minimum duration, no minimum billable object size; Standard-IA and One Zone-IA 30 days and **128 KB** minimum billable object size; "If an object is less than 128 KB, Amazon S3 charges you for 128 KB" | [Understanding and managing Amazon S3 storage classes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/storage-class-intro.html), read 2026-09-09 |
| Athena $5.00/TB scanned, 10 MB minimum per query, DDL and failed queries free | AWS Price List, `AmazonAthena` offer file, product `RQN88TYRT35JXK3M`, read 2026-09-09 |
| "you are charged standard Data Catalog rates" for Glue with Athena | [Amazon Athena pricing](https://aws.amazon.com/athena/pricing/), read 2026-09-09 |
| **`timescaledb` does not appear in any supported-extension table** for PostgreSQL 15, 16, 17, 18 or 19 | [Extension versions for Amazon RDS for PostgreSQL](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html), read 2026-09-09 |
| AWS's own answer for time series on RDS Postgres is declarative partitioning and `pg_partman`, not an extension | [Designing high-performance time series data tables on Amazon RDS for PostgreSQL](https://aws.amazon.com/blogs/database/designing-high-performance-time-series-data-tables-on-amazon-rds-for-postgresql/), read 2026-09-09 |
| "we have made the decision to close new customer access to Amazon Timestream for LiveAnalytics, effective 6/20/25… Existing customers with an active payer account currently using the service may continue to add new users and linked accounts under that payer account… We recommend that new customers evaluate Amazon Timestream for InfluxDB" | [Amazon Timestream for LiveAnalytics availability change](https://docs.aws.amazon.com/timestream/latest/developerguide/AmazonTimestreamForLiveAnalytics-availability-change.html), read 2026-09-09 |
| "Provisioned TCU is available only in the Asia Pacific (Mumbai) region"; "Each Timestream Compute Unit (TCU) is comprised of 4 vCPUs and 16GB of memory"; "The minimum number of provisioned TCUs is 4"; charged "for the duration of the… TCUs provisioned in your account, with a minimum charge of 1 hour" | [Provisioned Timestream Compute Units](https://docs.aws.amazon.com/timestream/latest/developerguide/provisioned-tcu.html), read 2026-09-09 |
| gp3: "a consistent baseline IOPS performance of 3,000 IOPS, which is included with the price of storage"; "a consistent baseline throughput performance of 125 MiB/s, which is included"; "99.8 percent to 99.9 percent volume durability with an annual failure rate (AFR) no higher than 0.2 percent"; 1 GiB to 64 TiB | [Amazon EBS General Purpose SSD volumes](https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html), read 2026-09-09 |
| Polars: `scan_parquet`'s `hive_partitioning` — "Infer statistics and schema from hive partitioned URL and use them to prune reads"; `storage_options` — "Options that indicate how to connect to a cloud provider. The cloud providers currently supported are AWS, GCP, and Azure" | [`polars.scan_parquet`](https://docs.pola.rs/api/python/stable/reference/api/polars.scan_parquet.html), read 2026-09-09 |
| Polars: `scan_*` "functions to read from cloud storage can benefit from predicate and projection pushdowns, where the query optimizer will apply them before the file is downloaded" | [Polars — Cloud storage](https://docs.pola.rs/user-guide/io/cloud-storage/), read 2026-09-09 |
| DuckDB: `SELECT * FROM read_parquet('orders/*/*/*.parquet', hive_partitioning = true);`; "By default the system tries to infer if the provided files are in a hive partitioned hierarchy"; "Filters on the partition keys are automatically pushed down into the files. This way the system skips reading files that are not necessary to answer a query" | [DuckDB — Hive partitioning](https://duckdb.org/docs/current/data/partitioning/hive_partitioning.html), read 2026-09-09 |
| All S3, Athena, RDS, Timestream, EBS and Glue unit prices | AWS Price List Bulk API, §1 above |
| 143 MB/day, 288 files per table per day, 2,792,972 rows/day, `reference-bars` 62% of the store, compaction bounded at ~1.5% on hourly files | [`docs/storage.md`](../../storage.md) §10, and [`docs/storage-start-here.md`](../../storage-start-here.md) |
| The compaction sequence — write tmp, verify by full read-back, write manifest, delete inputs, publish | [`engine/src/deltapayoff/store.py`](../../../engine/src/deltapayoff/store.py) `compact_partition`, and [`../lld/store.md`](../lld/store.md) |

**One thing not verified.** The S3 offer file carries **no `DELETE` request SKU**, and
compaction's 2,040 deletes a day are therefore priced at zero above. No AWS documentation
sentence saying "DELETE requests are free" was found in this session, so that zero is an
inference from an absent SKU rather than a quoted rule. At Tier-1 rates it would be
$0.31 a month if it were charged, which changes no ordering in §3.
