# The store — low-level design

**Two modules.** `bars.py` turns events into ticks and folds ticks into bars; `store.py`
writes bars to hive-partitioned Parquet and is the only module in the engine that touches a
file. Neither knows a venue.

Landed by #37, which moved the writer off the retired quote record and onto the canonical
events. #43 adds ETH — no change here: `HIVE_SCHEMA`'s `underlying/date` partitioning already
took the underlying's name off the event, not off a BTC-shaped assumption, so a second
underlying is a second value in a column that already existed, not a new code path. What the
tables mean and why they are shaped this way is `docs/storage.md`; this document is the part
a reader would otherwise reconstruct from code.

**The layout is `<table>/underlying=<asset>/date=<YYYY-MM-DD>/*.parquet` for all four tables.**
There is one venue, so no venue level exists. Expiry, strike and option type remain columns.
Underlying comes first because all readers select one underlying while `/smile` reads every date
without a date predicate; the first directory therefore prunes the other asset before dates are
scanned.

**Process boundary.** In split mode (`DELTA_BUS=redis`), `deltapayoff.store_main:app` owns
`BarWriter` and is the only Parquet writer; the api has no `BarWriter` or `BarStore` buffer.
A lossless `bar-buffer` subscription folds `md.option_bar` into `BarBuffer`, whose
table-specific rows are injected as `BarStore.pending_source`; `/smile` (`smile.read_smile`),
`/chain/minutes` (`historical.list_minutes`), `/chain/at` (`historical.read_ladder_at`) and
`/bars` (`contract_bars.read_contract_bars`) union disk with that source. Both modes expose
the newest sealed minute; overlap is one row and disk wins. `BUFFER_HORIZON_SECONDS` bounds
the buffer, and [store-numbers.md](store-numbers.md) says what it held. See
[0010-store-replay.md](../decisions/0010-store-replay.md),
[#81](https://github.com/lalitkarthik/delta-exchange-payoff/issues/81) and the crash protocol
[store-replay.md](store-replay.md). With `DELTA_BUS` unset, one in-process FanOut serves both.

---

## 1. Four tables, and where each comes from

| Table | Grain | Source | Grace |
|---|---|---|---|
| A `quote-bars` | contract × minute | `md.option_quote`, with `md.option_reference`'s own quote as fallback | 8.0 s |
| B `reference-bars` | contract × minute | `md.option_reference` | 8.0 s |
| C `computed-bars` | contract × minute | monolith: sampled from its chain cache; split store: folded from `computed.chain`, published by the api on the writer's own sampling schedule | 0.0 s monolith (`derived`); 2.0 s split (`derived`) |
| D `spot-bars` | underlying × minute | `md.index_quote` | 8.0 s |

**Table C differs by composition.** The monolith samples its own `ChainStream` cache and keeps
grace `0.0 s`; only the split composition publishes `computed.chain`. The split `BarWriter`
folds those per-leg events, with `2.0 s` grace (`derived`). The event schedule is the writer's
schedule, not a store-side resampling of a cache. The split event is `schema_version` 2 with
separate per-leg `call` and `put` blocks. Both graces, and what table C validation holds the
two compositions to, are in [store-numbers.md](store-numbers.md).

### `index-bars` — outside the four

A fifth table, and not one of the four above. `tools/backfill_index_bars.py` writes it by
walking Delta's `.DEXBTUSD` 1-minute index candles backwards from now, in pages of up to
4,000 bars; nothing else ever does, and the engine's `BarWriter` does not know it exists.

| Column | Type | Note |
|---|---|---|
| `minute` | timestamp | the venue's own minute, not ours |
| `symbol` | string | `.DEXBTUSD` — carried, not assumed |
| `index_open` / `index_high` / `index_low` / `index_close` | float | the venue's own OHLC |

No `spot_ticks` and no tick count of any kind: a venue candle carries no observation count
to report, unlike table D's. A minute the venue did not return produces no row, the same
rule as everywhere else in this store.

Same layout as the four above: `index-bars/underlying=<asset>/date=<YYYY-MM-DD>/*.parquet`.

**It sits outside `all_stores()`, so `compact_all` and `tools/migrate_store.py` never touch
it.** Compaction exists to fold many small five-minute flush files into one; this table is
never flushed in that shape — a backfill run writes it once, in large pages, not on the
five-minute timer the other four share. It is born straight into the current layout, so
`migrate_store.py`, which moves older tables into that layout, has nothing to do here either.

## 2. The translation, in one place

The catalogue follows `docs/chain-contract.md` and `models.Leg`. This store does not. The
whole of the difference is in `bars.samples_from_reference`, and it is here so a reader does
not have to diff two schemas to find it:

| Event field | Store column | Note |
|---|---|---|
| `oi` | `oi_contracts` | contracts on both transports; REST's own `oi` is a BTC notional and is not read |
| `oi_change_usd_6h` | `oi_change_usd_6h` | can go negative, which is what proves it is not a notional |
| `last_price` | `ltp_open`/`ltp_high`/`ltp_low`/`ltp_close` | aggregated, so one field becomes four columns |
| `mark` | `mark_open`/`mark_high`/`mark_low`/`mark_close` | likewise |
| `turnover` | `turnover` | last value in the bar, not aggregated |
| `delta` `gamma` `theta` `vega` `rho` | `venue_delta` … `venue_rho` | prefixed, because **ours** live in table C under the bare names |
| `bid_iv` `ask_iv` `mark_iv` | `venue_bid_iv` `venue_ask_iv` `venue_mark_iv` | same reason |
| `bid` `ask` (on the reference) | table A's fallback tick | not a column of table B |
| `lts` (on the quote) | `last_lts` | carried, never bucketed on |
| `product_id` | — | **not stored.** It is a static venue identifier the browser reads live off the ladder; a column repeating it on every row of every minute would be 588 copies a minute of a fact that does not change |

`oi_value_usd` and `tick_size` reach no table on this path: the reference frame carries
neither, and absent is not derived.

**Two things about table B changed with the events and neither is a rename.** A `NaN` or
`Infinity` from the venue used to reach Parquet as itself, because the old converter read
`wire` directly; every one of these columns now passes the adapter's non-finite guard, so
it arrives `null` and `adapter.non_finite` counts it. And a last trade spelled `"0"` is now
absent rather than four zeroes and a tick — `wire.decode_ticker_extras` reads it with
`to_quote_number`, for the same reason spot does.

## 3. Provenance, without a channel

Table A stores `from_book`. It says whether a minute's prices came from the order book or
from the slower stream standing in for a silent one — and a tick count cannot answer it,
because twelve samples could be a quiet book or no book at all.

**The event type is the provenance.** A tick built from `md.option_quote` carries
`BOOK_SOURCE`; one built from `md.option_reference`'s own bid and ask carries
`REFERENCE_SOURCE`. Each bucket keeps two independent sets of series and the emitted bar
takes the book's if it saw anything at all, the fallback's otherwise. **They are never
merged**: a book bar with two stale fallback samples folded into its high and low would be a
bar whose provenance is unanswerable, which is the whole point of the column.

This is why table A seals on the **larger** of the two watermarks: the smaller leaves every
fallback quote late and the flag a constant `True` -- [store-numbers.md](store-numbers.md) counts.

## 4. Bucketing, and the one clock that decides

`DELTA_STORE_ROOT` configures the store root and defaults to `<repo>/data`. Two engine processes
(including two stores) must never share a root: both would flush into the same directories and
corrupt the root-level `_store-checkpoint.json` and `_store-flush-intent.json` as well.

**The same variable is also the home switch, R3's decision (I8, #70):** an ordinary path
stays the host mount, unconditionally the default; `s3://bucket/prefix` names S3
Standard, the chosen home ([0004](../decisions/0004-durable-store.md)), parsed by the
pure, `BarStore`-free `store_home.py`. `BarStore.path` is the seam every
filesystem-touching method reaches disk through, and refuses loudly —
`StoreHomeUnavailable`, never a silent local fallback — since no backend reaches S3 from
this build. [store-numbers.md](store-numbers.md) says what closes it.

`ts_venue` alone decides which minute a tick belongs to, converted to microseconds by
integer arithmetic in `bars._micros` — never through `timestamp()`, which returns a float
that has already spent its 15–16 significant digits on the integer part of a microsecond
epoch. `adapters.delta._venue_time` takes the same care in the other direction, because
since #37 that stamp is what the writer buckets on.

**An event with no `ts_venue` is refused whole and counted in `skipped`.** Bucketing it on
our arrival time is the one thing this design exists not to do.

### Graceful stop in split mode

**Never forward-fill.** A minute with no arrivals produces no row — not nulls, never the previous
close — in every table. The monolith writes a partial stop bar with true tick counts; the store
process checkpoints the partial open minute, so a restart inside the `derived` thirty-minute Redis
retention completes it rather than sealing a truncated bar. A stop longer than that retention loses
the minutes trimmed from Redis, and the gap signal reports them.

## 5. Failure modes

| What goes wrong | What happens |
|---|---|
| An event type the writer does not store | `skipped` grows; the record is dropped. Not a defect — the subscription is to the whole bus |
| An event with no `ts_venue` | `skipped` grows; refused whole |
| A tick for a minute already sealed | `late` grows; refused. A discarded observation with no counter is the same lie as a silent drop |
| A symbol that will not parse into underlying/expiry/strike/type | `unparseable` grows; never filed under a guess |
| A reference event quoting neither side | No fallback tick; a pair of absent prices is not a quote |
| Recording switched off | The subscription is still **drained** and `discarded` grows; a lossless queue nobody empties backs up the socket reader |
| A flush raises | All or nothing (#101): the files that flush published are removed, the buffer and the flush ordinal are left as they were, and the next interval flushes the same bars — one `BarStore._flush_buffer` implements it and `_flush_legacy` and `_flush_generation` differ only in how they name a file. **Both compositions then say the same thing** (#107): `flush_errors` grows by exactly one, an error record carries the exception with `exc_info`, and an `alert` `store.flush_failed` is published. One `BarWriter._flush_failed` does all three, reached from `_flush_all` in the monolith and `_commit` in the split, so there is no second copy to drift; the message names a table in the monolith, where the four flush independently, and a generation in the split, where they commit as one. **The alert lands only where the writer was given a `publish`.** `store_main.py` hands the split `bus.publish`; `main.py` hands the monolith `StoreAlertSeam`, which forwards the `alert`, refuses every other event because the writer subscribes to that same fanout, and delivers through `loop.call_soon_threadsafe` because `_flush_all` raises on an `asyncio.to_thread` worker and `asyncio.Queue.put_nowait` is not thread-safe |

**A count is not a diagnosis.** `flush_errors` has been on `/health` since #103, so a failing flush
was already a number; until #107 the monolith wrote nothing saying *why*. The alert needed somewhere
to go too, and both compositions now hand the writer a `publish`; the row above says which and why.

## 6. What still speaks the venue's shape

`bars._parse_symbol` reads underlying, expiry, strike and option type out of the **venue's**
symbol, which arrives on the instrument as `venue_symbol`. Those four are typed fields on
`Instrument` and could be carried on the tick instead, which would delete the parse and its
`unparseable` counter outright.

It was **not** done in #37, deliberately: the store's row identity would have changed shape
in the same commit that changed its input, and the point of a contract step is that the
stored bytes do not move. So the converters **refuse** an instrument carrying no
`venue_symbol`, and are counted in `skipped`, rather than falling back to the canonical
string: `_parse_symbol` refuses that string, so a fallback would drop the row one layer
down as `unparseable` while reading like a working venue-neutral path. A requirement that
is visible can be removed; one that is disguised cannot. It is the obvious next simplification and it belongs to whichever
ticket next opens this table. The spot bars already made the move — `SpotTick` names its
underlying rather than the messenger's symbol — because `md.index_quote` carries no
instrument to parse in the first place.

## 7. The seam the tests drive

**A composition test drives the writer `build_feed_stack` builds, never one it assembles.**
#107's first test gave its `BarWriter` a `publish` `main.py` did not: true of no live process.

`BarWriter.attach(bus)` and `ingest(event)`, with `BarStore` on a `tmp_path` and an injected
clock, so sealing and flushing are test parameters rather than races. Events come from the
real adapter through `tests/fakes/decoder.py`. `tests/test_bars.py` drives the aggregators,
`tests/test_store.py` the writer and the files, `tests/test_recording.py` the pause over
HTTP, and `tests/test_composition.py` a scripted socket into the writer's counters.

## 8. Numbers

Every measured and derived number behind this design, with the run that produced it, is in
[store-numbers.md](store-numbers.md). Evidence grows and a design does not -- the same move
#62 made for the HLD and #63 made for the logging catalogue.
