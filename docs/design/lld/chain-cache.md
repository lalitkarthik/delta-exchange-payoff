# The chain cache — low-level design

**One module, `engine/src/deltapayoff/stream.py`, and it computes nothing.** It holds the
newest of each market-data event per instrument and folds them into a ladder when someone
asks. Everything expensive — the implied volatility, the Greeks, the forward — is
`compute.enrich`, called from here and owned elsewhere.

Landed by #37, which moved it off the venue's frames and onto the canonical events. #44
added watched-pair reference counting and the second, minute-cadence pass.

---

## 1. What it holds

| State | Keyed by | Filled by |
|---|---|---|
| `_reference` | canonical instrument string | `md.option_reference`, with its `Instrument` beside it |
| `_quote` | canonical instrument string | `md.option_quote` |
| `_spot` | underlying | `md.index_quote` |
| `_computed` | `(underlying, expiry)` | either recompute pass |
| `dirty` | `(underlying, expiry)` | any arrival that names a contract |
| `_arrived_at` | `(underlying, expiry)` | the wall clock of that arrival — #44 |
| `_watch` | `(underlying, expiry)` | `watch()` / `unwatch()`, a `Watch` per pair — #44 |

**The key is the canonical instrument, not the venue's symbol.** Until #37 this cache was
keyed by `(channel, symbol)` and held whole Delta frames, so it knew the venue had two
channels and which of them carried spot. It now dispatches on the **event type** and keys on
`Instrument.canonical()`; the venue's own symbol survives only inside the instrument, where
`Leg.symbol` reads it because `docs/chain-contract.md` is the engine↔web authority.

**The instrument is stored beside its reference event** rather than re-derived. The ladder
needs a strike and a side and neither is on the payload — both are typed fields on the
envelope's `instrument` — so nothing here parses a symbol.

## 2. How a ladder is built

`raw_chain(underlying, expiry)` walks the instruments seen for that pair, folds each into a
`models.Leg` with `leg_from_events`, and hands the triples to `chain.chain_from_legs`, which
is the **same fold the REST path uses**. `_compute()` then enriches.

**A row needs its reference event.** `md.option_quote` carries a top of book and nothing
else, so a ladder built from quotes alone would render as mostly empty lines. Quotes are
layered over the references that exist.

**The book wins wholesale.** Where a quote event exists and quotes either side, both its
prices replace the reference event's pair; neither side is taken from the other.
`measured` by `tools/measure_feed.py` on a live 136-symbol chain: the book republishes every
508 ms against the reference's 5,001 ms and both carry the same top of book, so the book's
copy is **9.8x fresher** — and one side from each would be a spread nobody quoted.

**Spot is per underlying and global to the cache**, from `md.index_quote`, which carries no
instrument at all. Two expiries can no longer disagree about what BTC was worth at one
instant.

## 3. Invalidation

**Arrival is the only thing that schedules work.** Every quote and reference event marks its
`(underlying, expiry)` dirty and stamps `_arrived_at`. At `measured` ~1,323 messages a
second a timer that recomputed regardless would burn a core reproducing unchanged numbers.

`chain()` **recomputes a dirty expiry synchronously**, so correctness never depends on any
loop having run: an event that arrived a millisecond ago is in the very next call. The loops
are what make it a cache hit almost always. They are an optimisation, not the mechanism.

**A key that fails goes back on the dirty set** and the pass continues — `_drain` is the one
place that happens, shared by both passes. Clearing the set up front and letting an exception
escape would drop every remaining expiry silently: they would leave `dirty` while `_computed`
still held their old ladders, so `chain()` would serve that stale cache indefinitely on any
expiry receiving no further events. `recompute_errors` counts them.

## 4. Two cadences — #44

| Pass | Interval | Covers | Drives |
|---|---|---|---|
| `recompute_watched` | `RECOMPUTE_INTERVAL_SECONDS`, 0.1 s | dirty **and** watched or in grace | the screen |
| `recompute_closing_minute` | `MINUTE_PASS_INTERVAL_SECONDS`, 60 s, run `MINUTE_PASS_LEAD_SECONDS` early | dirty **and** with an arrival inside the minute being closed | the store |

**Why two.** They were one loop because there was one consumer. The live pass exists so a
screen sees the book move; the minute pass exists so the record has our numbers for every
expiry — the volatility screen reads a day of stored implied volatility in `measured` 6.8 ms
where solving that day on demand takes seconds. Work now scales with viewers, not with what
the venue lists.

**An unwatched dirty pair stays dirty.** The live pass does not solve it and does not clear
it: the minute pass needs it marked, and so does `chain()`.

**The minute pass runs early and closes the minute it is inside.** A ladder is bucketed by
`bars.ComputedAggregator` on the instant it was computed, and that table's grace is zero — a
pass that woke *on* the boundary would land its rows in the minute just opening, and the
writer may already have sealed the one it meant to close.

**It skips a pair whose newest arrival is older than that minute**, leaves it dirty, and
counts it in `minute_pass_skipped`. Solving it regardless would write a computed bar into a
minute that had no frames — one manufactured row per expiry every time a feed goes quiet,
which is the forward-fill this project refuses. A skipped pair re-enters when a frame of its
own arrives; until then the store has nothing to say about it, which is the truth.

## 5. Interest, and the grace — #44

`watch(underlying, expiry)` on `/ws/chain`'s accept, `unwatch` in its `finally`. **A count,
not a flag**: two tabs on one expiry are two viewers and the first to close must not stop the
second's ladder. A release with no matching watch is ignored rather than driving the count
negative — the handler's `finally` runs on paths that never registered, and a negative count
would swallow the next real viewer and stop that expiry silently.

At zero the pair is not dropped. `released_at` is taken on the **monotonic** clock and the
pair keeps being solved for `GRACE_SECONDS` — `assumed` 30 s, so flipping between two
expiries never waits for a first solve. Returning inside the window clears the release rather
than stacking a second timer. `prune_watches` drops what has elapsed, and it is called by the
**live pass**, not by `/health`: a monitor must not be the thing driving the recompute set,
and one that stopped polling must not leave expired entries alive.

`watching()` is what `/health`'s `watched` list is built from — pair, viewers, and the grace
remaining when it is in grace. Read-only, and it drops nothing. Every change to that set is
one debug line, `compute.recompute_set`, once per connection per end, never per message.

## 6. What the bar writer takes from here

`live_computed_chains()` — the ladders for pairs the **live** pass is keeping fresh — is what
`BarWriter` samples six times a minute. `computed_chains()` beside it still hands back
everything.

**Why the writer takes the narrower list.** An unwatched expiry's ladder is refreshed once a
minute, so five of those six samples would meet the same one again, in a minute already
sealed, and each would be counted as `late` — thousands a minute, on a counter whose whole job
is to say how many real observations were lost. The unwatched board reaches the table through
`writer.sample_chains`, which `recompute_every_minute` calls directly with what it produced.
Handing it over rather than leaving it to be sampled is not decoration: the writer's own
sample runs on its drain loop, which may seal the closing minute before it next looks.

A ladder that stops being refreshed **while its pair is still watched** is still sampled and
still refused as late, which is the frozen-cache detection the store already had.

Both are lists, not the live dictionary: the writer walks one while a pass replaces entries.

## 7. Failure modes

| What goes wrong | What happens |
|---|---|
| An event type this cache does not read | `skipped` grows; nothing is stored |
| A quote or reference event with no `instrument` | `skipped` grows; never keyed under nothing |
| `md.index_quote` with a null spot | Ignored; an absent spot is not an observation of absence |
| An expiry asked for before anything arrived | `None`, and the socket sends `waiting` — **not** an empty ladder |
| A recompute raises | Re-queued and counted in `recompute_errors`; the pass continues |
| A viewer released twice | Floored at zero; the next viewer still starts the solve |
| A pair dirty from an earlier minute | Skipped by the minute pass, counted, left dirty |
| The subscription falls behind | Drop-oldest, counted by the bus. Harmless here: the cache only ever holds the newest event per contract |

## 8. The seam the tests drive

`apply(event)` and `chain(underlying, expiry)`, with the events produced by the **real**
adapter from captured frames — `tests/fakes/decoder.py`. `tests/test_stream.py` drives the
cache; `tests/test_watched_pairs.py` drives interest, the grace and both cadences, on an
injected clock and through real websocket clients under `TestClient`;
`tests/test_composition.py` drives a scripted socket all the way to a ladder.

## 9. Numbers

**Run `t44-solve`, `tools/measure_solve.py --fill 60 --repeats 25`, 2026-09-08, live
`api.india.delta.exchange`, BTC and ETH, 788 listed contracts, 16 `(underlying, expiry)`
pairs, 101,261 messages over a complete 70.0 s window, one connection, 25 repeats per
figure.** Taken on a machine concurrently running another engine process, so these are an
**upper bound** on a quiet machine rather than a floor.

| Number | Tag | Run |
|---|---|---|
| Per-expiry solve, median of the 16 medians | `measured` **9.790 ms** | `t44-solve` |
| Per-expiry solve, range of medians (ETH 18-09 26 rows → BTC 25-09 101 rows) | `measured` **8.040–14.368 ms** | `t44-solve` |
| Per-expiry solve, worst p95 | `measured` **19.441 ms** (ETH 25-09-2026) | `t44-solve` |
| A pass over **every** expiry — the pre-#44 live tick | `measured` **175.227 ms** median, 192.257 p95, 199.045 max | `t44-solve` |
| A pass with **one** browser open | `measured` **9.761 ms** median | `t44-solve` |
| A pass with **nothing** watched | `measured` **0.005 ms** median | `t44-solve` |
| ~~7.25 ms per expiry, 69-row fixture~~ | **superseded** | previous session, `tools/measure_bus.py` — a fixture figure carried as `assumed` for the live path, and wrong by 35% on one expiry and by 3x on the pass |
| ~~58 ms for a full eight-expiry tick~~ | **superseded** | `derived` from the above; the live board is 16 pairs and the pass is 175 ms |
| Recompute interval 0.1 s | `assumed` | chosen against Delta's `measured` 5,001 ms republish |
| Grace 30 s | `assumed` | judgement about how a trader flips between expiries; no measurement fixes it |
| Minute-pass lead 0.5 s | `assumed` | comfortably longer than a whole 175 ms pass, far short of a minute |
| Book 508 ms against reference 5,001 ms per contract; 9.8x | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| ~1,323 messages/s, 636.5 KB/s, BTC chain | `measured` | `tools/measure_feed.py`, 2026-09-03 |

**The one sentence the ticket asked for.** The live per-expiry solve is `measured` **9.790 ms**
against the fixture's `measured` **7.25 ms** — 35% *worse*, not better, and on a board of 16
pairs rather than eight. A whole pass is `measured` **175 ms**, so the pre-#44 loop could not
finish inside its own 100 ms tick: it slept 100 ms and then worked 175, running at roughly a
275 ms cadence and around 64% of one core, `derived`, with every screen a quarter of a second
stale rather than a tenth. The `derived` 58 ms this ticket was written against was three times
too kind.

## 10. What is deliberately still not here

**Publishing `computed.chain`.** The catalogue has the event; nothing publishes it, and this
ticket deliberately did not change that. The only consumer would be the bar writer, and it now
receives the minute pass's ladders **directly** through `sample_chains` — putting them on the
bus instead would add a second lossless subscription carrying `measured` ~1,323 market-data
messages a second to catch sixteen chain events a minute, which is the cost #40's
`FeedConnectionCache` was built to avoid and the same argument again. It becomes worth doing
the day a consumer exists outside this process; the seam is `recompute_every_minute`'s `sink`,
which is a callable precisely so it can become a publish.
