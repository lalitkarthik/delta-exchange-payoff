# R8 — Compact on the go, or at the close

Findings for #76, under epic #57. Decision
[../decisions/0009-compaction-cadence.md](../decisions/0009-compaction-cadence.md); spec
section [../cloud/durable-store.md](../cloud/durable-store.md) §4; prices from R3,
[0004a-prices-and-sources.md](0004a-prices-and-sources.md); probe `tools/measure_open_partition.py`.

## The question

Should the day's flushed Parquet files be folded together on the go — hourly — or only once
at the close, as now? Judged on lookup latency, compute and object-store requests, against
#57's criteria in order: invariants, ops burden, cost at 1× and 10×, path to the right half,
latency. **Options:** 1 — nightly only. 2 — two-tier: atomic flush, hourly folds of sealed
files older than ten minutes, nightly unchanged. 3 — rewrite the day file on every flush.

## 1. The mechanism, and where it already bites

**Parquet has no append.** "File metadata is written after the data to allow for single pass
writing", and readers "first read the file metadata" (Parquet file format). A file can be read
only once its footer is written, so adding rows means a new file or a rewrite.

**The flush writes straight to its final name** (`store.py:532`), and that is the only reason
`compact()` skips the open day (`store.py:656-667`). The compactor is already atomic: tmp,
read-back verify, manifest, delete, `os.replace` (`store.py:713-768`).

**On S3 the half-written file cannot exist.** "Amazon S3 never adds partial objects" (PutObject);
"Updates to a single key are atomic… never partial or corrupt data" (consistency model). So
option 2's atomic flush is a plain `PUT` on S3 and costs nothing; only its folds cost requests.

**Readers meet the race today.** `/smile` scans every date, the open one included (`smile.py:96`).
`measured` flush durations, `store.flush` records in `logs/2026-09-08..11.log`, live `data/`,
n = 830 a table, median / p95: quote 34.9 / 119.5 ms, reference 20.8 / 95.9, computed 17.2 /
93.8, spot 5.6 / 34.5. `derived`: a four-table read meets a file mid-write in ≈ 78.5 ÷ 300,000
≈ **0.026%** of reads; that Polars then raises (a 500) is inferred, not reproduced.

## 2. What the routes read of today, and how often

All four call `BarStore.scan()` — `scan_parquet("<table>/**/*.parquet", hive_partitioning=True)`
(`store.py:559-587`), unioned with the buffer (`store.py:1367`), on the writer's stores
(`main.py:859-868`). The glob lists every day's files; hive pruning drops other dates unopened.

| route | reads | called |
|---|---|---|
| `/chain/at` (`read_ladder_at`) | four tables at one minute of one expiry, **four separate collects** (`historical.py:80,87,93,164`) | once per stored minute the slider stands on, no throttle (`ChainScreen.tsx:230-246`) |
| `/chain/minutes` (`list_minutes`) | `quote-bars`, one expiry, one day (`historical.py:57`) | per underlying, expiry or date change (`ChainScreen.tsx:188`) |
| `/bars` (`read_contract_bars`) | quote and reference, one contract's day (`contract_bars.py:160`) | on opening a contract chart |
| `/smile` (`read_smile`) | `computed-bars`, one expiry, **every date** (`smile.py:96`) | on load and Refresh, "no polling" (`web/README.md:109`) |

No route counts its calls, so every read rate below is `assumed`.

## 3. The measurement

`measured`: `python tools/measure_open_partition.py --runs 30`, 2026-09-11T18:52:32Z–18:56:11Z,
Python 3.13.13, Polars 1.44.1, this machine, with the live engine recording beside it.

**Stand-in.** The census shows **no partition here has ever been compacted**. 2026-09-08 BTC is
the most file-rich closed day: 301 files a table, 288 plus 13 from a restart in hours 06–07.
Today held 2 files a table after the 18:39Z restart. Cut at 15:00Z, the stand-in holds **193
files a table**: the ~180 of a clean day plus those 13. The variants were built from the real
files in a `tempfile` directory outside the repo, on C: (`data/` is on D:), and removed at exit.
Rows are identical in every open variant (1,402,916) and every close variant (2,217,941).
Expiry 25-09-2026; `/chain/at` at 14:30Z returns 103 legs; `/bars` reads
`DELTA-BTC-20260925-74000-C-USD`.

**At 15:00Z** — wall median / p95 ms · CPU ms a call. CPU is the process total across Polars'
threads.

| read | 193 files — option 1 | 26 — option 2, worst instant | 15 — option 2, after fold | 1 file |
|---|---|---|---|---|
| `/chain/at` | **93.5 / 115.1** · 432 | **34.1 / 45.5** · 88 | 50.4 / 73.3 · 158 | 44.1 / 49.9 · 188 |
| `/chain/minutes` | 35.9 / 43.5 · 148 | 14.2 / 18.6 · 45 | 17.5 / 26.6 · 84 | 11.6 / 16.1 · 26 |
| `/bars` | 92.7 / 107.1 · 543 | 46.5 / 60.8 · 274 | 42.9 / 51.5 · 259 | 36.4 / 41.0 · 215 |
| `/smile` | **418.9** / 523.0 · 656 | **426.2** / 492.4 · 576 | 387.5 / 418.6 · 503 | **404.3** / 484.4 · 523 |
| footers only | 74.4 / 85.9 · 353 | 37.5 / 53.8 · 104 | 27.4 / 34.4 · 38 | 9.9 / 10.9 · 14 |
| all rows, four tables | 135.3 / 166.5 · 1,085 | 73.1 / 92.8 · 626 | 61.8 / 72.7 · 545 | 56.3 / 64.6 · 480 |

The worst instant is 14 hour files plus hour 14's 12 raw files, just before its 15:10 fold.

**At the close**, same run, 301 files → 24 hour files → 1: `/chain/at` 126.2 / 161.9 → 41.2 /
57.7 → 40.2 / 48.3 ms; `/bars` 118.6 → 56.3 → 44.6 ms median; `/smile` 677.3 → 632.6 → 627.6 ms;
footers only 106.6 → 26.4 → 12.7 ms.

**Cache.** Warm: each variant had just been written. One first read of the real `data/`, before
the build: `/chain/at` **252.9 ms** wall, 593.8 ms CPU, against 205.2 ms warm — not a clean cold
read (no purge without admin; `quote-bars` was read earlier this session). **Noise:** the live
engine and other agents share the machine; differences under ~15 ms are not resolved.

## 4. Opening files, or reading rows — #76's *what to notice*

**For three routes the cost is mostly opening files.** `footers only` opens every file and reads
its footer with no page decompressed — the path `BarStore._rows` uses (`store.py:871-874`). It
costs 74.4 ms at 193 files and 9.9 ms at one. `derived`: **≈ 0.084 ms for each file opened**,
and a `/chain/at` opens 4 × 193 = 772 of them. It falls 93.5 → 34.1 ms: `derived`, ~60 of its
94 ms is file count, and the ~35–45 ms left is rows plus ~100 `pydantic` legs, which no cadence
removes. `/chain/minutes` falls 36 → 12–14 ms; `/bars` falls 93 → 36–47 ms.

**`/smile` is rows, and file count is not its lever.** It takes 418.9 ms at 193 files and
404.3 ms at one, inside the noise. It reads every minute of an expiry on every stored date and
builds a point per strike-minute: 46,451 at 15:00, 74,753 at the close. It is the slowest route
by 4–10×, and its cost grows with history, not with today's file count. **No cadence fixes it.**

**File count costs more CPU than wall.** `/chain/at` uses 432 ms of CPU at 193 files against 88
at 26. These routes run in the feed's process. Whether a drag's burst competes with the socket
reader for cores is not measured.

## 5. The store as it stands is the bigger lever

`measured`, same run: the same 301-file day costs **205.2 ms** at `/chain/at` through the real
`data/` and **126.2 ms** alone under a temp root. The real glob also lists ~900 other files a
table, on D: not C:; the run does not separate the two. The nightly job exists and has never run
here (dev runs it "by hand", `durable-store.md` §1). **Running it buys more than re-timing it.**

## 6. What the folds cost to compute

`measured` during the build (`compact_partition`'s read, sort and write, without its verify):
one nightly fold of 301 files, four tables, **0.65 s wall, 5.71 s CPU**; 24 hourly folds of the
same day, **1.61 s wall, 5.45 s CPU**. `derived`: option 2 ≈ 11 s CPU a day against 5.7 s.

## 7. S3 requests — `derived`

**Mapping**, `assumed` call by call on `compact_partition` (no S3 implementation exists). A
fold of n inputs is **7 Tier 1** — three globs as `LIST`, `PUT` tmp and manifest staging, `COPY`
manifest and publish (`durable-store.md` §4); **3n + 3 Tier 2** — `HEAD` manifest, a footer `GET`
per input (`store.py:734`), footer + data per input and for the verify (`assumed` 2, as 0004a);
and **n + 3 `DELETE`**. That is heavier than 0004a's count, which left out the footer reads and
the globs. A flush is one `PUT`, atomic or not.

| per table, per partition, per day | Tier 1 | Tier 2 | DELETE |
|---|---|---|---|
| **1 — nightly only** | 295 (288 `PUT` + 7) | 867 | 291 |
| **2 — two-tier** | 456 (288 + 23 × 7 + 7) | 1,005 | 383 |

**Today-reads.** Objects in today's partition, averaged over the day in five-minute steps:
option 1 **144.5 a table**, option 2 **18.9**. Each `/chain/at` is four tables × (one `LIST` +
`assumed` 2 `GET`s an object). Rates are 0004a's ap-south-1: $0.005 per 1,000 Tier 1, $0.0004
per 1,000 Tier 2, `DELETE` $0 (no SKU). Scale follows 0004: 1× is one underlying's four tables,
10× is ten times the partitions. Reads are `assumed` at 1,000 today-reads a day at 1× — 0004's
ladder figure applied to today — and ten times that at 10×.

| $/month | writes + compaction | today-reads | total |
|---|---|---|---|
| 1×, option 1 | $0.22 | $14.47 | **$14.69** |
| 1×, option 2 | $0.32 | $2.41 | **$2.73** |
| 10×, option 1 | $2.19 | $144.72 | **$146.91** |
| 10×, option 2 | $3.22 | $24.12 | **$27.34** |

**Break-even: 8.6 today-reads a day.** `DELETE` billed at Tier 1 would add $0.055 a month to
option 2. Two underlyings are recorded today, which is 2× on this scale. **This moves 0004's
number.** Its $0.10 read line assumes reads land on compacted days. Under nightly, today is
never compacted, so a `/chain/at` on today touches ≈ 578 objects, not 4.

## 8. Option 3, costed so the loss is on record

Flush k rewrites k/288 of the day, so a day writes (288 + 1) ÷ 2 = **144.5 day-files**. The
day file is `measured` at 107.76 MB (2026-09-08 BTC, four tables). `derived`: **15.57 GB written
a day per underlying**, against 0.11 GB folded once, and ≈ 144.5 × 5.71 s ≈ 825 s of CPU. By
evening every flush rewrites ~108 MB on the writer's thread. And each flush overwrites the only
copy of the day, which fails criterion 1 before cost is counted.

## Still open

**No S3 number** — per-object latency and requests per read are unmeasured; 0004's plan applies,
plus `/chain/at` against a ~180-object today partition. Reads a day, a clean cold cache, more
than one day and underlying, and feed contention from reads: all unmeasured.

## What I learned

| Term | What it means here |
|---|---|
| **Footer** | The index at the end of a Parquet file: where each column lives, each row group's min and max. A reader opens it first; a file with no footer yet is unreadable |
| **Open partition** | Today's `date=` folder. The writer adds a file to it every five minutes |
| **Fold** | Read several small files and write their rows as one. Parquet cannot append, so this is the only way to get fewer files |
| **Two-tier** | Fold each hour just after it ends, then fold the hours into one at night |
| **File-bound vs row-bound** | Does a read spend its time opening files or reading rows? Only file-bound time shrinks when files are folded |

**Opening a file is cheap, but a read opens hundreds.** About a twelfth of a millisecond each
(`derived`), yet `/chain/at` at 15:00 opens 772 — four tables, read separately. Folding the
hours cuts it from `measured` 94 ms to 34.

**One route was never about files.** `/smile` takes `measured` ~410 ms at one file or 193: it
reads every row of an expiry on every day. Less data per request would help; fewer files not.

**The quickest win is the job already decided.** Nothing here has ever been compacted, so every
read lists the whole history: `measured` 205 ms through the real store, 126 ms for the day alone.

**Local disk and S3 answer oppositely.** On a laptop the file count costs a `derived` ~60 ms and
no money, so nightly is enough. On S3 each file is a paid round trip and `derived` 578 objects a
read become most of the bill — and a `PUT` is all or nothing, so the half-written file vanishes.
Nightly now; look again at the move to S3.

## Sources

| Claim | Source |
|---|---|
| Footer written after the data; readers read it first | [Parquet — File Format](https://parquet.apache.org/docs/file-format/), read 2026-09-11 |
| "Amazon S3 never adds partial objects" | [PutObject, S3 API Reference](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html), read 2026-09-11 |
| "Updates to a single key are atomic… never partial or corrupt data"; strong read-after-write | [What is Amazon S3? — consistency model](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Welcome.html#ConsistencyModel), read 2026-09-11 |
| Request prices, no `DELETE` SKU, 1,000 ladder reads a day, 2 requests an object | [0004a-prices-and-sources.md](0004a-prices-and-sources.md) §2–§3, [0004-durable-store.md](0004-durable-store.md) §2 |
| Flush, compaction, open-day skip, `scan()`, the routes and how the page calls them | `store.py`, `historical.py`, `contract_bars.py`, `smile.py`, `main.py`, `ChainScreen.tsx`, `web/README.md`, at the lines cited |
| Flush durations | `logs/2026-09-08.log` … `2026-09-11.log`, `store.flush` records on live `data/` paths |
