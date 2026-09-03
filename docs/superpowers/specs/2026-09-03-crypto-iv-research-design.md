# Implied volatility on Delta: a method-comparison study

**This is a research spec, not a build spec.** Its output is a *finding* — a documented,
measured answer to "which way of computing implied volatility should we trust, and what does
each one cost?" Code is the instrument, not the deliverable. A ticket that produces working
code but teaches us nothing has failed.

Every ticket is written to be *learned from*. Each carries the concept, why we chose this way,
what to read first, the task, how you know it worked, and what to notice in the result.

---

## Problem Statement

We want to price and risk-manage crypto options on Delta the way `payoff-project` does for NIFTY.
Delta publishes implied volatility and all five Greeks on both REST and websocket — but we cannot
build on those numbers, for two reasons.

**They are unusable as an input.** Checking our maths against a number we took as input is
circular. If we display Delta's delta, we have learned nothing and can prove nothing.

**They do not exist where we need them most.** Delta's historical candles carry mark price and
open interest, but no implied vol and no Greeks. A backtest that needs the vol surface on a past
date cannot get it from Delta at any price — that surface was never stored. So the ability to
compute IV ourselves is the *only* route to a historical vol surface.

Underneath sits a harder problem: **implied volatility is not observable.** It is inverted out of
a price under assumptions — about the forward, the discount rate, the model, the solver. Change
an assumption and you get a different number, and no experiment can tell you which is "correct",
because there is no ground truth to compare against.

So the question is not "what is the IV?" It is: **which assumptions actually change the answer,
by how much, and what does each cost to compute?**

## Solution

Implement several independent methods, run them on the same live chain, and measure two things
for every one: **agreement with the other methods**, and **wall-clock cost**.

Where methods agree, the assumption they differ on does not matter, and we take the cheapest.
Where they diverge, that divergence is the finding — it names the assumption that carries the
risk. Triangulation replaces the ground truth we do not have.

**The acceptance criterion is `dIV ≤ 0.1 vol points` (0.001 in decimal) between methods**, not
against Delta. Delta's own numbers are recorded alongside as an extra, unexplained data point:
if our methods cluster and Delta sits outside the cluster, that is informative; if it sits
inside, that is confirmation. Never an input.

---

## Research Questions

In place of user stories. Each is a question the study must answer with a number.

**On the forward**

1. As a researcher, I want to recover the forward by OLS over put-call parity across ATM±x
   strikes, so that I have a forward derived from traded prices rather than assumed.
2. As a researcher, I want to sweep **x = 3, 5, 7, 9** and measure how the fitted forward moves
   with x, so that I know how many strikes the fit actually needs.
3. As a researcher, I want to compare the parity forward against a single-strike parity forward
   at the ATM strike, so that I know what the regression buys over the simplest possible estimate.
4. As a researcher, I want to compare both against the carry forward `F = S·e^(rT)` at r = 6.5%,
   so that I know whether recovering a forward from the market beats assuming one.
5. As a researcher, I want to compare all three against **using spot directly with no forward at
   all**, so that I know whether the forward machinery earns its place in crypto. *This is the
   single most valuable comparison in the study: NIFTY needs it, and crypto may not.*
6. As a researcher, I want to recover the discount rate from the parity regression's slope and
   compare it against the assumed 6.5%, so that I know whether the assumed rate is defensible.

**On the model and the solver**

7. As a researcher, I want to price with Black-76 on the forward and Black-Scholes on spot and
   compare the resulting IVs, so that I know whether the model choice matters once the forward
   is handled correctly.
8. As a researcher, I want to solve for IV by Newton-Raphson with analytic vega, by Brent, and by
   Jäckel's "Let's Be Rational", so that I know the accuracy and cost of each.
9. As a researcher, I want to know where each solver fails — deep ITM, near expiry, illiquid
   wings — so that the production path knows when to fall back.
10. As a researcher, I want to understand what inverse settlement changes about the pricing, so
    that I do not silently reuse a NIFTY assumption that does not hold on a coin-margined venue.

**On cost and parallelism**

11. As a researcher, I want every function timed individually, so that I know where the time
    actually goes rather than guessing.
12. As a researcher, I want to compare scalar, NumPy-vectorised, and multiprocess execution
    across a full 136-contract chain, so that I know which parallelisation is worth its
    complexity.
13. As a researcher, I want to know the cost of a **full-chain** recompute against a
    **single-contract incremental** recompute, so that the live path can choose correctly.
14. As a researcher, I want to learn what a message bus is and why OpenAlgo and NautilusTrader
    both use one, so that I can judge whether we need one rather than cargo-culting it.

**On the surface**

15. As a researcher, I want to plot our IV against strike for one expiry, so that I can see
    whether the smile has the shape theory predicts.
16. As a researcher, I want to compute the same IV from history using stored `MARK:` prices, so
    that I prove the historical vol surface is reconstructable.

---

## Measured Facts This Study Rests On

All measured 2026-09-02/03 against `https://api.india.delta.exchange`. Re-verify before trusting;
venues change.

**The live feed**

| Fact | Value |
|---|---|
| Public websocket | `wss://public-socket.india.delta.exchange` |
| Channel name | **`ticker`** — `v2/ticker` is rejected as an invalid channel |
| Contracts on one connection | **967** — every live option, no cap reached |
| Quote updates, one contract | **0.186/s** — one every 5.4 seconds |
| Quote updates, one expiry chain (136 contracts) | **25.3/s** |
| Quote updates, all 967 | 187/s, 82 KB/s |
| Trades on an ATM contract | **~1 per 75 seconds** |
| `mark_price` channel | **produced nothing in 75s — treat as dead** |
| Connections allowed | 150 per IP per 5 minutes |

The websocket payload is abbreviated. Decoded against REST, field by field:

```
d[].g   = [delta, gamma, rho, theta, vega]
d[].qiv = [ask_iv, bid_iv, mark_iv]
d[].q   = [best_ask, ask_size, best_bid, bid_size, impact_mid]
d[].m   = mark price       d[].oi = [oi_contracts, oi_value_usd]
sp      = spot price       ts     = microsecond timestamp
```

**Two consequences that shape the design.** A quote update is not a trade — quotes move because
spot moved, ~400× more often than anyone trades. And recomputing all 136 contracts on every
update means ~3,400 IV solves per second to refresh data that individually changes every 5.4s.
**Incremental recompute, not full-chain recompute.**

**History**

Historical option data is usable, but only under one rule: **`end` must be the contract's
`settlement_time`, never `now`.** With `end = now`, `/v2/history/candles` pads every empty bucket
by copying the last trade forward, indefinitely — `C-BTC-60000-270624` returns 801 daily bars of
which 797 are fabricated. With `end = settlement_time` the same request returns 4 bars, all real.

Under that rule `MARK:` is the most useful series available: 64 hourly bars with 64 distinct
values across the full life of a contract that expired in June 2024. That is the input for
research question 16. Full detail in [`docs/delta-api-scope.md`](../../delta-api-scope.md).

**Prior art in `payoff-project`**

`src/payoff/forward.py` already implements parity OLS with a trust gate and two fallbacks
(`parity_fit`, `single_strike_parity`), and already discounts at `D = e^(-0.065·T)` — the 6.5%
is in place. `docs/calculations.md` documents the method and records that the regression
reproduced the source exactly on 316 of 376 minutes. **Read that before writing any maths.** The
method transfers; the settlement convention does not.

---

## Method Decisions

Four axes. The study is their cross-product.

**Forward** — `F1` parity OLS over ATM±x (x ∈ {3,5,7,9}) · `F2` single-strike parity at ATM ·
`F3` carry `F = S·e^(rT)`, r = 6.5% · `F4` spot directly, no forward

**Model** — `M1` Black-76 on the forward · `M2` Black-Scholes on spot

**Solver** — `S1` Newton-Raphson with analytic vega · `S2` Brent · `S3` Jäckel "Let's Be Rational"
· `S4` NumPy-vectorised Newton across the chain

**Rate** — `R1` assumed 6.5% · `R2` recovered from the parity regression slope

Every method is a **pure function with the same signature**, so they are interchangeable and
individually testable. No method may read Delta's `mark_iv` or `greeks`. Those travel as
reference columns only, and a test asserts they are never consumed.

Only strikes quoting **both** a call and a put can enter a parity fit — parity needs a pair.
`payoff-project` gates the regression rather than trusting OLS blindly, because OLS returns a
number whether the input deserves one or not. Carry that gate over.

**Inverse settlement is the one thing that does not transfer from NIFTY.** Delta's options are
quoted in USD but margined and settled in the underlying coin. Establish what that changes about
the payoff and the Greeks *before* porting the pricing code, and write it down. Getting this
wrong produces numbers that look plausible and are wrong — the worst failure mode available.

---

## Validation Decisions

**There is no ground truth, so nothing is validated against a "correct" answer.** Validation is
agreement between independent methods plus behaviour under known limits.

1. **Pairwise agreement.** For every pair of method combinations on the same contract, record
   `|IV_a − IV_b|`. The headline result is the matrix of these, by moneyness and by time to
   expiry. **Target: ≤ 0.001 decimal (0.1 vol point).**
2. **Round-trip.** Price with the model using the IV we solved for; the price must return to the
   input. This catches solver bugs without needing a reference implementation.
3. **Known analytic limits.** Deep ITM call delta → 1; ATM delta ≈ 0.5; put-call parity holds on
   our own prices; vega → 0 far from the money. These are checkable without market data.
4. **Delta's values as an unexplained reference.** Stored, plotted, never asserted against. If
   our four methods cluster within 0.1 and Delta sits 0.4 away, we report that as a finding about
   *their* assumptions, not a failure of ours.
5. **Timing.** Every function timed with `perf_counter` over repeated runs, reported as median
   and p95, per contract and per full chain. Timing is a result, not an afterthought — it appears
   in the findings document beside the accuracy numbers.

**Tests assert on external behaviour**, not internals: given this chain snapshot, this method
returns an IV within this tolerance, in under this time. Fixtures are real captured chains, so
tests never touch the network — the pattern `engine/tests/` already uses, where an autouse
fixture makes any test reaching for the wire fail.

---

## Ticket Format

Every ticket in this study carries six sections. A ticket missing the first three is not ready.

1. **The concept** — what this thing is, in plain language, assuming no prior exposure
2. **Why this way** — the alternative we rejected, and the reason
3. **Learn first** — two or three specific resources, each with *what to take from it*
4. **The task** — what to build
5. **How you'll know it worked** — the measurable outcome
6. **What to notice** — the thing worth understanding once it runs

Worked example, for the ticket on parity:

> **Concept.** Put-call parity is an identity, not a model: for European options on the same
> strike and expiry, `C − P = D·(F − K)`. It holds by arbitrage, with no assumption about
> volatility at all.
>
> **Why this way.** Plot `C − P` against `K` across strikes and it is a straight line. Its slope
> gives the discount factor and its zero-crossing gives the forward — so a regression recovers
> both from traded prices, without assuming a rate. The alternative, assuming `F = S·e^(rT)`,
> requires knowing `r`, which on a crypto venue nobody actually knows.
>
> **Learn first.** `payoff-project/docs/calculations.md` §1 — the derivation and the gating
> rules, our own prior art. Hull, *Options, Futures and Other Derivatives*, the parity section —
> for why it is arbitrage rather than a model.
>
> **Task.** Implement `F1` and `F2` as pure functions over a chain snapshot.
>
> **How you'll know.** On a captured chain, `F1` and `F2` agree within a few dollars, and the
> recovered discount implies a rate in a plausible range.
>
> **What to notice.** The forward is robust and the discount is fragile — they come from
> different features of the same line. The forward is where the line crosses zero; the discount
> is its slope, and a slope is far more sensitive to noise at the wings. Watch how much the
> recovered rate moves as you change x, and how little the forward does.

---

## Out of Scope

**Websocket capture and storage.** Measured and understood (see above), but not built here.
This study runs against live REST snapshots and captured fixtures. Capture is the next spec, and
it should not start until the maths is trusted — otherwise we spend a week storing data before
discovering the IV is wrong.

**The message bus.** Research question 14 asks us to *understand* one. Building one is out of
scope; at 82 KB/s it buys nothing today.

**The live UI.** The existing chain ladder already renders Delta's published values. Wiring our
computed values into it comes after we trust them.

**Execution, orders, and anything requiring an API key.** Nothing here needs authentication.

**Choosing a single winning method.** The output is a measured comparison. Picking a production
default is a decision to be made *from* the findings, in a later spec.

---

## Further Notes

**Sequencing.** The four axes are not equally uncertain. Do the forward axis first — it has the
most alternatives, the largest expected spread, and question 5 could invalidate a whole branch
of the work in an afternoon. Solvers second. Timing throughout, never bolted on at the end.

**The deliverable is a findings document** — `docs/iv-method-comparison.md` — carrying the
agreement matrix, the timing table, the smile plots, and a plain-language conclusion. The code
exists to produce it.

**Reference implementations worth reading, not copying.** OpenAlgo's ZeroMQ fan-out and DuckDB
history store, and NautilusTrader's message bus, are the two mature designs in this space; both
were reviewed and neither is needed at our volume. `optionchainstream` contributes one idea we
already use — the chain is a *view* joined at read time, never a stored object. OpenAlgo's
36-broker adapter layer is deliberately not copied: we have one venue, and that abstraction is
most of their complexity.

**Honesty rule, inherited from the API scope work.** Every number in the findings document is
tagged *measured* with the request or run that produced it, or *assumed*. This study reversed one
earlier verdict already, because a conclusion outran its evidence. Tagging is what made the
reversal cheap.
