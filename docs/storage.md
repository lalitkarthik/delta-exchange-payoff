# One-minute bars, in hive Parquet

**Verdict: all four of #5's tables run end to end, both watermarks are measured rather
than guessed, and the design's two headline claims have now been weighed — one of which
did not survive.** A frame published on the bus becomes a sealed one-minute bar and a
partitioned Parquet file, read back in Polars with its types intact and **no row for a
minute that had no arrivals** — in any of the four tables. A closed day's hourly fragments
fold into one file per table per partition, verified before anything is deleted and
recoverable from an interruption at any stage.

- **Table A, quote bars** — bid, ask and mid OHLC per contract per minute, from `ob_l2`,
  with `ticker` as the fallback and a `from_book` flag saying which.
- **Table B, reference bars** — mark and last traded price as OHLC, open interest,
  turnover, Delta's five Greeks and three implied vols as last-value-in-bar.
- **Table C, computed bars** — **ours**: implied volatility, five Greeks, the fitted
  forward, the discount and the year fraction, each row stamped with the model that
  produced it. Sampled from the chain cache at bar close, not folded from the bus.
- **Table D, spot bars** — one row per minute per **underlying**, never per contract.

The two channels **do not share a watermark**, and that is the finding this work turned
on. `ob_l2` seals at **2.0 s**; `ticker` seals at **8.0 s**, both `derived` from measured
arrival-lag distributions. Table A now seals on the ticker's number because the ticker is
its fallback source — see §1.1.

**The measured headline, replacing #5's arithmetic:** measured on the engine's own
hourly flush, the store runs at **2,792,972 rows/day** and **143 MB/day** against a
**measured 65.14 GB/day** of raw JSON — a **~454x** reduction. #5 estimated 50-100 MB/day
and 500-1000x, so the footprint estimate **did not survive**. And the gap #5 expected
between real row counts and the ceiling **is not there**: every listed contract quotes in
every minute. See §10.

Implemented in `engine/src/deltapayoff/{bars,store,wire}.py`, with the lossless
subscription in `fanout.py`, the sampling reader in `stream.py` and the writer task wired
into `main.py`'s lifespan. Measured by `tools/measure_arrival_lag.py`,
`tools/measure_store.py` and `tools/compact_store.py`.

## How to read this

**New to this? Read [storage-start-here.md](storage-start-here.md) first.** It is the
one-page map: where the files are, what the four tables hold, and the commands to read a
day back. This document is the detail behind it.

**Measured** names the run. Every number below came from a live connection unless it says
`derived`, which means arithmetic on a measured figure, or `assumed`, which appears
nowhere in this document. §10 adds a third tag, **`synthetic-layout`**: real values off
the wire in a manufactured file layout, used only for questions about files and paths and
never reported as a footprint.

**Sections 1 to 8 stamp their runs in local time (2026-09-04); section 10 stamps them in
UTC**, because the store partitions on a UTC date and a measurement of the store has to
name the same day its directories do. The two are the same night: 23:00 UTC on the 3rd is
04:30 local on the 4th.

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

## 8. Table C, and the clock it does not share

**Everything else in this store is an event the venue timed. Table C is not.** Our
implied volatility and Greeks never arrive on the wire: they are made by `ChainStream`'s
100 ms recompute loop, and until #12 they lived exactly as long as the process did.
Restart, and there was no way to answer what the screen had said at a given minute.

So the writer **samples** rather than folds. Once a minute, as the boundary passes, it
reads `ChainStream.computed_chains()` — the chains the loop has *already* built — and
flattens each into one row per listed leg. Three consequences follow, and each is a
decision rather than an accident:

**It is bucketed on our clock.** There is no venue timestamp on a number we computed. The
row lands in the minute named by the chain's own `fetched_at`, which is when the loop
produced it. The cost is stated rather than hidden: the prices behind a chain computed at
09:01:00.05 were read from the venue about 200 ms earlier and belong to 09:00, so a
sample landing within one transit lag of a boundary can be attributed to the wrong side
of it. Every other table would be wrong to do this; this one has no alternative — the
chain contract carries no venue stamp, and 136 contracts each with their own `ts` do not
have one answer between them.

**The same boundary has a second, smaller effect and it errs toward absence.** The writer
detects the crossing within about a millisecond, because its drain loop wakes on every
message and they arrive 1,323 a second. If the recompute loop happens to fire inside that
millisecond, the chain it hands over is stamped in the *new* minute and the minute just
closed gets no row for that expiry — roughly one minute in a hundred, per expiry. It is a
missing row rather than an invented one, which is the direction this design chooses
everywhere else, and the row is not lost so much as attributed to the following minute.

**Its grace is zero**, and that is what enforces the no-invention rule rather than merely
stating it. `ChainStream._computed` keeps answering after the socket dies — it holds the
last chain it managed to build, forever. Sealing minute M the instant M ends means a
chain still stamped inside M when M+1 closes is **late** by the existing watermark rule,
counted and refused. A dead feed therefore writes nothing at all, instead of writing the
same five plausible rows every minute until somebody notices. Sabotage-verified twice:
stamping the sample with the minute being closed instead of with the instant the chain
was computed makes `test_a_minute_with_no_computed_chain_gets_no_computed_row` grow two
invented minutes and `test_a_cache_that_stops_being_recomputed_stops_producing_rows`
write ten rows where two are true.

**The sampling is edge-triggered on the boundary and reads the cache without touching
it.** The drain loop spins on every message, measured at 1,322.9 a second, so flattening
600 contracts on every pass would be the one piece of the writer capable of starving the
socket reader. And it deliberately does not call `ChainStream.chain()`, which recomputes
a dirty expiry synchronously — that would move a chain build onto the writer's pass and
duplicate work the recompute task is already doing.

**A row with no volatility carries no Greeks**, matching `compute.py` on the live path.
Greeks at some default volatility would be five plausible numbers describing nothing.
Absence is null and never zero throughout, including `iv_reason`, which is stored as null
rather than as the empty string `ComputedLeg` uses in its JSON payload.

**Delta's own figures are not in this table.** They are #11's `venue_` columns in table B,
and the separation is the whole point: two columns called `delta` in one store is one
careless join away from measuring how well we imitate Delta rather than what the prices
imply.

### The model version, and why it is a hand-written string

Every row carries

    F1+assumed-6.5 / S1-newton / ACT365 / mid-OTM

One token per decision that defines the model: the parity regression with the 6.5%
borrowed rate standing in when the discount cannot be fitted, Newton-Raphson, ACT/365,
and inversion of the out-of-the-money leg's **midpoint**.

**The fourth token is already scheduled to change.** Mid-versus-mark is #9's one unticked
acceptance criterion. If that measurement says mark is the better input, the production
input changes and every row stored before then was computed differently — two populations
in one column with nothing to tell them apart, unless the stamp is on the row.

A **content hash** of the modules was rejected: it changes when a docstring is edited,
producing forty versions that are all the same model, and these files' docstrings are
edited often. A **commit SHA** has the identical defect and is harder to read. The
weakness of a hand-maintained string is that somebody forgets to bump it; that is partly
covered because `forward_method` is stored per row independently and pins the largest
single source of variation — `F1`, `F1+assumed-rate` or `F2` — whatever the string says.

### The row reproduces offline, and what that does and does not prove

`tests/test_store.py::test_a_stored_row_reproduces_offline_from_the_quote_bar_beside_it`
publishes the captured 136-symbol chain on the bus, lets it be enriched exactly as it
would be live, then **rebuilds the chain from the stored bytes alone** — bid and ask
closes from table A, spot from table D, and the snapshot instant recovered by inverting
table C's own `years_to_expiry` against the settlement time the expiry names — and runs
`compute.enrich` over it. Forward, discount, year fraction, method, and every leg's
volatility and five Greeks come back **exactly equal** to what table C holds.

**What that proves:** the store carries every input the model needs, the columns mean what
their names say, `Float64` survives Parquet without losing precision, and a reader with
nothing but these files can re-derive the numbers rather than having to trust them. It is
not tautological — the two tables are written by different paths, one folding ticks off
the bus and one sampling the recompute cache, and both values have been through the file
layer. Sabotage-verified: narrowing `iv` to `Float32` makes it fail on the seventh
significant digit (`4.2968864068252675` against `4.296886444091797`).

**What it does not prove:** in that test each contract gets exactly one tick in the
minute, so `bid_close` *is* the tick that was live when the chain was computed and the
agreement is exact by construction of the scenario. On a busy minute it is not: a bar's
close is the last tick of the minute while the sample came from whatever chain the 100 ms
loop had last produced, and those are usually but not always the same quote. So this
establishes that the model is **reproducible from the stored schema**, not that every
historical row will re-derive to the last decimal from its own bar. And it says nothing
about whether the model is right — only that the store is honest about which model ran.

**`fetched_at` is not stored as a column**, deliberately: `years_to_expiry` pins it
exactly against a settlement time the expiry already names, and #5 asks that nothing
trivially derivable be stored. The test inverts it rather than assuming it.

---

## 9. Compaction, and the ordering that keeps a day

**Hourly flushing buys a sixty-minute crash budget and pays for it in files.** Twenty-four
per table per partition per day is roughly **26,000 a year** across the four tables, and
Parquet is bad at that: every file carries its own header and footer, its own dictionary
pages and its own row-group statistics, and a reader has to open every one of them before
it can decide it wants none. Folding a closed day into one file per table per partition
takes the year to about **a thousand**.

`BarStore.compact_partition` does it; `tools/compact_store.py` is the nightly entry point;
`compact_all()` is the same thing as a function, because a scheduler is the operator's
choice.

**This is the only part of the store that deletes anything, and therefore the only part
that can lose something permanently.** Everywhere else a bug writes a wrong file and the
right one is still recoverable from the feed or from the file beside it. Here a bug
removes the inputs and there is nothing to re-run from. Everything below follows from
that asymmetry.

### The sequence, and what a crash at each point leaves behind

1. **Recover** any run already in flight (below).
2. **List** the partition's `*.parquet`. The list *includes* an earlier compacted file, so
   a partition that gained late hourly files after being compacted folds back to one file
   rather than to two. "Already compacted" is therefore `len(inputs) <= 1` — a property of
   the directory, not of a filename.
3. **Write** the concatenation to `compact-NNNNNN.parquet.tmp`.
   *Crash here:* the tmp is not a `*.parquet`, so no reader and no later compaction sees
   it. Every input is still present. The next run deletes it and starts over.
4. **Verify** the tmp by reading it back off the disk.
   *Nothing has been deleted at this point and nothing will be if this fails.*
5. **Write the manifest**, atomically. The commit point: it names the output and the exact
   list of files that output has been verified to contain.
6. **Delete the inputs**, then **publish** the tmp with `os.replace`.

### Deleting before publishing: a gap, never a doubling

The obvious order is publish-then-delete, and it is wrong. It leaves a window in which the
output and all twenty-four of its inputs are readable at once, and a reader landing in
that window gets **every row of the day twice**. Nothing in the output says so, and if the
machine dies in that window the day reads doubled until somebody runs compaction again.

Deleting first leaves the opposite window: the day reads **short** while its rows sit safe
in a tmp that the next run publishes. A gap is visible and recoverable; a silent doubling
is invention. This store refuses invention everywhere else — a minute with no arrivals
gets no row, a stale chain is counted late and discarded — and the file layout is held to
the same rule.

`tests/test_compaction.py::test_a_compaction_interrupted_at_any_stage_never_reads_back_a
_doubled_row` asserts it at every stage: whatever survives an interruption must be a
**subset** of what was written, value for value.

### Verification is a full read, not a footer peek

The row count and the schema both live in Parquet's metadata, so checking them there costs
nothing — and proves nothing about the pages underneath. This is the one gate between a bad
write and a deleted day, so it decompresses every page. The cost is one extra pass over a
file that was just written and is still in the page cache.

The expected count comes from the **inputs' own footers, one file at a time** — never from
the height of the frame about to be written. Deriving the expectation from the thing being
checked is how a verification passes by construction and proves nothing.

Sabotage-verified twice:
`test_nothing_is_deleted_when_the_compacted_file_fails_to_verify` makes the counts
disagree and asserts every input is still on disk **byte for byte**;
`test_a_schema_that_does_not_match_the_files_fails_before_any_delete` reaches the same gate
through the schema.

### The manifest is what makes a half-finished delete recoverable

Recovery driven by a directory listing is the trap. Interrupt the delete loop after three
of twenty-four inputs are gone, and a listing-driven rerun rebuilds from the surviving
twenty-one — **a truncated day, silently**. The manifest names all twenty-four, the deletes
are by name and `missing_ok`, and a rerun therefore finishes the same job rather than
starting a smaller one.

It is a sidecar in the partition it describes rather than a central journal, so a partition
is recoverable on its own and a lost index cannot orphan one. It is written to a temporary
name and `os.replace`d in, because a truncated JSON file would fail to parse on every later
run and wedge the partition forever.

**Recovery re-verifies before it deletes**, exactly as the first pass does. The manifest
already says the file verified once, but a recovery runs by definition after something went
wrong. `test_a_recovery_refuses_to_delete_inputs_for_an_output_that_no_longer_verifies`
damages the tmp between the two runs and asserts all four inputs survive.

### Every stage is interrupted on purpose

`store.COMPACTION_STAGES` names all six points at which `compact_partition` can be killed,
and three parametrised tests run over the whole tuple: rerun-to-correct, never-doubled, and
recoverable-any-number-of-times. `test_the_crash_tests_cover_every_compaction_stage`
asserts the parametrisation *is* `COMPACTION_STAGES`, so a stage added without a crash test
fails the suite rather than shipping untested.

The interruption is raised from **inside the real code path** rather than reconstructed
afterwards. Hand-built wreckage tests what the test's author imagined a crash would leave;
this tests what a crash actually leaves.

### Two things it does not defend against, stated rather than discovered

**Two compactors at once.** Two processes on one partition would race on one manifest name.
The nightly job is a single process, and a lock file is one more thing to leave behind
after a crash.

**The open day.** `compact()` skips today by default. `flush` writes with `write_parquet`
straight to its final name, which is not atomic, so compacting the partition the writer is
still flushing into races a half-written file. A torn read raises before anything is
deleted, so it is survivable rather than dangerous — but waiting one day removes it. A file
that appears *after* the input list is taken is never deleted, because only the names in
the manifest are, so an hourly flush landing mid-compaction survives and is folded in next
time. Pinned by `test_a_flush_that_lands_mid_compaction_is_not_deleted_by_it`.

### The regression compaction uncovered

**`pl.scan_parquet` handed a bare directory refuses the whole dataset the moment that
directory holds one file whose extension is not `.parquet`.** It raises
`InvalidOperationError: directory contained paths with different file extensions` — it does
not skip the file.

Compaction puts exactly two such things in a partition while it runs: its `.tmp` output and
its manifest. So on the code as #10 left it, **every partition would have been unreadable
for the duration of every compaction, and permanently unreadable after a crash** — a store
that cannot be read at all because of a file that is not part of it.

`scan()` now names `**/*.parquet` explicitly. Hive partitioning and its pruning are
unaffected — the keys are still read off the paths, which
`test_a_partition_filter_is_answered_by_the_paths_before_a_file_is_opened` now pins against
Polars' own optimised plan rather than against a row count that a full scan would satisfy
too. `test_a_scan_ignores_a_file_in_the_tree_that_is_not_part_of_the_dataset` pins the
regression itself.

---

## 10. The measurements, and what happened to #5's estimate

#5 asserts **50–100 MB/day** compressed against **~52 GB/day** of raw JSON, roughly a
**500–1000x** reduction. Both were arithmetic. Both are now weighed, and **the footprint
estimate did not survive**: the store is about **1.4–2.9x bigger** than #5 said and the
ratio correspondingly smaller.

| | #5 said (`derived`) | measured / derived |
|---|---|---|
| rows/day | ~2,541,600 | **2,792,972** (`derived` from run F) |
| raw JSON | ~52 GB/day, from 636.5 KB/s | **65.14 GB/day** (`derived` from run B's measured 736.3 KB/s) |
| store | 50–100 MB/day | **~143 MB/day** (`derived` from run F) |
| reduction | 500–1000x | **~454x** against run B's denominator, ~387x against run A's |

### The runs

Every figure below carries the run that produced it. Three tags are used and they mean
different things:

- **`measured`** — read off a disk or a socket in the named run.
- **`derived`** — arithmetic on a measured figure. A projection to a day is `derived`.
- **`synthetic-layout`** — the **values are real** and came off the wire; the **file layout
  is manufactured**, because a day of live data takes a day. Used only where the question
  is about files rather than about content, and never reported as a footprint.

| run | what | when (UTC) | how |
|---|---|---|---|
| **A** | raw wire only, 60 s | 2026-09-03 23:00 | `measure_store.py --raw-seconds 60 --skip-day` |
| **B** | full live pipeline into a scratch root, 600 s, flush every 120 s | 2026-09-03 23:00–23:10 | `measure_store.py --capture 600 --capture-flush 120 --dates 8` |
| **C** | a copy of run B's own files, compacted | 2026-09-03 23:12 | `compact_store.py --root <copy> --before 9999-01-01`, then `measure_store.py --skip-raw --skip-day --root <copy>` |
| **D** | run B's window shifted into a 24-fragment day, compacted | 2026-09-03 23:10 | run B's `day` phase |
| **E** | read time over 16 partitions | 2026-09-03 23:10 | run B's `read` phase |
| **F** | **the running engine's own first hourly flush** | 2026-09-03 22:39–23:38 | `measure_store.py --skip-raw --skip-day` over `data/` |
| **G** | the same hour rewritten as 1, 2, 4, 12 and 24 files | 2026-09-03 23:40 | one-off split over run F's files |

**Run F is the headline and it is as live as a measurement gets.** It is the engine's own
store, written by the process serving `/chain` and `/ws/chain`, flushed on its own hourly
timer, one file per table per partition, sixty whole minutes. Nothing about it was staged.

**Run B is also live**, and stands up the same `DeltaFeed`, the same lossless subscription,
the same four aggregators, the same `ChainStream` recompute loop and the same `BarWriter`
that `main.py` wires into its lifespan, over both channels and every listed BTC option. The
only thing changed is `flush_seconds`. It reported **0 skipped bus records and 0 flush
errors**. It exists because the engine's first file lands an hour after the engine starts,
and it is kept because two independent live runs agreeing is worth more than one.

### The raw stream: the denominator, measured on the engine's own subscription

| run | seconds | msg/s | KB/s | GB/day (`derived`) |
|---|---|---|---|---|
| A | 60 | 1,286.8 | 627.4 | 55.51 |
| B | 600 | 1,511.7 | 736.3 | 65.14 |

**Both channels, all 688 listed BTC options** — which is what `main.py` subscribes and
therefore what the store is a compression *of*. This matters: `tools/measure_feed.py`'s
636.5 KB/s, the figure #5 quotes, subscribes `ticker` over everything but `ob_l2` over
**one chain**. It is not the wrong number; it is the number for a different subscription,
and using it as the denominator would flatter the ratio.

The two runs disagree by 17%, ten minutes apart, on the same socket. That spread is itself
the finding: **there is no single raw byte rate**, and #5's 636.5 KB/s sits inside the range
rather than being contradicted by it. The ratio is quoted against run B's denominator
because both ends then come from one socket over one interval, and against run A's as well
so the spread is visible rather than hidden in a rounding.

### Bytes per row, per table

`measured`, run F — the engine's own hour, one file per table, no compaction needed
because an hourly flush is already one file:

| table | rows | bytes | **B/row** | minutes | run C's 11-minute figure |
|---|---|---|---|---|---|
| `quote-bars` | 41,280 | 1,059,689 | **25.67** | 60 | 27.33 |
| `reference-bars` | 41,280 | 3,711,046 | **89.90** | 60 | 94.24 |
| `computed-bars` | 30,941 | 1,098,225 | **35.49** | 55 | 37.10 |
| `spot-bars` | 60 | 3,541 | **59.02** | 60 | 229.00 |
| **all four** | **113,561** | **5,872,501** | **51.71** | | 54.10 |

The two independent live runs agree to within 6% on the three tables that matter, which is
the check that neither is an artefact of its own window.

**`reference-bars` costs 3.5x what `quote-bars` costs per row, and it is 62% of the whole
store.** It is only 18% wider in *columns* — 26 against 22 — but 20 of them are `Float64`
against the quote table's 13, and they hold Delta's five Greeks, its three implied vols,
open interest and turnover: high-entropy numbers that neither dictionary-encode nor
run-length-encode. The quote table's 13 floats are three OHLC quadruples — bid, ask and
mid — plus the strike: prices that move slowly and repeat, beside three `UInt32` counts and
three dictionary-encoded strings. **Column count is not row width; entropy is.**

**`spot-bars`'s number is an artefact and is included to say so.** Sixty rows in a file is
almost all header and footer; per-row cost there measures Parquet's overhead, not spot. At
1,440 rows a day the whole table is 80 KB and rounds to nothing. Run C's 229 B/row on
eleven rows is the same artefact, larger.

### The daily footprint, against the ceiling

`derived` from run F's bytes per row and its rows per minute, over 1,440 minutes:

| table | rows/min (`measured`) | rows/day | #5's ceiling | of #5 | ceiling at 688 listed | of that | MB/day |
|---|---|---|---|---|---|---|---|
| `quote-bars` | 688.0 | 990,720 | 846,720 | 117.0% | 990,720 | **100.0%** | 25.43 |
| `reference-bars` | 688.0 | 990,720 | 846,720 | 117.0% | 990,720 | **100.0%** | 89.07 |
| `computed-bars` | 562.6 | 810,092 | 846,720 | 95.7% | 990,720 | 81.8% | 28.75 |
| `spot-bars` | 1.0 | 1,440 | 1,440 | 100.0% | 1,440 | 100.0% | 0.08 |
| **total** | | **2,792,972** | **2,541,600** | **109.9%** | **2,973,600** | **93.9%** | **143.34** |

### The gap between real and ceiling is not there, and that is the finding

#5 says the row counts are ceilings rather than expectations, because "the venue publishes
on change rather than on a metronome, and far-dated contracts are silent for long
stretches", and asks for the gap between real and ceiling to be reported.

**Measured over a full hour, there is no gap.** 41,280 quote rows over 60 minutes is
**688.0 per minute exactly** — every one of the 688 listed contracts produced a bar in every
minute of the hour, on the quote table and on the reference table alike. Run B saw the same
688.0 over its eleven minutes. Two runs, seventy-one minutes, not one silent contract-minute.

**The book channel republishes about 118 times a minute per contract and the ticker every
5,001 ms, and neither goes quiet on a listed contract just because nobody is trading it.**
So "publishes on change" describes the venue's *semantics* and not its *rate*: a quote is
**republished**, not merely changed. The prediction that far-dated contracts would be silent
for long stretches is wrong for this venue — what is quiet on a far-dated strike is the
*price*, not the *feed*, and a bar whose open, high, low and close are all equal is still a
row.

The one table under its ceiling is `computed-bars`, at 81.8% — 562.6 rows a minute against
688 listed contracts. That is not silence either: it is legs whose expiry had no computed
chain in that minute, plus §8's boundary effect, which errs toward absence by design.

**And the total is 109.9% of #5's stated ceiling**, which is not a contradiction: #5's
846,720 is 588 contracts × 1,440 minutes, and the listing was **688** in this window. The
ceiling moved because the market did — the count is a property of Delta's listing calendar,
not of the design — and the sensible thing to carry forward is the *formula*, `listed ×
1,440`, rather than the number.

### The ratio, and the estimate it replaces

    65.14 GB/day of raw JSON  ->  143.34 MB/day stored   =  454x   (run B denominator)
    55.51 GB/day              ->  143.34 MB/day          =  387x   (run A denominator)

**#5's 50–100 MB/day and 500–1000x are replaced by ~143 MB/day and ~390–455x.** Three
things account for the difference, and none of them is a defect:

- **The listing grew from 588 contracts to 688** (+17%), and rows scale with it directly.
- **`reference-bars` is a much wider row than the estimate assumed** — 90 bytes against a
  store average of 52, and 62% of the footprint on its own. Aggregating the ticker channel
  was #5's late change and the one that decided the store's size; it decided the *shape* of
  it too.
- **Every listed contract quotes every minute**, so the store runs at its ceiling rather
  than comfortably under it, which is what the estimate implicitly assumed.

At 143 MB/day a year is **52 GB**, against #5's 20–35 GB. **#5's conclusion survives even
though its arithmetic does not**: a retention policy solves a problem this design does not
have, and bars are kept indefinitely.

### Compaction is a file-count decision, not a footprint decision

This is the measurement that changed the most on contact with evidence, and in the
direction nobody expected.

`measured`, run C — the eleven-minute window, six small files per table:

    24 files -> 4, 21,217 rows, 1.37 MiB -> 1.09 MiB (20.1% smaller)

`synthetic-layout`, run D — one window shifted into 24 hourly fragments:

| table | files | rows | before | after | saved | time |
|---|---|---|---|---|---|---|
| `quote-bars` | 24 → 1 | 181,632 | 4,847.7 KiB | 430.6 KiB | 91.1% | 0.32 s |
| `reference-bars` | 24 → 1 | 181,632 | 16,715.1 KiB | 1,407.6 KiB | 91.6% | 0.35 s |
| `computed-bars` | 24 → 1 | 145,680 | 5,278.7 KiB | 439.0 KiB | 91.7% | 0.33 s |
| `spot-bars` | 24 → 1 | 264 | 59.1 KiB | 3.3 KiB | 94.4% | 0.34 s |
| **all four** | **96 → 4** | **509,208** | **26.3 MiB** | **2.2 MiB** | **91.5%** | **1.34 s** |

**Neither of those percentages is the answer, and run D's is badly misleading.** Run D's day
is one window repeated twenty-four times, so its columns hold twenty-four copies of every
value and compress as nothing real ever would. What run D measures honestly is the file
layout and the cost: twenty-four fragments per table become one, and all four tables compact
in **1.34 s**. Run C's 20.1% is real data, but on files of about 1,260 rows, where the fixed
per-file cost is most of the file.

**Run G answers it properly**, by taking run F's real hour and writing the same rows as 1,
2, 4, 12 and 24 files. The marginal cost of one extra file, `measured`:

| table | 1 file | 24 files | inflation | marginal cost per extra file |
|---|---|---|---|---|
| `quote-bars` | 1,059,689 | 1,481,822 | 1.398x | 18.4–38.5 KiB |
| `reference-bars` | 3,711,046 | 4,417,898 | 1.190x | 30.7–38.3 KiB |
| `computed-bars` | 1,098,225 | 1,450,861 | 1.321x | 15.3–17.5 KiB |
| `spot-bars` | 3,541 | 45,193 | 12.763x | 1.8–2.3 KiB |

At the flush size the design actually uses — **41,280 rows an hour**, not 1,260 — the
marginal cost of a file is about **97 KiB across the four tables**. A day is 24 hourly files,
so compacting one saves roughly **23 × 97 KiB ≈ 2.2 MB out of ~141 MB: about 1.5%**
(`derived` from run G's measured curve).

**So compaction buys file count and almost no bytes, and that is the correct answer rather
than a disappointing one.** #5 asked for it to solve the small-files problem — 26,000 files
a year down to about a thousand — and it does exactly that. What it does *not* do is shrink
the store, because an hourly flush is already large enough to amortise Parquet's fixed
costs. `spot-bars`'s 12.8x inflation is the counter-example that proves the mechanism: it is
the one table whose files really are too small, and it is the one table where compaction
pays in bytes.

It also means **run F's 143.34 MB/day is within about 1.5% of a fully compacted day**, so
the footprint above is a figure rather than a bound.

### Read time, with and without partition pruning

`measured`, run E — 16 partitions (8 dates × 2 underlyings), median of 5, `synthetic-layout`
because the partitions are copies. Pruning is a decision taken on the **path**, so copies
are exactly as good as distinct data for measuring it.

| table | full scan | pruned | scan-then-filter | speedup | rows |
|---|---|---|---|---|---|
| `quote-bars` | 61.59 ms | **12.98 ms** | 63.58 ms | **4.74x** | 2,906,112 → 181,632 |
| `reference-bars` | 67.93 ms | **13.13 ms** | 66.01 ms | **5.18x** | 2,906,112 → 181,632 |
| `computed-bars` | 49.74 ms | **20.06 ms** | 49.61 ms | **2.48x** | 2,330,880 → 145,680 |
| `spot-bars` | 3.86 ms | 3.86 ms | 4.69 ms | **1.00x** | 4,224 → 264 |

**Partitioning pays, and it pays for the reason the theory gives.** The middle column is the
filter answered by the directory name; the right-hand one is the same filter applied after
every file has been opened and read, and it costs the **same as the full scan** — 63.58
against 61.59 — because it *is* the full scan. The whole of the 4.7x is files never opened.

Polars' own optimised plan is the proof rather than the timing: a scan filtered on one
underlying lists **8 sources of 16**, and one filtered on a date and an underlying lists
**1 of 4** in
`test_a_partition_filter_is_answered_by_the_paths_before_a_file_is_opened`.

**`spot-bars` gets nothing, and that is the honest negative result.** 264 rows in a
partition is smaller than the cost of opening the file at all, so pruning saves work that
was never there. It is the table that most argues partitioning is theory that does not pay —
and it is 1,440 rows a day against the other three tables' million.

Two caveats on the timings. They are **warm-cache** medians on a local SSD; a cold read
would widen the gap, because the pruned scan would also be reading less from the device.
And 16 partitions is a small store: pruning's advantage grows with the number of partitions
skipped, so 4.7x at sixteen is a floor for a year at 730.

### What these numbers do not say

- **They are one hour and one ten-minute window, both around 23:00 UTC.** Rows per minute
  are pinned to the listing and will not move much; the raw byte rate does, and the 17%
  spread between runs A and B ten minutes apart is the scale of it.
- **The listing count is not a constant.** 688 today, 588 when #5 was written. Every
  rows/day figure here is `listed × 1,440` and should be re-derived, not re-read, when the
  listing moves.
- **No day has yet been compacted from twenty-four real hourly files.** Run G bounds what
  that would change at about 1.5%; it does not perform it.
- **`synthetic-layout` is not live.** Runs D and E answer questions about files and paths.
  Neither is a statement about how much data a day contains.

---

## 11. Still open

- **No day has been compacted from twenty-four real hourly files.** §10's run G bounds
  what that would change at about 1.5% of the footprint, but bounding is not doing. The
  engine has to run a full day, and then `tools/compact_store.py` followed by
  `tools/measure_store.py --skip-raw --skip-day` gives the number with no projection in
  it at all.
- **The boundary attribution has not been measured against a live feed.** §8 argues that a
  computed sample taken within one transit lag of a boundary can land on the wrong side of
  it. How often that actually happens is a number nobody has taken.
- **`Leg.oi_value_usd` is fed from `oi_change_usd_6h` on the websocket path**, so the live
  screen shows a six-hour change under a USD-open-interest label. Left alone deliberately
  — renaming the field changes the chain contract the web app reads — and wanting its own
  ticket. See §5.
- **No USD open interest is stored**, because the ticker channel does not carry one.
- **The bars have not been validated against a raw capture.** #5's stated safety net is a
  one-off capture of raw frames kept as a fixture, with the aggregator checked against it
  offline. That capture has not been taken.
- **`lts`'s meaning is still unverified**, and stays that way until somebody asks Delta or
  finds it documented.
- **Compaction has no lock.** Two compactors on one partition would race on one manifest
  name. Stated in §9 rather than defended against, because the nightly job is one process.
