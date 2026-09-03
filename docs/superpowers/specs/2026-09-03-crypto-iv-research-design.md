# Crypto option analytics: a build-and-measure study

**Two tracks, one project.** We build a working live-analytics stack — ingestion, fan-out,
storage, caching, computed Greeks on screen — and treat every design choice inside it as a
measured experiment rather than a decision taken on taste.

**Every ticket is written to be learned from.** Six sections each: the concept explained from
zero, why this way, what to read first, the task, how you'll know it worked, and what to notice.
A ticket that produces working code and teaches nothing has failed.

---

## Problem Statement

Delta publishes implied volatility and all five Greeks on REST and websocket. We cannot build on
them. **As an input they are circular** — checking our maths against a number we took as input
proves nothing. **And they do not exist where we need them most**: Delta's history carries mark
price and open interest but no IV and no Greeks, so a backtest needing a past vol surface cannot
buy one at any price. Computing IV ourselves is the only route to a historical surface.

Underneath sits the harder problem. **Implied volatility is not observable.** It is inverted out
of a price under assumptions — about the forward, the rate, the model, the solver. Change an
assumption, get a different number, and no experiment says which is right, because there is no
ground truth to compare against.

The same is true one level up. Nobody can say from first principles whether we need a message
bus, or whether caching earns its complexity at our volume. Both get the same treatment: **build
the alternatives, measure them, write down the answer.**

## Solution

Seven tickets. Each produces working code *and* a measured finding.

Where methods agree, the assumption they differ on does not matter and we take the cheapest.
Where they diverge, that names the assumption carrying the risk. Triangulation replaces the
ground truth we do not have.

- **Accuracy: `dIV ≤ 0.1 vol points` (0.001 decimal) between our own methods**, not against
  Delta. Delta's values are an unexplained reference column; a test asserts they are never
  consumed as input.
- **Latency: full-chain recompute < 1 s, incremental single-contract < 40 ms.**

---

## Measured Facts This Rests On

Measured 2026-09-02/03 against `https://api.india.delta.exchange`. Re-verify before trusting.

**Delta migrated its public channels.** The old names — `v2/ticker`, `l1_orderbook`,
`l2_orderbook` — moved to the public endpoint as `ticker` / `ob_l1` / `ob_l2`, with the legacy
names scheduled for removal **31 July 2026**. This is why `v2/ticker` is now rejected as an
invalid channel. Source: OpenAlgo's Delta adapter, confirmed by our own probe.

| Fact | Value |
|---|---|
| Public websocket | `wss://public-socket.india.delta.exchange` |
| `ticker` channel | **every 5 s** per contract (measured 5.4 s) — mark, OI, quotes, IV, Greeks |
| `ob_l2` channel | **every 504 ms** per contract — top-15 order book, both sides |
| Contracts on one connection, `ticker` | **967** — every live option, no cap reached |
| Contracts on one connection, `ob_l2` | **136 verified** (a full chain), all delivering |
| Throughput, `ticker`, all 967 | 187/s, **82 KB/s** |
| Throughput, `ob_l2`, one chain (136) | 270/s, **131.7 KB/s** |
| Trades on an ATM contract | ~1 per 75 s |
| `mark_price` channel | produced nothing in 75 s — treat as dead |
| Connection limit | 150 per IP per 5 min |

**The consequence that shapes the project: Delta publishes IV every 5 seconds, but the order book
moves every 500 ms. If we compute our own IV from the book, our numbers can be ten times fresher
than theirs.** That turns "replicate exactly as Delta" into something we can exceed rather than
merely match.

**A second consequence.** A quote update is not a trade — quotes move because spot moved, roughly
400× more often than anyone trades. And recomputing 136 contracts on every `ob_l2` update is
~270 chain-recomputes per second. **Incremental recompute, not full-chain.**

The `ticker` payload is abbreviated. Decoded field-by-field against REST, and independently
confirmed by OpenAlgo's adapter:

```
d[].g   = [delta, gamma, rho, theta, vega]      d[].m    = mark
d[].qiv = [ask_iv, bid_iv, mark_iv]             d[].ohlc = [o, h, l, c]
d[].q   = [best_ask, ask_size, best_bid, bid_size, impact_mid]
d[].oi  = [oi_contracts, oi_value_usd]          sp = spot   ts = microseconds

ob_l2:  a = [[price, size], ...]  asks, best first
        b = [[price, size], ...]  bids
```

**History is usable under one rule:** `end` must be the contract's `settlement_time`, never `now`.
With `now`, `/v2/history/candles` pads empty buckets by copying the last trade forward
indefinitely — `C-BTC-60000-270624` returns 801 daily bars of which 797 are fabricated. With
`settlement_time`, 4 bars, all real. Under that rule `MARK:` gives 64 hourly bars with 64 distinct
values across a contract that expired in June 2024. Detail in `docs/delta-api-scope.md`.

**Prior art.** `payoff-project/src/payoff/forward.py` implements parity OLS with a trust gate and
two fallbacks, discounting at `D = e^(-0.065·T)` — r = 6.5% already in place.
`OpenAlgo broker/deltaexchange/` is prior art for our exact venue. **Trust our measurements over
their constants**: their `MAX_SYMBOLS_PER_FRAME[ob_l2] = 1` is wrong today — we subscribed 136.

---

## The Seven Tickets

Ordered by dependency. Each is one PR. **T1 and T4 can start in parallel.**

### T1 · Foundations: inverse settlement, the parity forward, the timing harness

**The concept.** An option's price depends on where the market thinks the underlying will be *at
expiry* — the **forward** — not where it is now. On a stock index those differ by financing cost.
On crypto they differ by whatever the funding market says.

**Put-call parity** lets you recover the forward from prices alone. For European options on the
same strike and expiry:

```
C − P = D · (F − K)
```

`C` and `P` are the call and put prices, `K` the strike, `F` the forward, `D` the discount factor.
**This is an identity, not a model** — it holds by arbitrage. If it were violated you could buy
one side, sell the other, and take a riskless profit. No assumption about volatility appears
anywhere in it.

Now read that equation as a straight line: plot `C − P` on the y-axis against `K` on the x-axis
across all strikes, and you get a line with slope `−D` and a zero-crossing at `K = F`. So
**fitting a line to observed prices recovers both the forward and the discount rate**, with no
assumption about either. That fit is ordinary least squares — the `atm ± x` regression.

**Why this way.** The alternative assumes `F = S·e^(rT)`, which needs an `r`. On a crypto venue
nobody actually knows `r` — there is no risk-free rate for BTC. Recovering it from traded prices
is the honest route. T2 measures whether it was worth the trouble.

**Learn first.**
- `payoff-project/docs/calculations.md` §1 — the derivation *and* the gating rules. Our own prior
  art, and it records that the regression reproduced the source exactly on 316 of 376 minutes.
- Hull, *Options, Futures and Other Derivatives*, the put-call parity section — for why it is
  arbitrage rather than a model. This distinction is the whole reason we trust it.
- Any explanation of ordinary least squares that covers **what the residuals mean**. You need to
  know when a fit is untrustworthy, not just how to compute one.

**The task.** First, and before any code: establish **in writing what inverse settlement
changes**. Delta's options are quoted in USD but margined and settled in the coin itself. A NIFTY
option pays rupees; a Delta BTC option pays BTC. Work out what that does to the payoff and to the
Greeks, and write it down.

Then implement four forward estimators as pure functions over a chain snapshot:
`F1` parity OLS over ATM±x for **x ∈ {3, 5, 7, 9}** · `F2` single-strike parity at the ATM strike
· `F3` carry `F = S·e^(rT)` at r = 6.5% · `F4` spot used directly, no forward.

Then the **timing harness** — median and p95 over repeated runs — that every later ticket reports
through.

**A design constraint.** These functions take a *chain snapshot* and must not care where it came
from. Today REST provides one; after T4 the websocket will. Same signature either way.

**How you'll know it worked.** `F1` and `F2` agree within a few dollars on a captured chain. The
recovered discount implies a rate in a plausible range. The harness prints per-function timings.

**What to notice.** **The forward is robust and the discount is fragile.** They come from
different features of the same line — the forward is where it crosses zero, the discount is its
slope. A slope is far more sensitive to noise at the wings than a crossing point is. Watch how
much the recovered rate moves as you change x, and how little the forward does. That asymmetry is
the single most useful intuition in this ticket.

Also notice: **OLS returns a number whether the input deserves one or not.** That is why
`payoff-project` gates the regression instead of trusting it. Carry the gate over.

**Highest-risk item in the project:** inverse settlement. Get it wrong and every number
downstream looks plausible and is wrong — the worst failure mode available, because nothing
crashes.

---

### T2 · IV solvers, the model axis, and the agreement matrix

**The concept.** Black-Scholes takes volatility in and gives a price out. But we observe the
*price* and want the *volatility*. There is no formula that inverts it — so you **search**: guess
a vol, price it, compare to the observed price, adjust, repeat. The vol that reproduces the
observed price is the **implied volatility**.

The search method matters more than you would expect:
- **Newton-Raphson** uses the derivative (vega) to jump straight toward the answer. Fast — a few
  iterations — but it can shoot off to nonsense when vega is tiny, which happens far from the
  money and near expiry.
- **Brent** brackets the answer between two bounds and never leaves them. Slower, but it cannot
  diverge.
- **Jäckel's "Let's Be Rational"** transforms the problem so that a well-behaved starting guess
  is always available. Roughly two iterations to machine precision.

**Black-76 vs Black-Scholes** is the other axis. Black-76 prices off the **forward**;
Black-Scholes prices off **spot** plus a rate. They are the same model wearing different clothes —
which is exactly why comparing them tells you whether the forward handling matters.

**Why this way.** With no ground truth, **agreement between independent methods is the only
evidence available.** This is the heart of the study.

**Learn first.**
- Jäckel, *Let's Be Rational* (the paper) — read the opening on **why naive Newton is fragile**.
  You do not need to follow the whole construction; you need to understand the failure mode.
- `py_vollib` source — a reference implementation to compare shapes against, not to copy.
- Any clear derivation of **vega** — because vega is both the Newton step size and the reason the
  solve fails when it is small. Understanding that one fact explains most solver failures.

**The task.** Implement `M1` Black-76 on the forward and `M2` Black-Scholes on spot; solvers `S1`
Newton with analytic vega, `S2` Brent, `S3` Jäckel, `S4` NumPy-vectorised Newton. Cross them with
T1's four forwards. Produce the **pairwise agreement matrix**, sliced by moneyness and by time to
expiry. Record where each solver fails — deep ITM, near expiry, illiquid wings. Recover `r` from
the regression slope (`R2`) and compare against the assumed 6.5% (`R1`).

**How you'll know it worked.** **Round-trip holds** — price with the IV you solved for and the
input price comes back. That catches solver bugs with no reference implementation needed.
**Analytic limits hold** — deep-ITM call delta → 1, ATM delta ≈ 0.5, vega → 0 far from the money,
put-call parity holds on our own prices. Every cell of the agreement matrix has an explanation.

**What to notice.** **The `F3` vs `F4` comparison is the most valuable result in the study.** It
measures what the forward is actually *worth*. NIFTY needs it — `payoff-project` exists largely
to recover one. If crypto does not, a whole branch of T1 becomes unnecessary. **That would be a
finding, not a failure**, and it is worth running early precisely because it can delete work.

Also notice where the solvers disagree. It will not be uniform — expect the wings and the
front expiry. Those are exactly the regions where vega is small.

---

### T3 · Parallelisation and hitting the latency target

**The concept.** "Make it fast" means nothing until you know where the time goes. **Profile
first, optimise the top item, measure again.** Intuition about performance is famously unreliable —
the bottleneck is usually somewhere you did not suspect.

Three ways to go faster, in increasing order of complexity:
- **Vectorisation** — NumPy does arithmetic on whole arrays in compiled C, so one call handles 136
  contracts instead of 136 Python loop iterations. Python loop overhead per element is large; this
  usually wins first and costs least.
- **Multiprocessing** — real parallelism across CPU cores. Python's **GIL** (a lock that lets only
  one thread run Python bytecode at a time) means *threads* do not help CPU-bound work. Processes
  do, but each has startup cost and must serialise data in and out.
- **Doing less work** — incremental recompute instead of full-chain. Usually the biggest win and
  the one people reach for last.

**Why this way.** At 136 contracts, plain per-contract solving may already be fast enough — in
which case parallelism is complexity bought for nothing. We find out rather than assume.

**Learn first.**
- NumPy vectorisation basics — specifically *why* array-at-a-time beats a Python loop. The answer
  is interpreter overhead, not arithmetic.
- Python's GIL — enough to know why `multiprocessing` and not `threading` for CPU-bound work.
- **Amdahl's law** — the ceiling on speedup when only part of a program parallelises. It tells you
  when to stop optimising.

**The task.** Time every function individually. Compare scalar, NumPy-vectorised and multiprocess
across a full 136-contract chain. Compare **full-chain recompute against single-contract
incremental**. Hit **< 1 s full chain**, target **< 40 ms incremental**.

**How you'll know it worked.** A timing table with medians and p95s, and both targets met — or a
documented reason why not.

**What to notice.** Where the time actually goes. Candidates: solver iterations, the
string-to-float conversion at the API boundary, the forward refit. **The measurement usually
surprises.** Note the chain size at which parallelism starts winning — below it, the simpler code
is also the faster code, and that is worth knowing before you reach for a process pool.

---

### T4 · Ingestion and fan-out: one socket, many consumers

**The concept — websockets.** A REST call is a letter: you ask, they answer, the connection
closes. A **websocket** is a phone line left open — the server pushes data whenever it has some,
with no request. That is the only way to get 500 ms updates without hammering the API.

**The concept — a message bus.** Suppose the websocket handler needs to feed three things: the
storage writer, the IV engine, the UI. The naive version has the handler call all three directly.
Now the handler knows about all three, a slow one blocks the socket, and adding a fourth means
editing the handler.

A **message bus** puts a middleman in between. The handler **publishes** messages; interested
components **subscribe**. The publisher never knows who is listening. This is the **publish/
subscribe** pattern, and it buys three things: consumers become independent, a slow consumer
cannot stall ingestion, and you add a consumer without touching the producer.

OpenAlgo runs exactly this — a websocket proxy fanning out over **ZeroMQ** (a library that gives
you sockets speaking pub/sub without needing a broker server). NautilusTrader does the same with
its own bus. **Two mature systems reached this independently.**

**Why this way, and why not all the way.** We need **one** socket carrying every contract — three
consumers each opening their own connection would burn the 150-per-5-min budget and give three
inconsistent views of the market. So the socket-owner/consumer split is **mandatory**.

A separate broker *process* is not. OpenAlgo needs ZeroMQ because it fans across 36 brokers and
many users; we have one venue and one user at 82 KB/s. **So we build the seam exactly where
OpenAlgo puts the bus, using an in-process async fan-out.** Promoting to ZeroMQ later becomes a
deployment change rather than a rewrite. Knowing *why* we are not using one is the point of this
ticket — not cargo-culting it, and not cargo-culting its absence either.

**Learn first.**
- **`OpenAlgo broker/deltaexchange/streaming/delta_websocket.py` — read this first.** It is prior
  art for our exact venue: channel names, the migration, auth signing, heartbeats.
- [ZeroMQ Guide](https://zguide.zeromq.org/) chapter 1, **pub/sub only** — enough to understand
  what OpenAlgo is doing. Do not read the whole guide.
- **Backpressure** — what happens when a consumer is slower than the producer. This is the
  problem the queue-per-consumer design exists to solve.

**The task.** One connection to `wss://public-socket.india.delta.exchange`. Subscribe **both
channels**: `ticker` (5 s — mark, OI, IV, Greeks) and `ob_l2` (504 ms — top-15 book). Decode the
abbreviated payloads into one normalised record. Publish into an in-process fan-out where each
consumer holds its own `asyncio.Queue` — **a slow consumer must never stall the socket.** Handle
reconnection with full resubscribe, and send heartbeats every 30 s (OpenAlgo's interval; the
documented 60 s idle disconnect did not reproduce in our 75 s test, so treat it as unverified and
send them anyway).

**Write the note**: what we took from OpenAlgo, what we left, and why.

**How you'll know it worked.** All 967 contracts on `ticker` and a full chain on `ob_l2`, one
connection. Throughput matches the 187/s and 270/s baselines. Killing a consumer does not disturb
ingestion. Pulling the network cable reconnects and resubscribes without losing the subscription
set.

**What to notice.** **Delta publishes IV every 5 s; the book moves every 500 ms.** That gap is the
opportunity — our computed IV can be ten times fresher than theirs. Measure what the fan-out
indirection costs in latency and what it buys in independence; that measurement is the argument
for or against promoting to a real bus later.

And notice this: **OpenAlgo's `MAX_SYMBOLS_PER_FRAME[ob_l2] = 1` is wrong** — we subscribed 136
successfully. Reference implementations go stale. Trust your own measurements.

---

### T5 · Storage: hive-partitioned Parquet via Polars

**The concept — columnar storage.** A CSV stores row by row, so reading one column of a billion
rows means reading all of them. **Parquet** stores column by column: all the marks together, all
the strikes together. Reading one field touches only that field's bytes. It also compresses far
better, because a column holds similar values.

**The concept — hive partitioning.** Instead of one enormous file, split the data into
directories whose *names encode the filter*:

```
date=2026-09-03/underlying=BTC/part-0.parquet
date=2026-09-03/underlying=ETH/part-0.parquet
```

A query for BTC on 3 September skips every other directory **without opening a single file** —
the filter is answered by the path. This is called partition pruning.

**The concept — buffering.** Writing a file per message would produce 270 tiny files a second,
and Parquet is terrible at tiny files. So you accumulate records in memory and flush a batch on a
size or time threshold. The flush must run somewhere it cannot block the socket.

**Why this way.** OpenAlgo uses DuckDB for its history store. We do not have to choose: **write
hive Parquet and both DuckDB and Polars read it natively.** That keeps the query engine a later
decision instead of a commitment made now.

**Learn first.**
- [Polars hive guide](https://docs.pola.rs/user-guide/io/hive/) — the practical mechanics.
- Any explanation of **why columnar beats row storage for analytics** — the reason is the one
  above, and it is worth understanding rather than accepting.
- The **small-files problem** in data engineering — why thousands of tiny Parquet files perform
  worse than a few large ones. This is what the partitioning decision below turns on.

**The task.** Define the record: bid, ask, mark, LTP, OHLC, open interest, Delta's three IVs and
five Greeks **as reference columns**, our computed IV and Greeks, spot, and the exchange
timestamp. Partition on `date/underlying` **only** — **expiry stays a column**. Adding expiry as a
partition level explodes into thousands of tiny directories and makes Parquet slower than CSV.
Buffer in memory, flush on size or time, on a thread that cannot block the socket.

**Store events as they arrive. Never forward-fill.**

**How you'll know it worked.** A day's capture reads back in Polars with correct types and no gaps
beyond real ones. Measured bytes-per-row and a projected daily footprint against the ~16M
rows/day estimate.

**What to notice.** The compression ratio against raw JSON, and the read-time difference
partitioning actually buys — measure it both ways rather than trusting the theory.

And the discipline point: **forward-filling is exactly the defect we caught Delta committing.**
They pad empty buckets with the last trade and do not say so, and it makes their history
untrustworthy. We must not build the same thing into our own store.

---

### T6 · Caching and the live read path

**The concept.** A **cache** keeps an expensive answer nearby so you do not recompute it. The hard
part is never storing — it is knowing **when the stored answer went stale**. That question is
cache invalidation, and it is the whole problem.

The design choice here is **materialise or view**. You could store an assembled option chain and
update it — but at 270 updates a second you would invalidate it 270 times a second. Or you store
**the latest quote per contract** and assemble the chain only when somebody asks. One cheap write
per update; assembly on demand. **The chain is a view, not an object.**

`optionchainstream` does exactly this with two Redis structures — live ticks, plus an instrument
master — joined at read time. Our REST engine already does the same thing with a per-request
pivot, so this ticket extends a pattern we have rather than introducing one.

**OpenAlgo's invalidation trick, worth stealing.** Their cache-invalidation messages ride **the
same ZeroMQ bus as market data**, under a `CACHE_INVALIDATE_*` topic prefix, with the proxy's
subscriber as the sole binder and every publisher connecting to it. No second port, no bind race,
one bus. It is a genuinely elegant reuse of infrastructure already present.

**Learn first.**
- `optionchainstream`'s Redis layout — the two-structure join, our closest model.
- `OpenAlgo database/cache_invalidation.py` — the shared-bus pattern above.
- Cache invalidation fundamentals — TTL versus event-driven invalidation, and why "when does this
  go stale" is the question that decides everything else.

**The task.** An in-memory latest-tick cache keyed by symbol, fed from T4's fan-out. Serve
`/chain` from it rather than calling Delta per request. Wire **our** computed IV and Greeks into
the existing ladder alongside Delta's, visibly distinguished. Coalesce repaints to 4–10 Hz — 25 Hz
is faster than the eye needs and faster than the data is meaningful.

**How you'll know it worked.** `/chain` served from cache is measurably faster than the REST round
trip. The ladder shows both sets of numbers. The screen does not flicker.

**What to notice.** This is where **"replicate exactly as Delta" gets its most honest test — their
numbers and ours, side by side, on the same screen, updating live.** Any disagreement becomes
immediately visible instead of buried in a report.

And watch the freshness gap. Ours recompute from the 500 ms book; theirs arrive every 5 s. On a
fast move you should be able to *see* our column lead theirs.

---

### T7 · Findings, and the historical vol surface

**The concept.** The research deliverable, and the proof the premise works: if we can compute IV
correctly, we can compute it for history — where Delta never stored any.

The **volatility smile** is the shape you get plotting IV against strike for one expiry.
Black-Scholes assumes one volatility for all strikes, which would make it flat. It is not — it
curves, because the market prices tail risk more richly than a lognormal assumes. **The shape is
the model's error made visible**, which is why plotting it is a correctness check and not
decoration.

**Why this way.** A study whose results live only in code has produced nothing. And the historical
surface is the commercial point — it is the thing that cannot be bought, because Delta never
stored it.

**Learn first.**
- `docs/delta-api-scope.md` §3, on the `end = settlement_time` rule — without it the historical
  input is fabricated.
- The volatility smile and skew — what shape theory predicts, and what a *wrong* shape looks like.

**The task.** Write `docs/iv-method-comparison.md`: the agreement matrix, the timing table, the
smile plots, and a plain-language conclusion naming **which assumptions mattered**. Then
reconstruct IV for an expired contract from stored `MARK:` prices and plot its surface through
time.

**How you'll know it worked.** Someone who was not in the room can read the document and know
which method to use and why. The historical smile has a recognisable shape.

**What to notice.** Whether the historical surface looks like the live one. If it does not, the
`MARK:` series is telling us something about how Delta computes marks — and that is worth knowing
before anyone backtests on it.

---

## Out of Scope

**Execution, orders, anything needing an API key.** Nothing here authenticates.

**A ZeroMQ broker process.** T4 builds the seam; crossing it is a later decision made from T4's
measured latency, not from taste.

**Choosing one winning method.** The output is a measured comparison. Picking a production default
is a decision made *from* the findings.

**OpenAlgo's multi-broker adapter layer.** Deliberately not copied — we have one venue, and that
abstraction is most of their complexity.

---

## Further Notes

**Sequencing.** T1 → T2 → T3 is the maths spine and needs no infrastructure — it runs against
captured snapshots. T4 → T5 → T6 is the stack. **T1 and T4 can run in parallel**: different
people, no shared state. T7 closes both.

**Do T1's inverse-settlement note before anything else.** It is the one unknown that silently
corrupts everything downstream.

**Honesty rule, inherited from the API scope work.** Every number in the findings is tagged
*measured*, with the request or run that produced it, or *assumed*. This project has already
reversed one verdict because a conclusion outran its evidence, and twice corrected claims taken
from documentation rather than measurement. Tagging is what makes those corrections cheap.

**Reference implementations.** [OpenAlgo](https://github.com/marketcalls/openalgo) — architecture
and, crucially, `broker/deltaexchange/`, prior art for our exact venue.
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) — the mature version of the
bus idea. [optionchainstream](https://github.com/ranjanrak/optionchainstream) — chain-as-a-view.
Note that `openalgo-portfoliogreeks`' product documentation is about API-key management and
contains no Greeks maths; read source, not READMEs. That mistake cost us a round of this spec.
