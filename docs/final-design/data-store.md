# Data store

The store is the system's permanent record: **one-minute bars, folded from the same event stream
the screen reads, written as Parquet and never forward-filled.** It is written by exactly one
process and read by four routes.

## Five dataset roots

Under a gitignored `<repo>/data/` locally, or one S3 bucket per environment in production.

| Root | Holds | Written by |
|---|---|---|
| `quote-bars` | What the book did: bid, ask, sizes, `from_book`, `last_lts` | the bar writer |
| `reference-bars` | What the venue said: mark, OI, turnover, its own IV and Greeks | the bar writer |
| `spot-bars` | The underlying's index price, and `spot_ticks` | the bar writer |
| `computed-bars` | What we made of it: our forward, IV and Greeks | the bar writer |
| `index-bars` | Index history, backfilled | `tools/backfill_index_bars.py`, read by `/volatility` |

**Four roots, not one with a `table=` key.** A shared root forces every scan to carry a filter a
prefix should have answered, and puts four schemas in one dataset for Parquet's metadata to
reconcile on every read. They share the same two partition keys, so a reader joins spot to quotes to
our volatility on `date` and `underlying` with no translation.

## The layout

```
quote-bars/underlying=BTC/date=2026-09-08/20260908T131500Z-000287.parquet
```

**`underlying=` and `date=`, and nothing else.** Expiry, strike and option type stay **columns**:
as partition levels they explode into thousands of directories holding a handful of rows each, and
object storage is worse at that than a disk is.

**Polars is not allowed to lay out the tree.** `write_parquet(partition_by=...)` names its output
`00000000.parquet` in every partition on every call, so the 10:00 flush would silently overwrite
the 09:00 one. Directories are built by hand, each flush writes a uniquely named file, and a test
pins it. On S3 that same default would be a silent overwrite rather than a noisy one.

## Sealing a minute

1. A tick is bucketed on **`ts_venue` alone**. `lts`, the venue's last-trade stamp, is carried as a
   column and never bucketed on.
2. A minute seals once its **grace** elapses. The two channels seal on different graces because
   their arrival lags differ by an order of magnitude -- `measured` p50 212.6 ms on the book against
   a median 3,176 ms on the ticker.
3. **A minute with no arrivals produces no row.** Not nulls, and never the previous close.
4. A sealed minute is published as `md.option_bar` (in the store process) and accumulates in memory.
5. Every `FLUSH_SECONDS` -- 300 by default -- the buffer is written, **one object per table per
   partition per flush**, on a worker thread.

**The flush interval is the crash-loss budget**, and replay from the last flushed message id is what
closes it. A write is one whole object; there is no append, which is why the move to object storage
changes no code path.

### Quote provenance

The quote bars carry `from_book`: whether a minute's prices came from the venue's order book or from
the slower channel standing in for a silent one. **The event type is the provenance** -- a tick from
`md.option_quote` is a book tick, one from `md.option_reference`'s bid and ask is a fallback tick.
Nothing on the bus carries a channel name.

## Replay and the checkpoint

The store keeps a **per-stream checkpoint** -- an id and a logical index -- in
`<root>/_store-checkpoint.json`, and restarts from it. Never from the pending list, and **never from
`0`**, which would re-record up to thirty minutes the old writer already wrote as duplicates.

**A trimmed position is a replay gap, not a refusal to start.** The store replays the retained
suffix, reports both bounds and an exact `lost` count, raises a `store.replay_gap` alert, and keeps
going. The check runs continuously on the ten-second `store.state` cadence, not once at start-up.

**While any stream is behind, the seal clock is `min(wall clock, the time inside the last id of any
stream still behind)`.** Without that, the first drain pass after any absence would seal the whole
backlog as late and discard the bytes it just replayed.

## Reading it

Four read paths, all `pl.scan_parquet(..., hive_partitioning=True)` against the same tree. The glob
stays `**/*.parquet`: a bare prefix refuses the whole dataset the moment it holds one non-Parquet
file, and compaction puts two there while it runs.

| Route | Reads | uncompacted | compacted |
|---|---|---|---|
| `/chain/at`, `/chain/minutes` | one minute, four tables | 117.8 ms | **21.3 ms** |
| `/bars` | one contract's day, two tables | 137.2 ms | **41.2 ms** |
| a whole-day backtest read | four tables | 206.4 ms | **82.0 ms** |

`measured` 2026-09-09 on 2026-09-08, BTC, 2,217,941 rows, local disk, warm cache, minimum of five.
**These are a floor for S3, and the ratio is the part that transfers.**

In split mode the api also holds a lossless `BarBuffer` of `md.option_bar` events, so the read paths
can union the newest sealed minutes with what is on disk without a local writer.

**A backtest need not be written in this language.** The same tree answers DuckDB with
`read_parquet('.../*/*/*.parquet', hive_partitioning = true)` and partition-key filters pushed down,
with no export step and no service to keep running. Athena reads it too; adding it changes no file,
only a catalogue.

## Compaction

**Nightly, one process, every partition strictly before today.** `tools/compact_store.py`.

It reads every input in a partition, writes a temporary file, **reads that file back in full to
verify**, writes a manifest, deletes the inputs, and only then publishes. A gap is visible and
recoverable; a doubling is invention.

`measured` 2026-09-08, one real closed day of five-minute flush files, BTC and ETH:

```
2,040 objects -> 8,  3,061,741 rows,  174.22 MiB -> 144.81 MiB  (16.9% smaller),  4.6 s
```

**On object storage the file count is a bill.** Uncompacted, one `/chain/at` read touches 1,152
objects, each costing at least a footer request and a data request; compacted it touches 4. Same
dashboard, same data, 288x the requests -- `derived` **$27.55 a month** of `GET` charges saved at an
`assumed` 1,000 ladder reads a day, against $0.016 of compaction requests.

**Nightly, not on the go.** Parquet cannot append, so "on the go" means folding today's partition
while the store flushes into it. `measured` on a real day cut at 15:00Z: `/chain/at` 93.5 ms median
and 115.1 ms p95, against 34.1 / 45.5 ms folded into hour files. A `derived` ~60 ms does not pay for
an hourly job, a fold and a compactor beside the live writer. That flips when a route reads *today*
from S3.

## Storage class, and the 128 KB cliff

**S3 Standard.** Both cheaper classes have a 128 KB rule, and `measured` we sit on the wrong side of
it before compaction:

| table | objects | median object | under 128 KB |
|---|---|---|---|
| `quote-bars` | 821 | 73,442 B | **98.4%** |
| `reference-bars` | 821 | 230,112 B | 0.6% |
| `computed-bars` | 821 | 90,240 B | **98.4%** |
| `spot-bars` | 821 | 2,334 B | **100%** |

**Standard-IA bills a 128 KB minimum per object**, so `spot-bars` at 2.3 KB would be billed at 55
times its own size. **Intelligent-Tiering does not monitor or tier an object under 128 KB** at all.
After compaction every daily object is megabytes and Intelligent-Tiering would work at `derived`
$0.70/month against Standard's $1.52 -- but its saving is conditional on nobody reading a day older
than thirty days, and the product is a backtester. Revisit with a measured access pattern.

## Bucket settings

| Setting | Value | Why |
|---|---|---|
| Versioning | **off** | Compaction deletes inputs only after a verified read-back; versioning would keep them alive and let a naive `list` read the day doubled |
| Block Public Access | on | |
| Default encryption | SSE-S3 | |
| Lifecycle | one rule, aborting incomplete multipart uploads after 7 days | Nothing transitions and nothing expires: bars are kept indefinitely |

## What it costs

| Line | BTC alone | BTC+ETH |
|---|---|---|
| Objects written per day | `derived` 1,152 | `derived` 2,304 |
| Bytes written per day, before compaction | `derived` 172.1 MB | `derived` 201.5 MB |
| Bytes retained per day, after compaction | `derived` 143 MB | `derived` 167.4 MB |
| **S3 Standard, compacted, a month** | `derived` **$1.52** | `derived` **$1.92** |

**Do not put 201.5 MB/day beside 143 MB/day and call the gap 1.4x.** They are different quantities:
one is bytes **written** before compaction, the other bytes **retained** after it. Like for like, the
second underlying costs `derived` 1.17x the bytes and 2x the objects.

## Related guides

[Events](events.md) | [Message bus](message-bus.md) | [Deployment](deployment.md)
