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
2. A minute seals once its **grace** elapses. The two channels seal on different graces, because
   their arrival lags differ by an order of magnitude.
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

Four read paths, all `pl.scan_parquet(..., hive_partitioning=True)` against the same tree, and all
answering from local disk or S3 with the same call and a different root.

| Route | Reads |
|---|---|
| `/chain/at`, `/chain/minutes` | one minute, four tables |
| `/bars` | one contract's day, two tables |
| `/smile` | one expiry's stored volatility |

**The glob stays `**/*.parquet`.** A bare prefix refuses the whole dataset the moment it holds one
non-Parquet file, and compaction puts two there while it runs. A filter on `date` or `underlying` is
answered by the key before an object is opened.

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
recoverable; a doubling is invention. A real closed day folds ~2,000 flush objects into 8 and loses
about a sixth of its bytes.

**On object storage the file count is a bill.** Uncompacted, one `/chain/at` read touches over a
thousand objects, each costing at least a footer request and a data request; compacted it touches
four. Same dashboard, same data.

**Nightly, and not on the go.** Parquet cannot append, so "on the go" would mean folding today's
partition while the store is still flushing into it -- a fold and a compactor running beside the
live writer, for a read the dashboard does not wait on. **`os.replace` is not atomic on S3**, so
publishing there is a `COPY` plus a `DELETE`, and the manifest is what makes that window
recoverable. It is a sidecar in the prefix it describes, so a partition is recoverable on its own.

## Storage class

**S3 Standard.** Both cheaper classes have a 128 KB rule, and before compaction most of our objects
sit on the wrong side of it: Standard-IA bills a 128 KB minimum per object, so a 2.3 KB spot-bars
file would be billed at fifty times its own size, and Intelligent-Tiering does not monitor or tier
an object that small at all. After compaction the daily objects are megabytes, which is a class
decision to revisit against a real access pattern and not before.

## Bucket settings

| Setting | Value | Why |
|---|---|---|
| Versioning | **off** | Compaction deletes inputs only after a verified read-back; versioning would keep them alive and let a naive `list` read the day doubled |
| Block Public Access | on | |
| Default encryption | SSE-S3 | |
| Lifecycle | one rule, aborting incomplete multipart uploads after 7 days | Nothing transitions and nothing expires: bars are kept indefinitely |

## What it costs

`derived` **$1.92 a month** on S3 Standard for BTC and ETH, compacted, at today's rate. The line
sits in the bill beside the compute in [Deployment](deployment.md).

## Related guides

[Events](events.md) | [Message bus](message-bus.md) | [Deployment](deployment.md)
