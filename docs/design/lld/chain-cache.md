# The chain cache — low-level design

**One module, `engine/src/deltapayoff/stream.py`, and it computes nothing.** It holds the
newest of each market-data event per instrument and folds them into a ladder when someone
asks. Everything expensive — the implied volatility, the Greeks, the forward — is
`compute.enrich`, called from here and owned elsewhere.

Landed by #37, which moved it off the venue's frames and onto the canonical events. #44 adds
watched-pair reference counting and the minute-cadence pass; the rows below marked *#44* are
where that will attach.

---

## 1. What it holds

| State | Keyed by | Filled by |
|---|---|---|
| `_reference` | canonical instrument string | `md.option_reference`, with its `Instrument` beside it |
| `_quote` | canonical instrument string | `md.option_quote` |
| `_spot` | underlying | `md.index_quote` |
| `_computed` | `(underlying, expiry)` | the recompute pass |
| `dirty` | `(underlying, expiry)` | any arrival that names a contract |

**The key is the canonical instrument, not the venue's symbol.** Until #37 this cache was
keyed by `(channel, symbol)` and held whole Delta frames, so it knew the venue had two
channels and which of them carried spot. It now dispatches on the **event type** and keys on
`Instrument.canonical()`; the venue's own symbol survives only inside the instrument, where
`Leg.symbol` reads it because `docs/chain-contract.md` is the engine↔web authority for that
field.

**The instrument is stored beside its reference event** rather than re-derived. The ladder
needs a strike and a side and neither is on the payload — both are typed fields on the
envelope's `instrument` — so nothing here parses a symbol.

## 2. How a ladder is built

`raw_chain(underlying, expiry)` walks the instruments seen for that pair, folds each into a
`models.Leg` with `leg_from_events`, and hands the triples to `chain.chain_from_legs`, which
is the **same fold the REST path uses**. `chain()` then enriches and caches.

**A row needs its reference event.** `md.option_quote` carries a top of book and nothing
else — no mark, no open interest, no venue Greeks — so a ladder built from quotes alone
would render as mostly empty lines. Quotes are layered over the references that exist,
which is the precedence the two channels had before the events replaced them.

**The book wins wholesale.** Where a quote event exists and quotes either side, both its
prices replace the reference event's pair; neither side is taken from the other.
`measured` by `tools/measure_feed.py` on a live 136-symbol chain: the book republishes every
508 ms against the reference's 5,001 ms and both carry the same top of book, so the book's
copy is **9.8x fresher** — and one side from each would be a spread nobody quoted.

**Spot is per underlying and global to the cache.** Before #37 each ladder took spot from
the first ticker frame in its own expiry's set; it now comes from `md.index_quote`, which
carries no instrument at all. That is a real change and it is an improvement: two expiries
can no longer disagree about what BTC was worth at one instant.

## 3. Invalidation

**Arrival is the only thing that schedules work.** Every quote and reference event marks its
`(underlying, expiry)` dirty. At `measured` ~1,323 messages a second a timer that recomputed
regardless would burn a core reproducing unchanged numbers.

`recompute_forever` drains the dirty set every `RECOMPUTE_INTERVAL_SECONDS` — `assumed`
0.1 s, chosen against Delta's own 5,001 ms republish, so every number on screen is at most a
tenth of a second old while the ceiling is roughly 10% of one core.

`chain()` **recomputes a dirty expiry synchronously**, so correctness never depends on the
loop having run: an event that arrived a millisecond ago is in the very next call. The loop
is what makes it a cache hit almost always, and what bounds staleness for a screen nobody is
looking at. It is an optimisation, not the mechanism.

**A key that fails goes back on the dirty set** and the pass continues. Clearing the set up
front and letting an exception escape would drop every remaining expiry silently: they would
leave `dirty` while `_computed` still held their old ladders, so `chain()` would serve that
stale cache indefinitely on any expiry receiving no further events. A screen showing last
minute's volatility with nothing to say so is the plausible-and-wrong failure this project
keeps refusing. `recompute_errors` counts them.

## 4. What the bar writer takes from here

`computed_chains()` hands back the ladders the loop has **already** computed — a list, not
the live dictionary, because the writer walks it while the loop may be replacing entries.
Deliberately not `chain()`: that recomputes a dirty expiry synchronously, which would move a
chain build onto the writer's drain pass and duplicate work the loop is already doing.

Each ladder carries `fetched_at`, which is how `bars.ComputedAggregator` recognises a chain
the loop has stopped refreshing as **stale** rather than storing it again. A dead feed
leaves this cache holding its last ladder forever; without that check the store would fill
with identical fabricated rows.

## 5. Failure modes

| What goes wrong | What happens |
|---|---|
| An event type this cache does not read | `skipped` grows; nothing is stored |
| A quote or reference event with no `instrument` | `skipped` grows; never keyed under nothing |
| `md.index_quote` with a null spot | Ignored; an absent spot is not an observation of absence |
| An expiry asked for before anything arrived | `None`, and the socket sends `waiting` — **not** an empty ladder, which would read as "the venue lists nothing" |
| A recompute raises | Re-queued and counted in `recompute_errors`; the pass continues |
| The subscription falls behind | Drop-oldest, counted by the bus. Harmless here: the cache only ever holds the newest event per contract anyway |

## 6. The seam the tests drive

`apply(event)` and `chain(underlying, expiry)`, with the events produced by the **real**
adapter from captured frames — `tests/fakes/decoder.py` is that helper, and it exists so no
consumer test hand-builds an event and quietly stops agreeing with the producer.
`tests/test_stream.py` drives the cache; `tests/test_ws_endpoint.py` drives it through the
websocket; `tests/test_composition.py` drives a scripted socket all the way to a ladder.

## 7. Numbers

| Number | Tag | Run |
|---|---|---|
| Recompute interval 0.1 s | `assumed` | chosen against Delta's `measured` 5,001 ms republish |
| Book 508 ms against reference 5,001 ms per contract; 9.8x | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| ~1,323 messages/s, 636.5 KB/s, BTC chain | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| 136 captured frames, one distinct spot across all of them | `measured` | `tools/capture_ws.py`, 2026-09-03 |
| A 69-row ladder rebuilt from the 136-symbol capture | `measured` | `tests/test_stream.py`, this session |

## 8. What is deliberately not here

**Watched-pair reference counting and the grace window** (#44). Today every dirty expiry is
recomputed whether or not a browser is looking at it; the point of #44 is that the hot loop's
cost should scale with viewers rather than with the venue's listing.

**Publishing `computed.chain`.** The catalogue has the event and the bar writer samples this
cache directly instead. Wiring the recompute pass to publish it is #44's, alongside the
minute-cadence pass that will produce it on a schedule.
