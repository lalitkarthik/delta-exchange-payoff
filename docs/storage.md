# One-minute quote bars, in hive Parquet

**Verdict: the tracer bullet runs end to end, and the watermark is measured rather than
guessed.** A tick published on the bus becomes a sealed one-minute bar and a partitioned
Parquet file, read back in Polars with its types intact and **no row for a minute that
had no ticks**. The grace period for sealing a bar is **2.0 s**, `derived` from a
**measured** arrival-lag distribution whose p99.9 is 438.7 ms and whose maximum over
61,648 frames is 510.3 ms.

Implemented in `engine/src/deltapayoff/{bars,store}.py`, with the lossless subscription in
`fanout.py` and the writer task wired into `main.py`'s lifespan. Measured by
`tools/measure_arrival_lag.py`.

## How to read this

**Measured** names the run. Every number below came from a live connection on
**2026-09-04** unless it says `derived`, which means arithmetic on a measured figure, or
`assumed`, which appears nowhere in this document.

---

## 1. The arrival lag, and the grace period read off it

Bars are bucketed on **Delta's** clock and discovered on **ours**. Those are different
clocks, so a tick stamped 12:00:59.900 by the venue can reach us at 12:01:00.240 — after
its minute has closed. Sealing a bar is therefore a decision about **lateness**, not about
time. Wait too little and real ticks are counted late and discarded; wait too much and
every bar is delayed for nothing.

**Measured**, `tools/measure_arrival_lag.py`, 2026-09-04, 45 s per channel, all 685 listed
BTC options subscribed, lossless queue so a burst could not evict its own tail:

| channel | frames | p50 | p90 | p95 | p99 | p99.9 | max | min |
|---|---|---|---|---|---|---|---|---|
| `ob_l2` | 61,648 | 212.6 | 218.4 | 226.6 | 365.3 | **438.7** | **510.3** | 204.2 |
| `ticker` | 6,165 | 3,176.0 | 4,743.8 | 4,904.1 | 5,295.9 | 5,298.6 | 5,298.8 | 391.0 |

All figures in milliseconds. Mean `ob_l2` lag 216.7 ms; mean `ticker` lag 3,033.5 ms.
Nothing was dropped and nothing was malformed in either run.

**The grace period is 2.0 s** — about **3.9x the measured maximum** on `ob_l2`, and 4.6x
its p99.9. The headroom is deliberate, and the argument is the asymmetry of the two
mistakes:

- **Too short discards real observations.** Every late tick is a quote that happened and
  is now gone, and because congestion is what makes ticks late, the ticks lost are
  disproportionately the ones from fast-moving minutes.
- **Too long delays a bar by two seconds inside an hourly flush.** Nothing downstream can
  notice.

Two properties of the number stop it being a network latency, and both argue for headroom
rather than against it:

- **It is transit *plus clock skew*.** `time.time()` here and Delta's `ts` are two
  unsynchronised clocks; an NTP offset of tens of milliseconds is ordinary, and it drifts
  between runs in a way transit does not. A watermark has to absorb skew as well as
  transit, because both move a tick across a boundary the same way.
- **It excludes queue latency.** The watermark is read when the writer drains, so a
  backlog adds to observed lateness. The measured figure is the floor, not the ceiling.

If the store ever shows `late` climbing, this is the number to re-measure — and the
re-measurement, not a bigger guess, is the fix.

### The finding the ticket did not predict

**`ticker`'s `ts` is not a publish time.** It runs a median 3,176 ms and up to 5,298.8 ms
behind our arrival — very nearly the channel's whole 5,001 ms republish interval, and
*ten times* `ob_l2`'s lag on the same socket at the same moment. The shape says the stamp
marks the quote the frame describes rather than the moment it was sent.

So **the two channels cannot share a watermark.** Bucketing `ticker` on `ts` under this
2 s grace would call almost every frame late. `tick_from_quote` refuses `ticker` frames
outright, and #5's table B needs a watermark of its own measured the same way. This is the
kind of thing that would otherwise have been discovered as "the reference bars are
mysteriously empty".

### `lts` is stored and decides nothing

**Measured** in the same run: `lts` sits a **median 377.0 ms before `ts`**, ranging from
13.7 ms *after* to 7,979.5 ms *before*. That range is far too wide for a field whose
meaning is unverified to be bucketed on — a 7.9 s displacement is two minute boundaries.
It is carried as `last_lts`, a column, and nothing reads it.

---

## 2. What a bar keeps and what it throws away

**Aggregation is compression; forward-filling is fabrication.** A bar summarises events
that happened. A forward-fill invents events that did not. Delta's own
`/v2/history/candles` pads empty buckets with the last trade and does not say so:
`C-BTC-60000-270624` returns 801 daily bars of which **797 are fabricated**.

So **a minute with no arrivals produces no row.** Not a row of nulls, and never the
previous close. `tests/test_bars.py::test_a_minute_with_no_arrivals_produces_no_row_at_all`
feeds a deliberate multi-minute silence and asserts nothing exists for it, and
`tests/test_store.py::test_the_row_count_equals_the_minutes_that_actually_had_ticks`
carries the same assertion through the file layer as a row count. Both were verified by
sabotage: with a forward-fill patched into `seal`, both fail; with it removed, both pass.

What a bar destroys is **path** — whether the high came before the low. That is an
acceptable loss for research over days and an unacceptable one for microstructure.

### The mid, and its trap

`mid_open` is **not** `(bid_open + ask_open)/2`. The highest bid and the highest ask need
not have occurred at the same instant, so the midpoint of the two bar extremes is a price
that never existed. The mid is therefore computed **per tick** and then aggregated, and
the three separate tick counts exist so that a mid built from fewer samples than its bid
is explicable rather than baffling.

A **one-sided tick advances only its own series** — values *and* counts. Measured on a
production snapshot all 588 BTC options were two-sided, so the path is rare, which is
exactly why it is pinned by a test now rather than found in six months of data.

---

## 3. The layout, and why `date/underlying`

```
data/quote-bars/date=2026-09-04/underlying=BTC/20260904T090000Z-000001.parquet
```

Hive partitioning puts the filter in the **directory name**, so a query for BTC on
4 September skips every other directory without opening a file. **Expiry, strike and
option type are columns.** Expiry as a partition level explodes into thousands of
directories holding a handful of rows each, and Parquet performs badly with many small
files — each carries header and footer overhead and a reader has to open all of them.

**Polars is not allowed to lay out the tree.** `write_parquet(partition_by=...)` names its
output `00000000.parquet` in every partition on every call, so the 10:00 flush would
silently overwrite the 09:00 one and the day would end holding only its last hour. The
file would be perfectly valid and simply short — the invisible kind of loss. So the
directories are built by hand and each flush writes its own uniquely named file.
Sabotage-verified: handing the layout to Polars makes
`test_a_second_flush_adds_to_a_partition_rather_than_overwriting_it` fail.

### Types, fixed now because they are expensive to change later

| columns | type | why |
|---|---|---|
| all prices, strike | `Float64` | 32-bit carries ~7 significant digits; a five-figure BTC price with decimals already spends 6 |
| `minute`, `last_lts` | `Datetime("us", "UTC")` | Delta's native resolution, no conversion. A `us` stamp routed through a float rounds its last digits away silently |
| `symbol`, `expiry`, `option_type`, `underlying` | `Categorical` | 588 distinct symbols repeated millions of times a day is the single largest compression win available |
| `bid_ticks`, `ask_ticks`, `mid_ticks` | `UInt32` | a count cannot be negative, and the measured ceiling is 118 per minute |

`tzdata` is a **runtime dependency on Windows, for reading only.** Polars stores the time
zone as a label and asks `zoneinfo` for it when converting a value back to a Python
`datetime`; without the package that raises `ZoneInfoNotFoundError` from inside Rust. The
files are correct either way.

---

## 4. The bus, and why the writer's queue is lossless

The fan-out's drop-oldest policy is right for a screen and wrong for a store — **and not
for the reason previously recorded here and in `fanout.py`.** That reason said a drop
leaves "a permanent hole in the historical record". Under bars it does not: a dropped tick
perturbs a bar and the bar still exists.

The real problem is worse because it is invisible. **Drops happen under load, load is when
price moves fastest, so drop-oldest systematically shaves the highs and the lows** — the
columns the bars exist to capture. That is a **bias, not noise**, and nothing in the
output says so.

So `FanOut.subscribe(name, maxsize, lossless=True)` gives an unbounded queue, and
`maxsize` stops being a ceiling and becomes a **watermark**: every offer made while the
queue already sits at or above it increments `over_capacity`, and `backlog_peak` records
the deepest it ever went. That is what stops an unbounded queue being a memory leak with
good manners — it fails an hour later, somewhere else, out of memory, unless somebody is
counting.

**And the flush cannot run on the event loop.** If it did, the socket reader could not be
scheduled while the disk worked, the receive buffer would fill, and Delta would close a
connection we simply failed to drain. `BarWriter` hands the write to `asyncio.to_thread`.
Sabotage-verified: with the flush inlined, the reader stand-in goes **0.253 s** without a
turn and `test_a_slow_flush_cannot_block_the_socket_reader` fails.

*That test caught itself first.* Its first version measured the time around `bus.publish`
and passed under the sabotage — `publish` is synchronous and returns instantly whether or
not the loop behind it is wedged. It now measures the gap between a reader task's turns,
which is the thing that actually kills a connection.

---

## 5. Still open

- **The compression ratio against raw JSON is not measured.** #5's central claim is
  50–100 MB/day against ~52 GB/day of raw, roughly 500–1000x. That is `derived` arithmetic
  and needs a recorded session to replace it with a number.
- **Read time with and without partition pruning is not measured.** Pruning is verified as
  *behaviour*; whether it buys anything at our size is a separate question and a
  measurement, not an assertion.
- **Tables B, C and D are not built** — reference bars, computed values and spot bars.
  Table B needs its own watermark, for the reason in §1.
- **Nightly compaction is not built.** Hourly files accumulate; the design calls for one
  file per table per partition per day.
- **The bars have not been validated against a raw capture.** #5's stated safety net is a
  one-off capture of raw frames kept as a fixture, with the aggregator checked against it
  offline. That capture has not been taken.
- **`lts`'s meaning is still unverified**, and stays that way until somebody asks Delta or
  finds it documented.
