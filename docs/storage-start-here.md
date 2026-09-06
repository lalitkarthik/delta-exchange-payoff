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
├── quote-bars/       date=2026-09-04/underlying=BTC/*.parquet
├── reference-bars/   date=2026-09-04/underlying=BTC/*.parquet
├── computed-bars/    date=2026-09-04/underlying=BTC/*.parquet
└── spot-bars/        date=2026-09-04/underlying=BTC/*.parquet
```

The folder names carry the date and the asset. A reader skips a day without opening a file.

---

## The four tables

| Table | Holds | Columns |
|---|---|---|
| **quote-bars** | bid, ask and mid — each with open/high/low/close | 24 |
| **reference-bars** | mark, LTP, open interest, **Delta's** IV and Greeks | 28 |
| **computed-bars** | **our** IV and **our** Greeks, plus the model stamp | 20 |
| **spot-bars** | Spot, once a minute for the whole asset | 8 |

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

---

## The restart-loss window is five minutes

**The engine has no graceful stop.** Whatever is sitting in the buffer when the process
dies — a crash, a closed laptop, a `taskkill`, a restart to pick up a code change — is
gone, and no restart can recover it. The flush interval *is* that window.

It was an hour. That cost real data three times in one day, so [#16](https://github.com/lalitkarthik/delta-exchange-payoff/issues/16)
made it **five minutes**.

Two consequences worth carrying:

- **Time a restart just after a flush.** Restarting five minutes into an interval throws
  away the five minutes. There is no way to ask the engine to flush first.
- **288 files per table per day** before compaction, not 24. Compaction folds them back
  to one overnight — which means compaction now matters more than it did.

---

## The one rule that matters

**A minute with no data gets no line.**

Never a copied-forward price. Never a line of empty values.

**Why this is not a detail.** Delta's own history does the opposite. Its `/v2/history/candles` fills empty minutes with the last trade and does not say so. For `C-BTC-60000-270624` it returns 801 daily bars — **797 of them are invented**.

Aggregation is compression. Forward-filling is fabrication.

**How we know the rule holds.** We put the fault into the code on purpose. Six tests failed. We took it out. Six tests passed. A guard that has never been seen to fail is not a guard.

---

## What we built — 4 tickets

| # | Commit | What it added |
|---|---|---|
| [#10](https://github.com/lalitkarthik/delta-exchange-payoff/issues/10) | `ba22314` | quote-bars, end to end |
| [#11](https://github.com/lalitkarthik/delta-exchange-payoff/issues/11) | `a002ee1` | reference-bars + spot-bars |
| [#12](https://github.com/lalitkarthik/delta-exchange-payoff/issues/12) | `e338aaa` | computed-bars + model stamp |
| [#13](https://github.com/lalitkarthik/delta-exchange-payoff/issues/13) | `d164ba2` | compaction + measurements |

**466 tests passing. ruff clean. `tsc --noEmit` clean.**

---

## How a price becomes a line — 5 steps

1. **A tick arrives.** One price message from Delta, on one of two channels.
2. **It joins a minute.** Bucketed by *Delta's* clock, not ours — so our network cannot move a price into the wrong minute.
3. **The minute seals.** We wait 8 seconds past the boundary for stragglers, then close it. Late arrivals are counted and dropped, never silently lost.
4. **Bars flush to disk.** Every five minutes. A crash costs at most 5 minutes.
5. **A day compacts.** 24 files become 1. Verified by full read-back *before* anything is deleted.

**`computed-bars` does not come this way.** Our IV and Greeks are never on the wire, so
that table is **sampled** from the chain cache — **every ten seconds**, plus once as each
minute boundary passes — and the minute keeps the freshest sample taken inside it.
Sampling once a minute lost a quarter of them; see below.

---

## Word list

| Word | Meaning |
|---|---|
| **tick** | one price message from Delta |
| **bar** | one minute summarised: open, high, low, close |
| **Parquet** | the file format. Stores columns apart, so it compresses well |
| **partition** | a folder whose name is the filter — `date=…/underlying=…` |
| **pruning** | skipping folders by name, without opening files |
| **flush** | write buffered bars to disk. Every five minutes |
| **watermark** | how long we wait before sealing a minute. 8 seconds |
| **seal** | close a minute. Nothing more goes in |
| **compaction** | join a day's 288 flush files into one daily file |
| **forward-fill** | copy the last price into an empty minute. **We never do this** |

---

## Three things that proved the spec wrong

**1. The size estimate was too small.** I predicted 50–100 MB/day. Measured: **143 MB/day**. `reference-bars` is 62% of the store on its own.

**2. Nothing goes quiet.** I predicted far-dated options would be silent for long stretches. Measured across 71 minutes: **688.0 lines per minute, exactly, with no silent contract-minute.** Delta republishes; it does not wait for a change.

**3. The two channels need different waits.** The slower channel's timestamps run a median **3,176 ms** behind arrival, against **212.6 ms** for the fast one. They cannot share a watermark.

---

## Two bugs this work uncovered

**Fixed.** `scan_parquet` on a bare folder *raises* if one non-Parquet file sits in it. Compaction writes a temp file there — so every partition would have been unreadable during compaction, and permanently unreadable after a crash.

**Open — [#14](https://github.com/lalitkarthik/delta-exchange-payoff/issues/14).** The ladder labels a **six-hour change** as "open interest in USD". It matches `oi_change_usd_6h` on all 136 options and **goes below zero**, which open interest cannot. Not fixed here: the fix changes the chain contract, and that was out of scope for a storage ticket.

---

## Commands

**Read a day back:**
```bash
cd engine && ./.venv/Scripts/python.exe -c "import polars as pl; print(pl.scan_parquet('../data/quote-bars/**/*.parquet', hive_partitioning=True).collect())"
```

**Run the engine (it writes as it runs):**
```bash
cd engine && ./.venv/Scripts/python.exe -m uvicorn --app-dir src deltapayoff.main:app --port 8000
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

1. No day compacted from a full day of **real** flush files — 24 hourly ones before
   [#16](https://github.com/lalitkarthik/delta-exchange-payoff/issues/16), 288 five-minute
   ones after it. Bounded at ~1.5% of bytes; bounding is not doing.
2. **The whole-day read against the five-minute layout is not measured.** `derived` in #16
   at roughly 88 ms, up from a measured 6.8 ms. It needs a real day at the new cadence
   before it is a number.
3. No lock stops two compactors running at once. Documented, not defended against.
4. The aggregator is not yet checked against a raw frame capture.
5. `lts`'s meaning is unverified. It is stored and decides nothing.
6. Table C loses a row when the cache is stale for a whole minute. `measured` on
   2026-09-04, expiry 25-09-2026: sampling once a minute lost **217 of 904 minutes —
   24%** — every gap exactly one minute long, while the quotes for those minutes were
   captured all along. [#23](https://github.com/lalitkarthik/delta-exchange-payoff/issues/23)
   samples every ten seconds instead. That **narrows** the window from one instant to ten
   seconds; it does not close it, and the 217 stay lost. The rate that survives it is
   unmeasured — run `tools/measure_computed_gaps.py` after a full day. Always a
   **missing** row, never an invented one.

---

**Next action:** run the read-back command at the top. It takes about 5 seconds and tells you the store is real.
