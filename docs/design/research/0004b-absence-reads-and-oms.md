# R3 — absence, the three read paths, and OMS, per option

The per-option detail behind [0004-durable-store.md](0004-durable-store.md), split out
because that file holds to 200 lines. Nothing is decided here. Prices and sources are in
[0004a-prices-and-sources.md](0004a-prices-and-sources.md); the decision is
[../decisions/0004-durable-store.md](../decisions/0004-durable-store.md).

## 1. Can it hold a missing minute as absent — no row, not null, not filled?

| Option | Plain answer |
|---|---|
| **S3 Parquet** | **Yes, and it is the only one where absence needs no mechanism.** A minute with no arrivals produces no bar, so no row is appended and no file records it. There is nothing to set to null and nothing to default |
| **S3 + Athena** | **Yes** — same files. Athena reads what is there; a `GROUP BY` over minutes returns the minutes present. A `date_series` join would invent them, and that would be the query's fault, not the store's |
| **RDS Postgres** | **Yes**, if it is built that way. A row is absent because nobody inserted one. The risk moves from the storage to the schema: a column with a `DEFAULT 0`, an `INSERT` that coalesces, an ORM that writes zeros for unset fields. `docs/storage.md`'s rule is enforced today by *not writing a row*; in Postgres it would need `NOT NULL` where a value is required and no defaults anywhere |
| **RDS + Timescale** | Not answerable — not available |
| **Timestream** | **Yes.** A record is written or it is not; there is no fixed grid. But its `interpolate_*` and `fill` functions exist precisely to manufacture the missing points, and a query written by somebody else would forward-fill silently. Our rule would live in a convention rather than in the data |
| **Timestream for InfluxDB** | **Yes**, with the same caveat, and a stronger one: InfluxQL/Flux `fill(previous)` is idiomatic in that ecosystem |
| **EBS gp3** | **Yes** — it is the current file layout on a different block device. Identical behaviour |

## 2. The read path per option, and what the file count does to it

Three readers, and they are not the same read. **The dashboard's historical routes**
(`/chain/minutes`, `/chain/at`, `/bars`) read one partition and filter to one minute or one
contract. **Compaction** reads every input in a partition whole, writes one file and reads
that back in full before deleting anything. **A backtest** reads whole days across many
partitions. `measured` 2026-09-09, `tools/measure_store_cloud.py`, one real day
(2026-09-08, BTC, 2,217,941 rows) on a scratch copy, warm cache, minimum of five:

| Read | uncompacted, 301 files/table | compacted, 1 file/table | ratio |
|---|---|---|---|
| whole day, four tables — backtest, compaction | **206.4 ms** | **82.0 ms** | 2.5x |
| one minute, four tables — `/chain/at` | **117.8 ms** | **21.3 ms** | **5.5x** |
| one contract's day, two tables — `/bars` | **137.2 ms** | **41.2 ms** | 3.3x |

**This is the first whole-day read measured against the five-minute layout** — `docs/storage-start-here.md`
*Still open* item 2 had it `derived` at roughly 88 ms and asked for a real day.

The per-option read path, in one line each. **S3 Parquet:** `pl.scan_parquet("s3://…/**/*.parquet",
hive_partitioning=True, storage_options=…)` — the same call the engine makes today with a
different prefix; Polars documents that `scan_*` over cloud storage "can benefit from
predicate and projection pushdowns, where the query optimizer will apply them before the
file is downloaded", so `date=`/`underlying=` still prunes on the path. DuckDB reads the
identical tree — `read_parquet('…/*/*/*.parquet', hive_partitioning = true)`, with "filters
on the partition keys … automatically pushed down" — which is what makes a backtest not
have to be written in our language. **Athena:** the same tree registered as an external
table; ad hoc SQL only, not a route. **RDS:** every read becomes a query, and
`historical.py`, `contract_bars.py` and `smile.py` are rewritten from Polars frames onto a
cursor. **Timestream:** the same rewrite, in a different SQL. **EBS:** no change at all —
it is the code as written.

## 3. What changes when OMS state arrives

**S3 Parquet.** Nothing about the bars, and S3 is the wrong home for the new thing.
Order intents and fills are row-shaped, mutable and transactional — an object store has no
row-level update and no transaction, so an OMS on S3 means either a table format (Iceberg,
Delta) or a rewrite of a whole object to change one row. The answer is a small Postgres
*beside* the bars, not instead of them, and that is a cheaper change than moving 52 GB a
year of immutable bars into a database that does not want them.

**S3 + Athena.** The same, plus a second catalogue entry. Athena can join the OMS tables to
the bars only if the OMS also lands in S3, which returns to the paragraph above.

**RDS Postgres.** This is the option OMS argues for, and the argument is real: one
instance, one transaction, a fill and its position update committed together. It is also
the argument for adding an instance later rather than for putting the bars on one now —
an OMS is megabytes a day of state that must be correct, and the bars are 143 MB a day of
append-only history that must be cheap. Putting both on one `db.t4g.medium` makes the
noisy one the expensive one.

**Timestream / Timestream for InfluxDB.** A time-series store holds no OMS state at all —
there is nothing transactional in it and no foreign key. So a second store is required
whatever happens, and the bars would then be the only thing it holds.

**EBS gp3.** An OMS on a single-AZ volume is the one combination criterion 1 refuses
outright: order state is not rebuildable from the venue, and a volume with an annual
failure rate of up to 0.2% holding the only copy of it is a loss waiting for a date.

