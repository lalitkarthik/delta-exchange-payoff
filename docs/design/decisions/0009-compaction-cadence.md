# 0009 — Compact on the go, or at the close

**Status** decided, #76, 2026-09-12. **Supersedes** nothing. **Fills**
[../cloud/durable-store.md](../cloud/durable-store.md) §4. **Evidence**
[../research/0009-compaction-cadence.md](../research/0009-compaction-cadence.md), the probe
`tools/measure_open_partition.py`, prices from
[../research/0004a-prices-and-sources.md](../research/0004a-prices-and-sources.md).

## The question

Should the day's flushed Parquet files be folded together on the go — hourly — or only once at
the close, as now? Judged on lookup latency, compute and object-store requests.

## The options

| | Considered |
|---|---|
| **1. Nightly only** | As now. A closed day's 288 files a table fold to one; the open day is never touched |
| 2. Two-tier | Atomic flush (`.tmp` then `os.replace`). Fold each hour's sealed files once the newest is ten minutes old. Nightly fold unchanged |
| 3. Rewrite the day file on every flush | True append, by rewriting |

Appendable open-day formats (Arrow IPC, JSONL) were out of scope: a second format means a
second reader path.

## The decision

**Nightly only, as now.** While the store is on local disk, the open partition costs
`/chain/at` `measured` **93.5 ms median, 115.1 ms p95** at 15:00 (193 files a table) and
126.2 / 161.9 ms at the close (`tools/measure_open_partition.py --runs 30`,
2026-09-11T18:52Z). Two-tier would take that to `measured` 34.1 / 45.5 ms. The `derived`
~60 ms saved does not pay for an hourly job, a windowed fold, and a compactor running beside
the live writer.

**This is a decision for local disk, and it names its own expiry.** On S3 the same file count
is a paid request and a round trip per object, and the half-written file cannot exist. Re-open
this record before any route reads today's partition from S3. See the threshold below.

**No implementation ticket follows now.** The contents of the one that would follow are
below.

## Why, in the criteria's order

**1. Invariants.** Nightly keeps the compactor off the partition being written and read.
Two-tier moves compaction's delete-then-publish window — a read that comes back short, never
doubled (`store.py:376-382`) — into the trading day, 23 times, on the partition the dashboard
reads. That is recoverable and not a loss. But at that instant the slider's domain is missing
an hour. Two-tier's atomic flush does remove a real exposure: a read that lands on a file
mid-write. `derived` ≈ 0.026% of today-reads, from `measured` flush durations of 34.9 / 20.8 /
17.2 / 5.6 ms median. It is small either way.

**2. Operations burden.** Nightly adds nothing; the job and its six crash-tested stages exist.
Two-tier adds:
- the atomic flush;
- an hourly trigger;
- a fold over one hour's files. `compact_partition` folds everything in the directory, so if
  it were reused hourly it would rewrite the day so far every hour;
- a compactor beside the live writer, which `store.py` states is not defended against;
- crash tests for all of the above.

That is a lot to own for a `derived` ~60 ms.

**3. Cost.** On local disk, $0 either way. Fold compute is `measured` at 5.71 s CPU for the
nightly fold of a real day and 5.45 s for its 24 hourly folds — seconds a day either way.
**On S3, two-tier is cheaper.** `derived` at `assumed` 1,000 today-reads a day: $14.69 a month
nightly against $2.73 two-tier at 1×, and $146.91 against $27.34 at 10×. Break-even is **8.6
today-reads a day**. The write side barely differs: $0.22 against $0.32 at 1×.

**4. Path to the right half.** A strategy or OMS reading today's bars multiplies today-reads.
That pushes toward two-tier, but only once those reads hit S3.

**5. Latency.** Neither option touches the feed path; compaction is a separate process. Lookup
latency is already inside a slider step, and `/smile` — the slowest route, `measured`
418.9 ms at 193 files and 404.3 ms at 1 — is row-bound: **the same within noise**. No cadence
moves it.

## Rejected, and why

| Rejected | Why |
|---|---|
| **2. Two-tier, now** | Criteria 1 and 2 before 3. It saves a `derived` ~60 ms a read on a local disk, and costs a scheduler, a windowed fold and 23 delete windows a day on the live partition. **Kept as the named successor for S3** |
| **3. Rewrite the day file every flush** | `derived` **15.57 GB written a day per underlying**, 144.5× the `measured` 107.76 MB day it produces. And every flush overwrites the only copy of the day, which fails criterion 1 before cost is counted |
| **Appendable open-day format** | Out of scope by the ticket: a second reader path |

## The threshold that flips it

**Flip to two-tier when any route reads today's partition from S3 and either of these holds:**

- a `measured` `/chain/at` against a ~180-object S3 partition has a **p95 above 250 ms**
  (`assumed` budget for one slider step; local today is `measured` 115.1 ms), or
- today-reads from S3 pass **~690 a day at 1×**, where option 1's today-read `GET`s pass
  $10 a month (`derived`: $14.47 at 1,000).

On local disk it would take a p95 of 250 ms at the open partition's file count — `derived`
2.2× today's. A cadence change would not fix that anyway: at the close, the one-file read is
still `measured` 40.2 ms, and that part is rows.

## The implementation ticket, if the threshold trips

Blocked on this record and on an S3 measurement. It would contain:

1. **Atomic flush.** Write `<name>.parquet.tmp`, then `os.replace` it, on local disk. On S3 a
   plain `PUT`, which is already atomic.
2. **A fold over an explicit file list.** One hour's sealed flush files, the newest at least
   ten minutes old. It reuses `compact_partition`'s verify, manifest, delete and publish, and
   writes `hour-YYYYMMDDTHH.parquet`. It must not fold the whole directory.
3. **An hourly trigger at :10**, in the compaction process, never in the writer.
4. **A one-compactor lock**, now that the compactor runs during the day.
5. **Crash tests at every stage** against a partition that is receiving flushes.
6. **The nightly fold unchanged**, folding hour files and stragglers to one.

## What would change this decision

- **The store reaching S3** — the threshold above. The probe and 0004's S3 plan together
  measure it in an hour.
- **A read counter.** Every read rate here is `assumed`. A measured today-read rate either
  confirms 1,000 a day or moves the break-even.
- **A consumer that polls today** — strategy, OMS, a second dashboard. Reads multiply, and so
  does option 1's bill.

## Still open

**Nightly compaction has never run on this machine.** No partition holds a `compact-*` file.
The same day reads `measured` 205.2 ms through the real `data/` against 126.2 ms on its own
(same run). Running the
decided job is the cheapest latency available, and it is not a cadence question.
