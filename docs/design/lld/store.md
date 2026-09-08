# The store — low-level design

**Two modules.** `bars.py` turns events into ticks and folds ticks into bars; `store.py`
writes bars to hive-partitioned Parquet and is the only module in the engine that touches a
file. Neither knows a venue.

Landed by #37, which moved the writer off the retired quote record and onto the canonical
events. #43 adds ETH — no change here: `HIVE_SCHEMA`'s `date/underlying` partitioning already
took the underlying's name off the event, not off a BTC-shaped assumption, so a second
underlying is a second value in a column that already existed, not a new code path. What the
tables mean and why they are shaped this way is `docs/storage.md`; this document is the part
a reader would otherwise reconstruct from code.

---

## 1. Four tables, and where each comes from

| Table | Grain | Source | Grace |
|---|---|---|---|
| A `quote-bars` | contract × minute | `md.option_quote`, with `md.option_reference`'s own quote as fallback | 8.0 s |
| B `reference-bars` | contract × minute | `md.option_reference` | 8.0 s |
| C `computed-bars` | contract × minute | **sampled** from the chain cache, not from the bus | 0.0 s |
| D `spot-bars` | underlying × minute | `md.index_quote` | 8.0 s |

**Table C is the odd one out and stays so.** Our implied volatility and Greeks are produced
by the recompute loop, so the writer samples `ChainStream.computed_chains` through a
callable — a callable rather than the stream, so this module never learns a chain cache
exists. Its grace is zero on purpose: a sample has no stragglers to wait for, and sealing
minute M the instant M ends is what makes a chain still stamped inside M when M+1 closes
**late**, and therefore refused. Without that, a dead feed would re-sample its last ladder
every minute and the store would fill with identical fabricated rows.

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

This is why table A seals on the **larger** of the two watermarks. A bar sealed at the book's
2.0 s closes four seconds before its fallback could arrive, so every fallback quote would be
counted late, the fallback would be dead code and the flag would be a constant `True`.

## 4. Bucketing, and the one clock that decides

`ts_venue` alone decides which minute a tick belongs to, converted to microseconds by
integer arithmetic in `bars._micros` — never through `timestamp()`, which returns a float
that has already spent its 15–16 significant digits on the integer part of a microsecond
epoch. `adapters.delta._venue_time` takes the same care in the other direction, because
since #37 that stamp is what the writer buckets on.

**An event with no `ts_venue` is refused whole and counted in `skipped`.** Bucketing it on
our arrival time is the one thing this design exists not to do.

**Never forward-fill.** A minute with no arrivals produces no row — not nulls, never the
previous close — in every table, and a partial bar at process stop is written with its true
tick counts and no flag.

## 5. Failure modes

| What goes wrong | What happens |
|---|---|
| An event type the writer does not store | `skipped` grows; the record is dropped. Not a defect — the subscription is to the whole bus |
| An event with no `ts_venue` | `skipped` grows; refused whole |
| A tick for a minute already sealed | `late` grows; refused. A discarded observation with no counter is the same lie as a silent drop |
| A symbol that will not parse into underlying/expiry/strike/type | `unparseable` grows; never filed under a guess |
| A reference event quoting neither side | No fallback tick; a pair of absent prices is not a quote |
| Recording switched off | The subscription is still **drained** and `discarded` grows; a lossless queue nobody empties backs up the socket reader |
| A flush raises | `flush_errors` grows; the loop continues and retries next interval |

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

`BarWriter.attach(bus)` and `ingest(event)`, with `BarStore` on a `tmp_path` and an injected
clock, so sealing and flushing are test parameters rather than races. Events come from the
real adapter through `tests/fakes/decoder.py`. `tests/test_bars.py` drives the aggregators,
`tests/test_store.py` the writer and the files, `tests/test_recording.py` the pause over
HTTP, and `tests/test_composition.py` a scripted socket into the writer's counters.

## 8. Numbers

| Number | Tag | Run |
|---|---|---|
| Table A/B/D grace 8.0 s | `derived` | 1.45x the 5,511 ms ceiling from `tools/measure_arrival_lag.py`, 2026-09-04 |
| Table C grace 0.0 s | `derived` | a sample has no stragglers; see §1 |
| Arrival lag: book p50 212.6 ms, max 510.3 ms; reference median 3,176 ms, max 5,298.8 ms | `measured` | `tools/measure_arrival_lag.py`, 2026-09-04 |
| Flush every 5 minutes, 288 files per table per day before compaction | `derived` | #16, from the measured hourly file sizes |
| ~7,056 spot observations a minute against ~118 for one contract's book | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| 2,460 quote-bar rows, all carrying `last_lts`, over 410 s | `measured` | #36's live run, 2026-09-07T13:24:41Z |
| One production-interval (300 s) flush, BTC alone: 7,535 rows, 8 files, 877,975 bytes across the four tables | `measured` | #37's live run |
| The same flush shape, BTC+ETH: scheduled flush 11,740 rows/8 files/699,591 bytes; plus the trailing open-minute flush, 14,088 rows/16 files/909,237 bytes total. File count doubles cleanly — one `underlying=` partition per table per flush, per underlying; rows and bytes do not, because the two runs cover different minutes and market activity, not only a different recorded set | `measured` | `tools/measure_store.py`'s `capture()`, generalised to two underlyings, scratch root, 2026-09-08 — full breakdown in `docs/storage.md` |
| Every one of the four tables holds an ETH partition with rows after two flush intervals | `measured` | same run |
