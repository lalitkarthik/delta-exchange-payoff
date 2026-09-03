# One-minute bars, in hive Parquet

**Verdict: three of #5's four tables run end to end, and both watermarks are measured
rather than guessed.** A frame published on the bus becomes a sealed one-minute bar and a
partitioned Parquet file, read back in Polars with its types intact and **no row for a
minute that had no arrivals** — in any of the three tables.

- **Table A, quote bars** — bid, ask and mid OHLC per contract per minute, from `ob_l2`,
  with `ticker` as the fallback and a `from_book` flag saying which.
- **Table B, reference bars** — mark and last traded price as OHLC, open interest,
  turnover, Delta's five Greeks and three implied vols as last-value-in-bar.
- **Table D, spot bars** — one row per minute per **underlying**, never per contract.

The two channels **do not share a watermark**, and that is the finding this work turned
on. `ob_l2` seals at **2.0 s**; `ticker` seals at **8.0 s**, both `derived` from measured
arrival-lag distributions. Table A now seals on the ticker's number because the ticker is
its fallback source — see §1.1.

Implemented in `engine/src/deltapayoff/{bars,store,wire}.py`, with the lossless
subscription in `fanout.py` and the writer task wired into `main.py`'s lifespan. Measured
by `tools/measure_arrival_lag.py`.

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

**A second run, 2026-09-04, 60 s per channel, all 685 listed BTC options**, taken while
building #11 and confirming the first:

| channel | frames | p50 | p90 | p95 | p99 | p99.9 | max | min |
|---|---|---|---|---|---|---|---|---|
| `ob_l2` | 82,337 | 194.6 | 202.2 | 208.8 | 334.2 | 444.8 | 449.7 | 185.7 |
| `ticker` | 8,220 | 2,882.5 | 4,415.8 | 4,557.4 | 4,691.4 | 4,696.4 | **4,696.6** | 981.7 |

All figures in milliseconds. Mean `ob_l2` 198.5 ms, mean `ticker` 3,078.7 ms. Nothing
dropped, nothing malformed, backlog drained in full on both runs.

**The `ob_l2` grace period is 2.0 s** — about **3.9x the measured maximum**, and 4.6x
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

So **the two channels cannot share a watermark**, which is now resolved rather than
merely recorded: `tick_from_quote` still refuses `ticker` frames, `samples_from_ticker`
handles them, and both the reference and spot tables seal at 8.0 s. This is the kind of
thing that would otherwise have been discovered as "the reference bars are mysteriously
empty".

### 1.1 The ticker's own watermark: 8.0 s

**`ticker` cannot use 2.0 s and table A cannot keep it either.**

The ticker grace is **8.0 s**, `derived` from the two measured runs above: a worst
observed lag of **5,298.8 ms** (2026-09-04, 45 s run) and **4,696.6 ms** (2026-09-04,
60 s run). Eight seconds is **1.5x** the larger of those.

A 3.9x multiple like `ob_l2`'s would give twenty seconds, and it is not needed, because
this lag is **structural rather than stochastic**. The stamp marks the quote the frame
describes and the channel republishes every 5,001 ms, so the lag is bounded by one
republish interval plus transit: 5,001 ms plus `ob_l2`'s measured 510.3 ms maximum is a
**5,511 ms ceiling** (`derived`). Eight seconds is 1.45x that ceiling, and the 2.5 s of
headroom absorbs the two things the measurement cannot — unsynchronised clocks, and
queue latency, since the watermark is read when the writer drains.

**Table A's grace moved from 2.0 s to 8.0 s, and #10's number is not wrong — it is no
longer the only input.** #10 sealed the quote bars at 2.0 s because `ob_l2` was their
only source. #11 gives them a second: the ticker's `q` array is the fallback when the
book is silent for a contract, exactly as `wire.chain_from_frames` overrides one with the
other on a live chain. A bar that sealed at 2.0 s would close four seconds before its
fallback could arrive, so every fallback quote would be counted late, the fallback would
be dead code, and `from_book` would be a constant `True` — a column storing nothing.
Sealing on the larger of the two watermarks is the only choice that makes the fallback
reachable.

The cost is that a quote bar is written six seconds later, inside an hourly flush;
nothing downstream can notice. `tests/test_bars.py::test_the_quote_bars_wait_long_enough
_for_a_fallback_to_arrive` fails when the grace is put back to 2.0 s — sabotage-verified.

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

## 5. Table B, and the mislabel it uncovered

**Mark and the last traded price get a range; everything else gets one sample.** Mark and
LTP are prices and they move. Open interest, turnover, Delta's five Greeks and its three
implied vols are levels — an open/high/low/close of rho would be four numbers describing
nothing.

**Only the close of Delta's 24-hour `ohlc` field is stored.** That array is a rolling
24-hour trade candle; its close is the last traded price and worth keeping, while its
open, high and low are a 24-hour window that would be re-stored identically 1,440 times a
day. The ordering is checked against the **REST snapshot captured beside the frames** —
119 of the 120 symbols REST reports a candle for agree element for element, and the one
that does not traded between the two captures. On 81 of them the close differs from the
open, the high *and* the low, so a transposed index cannot pass by coincidence.

This knowingly discards **eleven of every twelve** samples of Delta's own vols and Greeks,
which republish every 5,001 ms. Accepted: the finding that would preserve — that their
vol steps while ours moves continuously underneath it — is a live observation to capture
once, not a reason to store ten million rows a day forever.

**Delta's figures all carry a `venue_` prefix.** #5's table C stores our computed Greeks
under the bare names, and two columns called `delta` in one store is one careless join
away from measuring how well we imitate Delta rather than what the prices imply.

### `oi[1]` is not open interest in USD — `measured`, and it changes what is stored

`wire.decode_ticker` has called `oi[1]` `oi_value_usd` since #3. It is not. Checked
against the paired REST snapshot on **all 136** captured symbols, it equals Delta's
`oi_change_usd_6h` exactly; it equals REST's `oi_value_usd` on **ten**; and it goes
**negative**, which a notional cannot.

So the ticker channel carries **no USD open interest at all**, and #11's acceptance
criterion "open interest in contracts and USD" is met only in contracts. Table B stores
`oi_contracts` and `oi_change_usd_6h` under the name the number actually has, and stores
no USD notional. Deriving one from contracts, contract size and spot was rejected: that
is a calculation, not an observation, and it would sit in a column readers would take for
something the venue published.

`Leg.oi_value_usd` is still fed from that position on the websocket path, because
renaming it changes the chain contract `web/lib/contract.ts` reads and that is not this
ticket's to change. **The live screen therefore shows a six-hour change in a column
labelled USD open interest, and that is a bug wanting its own ticket.**
`tests/test_wire.py::test_the_open_interest_second_element_is_a_six_hour_change_not_a_usd
_notional` pins the finding so the mistake cannot be made twice.

### The `g` and `qiv` orderings, re-checked

`wire.py` declares `g = [delta, gamma, rho, theta, vega]` and
`qiv = [ask_iv, bid_iv, mark_iv]`. Both were re-verified against the paired REST snapshot,
which names all eight fields: taking, per symbol, the array index that minimises relative
error against REST's named value gives delta→0 (136/136), gamma→1 (136/136), rho→2
(135/136), theta→3 (125/136) and vega→4 (124/136), and ask_iv→0 (134/136), bid_iv→1
(136/136), mark_iv→2 (136/136). The stragglers are contracts whose values moved between
the two captures, which are seconds apart. **The declared orderings are correct** and
nothing was transposed.

## 6. The provenance flag, and why a tick count is not enough

The ticker channel carries a bid and an ask about ten times more slowly than the book,
and it is the fallback when the book is silent for a contract. `wire.chain_from_frames`
already applies exactly that precedence to a live chain, overriding a ticker quote with a
book quote **wholesale** rather than averaging them, and a bar makes the same choice: each
bucket keeps two independent sets of series and the emitted bar takes the book's if it saw
anything at all.

`from_book` is a real `Boolean` column. **A bar sampled 118 times and one sampled 12 times
are different objects, and a tick count alone cannot tell "quiet book" from "no book at
all" — twelve could be either.** Under #10 a contract with no book produced *no row*, a
silence indistinguishable from the ingester being down. It now produces a row flagged
`False`.

Sabotage-verified: making `_emit` always take the book's series fails four tests,
including the end-to-end writer test.

## 7. Table D, and why spot is not a column

**Spot is a property of the underlying, not of a contract.** Measured: all 136 ticker
frames captured inside a 0.06 s window carried an identical `sp` of 77651.9. A column on
contract rows would store the same four numbers 588 times a minute and — the real
objection — would let two contracts whose frames straddled a minute boundary disagree
about what spot was. A store that contradicts itself about the underlying is worse than
one that does not carry it.

So table D is one row per minute per underlying, carrying open, high, low, close and a
tick count, with **no contract identity at all**. It is the best-sampled series in the
feed at roughly 7,056 observations a bar, because every contract's frame carries it —
which is why the tick count is worth storing: it is the one number in the store that says
whether the ingester was actually running.

`spot_ticks` is `UInt32`. `UInt16` would have fitted 7,056 today and overflowed the moment
ETH was turned on.

## 8. Still open

- **The compression ratio against raw JSON is not measured.** #5's central claim is
  50–100 MB/day against ~52 GB/day of raw, roughly 500–1000x. That is `derived` arithmetic
  and needs a recorded session to replace it with a number.
- **Read time with and without partition pruning is not measured.** Pruning is verified as
  *behaviour*; whether it buys anything at our size is a separate question and a
  measurement, not an assertion.
- **Table C is not built** — our computed implied vol and Greeks, sampled at bar close and
  stamped with the model that produced them. It is the last of #5's four.
- **`Leg.oi_value_usd` is fed from `oi_change_usd_6h` on the websocket path**, so the live
  screen shows a six-hour change under a USD-open-interest label. Left alone deliberately
  — renaming the field changes the chain contract the web app reads — and wanting its own
  ticket. See §5.
- **No USD open interest is stored**, because the ticker channel does not carry one.
- **Nightly compaction is not built.** Hourly files accumulate; the design calls for one
  file per table per partition per day.
- **The bars have not been validated against a raw capture.** #5's stated safety net is a
  one-off capture of raw frames kept as a fixture, with the aggregator checked against it
  offline. That capture has not been taken.
- **`lts`'s meaning is still unverified**, and stays that way until somebody asks Delta or
  finds it documented.
