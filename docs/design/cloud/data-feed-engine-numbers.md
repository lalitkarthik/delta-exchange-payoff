# The numbers behind the data feed engine

Evidence for [data-feed-engine.md](data-feed-engine.md) §2.2, moved out by #72 to keep that file
under the 200-line bound. **Quote a number from this table, never from a sentence elsewhere** — the
same rule [message-bus-numbers.md](message-bus-numbers.md) states for the bus. The platform and
topology figures are [compute-numbers.md](compute-numbers.md); the store's own arithmetic is
[durable-store.md](durable-store.md).

---

## 1. The durable store, per rule

| Rule | Value | Tag | Run behind it |
|---|---|---|---|
| Layout | four tables, one S3 prefix each; `date=` and `underlying=` partitions below it, nothing else | — | [0004](../decisions/0004-durable-store.md) |
| Storage class | S3 Standard | — | Standard-IA bills AWS's published 128 KB minimum per object, and `measured` 100% of `spot-bars` objects are under it: [durable-store.md](durable-store.md) §5, 821 objects a table |
| Objects a day, per underlying | 1,152 | `derived` | 288 store flushes x 4 tables, [durable-store.md](durable-store.md) §3 |
| Objects a compacted `/chain/at` read touches | 4 | `derived` | one object per table in one partition, [durable-store.md](durable-store.md) §3 |
| Bytes retained a day, BTC alone | 143 MB | `derived` | run F's `measured` bytes per row and rows per minute, `docs/storage.md` §10 |
| Compaction | nightly, every partition strictly before today | — | [0009](../decisions/0009-compaction-cadence.md) |
| Compaction result | 2,040 objects → 8, 16.9% smaller, 4.6 s | `measured` | one real closed day, BTC+ETH, 2026-09-08 |
| `/chain/at`, uncompacted → compacted | 117.8 ms → 21.3 ms | `measured` | 2026-09-09, 2,217,941 rows, **local disk** |
| S3 read latency, and requests per Parquet read | — | **unmeasured** | no AWS account; [services.md](services.md) §5 |

**No latency figure here touched AWS.** Local file system numbers are a floor for S3, and the ratio
between the two `/chain/at` columns is the part that transfers.

**`143 MB` is BTC alone and `derived`, not `measured`.** `docs/storage.md` §10 says so twice — once
in its reconciliation table and once under *The daily footprint* — and [services.md](services.md)
§4 already carries the tag this way. The BTC+ETH figure is 167.4 MB/day
([durable-store.md](durable-store.md) §3).
