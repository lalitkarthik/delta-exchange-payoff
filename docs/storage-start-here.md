# Storage — start here

Read one thing back, right now:

```bash
cd engine && ./.venv/Scripts/python.exe -c "import polars as pl; print(pl.scan_parquet('../data/quote-bars/**/*.parquet', hive_partitioning=True).collect())"
```

That prints the option chain, minute by minute, from disk. No engine, no network.

> Deep version of this document: [storage.md](storage.md), 870 lines. This one is the map.
> Design and decisions: [#5](https://github.com/lalitkarthik/delta-exchange-payoff/issues/5).

---

## Where the data is

```
D:\Convex Hedge\delta-exchange-payoff\data\
```

**Not in Git.** `data/` is in `.gitignore`. Market data stays on your disk.

```
data/
|-- _store-checkpoint.json
|-- _store-flush-intent.json
|-- quote-bars/       underlying=BTC/date=2026-09-04/*.parquet
|-- reference-bars/   underlying=BTC/date=2026-09-04/*.parquet
|-- computed-bars/    underlying=BTC/date=2026-09-04/*.parquet
`-- spot-bars/        underlying=BTC/date=2026-09-04/*.parquet
```

The folder names carry the date and the asset. A reader skips a day without opening a file. The
two root JSON files are human-readable incident evidence and safe to delete: deletion costs a
reported gap, never a duplicate.

**`DELTA_STORE_ROOT` can also name `s3://bucket/prefix`**, S3 Standard, R3's chosen home
([0004](design/decisions/0004-durable-store.md)) — the switch and the default are done
(I8, #70), no backend is wired, so this raises `StoreHomeUnavailable` rather than write
here or nowhere. Item 7 below.

---

## The five tables

| Table | Holds | Columns |
|---|---|---|
| **quote-bars** | bid, ask and mid — each with open/high/low/close | 24 |
| **reference-bars** | mark, LTP, open interest, **Delta's** IV and Greeks | 28 |
| **computed-bars** | **our** IV and **our** Greeks, plus the model stamp | 20 |
| **spot-bars** | Spot, once a minute for the whole asset | 8 |
| **index-bars** | Delta's own index candles, written only by a backfill job — never the engine | 6 |

**Ours and theirs sit side by side. Ours are added, never substituted.** That is what makes any agreement between them evidence rather than construction.

---

## The problem, in three numbers

| | |
|---|---|
| Delta sends | **1,323 messages/second** |
| That is | **52 GB/day** of raw JSON |
| We store | **143 MB/day** |

Most of those messages repeat. Delta republishes a price whether or not it changed.

**One minute becomes one line per option.** Four prices survive: the first, the largest, the smallest, and the last.

## Restart loss depends on the mode

### Split store: replay within retention

With `DELTA_BUS=redis`, `store` replays from the id of its last flush. Redis retains `derived`
thirty minutes whether or not anyone acked, so a crash loses nothing inside that window. A
graceful stop checkpoints an open minute instead of writing a truncated one. A stop longer than
retention loses whatever fell out, counted and reported as a gap, never silently.

### Default in-process monolith: five-minute window

With `DELTA_BUS` unset, the old rule is unchanged: a crash loses the buffer and the `derived`
five-minute flush interval is the loss window ([#16](https://github.com/lalitkarthik/delta-exchange-payoff/issues/16)).
Time a restart just after a flush applies to this monolith only; the `derived` 288 files per
table per day figure is unchanged, and compaction folds them back to one overnight.

## The one rule that matters

**A minute with no data gets no line.**

Never a copied-forward price. Never a line of empty values.

**Why this is not a detail.** Delta's own history does the opposite. Its `/v2/history/candles` fills empty minutes with the last trade and does not say so. For `C-BTC-60000-270624` it returns 801 daily bars — **797 of them are invented**.

Aggregation is compression. Forward-filling is fabrication.

**How we know the rule holds.** We put the fault into the code on purpose. Six tests failed. We took it out. Six tests passed. A guard that has never been seen to fail is not a guard.

## How a price becomes a line — 5 steps

1. **A tick arrives.** One price message from Delta, on one of two channels.
2. **It joins a minute.** Bucketed by *Delta's* clock, not ours — so our network cannot move a price into the wrong minute.
3. **The minute seals.** We wait 8 seconds past the boundary for stragglers, then close it. Late arrivals are counted and dropped, never silently lost.
4. **Bars flush to disk.** The monolith flushes every five minutes; split `store` commits a checkpoint with each flush, so replay covers the retained suffix.
5. **A day compacts.** The `derived` 288 files per table become one, verified by full read-back *before* anything is deleted.

**`computed-bars` differs by mode.** In the monolith, our IV and Greeks are sampled from the
local chain cache — every ten seconds and at each minute edge. In split mode, table C is folded
from `computed.chain` events (`schema_version` 2) published by the api on that schedule, not
sampled from a store-local cache; the minute keeps the freshest sample.

## Word list

| Word | Meaning |
|---|---|
| **tick** | one price message from Delta |
| **bar** | one minute summarised: open, high, low, close |
| **Parquet** | the file format. Stores columns apart, so it compresses well |
| **partition** | a folder whose name is the filter — `underlying=…/date=…` |
| **pruning** | skipping folders by name, without opening files |
| **flush** | write buffered bars to disk. Every five minutes |
| **watermark** | how long we wait before sealing a minute. 8 seconds |
| **seal** | close a minute. Nothing more goes in |
| **compaction** | join a day's 288 flush files into one daily file |
| **forward-fill** | copy the last price into an empty minute. **We never do this** |

## Three things that proved the spec wrong

**1. The size estimate was too small.** I predicted 50–100 MB/day. Measured: **143 MB/day**. `reference-bars` is 62% of the store on its own.
**2. Nothing goes quiet.** I predicted far-dated options would be silent. Measured across 71 minutes: **688.0 lines per minute**, with no silent contract-minute. Delta republishes.
**3. The two channels need different waits.** The slower channel timestamps run median **3,176 ms** behind arrival, against **212.6 ms** for the fast one. They cannot share a watermark.

## Two bugs this work uncovered

**Fixed.** `scan_parquet` on a bare folder *raises* if one non-Parquet file sits in it. Compaction writes a temp file there — so every partition would have been unreadable during compaction, and permanently unreadable after a crash.

**Open — [#14](https://github.com/lalitkarthik/delta-exchange-payoff/issues/14).** The ladder labels a **six-hour change** as "open interest in USD". It matches `oi_change_usd_6h` on all 136 options and **goes below zero**, which open interest cannot. Not fixed here: the fix changes the chain contract, and that was out of scope for a storage ticket.

## Commands

**Read a day back:**
```bash
cd engine && ./.venv/Scripts/python.exe -c "import polars as pl; print(pl.scan_parquet('../data/quote-bars/**/*.parquet', hive_partitioning=True).collect())"
```

**Run the engine (it writes as it runs):**
```bash
cd engine && ./.venv/Scripts/python.exe -m uvicorn --app-dir src deltapayoff.main:app --port 8000
```

**Run the store (split mode; use a free store port):**
```bash
cd engine && ./.venv/Scripts/python.exe -m uvicorn --app-dir src deltapayoff.store_main:app --port <store-port>
```

**Compact yesterday:**
```bash
./engine/.venv/Scripts/python.exe tools/compact_store.py --dry-run
```

**Count the minutes that have quotes but no volatility of ours:**
```bash
./engine/.venv/Scripts/python.exe tools/measure_computed_gaps.py --expiry 25-09-2026
```

**Measure the store:**
```bash
./engine/.venv/Scripts/python.exe tools/measure_store.py
```

---

## Still open

1. ~~No day compacted from a full day of **real** flush files.~~ **Answered by #67**, on a
   scratch copy of 2026-09-08 (BTC+ETH): 2,040 files → 8, 174.22 MiB → 144.81 MiB, 16.9%
   smaller, 4.6 s. `measured`, `docs/design/research/0004-durable-store.md` §2.
2. ~~The whole-day read against the five-minute layout is not measured.~~ **Answered by
   #67**: 2026-09-08 BTC, four tables, 2,217,941 rows, warm cache, min of five — 206.4 ms
   uncompacted, 82.0 ms compacted. `measured`, same file.
3. No lock stops two compactors running at once. Documented, not defended against.
4. The aggregator is not yet checked against a raw frame capture.
5. `lts`'s meaning is unverified. It is stored and decides nothing.
6. In the default monolith, Table C loses a row when the cache is stale for a whole minute. `measured` on 2026-09-04,
   expiry 25-09-2026: sampling once a minute lost **217 of 904 minutes — 24%** — every gap
   exactly one minute long, while quotes for those minutes were captured. [#23](https://github.com/lalitkarthik/delta-exchange-payoff/issues/23)
   samples every ten seconds instead. That **narrows** the window from one instant to ten
   seconds; it does not close it, and the 217 stay lost. A day-long `tools/measure_computed_gaps.py`
   run scheduled by #63 fills in the surviving rate; leave that number blank until the run.
   Always a **missing** row, never an invented one.
7. I8 (#70)'s seam is done; S3 is unreachable from this build — no AWS account here, no
   `boto3`/`fsspec` dependency (`measured` 0, `docs/design/lld/store-numbers.md`). With
   access: wire a backend behind `BarStore.path`'s S3 branch, run
   `tools/measure_store_cloud.py --root s3://…` for criterion 4, a live minute's
   read-back for criterion 2, one compaction with full read-back for criterion 3.

---

**Next action:** run the read-back command at the top. It takes about 5 seconds and tells you the store is real.
