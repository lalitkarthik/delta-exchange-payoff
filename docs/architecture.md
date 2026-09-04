# The architecture, end to end

**What the platform is: one connection to Delta Exchange India, fanned out to three
consumers that never block each other — a screen, a pricing pass, and a disk.** A quote
arrives on a websocket, is decoded into a shape the REST path already understood, is
published to an in-process bus, and from there becomes three different things: the latest
state of a ladder, our own implied volatility and Greeks solved from the order book, and a
sealed one-minute bar in a Parquet file. Two processes serve it — a Python engine and a
Next.js page — and the interface between them is one document, `chain-contract.md`.

This is a **map**, not a record. Every number quoted here was measured elsewhere and the
findings documents are where the evidence lives; §13 says which one holds what. Where this
file and a findings document disagree, the findings document wins, and where an issue and
any file disagree, the issue wins.

## How to read this

§1 and §2 are the whole system at a glance — the parts, and the path one quote takes
through all of them. §3 to §11 take each part in turn. §12 is the seams: what is
deliberately absent, and where the next thing plugs in.

Modules are named as `engine/src/deltapayoff/<module>.py` throughout, shortened to
`<module>.py` after first mention. **Pure** means what it means in this codebase: no
socket, no clock, no filesystem, no state — data in, data out, and therefore testable
without a fake anything.

---

## 1. The shape of the system

```
                    Delta Exchange India (public market data, no API key)
                     │                                   │
        wss://public-socket…                    https://api.india.delta.exchange
                     │                                   │
              ┌──────┴───────┐                    ┌──────┴───────┐
              │   feed.py    │                    │delta_client.py│
              │ socket owner │                    │  GET /v2/tickers
              └──────┬───────┘                    └──────┬───────┘
                     │ Quote                             │ raw tickers
              ┌──────┴───────┐                           │
              │  fanout.py   │  in-process bus           │
              └──┬────────┬──┘                           │
       drop-oldest│        │lossless                     │
        ┌─────────┴──┐  ┌──┴────────────┐                │
        │ stream.py  │  │  store.py     │                │
        │ChainStream │  │  BarWriter    │                │
        │ latest     │  │  every state  │                │
        │ state/leg  │  └──┬────────────┘                │
        └─────┬──────┘     │ ticks                       │
              │            │                             │
              │      ┌─────┴──────┐                       │
              │      │  bars.py   │ pure: ticks → sealed  │
              │      │aggregators │ one-minute bars       │
              │      └─────┬──────┘                       │
              │            │                             │
              │      ┌─────┴──────┐                       │
              │      │ Parquet    │  <repo>/data/         │
              │      │ 4 datasets │  hive date=/underlying=
              │      └────────────┘                       │
              │                                           │
        ┌─────┴──────────────────────────────┐            │
        │        compute.enrich()            │◄───────────┘
        │  forward.py → solvers.py → greeks.py│   (REST path enriches too)
        │            PURE                    │
        └─────┬──────────────────────────────┘
              │ ChainResponse
        ┌─────┴──────┐
        │  main.py   │  FastAPI :8000
        │ /expiries  │  /chain  │  /ws/chain  │  /health
        └─────┬──────┘
              │  the identical ChainResponse over either transport
        ┌─────┴──────┐
        │  web/      │  Next.js :3000 — renders, does no arithmetic
        └────────────┘
```

Nine parts, and the table is the whole inventory:

| Part | Files | What it is |
|---|---|---|
| **Venue boundary** | `delta_client.py`, `feed.py`, `wire.py` | The only code that talks to Delta, over REST and over the socket, and the decoder that makes the two look alike |
| **Bus** | `fanout.py` | One producer, many consumers, per-subscription overflow policy |
| **Live chain cache** | `stream.py` | Newest frame per contract, rebuilt into a ladder on demand |
| **Pricing core** | `forward.py`, `solvers.py`, `black76.py`, `black_scholes.py`, `greeks.py`, `compute.py` | The forward, the volatility, the Greeks. Entirely pure |
| **Store** | `bars.py`, `store.py` | Ticks folded into sealed one-minute bars, written as hive-partitioned Parquet |
| **Public surface** | `main.py`, `models.py`, `chain.py`, `convert.py` | Two REST routes, one websocket, and the pivot and type conversion behind them |
| **Comparison harness** | `agreement.py`, `timing.py` | How far apart two methods land, and how long each took |
| **Web app** | `web/` | One page, one ladder, streaming |
| **Probes** | `tools/` | Measurement scripts. Not engine code |

**Two processes, and they are not symmetric.** The engine is stateful and long-lived: it
owns the venue connection, the cache and the disk. The web app is stateless and
disposable — it renders what it is handed and computes nothing. Engine on **8000**, web on
**3000**, and CORS admits **only** port 3000 (`localhost` and `127.0.0.1`), so serving the
page from any other port fails in a way that looks exactly like the engine being down.
`/ws/chain` is not covered by CORS at all — a websocket handshake is not subject to it —
which is acceptable only while the server binds to loopback and carries public data, and
stops being acceptable the moment either changes.

---

## 2. One quote's journey

Worth reading once end to end; every section after this is a detail of one hop.

1. **Delta publishes an `ob_l2` frame** for `C-BTC-81000-040926`. It carries a best bid
   and a best ask and nothing else — no spot, no open interest, no Greeks.
2. **`feed.py` reads it off the socket and publishes.** It decodes the frame into a
   `Quote` and hands it to the bus. It does not compute, does not store, and does not
   await anything a consumer controls. That restraint is the design: if the reader ever
   blocks, the OS receive buffer fills and **Delta closes the connection** — nobody
   enforces a limit, we simply fail to keep up.
3. **`fanout.py` copies it into two queues.** `ChainStream`'s, which is bounded and drops
   the oldest on overflow, and `BarWriter`'s, which is unbounded and drops nothing.
   `publish` is synchronous and never blocks.
4. **`stream.py` records it as the newest frame for that contract** and marks
   `("BTC", "04-09-2026")` dirty. One frame per `(channel, symbol)` is kept; the previous
   one is discarded, which is why falling behind on this queue costs nothing.
5. **The recompute loop wakes** every 100 ms, sees the dirty key, rebuilds that expiry's
   raw ladder from the frames it holds, and runs `compute.enrich` over it — fitting a
   forward, inverting a midpoint per strike, and reporting five Greeks. The result
   replaces the cached `ChainResponse` for that expiry.
6. **`store.py`'s writer folds the same frame into an open one-minute bar** — a different
   consumer, from a different queue, with a different policy. Nothing is dropped, and the
   arithmetic is pure and synchronous.
7. **A browser's websocket wakes** on its own once-a-second tick, reads the cached chain,
   and sends it as `{"type": "chain", "data": {...}}` — the identical object `GET /chain`
   returns.
8. **The page renders it.** It multiplies IV by 100 to show a percentage, compares this
   push against the last to draw a direction arrow, and prints everything else exactly as
   it arrived.
9. **Eight seconds after the minute closes**, the bar for that minute is sealed and
   queued; an hour later it is written to
   `data/quote-bars/date=2026-09-04/underlying=BTC/<name>.parquet`. A minute with no
   arrivals produces **no row at all**.

---

## 3. The engine process, and what starts in it

`main.py`'s `lifespan` is the composition root, and it is the only place that decides what
exists. On start-up it creates, in order: one `DeltaClient`, one `FanOut`, one
`ChainStream` attached to it, one `BarWriter` attached to it, and one `DeltaFeed`. Then it
fetches the full symbol list over REST, subscribes every symbol on **both** channels, and
launches four tasks — `delta-feed`, `chain-stream`, `chain-recompute`, `bar-writer`.

**One connection for the whole process, not one per consumer.** Three consumers each
opening their own would burn Delta's 150-connections-per-5-minutes budget and — worse —
give three inconsistent views of one market. A second browser tab costs a queue, not a
connection.

**Start-up degrades rather than dies.** If Delta is unreachable at boot, `DeltaUnavailable`
is caught and the process comes up anyway: the REST routes still answer, and `/ws/chain`
reports `waiting`. A start-up that dies because the venue blinked is worse than one that
comes up degraded and says so.

**A background task that ends is an error.** `_report_finished_task` logs any task that
finishes without being cancelled, because `DeltaFeed.run` returns *normally* once its retry
budget is exhausted — and a task that simply returns raises nothing. Without that callback
the feed can give up permanently while `/health` still says ok and the only symptom is
`waiting` forever.

**Shutdown flushes the open minute.** Tasks are cancelled and gathered first, then
`writer.aclose()` runs — after the cancellations, not inside them, so the flush is not
itself racing one. The partial minute is a real observation and is written with its true
tick counts rather than discarded for tidiness. A failed final flush costs that minute and
is logged, but must not take the shutdown with it and leave the HTTP client unclosed.

Two switches matter:

| | |
|---|---|
| `DELTA_LIVE_FEED=0` | Serve REST and the websocket with no socket to Delta. The test suite sets it |
| `LIVE_UNDERLYINGS` | `("BTC",)`. **ETH is served over REST but is not on the live feed and is not stored** |

---

## 4. The venue boundary

Three modules, and everything else in the engine is downstream of them.

**`delta_client.py` — the REST side.** A thin wrapper over `GET /v2/tickers` at
`https://api.india.delta.exchange`, with a 10-second timeout. There is no option-chain
endpoint at this venue: a chain *is* `/v2/tickers` filtered by `contract_types`,
`underlying_asset_symbols` and `expiry_date`, pivoted on our side. Every request carries a
`User-Agent` because without one Delta's edge answers **403 with an HTML body**, not JSON.
`parse_envelope` unwraps `{"success": true, "result": [...]}` and raises `DeltaUnavailable`
otherwise, which `main.py` maps to a 502.

**`feed.py` — the socket owner.** It has four jobs and the failures live in the last two.

*Subscribe both channels.* They are not interchangeable and the asymmetry is the project's
whole thesis:

| Channel | Refresh | Carries |
|---|---|---|
| `ob_l2` | **508 ms** (measured) | best bid, best ask |
| `ticker` | **5001 ms** (measured) | spot, open interest, mark, Delta's own Greeks and implied vols |

Everything the pricing needs is on the fast channel. Delta computes an implied volatility
from those prices and republishes it **9.8x more slowly than the prices underneath it
move**. Delta's own IV and Greeks travel as **reference columns only** and are never
consumed as inputs — `tests/test_no_delta_inputs.py` pins that.

*Heartbeat.* A quiet connection and a dead one are indistinguishable over TCP. Delta's
documented 60 s idle disconnect did not reproduce in a 75 s test here, so it is treated as
unverified and pings go out anyway every 30 s.

*Reconnect on a budget that resets on **data**, not on connecting.* Ten retries, backing
off 1 s to 60 s. A cumulative counter looks right and dies after a month — a feed
reconnecting once a day silently exhausts a lifetime budget and never returns. Resetting
when the socket merely *opens* is the same bug inverted and worse, because Delta can accept
a handshake and close immediately, so a budget that resets every pass never exhausts at
all. A connection that **delivered a message** restores the counter.

*Publish and return.* `_pump` reads until the connection ends. It never computes.

**`wire.py` — the decoder.** It turns websocket frames into the shapes the REST path
already uses: `decode_ticker` yields a `Leg`, `decode_ob_l2` yields best bid and ask, and
`chain_from_frames` assembles a full `ChainResponse` — the same type, so the REST path's
tests cover the websocket path's decoding too. `_as_rest_ticker` is the hinge: a decoded
leg reshaped into the ticker dict `build_chain` already understands.

---

## 5. The bus

`fanout.py` is one class and a queue per consumer, and it exists for one reason: **the
socket handler must never run inside a consumer.** If a slow disk flush or a raised
exception stops the reader, the receive buffer fills and the connection is closed on us.
So the handler publishes and returns; each consumer drains its own queue in its own task.

**The overflow policy is per subscription, and the two consumers want opposite things.**

| Consumer | Policy | Why |
|---|---|---|
| `ChainStream` | drop-oldest, bounded at 10,000 | A quote from four seconds ago is not slightly worse than the current one, it is **worthless**. A consumer that falls behind should skip to now |
| `BarWriter` | lossless, unbounded, watermark 100,000 | A dropped message here is a **permanent hole in the historical record**. `maxsize` stops being a ceiling and becomes the depth past which the backlog is counted |

Lossless is deliberately not the default. And **every drop is counted** — a silent drop is
a lie, and this project has twice been damaged by numbers that looked plausible and were
not.

**Why this and not ZeroMQ.** OpenAlgo fans out over ZeroMQ because it serves 36 brokers
across separate processes. This is one venue, one user, 82 KB/s per consumer. A broker
process would cost a deployment step, a failure mode and a serialise/deserialise round trip
per message, and buy nothing. The **seam** sits exactly where OpenAlgo puts its bus, so
promoting to ZeroMQ later replaces what is behind the seam rather than rewriting the
producer. Measured: the in-process fan-out costs **2.5 microseconds** per record across
three consumers, against a 508 ms publish interval.

---

## 6. The live chain cache, and the recompute loop

`stream.py`'s `ChainStream` is **a cache with a filter, and it computes nothing itself.**
It keeps one frame per `(channel, symbol)` in two dictionaries — one for books, one for
tickers — and adds two things the bus cannot.

**Which frames belong to the chain a browser asked for.** One connection carries every
listed expiry and every underlying; a chain screen shows one of each. A frame carries
neither underlying nor expiry as a field — both live only in the symbol's `DDMMYY` suffix,
so `_key_for` parses them back out, and an unparseable symbol marks nothing dirty rather
than raising.

**The answer that there is no chain yet**, which is not the same as an empty one. A
`ChainResponse` with no rows renders as a blank ladder and reads as "Delta lists nothing",
when the truth is that the socket has not spoken. `raw_chain` returns `None`, and the
websocket says `waiting` rather than sending an empty ladder.

**A row needs its `ticker` frame.** `ob_l2` carries no spot, no Greeks and no open
interest, so a ladder built from books alone would render as mostly empty lines. Books are
layered onto the tickers that exist, not the other way round.

**The recompute loop is an optimisation, not the mechanism.** `recompute_forever` wakes
every **100 ms** and enriches every expiry marked dirty since its last pass. But `chain()`
recomputes synchronously if its key is dirty, so **correctness never depends on the loop
having run** — a frame that arrived a millisecond ago is reflected in the very next call.
The loop's job is to compute once for every reader rather than once per reader, and to
bound staleness for an expiry nobody is currently watching.

Three failure decisions worth knowing:

- The dirty set is taken as a **snapshot** and replaced, so frames arriving mid-pass belong
  to the next pass rather than being silently cleared by this one.
- A key whose enrichment raises **goes back on the dirty set** and is counted; one failure
  does not abandon the rest of the pass. Clearing the set up front and letting the
  exception escape would leave those expiries holding *old* chains that `chain()` would
  then serve indefinitely — a screen showing last minute's volatility with nothing to say
  so, which is exactly the plausible-and-wrong failure this project keeps refusing.
- `computed_chains()` hands back a **list** of what has already been computed — not
  `chain()`, which would move a chain build onto the writer's drain loop, and not the live
  dictionary, which the loop may be replacing entries in while the writer walks it.

---

## 7. The pricing core

Entirely pure, entirely testable without a network, and the only part of the system that
does mathematics. `compute.enrich` is its single entry point: **a chain in, the same chain
out with `computed` populated**, a new object rather than a mutation.

**The forward, first.** `forward.py` holds four independent answers to "what is the forward
for this expiry?" — F1 an OLS fit of `C - P` against `K` across every paired strike, F2
parity inverted at a single strike, F3 spot carried at an assumed rate, F4 spot itself.
The live path uses **F1**, with `MIN_PAIRS = 5` and a plausibility gate at
`MAX_PLAUSIBLE_RATE = 0.30`. Time is ACT/365 to a 12:00 UTC settlement.

**A failed gate discredits the discount, not the forward** — this is the subtlest decision
in the module. F1 recovers both from one line: the forward is where `C - P` crosses zero,
the discount is the line's slope. A crossing is an interpolation inside the strike range
and noise barely moves it; a slope is a tilt measured across that range, where a small
error is a large one in `D`. Measured: across window choices the forward spans **$1.23 on
a $77,590 number** while the implied rate runs **-17.1% to +9.4%**. So when the gate fails
the forward stands and only the discount is replaced, with an assumed **6.5%** — a borrowed
constant, and `forward_method` reports `F1+assumed-rate` rather than passing it off as a
fit. If there are too few pairs even for that, F2 prices from a single strike.

**One volatility per strike, from the out-of-the-money leg.** Put-call parity says either
side implies the same number. In practice the out-of-the-money option holds no intrinsic
value, so its whole price is time value and its vega is at its largest, while the
in-the-money option prices the same volatility with most of its value insensitive to it —
same answer, far better conditioned. Calls above the forward, puts below. That single
number is written to **both** legs, with `iv_leg` naming the side it came from so the
repetition cannot be misread as two independent solves.

**The solver declines rather than guessing.** `solvers.py` holds four — S1 Newton with
analytic vega (the live path), S2 Brent, S3 a Jaeckel-shaped Householder iteration, S4 S1
vectorised over NumPy. They exist to be compared, not because four were needed. Newton's
division by vega is both why it is fast and how it fails: **where vega collapses, the step
explodes**, and vega collapses exactly where the price stops carrying information about
volatility — deep in the money, far out of it, close to expiry. In those regions there is
no answer to find and the correct behaviour is to refuse. A refusal is recoverable; a
plausible wrong number is not.

**No volatility means no Greeks.** `iv` is `null` and never `0`, `iv_reason` says why, and
the leg carries no Greeks at all. Reporting five Greeks at some default sigma would put
five plausible numbers on screen that describe nothing.

**`greeks.py`'s conventions are the sibling project's, not the textbook's** — delta and
gamma undiscounted, vega and rho discounted and quoted per one percent, theta a
one-calendar-day repricing rather than the analytic derivative. Delta is with respect to
the **forward**; Delta-the-venue's own delta is with respect to **spot**, so the two are
recorded side by side and never graded against each other. Theta uses a **365-day** year,
not the sibling's 252 — crypto trades weekends and this venue lists weekend expiries;
measured, a 1/252 step overstates theta by **1.456x**.

**Every enriched chain is stamped.** `MODEL_VERSION` is
`"F1+assumed-6.5 / S1-newton / ACT365 / mid-OTM"`, hand-maintained and bumped when any of
those four decisions changes. A content hash was rejected — it changes when a docstring is
edited, producing forty versions that are all the same model. `forward_method` is stored
per row independently and pins the largest single source of variation whatever the string
says.

`black76.py` prices from the forward, `black_scholes.py` from spot and a rate; `agreement.py`
and `timing.py` are the harness that puts two methods side by side in vol points and
milliseconds. None of them are on the live path.

---

## 8. The store

Two modules: `bars.py` is pure aggregation, `store.py` is the only code in the engine that
touches a file.

**Why bars at all.** Measured on a live connection, both channels together carry
**1,322.9 msg/s at 636.5 KB/s** — roughly 114M rows and ~52 GB of raw JSON a day. Almost
all of it is repetition: the same contract's book republished 118 times a minute, most
ticks identical to the one before. A one-minute bar keeps the extremes and discards the
repetition at ~45x fewer rows. What it destroys is **path** — a bar cannot say whether the
high came before the low. That is an acceptable loss for research over days and an
unacceptable one for microstructure, and #5 chose the former deliberately.

**There is no single raw byte rate**, which is itself a finding. A later ten-minute run
measured 1,511.7 msg/s at 736.3 KB/s — 65.14 GB/day — because the rate depends on which
contracts are subscribed on which channel, and 636.5 KB/s sits inside the range rather
than naming it. Any compression ratio therefore has to name its denominator.

**Aggregation is not forward-filling, and that distinction is the whole discipline.** A bar
summarises events that happened; a forward-fill invents events that did not. A minute with
no arrivals produces **no row** — not a row of nulls, and never the previous close. This is
the same defect the project caught in Delta's own `/v2/history/candles`, where
`C-BTC-60000-270624` returns 801 daily bars of which **797 are fabricated**.

**Four tables, four dataset roots**, sharing the same partition keys so a reader joins them
with no translation:

| Table | Root | What it holds | Filled from |
|---|---|---|---|
| A | `quote-bars` | bid/ask/mid OHLC per contract per minute, with a `from_book` flag | `ob_l2`, falling back to `ticker` |
| B | `reference-bars` | mark and last-traded OHLC, open interest, turnover, Delta's Greeks and IVs | `ticker` |
| C | `computed-bars` | **ours** — IV, five Greeks, the forward, the discount, the year fraction, each row stamped with its model | **sampled** from the chain cache at bar close |
| D | `spot-bars` | one row per minute per **underlying**, never per contract | `ticker` |

**Table C is the odd one and for a good reason:** our numbers are not on the wire. They are
made by the recompute loop, so the writer reads `ChainStream.computed_chains()` once as
each minute closes rather than folding ticks. It is the one place in `store.py` that reads
something other than its own queue — and it is handed the stream's *reader*, not the
stream, so the store never learns a chain cache exists.

**Four roots and not one root with a `table` partition key**, because a shared root forces
every scan to carry a filter a directory should have answered, and puts four schemas in one
dataset for Parquet's metadata to reconcile on every read.

**The layout puts the filter in the directory name:**

```
data/quote-bars/date=2026-09-04/underlying=BTC/20260904T090000Z-000001.parquet
```

**Expiry, strike and option type are columns, not partition levels.** Expiry as a level
explodes into thousands of directories holding a handful of rows each, and Parquet performs
badly with many small files.

**Polars is not allowed to lay out the tree.** `write_parquet(partition_by=...)` names its
output `00000000.parquet` in every partition on every call, so the 10:00 flush would
silently overwrite the 09:00 one and the day would end holding only its last hour — a
perfectly valid file, simply short, which is the invisible kind of loss. Directories are
built by hand, each flush writes a uniquely named file, and a sabotage-verified test pins
it.

**The two channels do not share a watermark**, which is the finding this work turned on.
`ob_l2` seals at **2.0 s** after the minute closes; `ticker` seals at **8.0 s**, both
derived from measured arrival-lag distributions. Table A seals on the *ticker's* number
because the ticker is its fallback source. Table C's grace is **0.0** — it is sampled, not
awaited.

**Writes are buffered for an hour and always leave the event loop.** `FLUSH_SECONDS` is
3600; `_flush_all` runs on a worker thread, because a flush on this event loop would stop
the socket reader, fill the receive buffer and get us disconnected.

**Compaction folds a closed day's hourly fragments into one file per table per partition.**
It verifies the output by reading it back — row count and schema — before deleting
anything, writes its manifest to a temporary name and `os.replace`s it into place as the
commit point, and recovers idempotently from an interruption at any stage. It has **no
lock**: two compactors on one partition would race on a manifest name, which is stated
rather than defended against because the nightly job is one process.

Measured on the engine's own hourly flush: **2,792,972 rows/day and 143.34 MB/day**, a
**454x** reduction against the 65.14 GB/day denominator and 387x against 55.51 GB/day.
#5's own estimate of 50–100 MB/day and 500–1000x did not survive — though its conclusion
did, since a year at this rate is 52 GB against the 20–35 GB it expected.

---

## 9. The public surface

Four routes, and `docs/chain-contract.md` is the authority over three of them.

| | |
|---|---|
| `GET /health` | Liveness only. Says nothing about Delta |
| `GET /expiries?underlying=BTC` | Every listed expiry, ascending **by parsed date** — sorted as text, `30-10-2026` would land after `27-11-2026` |
| `GET /chain?underlying=BTC&expiry=04-09-2026` | The pivoted ladder, **enriched** |
| `WS /ws/chain?underlying=…&expiry=…` | The same object, pushed once a second |

**Both transports return the same populated shape.** The REST path enriches too — a reader
that got null Greeks over REST where the websocket sends real ones would be reading a
different contract.

**The socket's envelope has three cases** because they are genuinely different facts:
`{"type": "chain"}` is the ladder, `{"type": "waiting"}` means nothing has arrived for this
expiry yet, `{"type": "error"}` means the request can never succeed. A websocket cannot
return 400, so a bad parameter is reported as an `error` and the socket closed — closing
silently would leave the browser reconnecting forever against a request that cannot work.

**A browser's socket owns no subscription of its own.** It reads the shared cache on its own
tick, so a disconnect has nothing to clean up and a second tab costs a queue rather than a
connection.

Behind the routes: `chain.py` is the pivot (`build_chain`, `build_expiries`, `nearest_strike`,
and the symbol-suffix expiry parser, all pure and network-free); `convert.py` turns Delta's
decimal strings into JSON numbers **exactly once, at the boundary**; `models.py` is the
contract's shapes. Errors are FastAPI's `{"detail": "..."}` — 400 for a bad parameter, 404
when Delta lists nothing, 502 when Delta was unreachable or answered `success: false`.

Three conversion rules carry the most weight, and all three exist because a plausible wrong
number is worse than a gap:

- **`"0"` means different things in different places.** In a quote field it means nobody is
  quoting, so it becomes `null`. In `oi`, `oi_value_usd` or a greek it is a real zero and
  stays `0.0`. That split is `to_quote_number` against `to_number`.
- **Every decimal is a JSON number or `null`, never a string.**
- **`spot` is Delta's top-level `spot_price`, never `greeks.spot`.** The two disagree —
  78111.9 against 78112.5 on one measurement — and that is not rounding.

---

## 10. The web app

Next.js 16 App Router, one page, one ladder, built with **bun**. `web/lib/contract.ts`
mirrors `chain-contract.md` field for field.

**It does no arithmetic.** The only number it touches is IV, multiplied by 100 to display a
percentage, because the contract makes that the web app's job. Nothing calls `parseFloat`:
every decimal already arrives as a JSON number, and `lib/engine.ts` raises a
`ContractViolationError` naming the offending fields if one does not — the breach is
reported, not worked around.

| File | |
|---|---|
| `app/page.tsx` | The page: header figures, the two pickers, and the one live subscription |
| `lib/live.ts` | The websocket, with reconnection and backoff **as the browser's job** |
| `lib/engine.ts` | The only place that talks to the engine; also the fixture fallback |
| `lib/direction.ts` | Which way a price moved since the last push |
| `lib/format.ts` | The only place a number becomes text |
| `components/ChainLadder.tsx` | The table |

**One subscription, torn down and rebuilt when the series changes.** Without the cleanup,
the old socket keeps pushing the old expiry's chain and the two interleave on screen.

**Reconnection is the browser's job, not the engine's.** A dev server restart, a laptop
waking, a wifi blip — all close the socket with no error the page can act on, so a closed
socket is retried with backoff until it opens.

**The status chip distinguishes `waiting` from `live`** for the same reason the envelope
does: an empty ladder and a ladder that has not arrived look identical on screen and are
not the same thing.

**`lib/direction.ts` compares pushes, not ticks**, and says so in its own docstring. The
engine recomputes every 100 ms and pushes once a second, so the browser sees roughly every
tenth state. Measured: thirty seconds on the at-the-money call produced **40 distinct
quotes and the screen showed 30**. Fine for a screen, wrong for anything that counts —
which is why the store subscribes to the bus rather than to the pushed chain.

**Rendering rules that are contract, not taste.** `null` is an empty cell, never `0` and
never a dash — a null bid means nobody is bidding, and printing `0.00` would claim someone
bid zero. A zero is printed as a zero, because open interest of exactly `0` is routine
here. A strike whose call or put is not listed renders **hatched** — five cells of 45°
stripes with no text — because absence must look deliberate and must never look like a
price. Rows are never dropped.

Two known drift points: `web/README.md` still describes a Refresh button and "no
websocket", both true before the streaming rewrite; and `NEXT_PUBLIC_USE_FIXTURE=1` still
populates the expiry dropdown but **no longer produces a ladder**, because the ladder now
comes from `subscribeChain`, which has no fixture branch.

---

## 11. The probes

`tools/` is not engine code and is not imported by it. Every number in the findings
documents came from one of these, and the convention is to re-run them rather than trust a
quoted figure.

| Tool | |
|---|---|
| `probe_api.py`, `probe_ws.py` | What the public REST API and websocket actually give you |
| `measure_feed.py` | Message rate, byte rate, per-contract refresh interval, and what the fan-out costs |
| `measure_arrival_lag.py` | How late a tick is — the measurement the watermarks are read off |
| `measure_store.py` | Footprint and compression, because #5's estimates were arithmetic |
| `compact_store.py` | The nightly compaction job |
| `capture_ws.py` | Real frames plus the matching REST snapshot, as test fixtures |

---

## 12. The seams, and what is deliberately not here

**`FanOut` has two subscribers and was built for more.** It is a seam for the remaining
tickets to plug into, not a fan-out doing heavy work yet.

**Nothing is computed on Delta's numbers.** Their IV and Greeks are stored and displayed
beside ours and never read as inputs, which is what makes any agreement between the two
evidence rather than circularity.

**No smoothing, no surface, no payoff.** The platform prices a ladder; it does not fit a
curve through it or evaluate a position against it.

**ETH is half-connected.** REST serves it; the live feed and the store do not.

**Compaction has no lock, and `Leg.oi_value_usd` is mislabelled** on the websocket path —
it is fed from `oi_change_usd_6h`, so the live screen shows a six-hour change under a
USD-open-interest label. Left alone deliberately: renaming the field changes the chain
contract the web app reads, and it wants its own ticket.

**Three things nobody has measured**, carried forward as open questions: mid against mark
(every implied vol here inverts a midpoint, and nobody has measured how far Delta's own
mark-fitted surface sits from it), our Greeks against Delta's reference columns, and how
often a computed sample taken near a minute boundary lands on the wrong side of it.

---

## 13. Where the numbers came from

This document quotes; those documents measure.

| For | Read |
|---|---|
| What the public API gives you, and the padding trap | `delta-api-scope.md` |
| That Delta is vanilla and USD-settled, not inverse | `settlement.md` |
| Four forwards, and why the discount is the fragile half | `forward.md` |
| Two models, four solvers, and where they agree | `implied-vol.md` |
| What the forward choice costs each Greek, and what it saves | `greeks.md` |
| One socket, the fan-out, and what it costs | `ingestion.md` |
| Bars, watermarks, the four tables, compaction, footprint | `storage.md` |
| The engine ↔ web interface | `chain-contract.md` |
| What is decided, what is deliberately unbuilt, what the repo gets wrong | `handoff.md` |

Then read `git log`. The commit messages are written to explain decisions rather than to
label diffs, and they are worth more than a summary of them.
