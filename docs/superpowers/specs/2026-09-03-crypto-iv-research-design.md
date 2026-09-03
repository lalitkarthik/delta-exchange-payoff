# Crypto option analytics: a build-and-measure study

**Two tracks, one project.** We build a working live-analytics stack — websocket ingestion,
fan-out, storage, caching, computed Greeks on screen — and we treat every design choice inside it
as a measured experiment rather than a decision taken on taste.

The architecture is borrowed deliberately from OpenAlgo and NautilusTrader. **Understanding those
designs is a deliverable, not background reading**: each architecture ticket begins by studying
how they solved it, and ends with our version plus a written note on what we took, what we left,
and why.

Every ticket carries: **the concept · why this way · learn first · the task · how you'll know ·
what to notice.**

---

## Problem Statement

We want to price and risk-manage Delta's crypto options the way `payoff-project` does for NIFTY,
and we want the live stack that feeds it.

Delta publishes implied volatility and all five Greeks on REST and websocket. We cannot build on
them. **As an input they are circular** — checking our maths against a number we took as input
proves nothing. **And they do not exist where we need them most**: Delta's history carries mark
price and open interest but no IV and no Greeks, so a backtest needing a past vol surface cannot
buy one at any price. Computing IV ourselves is the only route to a historical surface.

Underneath sits the harder problem. **Implied volatility is not observable.** It is inverted out
of a price under assumptions — about the forward, the rate, the model, the solver. Change an
assumption, get a different number, and no experiment says which is right, because there is no
ground truth.

The same is true one level up. Nobody can tell us from first principles whether we need a message
bus, whether Polars beats DuckDB, or whether caching earns its complexity at our volume. Both
questions get the same treatment: **build the alternatives, measure them, write down the answer.**

## Solution

A stack built in seven tickets, where each ticket produces working code *and* a measured finding.

For the maths: implement several independent methods, run them on the same chain, measure
**agreement between them** and **cost of each**. Where methods agree, the assumption they differ
on does not matter and we take the cheapest. Where they diverge, that names the assumption
carrying the risk. Triangulation replaces the ground truth we do not have.

**The accuracy criterion is `dIV ≤ 0.1 vol points` (0.001 decimal) between our own methods**, not
against Delta. Delta's numbers are recorded alongside as an unexplained reference: if our methods
cluster and Delta sits outside, that is a finding about their assumptions. A test asserts their
values are never consumed as input.

**The latency criterion is a full-chain recompute under 1 second**, and an incremental
single-contract recompute under 40 ms — one frame at the chain's real update rate.

---

## Measured Facts This Rests On

Measured 2026-09-02/03 against `https://api.india.delta.exchange`. Re-verify before trusting.

| Fact | Value |
|---|---|
| Public websocket | `wss://public-socket.india.delta.exchange` |
| Channel | **`ticker`** — `v2/ticker` is rejected as invalid |
| Contracts on one connection | **967** — every live option, no cap reached |
| Quote updates, one contract | **0.186/s** — one every 5.4 s |
| Quote updates, one expiry chain (136 contracts) | **25.3/s** |
| Quote updates, all 967 | 187/s, **82 KB/s** |
| Trades on an ATM contract | **~1 per 75 s** |
| `mark_price` channel | **produced nothing in 75 s — treat as dead** |
| Connection limit | 150 per IP per 5 min |
| Rate limit, REST | weight 3 per market-data call; budget against 10,000 per 5 min |

The websocket payload is abbreviated. Decoded field-by-field against REST:

```
d[].g   = [delta, gamma, rho, theta, vega]
d[].qiv = [ask_iv, bid_iv, mark_iv]
d[].q   = [best_ask, ask_size, best_bid, bid_size, impact_mid]
d[].m   = mark      d[].oi = [oi_contracts, oi_value_usd]
d[].ohlc = [o,h,l,c]   d[].pb = [band_lower, band_upper]
sp      = spot      ts     = microsecond timestamp
```

**Two consequences that shape everything.** A quote update is not a trade — quotes move because
spot moved, roughly 400× more often than anyone trades. And recomputing 136 contracts on every
update is ~3,400 IV solves per second to refresh data that individually changes every 5.4 s.
**Incremental recompute, not full-chain recompute.**

**History is usable under one rule:** `end` must be the contract's `settlement_time`, never `now`.
With `end = now`, `/v2/history/candles` pads empty buckets by copying the last trade forward
indefinitely — `C-BTC-60000-270624` returns 801 daily bars of which 797 are fabricated. With
`end = settlement_time`, 4 bars, all real. Under that rule `MARK:` gives 64 hourly bars with 64
distinct values across a contract that expired in June 2024. Detail in `docs/delta-api-scope.md`.

**Prior art.** `payoff-project/src/payoff/forward.py` implements parity OLS with a trust gate and
two fallbacks, discounting at `D = e^(-0.065·T)` — r = 6.5% already in place. `docs/calculations.md`
documents it and records the regression reproducing the source exactly on 316 of 376 minutes.
The method transfers; the settlement convention does not.

---

## The Seven Tickets

Ordered by dependency. Each is one PR.

### T1 · Foundations: inverse settlement, the parity forward, and the timing harness

**Concept.** Put-call parity is an identity, not a model: `C − P = D·(F − K)` holds by arbitrage,
with no assumption about volatility. Plot `C − P` against `K` and it is a straight line — its
zero-crossing is the forward, its slope is the discount factor. So a regression recovers both
from traded prices without assuming a rate.

**Why this way.** The alternative is assuming `F = S·e^(rT)`, which needs an `r` that nobody
actually knows on a crypto venue. Recovering it from the market is the honest route — and T2
measures whether it was worth the trouble.

**Learn first.** `payoff-project/docs/calculations.md` §1, for the derivation and the gating
rules — our own prior art. Hull's parity section, for why it is arbitrage rather than a model.

**Task.** Establish in writing what **inverse settlement** changes — Delta's options are quoted
in USD but margined and settled in the coin — before porting any pricing code. Then implement
`F1` parity OLS over ATM±x for **x ∈ {3,5,7,9}**, `F2` single-strike parity, `F3` carry at
r = 6.5%, `F4` spot with no forward. Build the timing harness (median and p95 over repeated runs)
that every later ticket reports through.

**How you'll know.** `F1` and `F2` agree within a few dollars on a captured chain; the recovered
discount implies a plausible rate; the harness reports per-function timings.

**What to notice.** The forward is robust and the discount is fragile — they come from different
features of the same line. Watch how much the recovered rate moves as x changes, and how little
the forward does. **Inverse settlement is the highest-risk unknown in the project**: get it wrong
and every number after looks plausible and is wrong.

---

### T2 · IV solvers, the model axis, and the agreement matrix

**Concept.** Implied volatility is the number you must put into a pricing model to make it return
the price you observe. There is no formula for it — you search. Different searches and different
models give different answers, and the spread between them is the only error measure available.

**Why this way.** With no ground truth, agreement between independent methods is the evidence.
This is the heart of the study.

**Learn first.** Jäckel, *Let's Be Rational* — why the naive Newton solve is fragile and what
fixes it. `py_vollib` source, for a reference implementation to compare shapes against.

**Task.** Implement `M1` Black-76 on the forward and `M2` Black-Scholes on spot; solvers `S1`
Newton-Raphson with analytic vega, `S2` Brent, `S3` Jäckel, `S4` NumPy-vectorised. Cross with T1's
forwards. Produce the **pairwise agreement matrix** by moneyness and time-to-expiry, and record
where each solver fails — deep ITM, near expiry, illiquid wings. Also recover `r` from the
regression slope (`R2`) and compare against the assumed 6.5% (`R1`).

**How you'll know.** Round-trip holds: pricing with the IV we solved returns the input price.
Analytic limits hold: deep-ITM call delta → 1, ATM ≈ 0.5, vega → 0 far from the money. The
agreement matrix is populated and every cell is explained.

**What to notice.** **The `F3` vs `F4` comparison is the most valuable result in the study** — it
measures what the forward is actually worth. NIFTY needs it. If crypto does not, a whole branch
of T1 becomes unnecessary, and that is a real finding, not a failure.

---

### T3 · Parallelisation and hitting the latency target

**Concept.** "Make it fast" is meaningless without knowing where time goes. Profile first,
optimise the top item, re-measure. Parallelism has a cost — process startup, data serialisation —
that can exceed the work at small sizes.

**Why this way.** At 136 contracts per chain, naive per-contract solving may already be fast
enough, in which case parallelism is complexity for nothing. We find out rather than assume.

**Learn first.** NumPy vectorisation basics — why array-at-a-time beats loops. Python's GIL and
why `multiprocessing` (not threads) is the CPU-bound answer.

**Task.** Time every function individually. Compare scalar, NumPy-vectorised and multiprocess
across a full 136-contract chain. Compare **full-chain recompute against single-contract
incremental**. Hit **< 1 s full chain**, target **< 40 ms incremental**.

**How you'll know.** A timing table with medians and p95s; both targets met or a documented
reason why not.

**What to notice.** Where the time actually goes — likely the solver iterations, possibly the
string-to-float conversion, possibly neither. The measurement usually surprises. Note the size at
which parallelism starts winning; below it, the simpler code is also the faster code.

---

### T4 · Ingestion and fan-out: one socket, many consumers

**Concept.** A **message bus** is a middleman. Instead of the websocket handler calling the
storage writer and the UI directly, it publishes messages to a bus, and anything interested
subscribes. The publisher never knows who is listening. OpenAlgo runs exactly this — a websocket
proxy on port 8765 fanning out over **ZeroMQ**; NautilusTrader does the same with its own bus.

**Why this way, and why not all the way.** We need **one** socket carrying all 967 contracts —
three consumers each opening their own connection would burn the 150-per-5-min budget and give
three inconsistent views. So the socket-owner/consumer split is mandatory. But a *broker process*
is not: OpenAlgo needs ZeroMQ because it fans across 36 brokers and many users; we have one venue
and one user at 82 KB/s. **We build the seam where OpenAlgo puts the bus, using an internal async
fan-out** — so promoting to ZeroMQ later is a deployment change, not a rewrite.

**Learn first.** [ZeroMQ Guide](https://zguide.zeromq.org/) chapter 1, pub/sub only — enough to
know what OpenAlgo is doing. OpenAlgo's `websocket_proxy/` module. NautilusTrader's message-bus
docs, for the mature version of the same idea.

**Task.** One connection to `wss://public-socket.india.delta.exchange`, `ticker` channel, all
live options. Decode the abbreviated payload into a normalised record. Publish to an in-process
fan-out where each consumer holds its own `asyncio.Queue`; a slow consumer must never stall the
socket. Handle reconnection with resubscribe, and heartbeats — the documented 60 s idle
disconnect did not reproduce in a 75 s test, so treat it as unverified and send them anyway.
**Write the note: what we took from OpenAlgo, what we left, and why.**

**How you'll know.** All 967 contracts subscribed on one connection; measured throughput matches
the 187/s and 82 KB/s baseline; killing a consumer does not disturb ingestion; pulling the network
cable reconnects and resubscribes without losing the subscription set.

**What to notice.** How much the indirection costs in latency, and how much it buys in
independence. This is the pattern two mature systems reached independently — worth understanding
why before deciding we are the exception.

---

### T5 · Storage: hive-partitioned Parquet via Polars

**Concept.** **Parquet** stores columns rather than rows, so reading one field of a billion rows
touches only that field. **Hive partitioning** encodes filters into directory names —
`date=2026-09-03/underlying=BTC/` — so a query for one day skips every other directory without
opening a file. **Polars** is a DataFrame library that reads both natively.

**Why this way.** OpenAlgo uses DuckDB for its history store ("Historify"). We do not have to
choose: write hive Parquet and *both* DuckDB and Polars read it. That keeps the query engine a
later decision rather than a commitment made now.

**Learn first.** [Polars hive guide](https://docs.pola.rs/user-guide/io/hive/). OpenAlgo's
Historify module, for how they laid out market data. Why columnar beats row storage for analytics.

**Task.** Define the record: bid, ask, mark, LTP, OHLC, open interest, all three of Delta's IVs
and five Greeks **as reference columns**, plus our computed IV and Greeks, plus spot and the
exchange timestamp. Partition on `date/underlying` only — **expiry stays a column**, because
adding it as a partition level explodes into thousands of tiny files and makes Parquet slower
than CSV. Buffer in memory, flush on a size or time threshold, on a thread that cannot block the
socket. **Store events as they arrive; never forward-fill.**

**How you'll know.** A day's capture reads back in Polars with correct types and no gaps beyond
real ones; measured bytes-per-row and a projected daily footprint against the ~16M rows/day
estimate.

**What to notice.** The compression ratio against raw JSON, and the read-time difference the
partitioning buys. And the discipline point: **forward-filling is exactly the defect we caught
Delta committing.** We must not build it into our own store.

---

### T6 · Caching and the live read path

**Concept.** The option chain is a **view**, not a stored object — the latest quote per contract,
joined to the instrument master, pivoted at read time. `optionchainstream` does precisely this
with two Redis structures; our REST engine already does it with a pivot per request.

**Why this way.** Storing an assembled chain means invalidating it 25 times a second. Storing the
latest tick per contract means one cheap write per update and assembly only when someone looks.

**Learn first.** `optionchainstream`'s Redis layout. Cache invalidation basics — why "when does
this become stale" is the whole question. OpenAlgo's choice to cache in the browser (TanStack
Query) rather than the server, and what that trades away.

**Task.** An in-memory latest-tick cache keyed by symbol, fed from T4's fan-out. Serve `/chain`
from it instead of hitting Delta per request. Wire **our** computed IV and Greeks into the
existing ladder alongside Delta's, visibly distinguished. Coalesce repaints to 4–10 Hz — 25 Hz is
faster than the eye needs and faster than the data is meaningful.

**How you'll know.** `/chain` served from cache is measurably faster than the REST round trip;
the ladder shows both sets of numbers; the screen does not flicker.

**What to notice.** This is where "replicate exactly as Delta" gets tested in the most honest way
available — **their numbers and ours, side by side, on the same screen, updating live.** Any
disagreement is immediately visible rather than buried in a report.

---

### T7 · Findings, and the historical vol surface

**Concept.** The research deliverable. Also the proof that the whole premise works: if we can
compute IV correctly, we can compute it for history where Delta never stored any.

**Why this way.** A study whose results live only in code has not produced anything. And the
historical surface is the commercial point — it is the thing that cannot be bought.

**Learn first.** `docs/delta-api-scope.md` §3, on the `end = settlement_time` rule. The volatility
smile — what shape theory predicts and why.

**Task.** Write `docs/iv-method-comparison.md`: the agreement matrix, the timing table, the smile
plots, and a plain-language conclusion naming which assumptions mattered. Then reconstruct IV for
an expired contract from stored `MARK:` prices and plot its surface through time.

**How you'll know.** Someone who was not in the room can read the document and know which method
to use and why. The historical smile has a recognisable shape.

**What to notice.** Whether the historical surface looks like the live one. If it does not, the
`MARK:` series is telling us something about how Delta computes marks.

---

## Out of Scope

**Execution, orders, and anything needing an API key.** Nothing here authenticates.

**A ZeroMQ broker process.** T4 builds the seam; crossing it is a later decision, made from T4's
measured latency rather than from taste.

**Choosing one winning method.** The output is a measured comparison. Picking a production
default is a decision made *from* the findings.

**OpenAlgo's multi-broker adapter layer.** Deliberately not copied — we have one venue, and that
abstraction is most of their complexity.

---

## Further Notes

**Sequencing.** T1 → T2 → T3 is the maths spine and can proceed against live REST with no
infrastructure. T4 → T5 → T6 is the stack. **T1 and T4 can start in parallel** — different people,
no shared state. T7 closes both tracks.

**Do T1's inverse-settlement note before anything else.** It is the one unknown that silently
corrupts everything downstream.

**Honesty rule, inherited from the API scope work.** Every number in the findings is tagged
*measured*, with the request or run that produced it, or *assumed*. This project already reversed
one verdict because a conclusion outran its evidence; tagging is what made that cheap.

**Reference implementations.** [OpenAlgo](https://github.com/marketcalls/openalgo) —
architecture, the primary source. [NautilusTrader](https://github.com/nautechsystems/nautilus_trader)
— the mature version of the same bus idea. [optionchainstream](https://github.com/ranjanrak/optionchainstream)
— the chain-as-a-view pattern. Note that `openalgo-portfoliogreeks`' product documentation is
about API-key management and contains no Greeks maths; read their source instead.
