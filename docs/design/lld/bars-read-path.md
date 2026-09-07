# The bars read path

Landed by [#46](https://github.com/lalitkarthik/delta-exchange-payoff/issues/46). Parts
and traffic between them: [../hld.md](../hld.md) §2.6. Contract:
[../../bars-contract.md](../../bars-contract.md).

## What it is

One route, `/bars`, and one module, `contract_bars.py`, sibling to `smile.py` and
`historical.py`. It solves nothing and calls Delta for nothing: it joins `quote-bars`
(mid, bid, ask) to `reference-bars` (last-traded price) on `symbol` and `minute`, for one
contract across a whole stored day, exactly the shape a candle chart needs and neither
existing read path serves — `/smile` reads one table across a whole expiry; `/chain/at`
reads four tables across every listed strike at one minute.

## Addressed by canonical string

`/bars?instrument=DELTA-BTC-20260904-77600-C&date=2026-09-04` takes
`events.instrument.Instrument.from_canonical`, not separate
`underlying`/`expiry`/`strike`/`option_type` parameters the way every earlier route is
addressed. This is the first route in the engine to be addressed this way, and it is
deliberate: the chart panel opens from a strike a reader clicked on the ladder, and the
one string the panel's URL already needs to carry (#33's *Implementation Decisions*,
*The contract chart*) is the one string this route needs to be asked with. `main.bars`
parses it, rejects a malformed one as 400, and **normalises the underlying** — `btc` and
`BTC` read the same rows — before either the store filter or the echoed response uses it,
so the two cannot disagree about which underlying answered.

**`venue` is parsed and then not filtered on.** No column in `store.py`'s four schemas
carries a venue at all: `underlying`, `expiry`, `strike` and `option_type` are the store's
grain, unchanged since before the instrument existed (#33's *Implementation Decisions* is
explicit that this was deliberate — NSE's fields are deferred to the second adapter, not
added speculatively). So a canonical string naming `NSE-` reads exactly the same rows a
`DELTA-` one would, filtered on the four columns the store actually has. This is not a
gap to close later: the day a second venue's bars land in this store, they will need a
venue column to be told apart by, and that column does not exist yet because nothing
has ever needed it to. `test_venue_is_parsed_but_not_filtered_on` pins the current
behaviour so a later change is a decision, not a surprise.

## No forward-fill

`read_contract_bars` builds its minute set from **`quote-bars` alone** — `dict(row for
row in quote rows)` — and looks reference bars up against that set rather than unioning
the two. A minute `reference-bars` holds and `quote-bars` does not is real (see below)
and is deliberately excluded: `quote-bars` gating "stored" is the same rule
`historical.list_minutes` uses for the slider's domain, so this route and the two
historical ones agree on what "stored" means across the whole engine, not just within
themselves.

`engine/tests/test_contract_bars.py`'s first test is three minutes written, the middle
one empty, asserting the response lists two and the third does not appear — not as a
null row, not at all. Red-green verified during development: with the gate's `sorted(quotes)`
replaced by a filled minute-by-minute walk that carried the last bar's values forward
into the gap, the same test failed on the injected minute appearing where it should have
been absent — an invented candle, exactly the failure this route exists to refuse.

## The LTP field is not a trade record

`reference-bars`' `ltp_*` looks like a trade OHLC and is not one. `store.py`'s own
docstring says why: Delta's `ltp` is the close of its rolling 24-hour candle, republished
on every ticker frame — roughly every 508 ms per the feed's measured cadence — regardless
of whether anything traded in the minute just closed. So the stored OHLC of it is, on a
thin contract, very often flat for many consecutive minutes and then steps once when a
real trade clears. This is exactly the shape `docs/bars-contract.md`'s toggle exists to
show against the mid series, which moves every minute the book was quoted regardless of
whether anyone traded.

**Measured, not asserted:** reading `data/reference-bars` and `data/quote-bars` for
`date=2026-09-04/underlying=BTC` (the machine's own recorded day, outside any test
fixture), for the four furthest-listed 04-09-2026 calls against an ~80,414 spot
(`spot-bars`' own mean that day) — 85000, 86000, 87000 and 88000, all 0DTE:

| Strike | `ltp_close` distinct values (of 658 minutes) | `mid_close` distinct values (of 659) | `ltp` longest flat run | `mid` longest flat run |
|---|---|---|---|---|
| 85000 | 64 | 135 | 25 | 75 |
| 86000 | 32 | 68 | 24 | 80 |
| 87000 | 31 | 72 | 29 | 77 |
| 88000 | 45 | 75 | 27 | 185 |

Neither series is flat the whole day and neither is continuous the whole day — both hold
long stretches unchanged, and this run measured `mid`'s longest stretch as the *longer*
one at three of the four strikes, which is not the shape a naive "LTP steps, mid moves"
story predicts. What is consistent across all four: `mid_close` visits roughly twice as
many distinct price levels as `ltp_close` over the same day, which is the republishing
behaviour above made visible as a number — a trade-price field republished on a fixed
cadence regardless of whether a trade happened touches fewer distinct values than a
book-derived one over the same stretch, because it is repeating one of a smaller set of
recent print prices rather than tracking wherever the book currently sits. **The two
series are not merely offset copies of one another; they go quiet at different times**,
so a chart that can only show one of them is hiding whichever one moved while the other
was flat. This is `What to notice` in the ticket, captured against real stored data
rather than a synthetic store — see the screenshot named in the hand-back.

## Where each field comes from

See `docs/bars-contract.md`'s table. In short: `quote-bars` gives `bid_*`/`ask_*`/`mid_*`;
`reference-bars` gives `ltp_*`. `mark_*` is on `reference-bars` too and is never read
here — the chart draws mid, bid, ask and last-trade, never mark, so there was no reader
for it to translate. The store's own column spellings (`ltp_*`) are kept as this
response's field names for that series; nothing needed renaming beyond what `mark_*`
being absent already settles.

## The disk-and-buffer union is shared, not copied a third time

Three read paths now want "everything this store holds matching a filter, disk and
buffer together": `smile._rows`, `historical._union` and this module's `_rows`. The
first review pass on this ticket found the third copy about to be written, so
`store.scan_and_pending(store, clause)` was added instead and `historical.py`'s `_union`
and `_spot_at` were moved onto it alongside this module. `smile.py`'s own version does
one thing extra — widening categorical columns to strings before concatenating — that
`vertical_relaxed` already covers for every path that does not carry `smile.py`'s
specific reason for doing it by hand, so `smile.py` was left as it was rather than
folded into a shared function it would have had to special-case.

## Cost

**`measured` 13.0 ms minimum (13.8–14.8 ms median across four invocations) for one
contract's 660-minute day, both tables** — against `/chain/at`'s `measured` 15.3 ms for
one minute across four tables and 136 legs, and `/smile`'s `measured` 6.8 ms for a whole
expiry's stored day.

Run: `python tools/measure_bars.py --minutes 660 --runs 20`, four invocations, minimum of
the four invocations' own minimums, on the machine this ticket was built on, 2026-09-07.
The script builds a throwaway store under a temporary directory — no `data/` is read —
writes 660 minutes of one contract across `quote-bars` and `reference-bars`, flushes, and
times `contract_bars.read_contract_bars` directly, bypassing FastAPI and HTTP.

**The two tables are collected together, not one after the other.** `read_contract_bars`
builds both lazy frames first and hands them to `pl.collect_all` in one call, so Polars'
own thread pool can overlap the two independent Parquet scans rather than this route
paying for both in series — the first version of this function called `.collect()`
separately on each and measured a `13.9`–`14.7` ms minimum across the same four-invocation
protocol; `collect_all` moved that to the `13.0` ms figure quoted above. A modest gain
rather than a dramatic one, because the fixed cost of opening a handful of small Parquet
files dominates either way at this row count.

**This is not the same shape of read as `/chain/at`'s, and the two numbers being close is
coincidence rather than a comparison.** `/chain/at` reads **one** minute across **four**
tables and **136 legs**; `/bars` reads **660** minutes across **two** tables and **one**
contract. Both build a Python object per row rather than staying inside a lazy Polars
frame — `_bar` constructs one `ContractBar` per matched minute, the same shape `_leg`
takes in `historical.py` — which is `derived`, not `measured` on its own, as where the
cost most likely sits for both routes; neither was profiled line by line.

## What is not measured

**A full trading day (~1,440 minutes) rather than 660.** The default above is chosen so
`--runs 20` stays fast, matching `measure_historical_chain.py`'s own default-vs-realistic
split; `--minutes 1440` was not run for this LLD and the number would not simply double,
because both tables share a Parquet file whose row-group count grows with the day, not
linearly with the read cost of one contract inside it.

**The live gap rate between `quote-bars` and `reference-bars` for this route's specific
join.** `docs/design/lld/historical-read-path.md` already flags this as owed for its own
four-table join; this route's two-table join is a narrower question and shares the same
answer — unmeasured, owed to whichever ticket next reads the live `data/` directory.

## Seam

The FastAPI app under `TestClient`, `DELTA_LIVE_FEED=0`, exactly as `smile.py`'s and
`historical.py`'s tests use it — `engine/tests/test_contract_bars.py`. Two `BarStore`s on
a `tmp_path`, injected through `get_historical_source`'s existing override — the same
`HistoricalSource` the two historical routes already share, reused rather than given a
narrower dependency of its own, per this body of work's stated preference for an existing
seam over a new one. One test per file checks the seam itself with nothing overridden,
reading whatever a `BarWriter` built on the same `tmp_path` buffered.
