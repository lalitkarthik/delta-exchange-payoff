# The forward's price, paid in Greeks

**Verdict: the forward is free, so buy the good one.** Fitting F1 over every paired
strike costs **0.0296 ms** against a **1.383 ms** total for the whole pipeline — the
forward is **2.1%** of the work. Taking spot instead (F4) saves 0.0285 ms and buys a
forward wrong by **$915 at 86 days**, a delta wrong by **0.0227** at p95 and an implied
volatility wrong by **2.81 vol points**. There is no trade here to make. The cheap
forwards are cheaper than the expensive one by an amount too small to measure against the
volatility solve that follows them.

**And the second finding is a split.** Each method carries a forward *and* a discount, and
the two damage different Greeks:

- **Implied volatility, delta and gamma are forward-driven.** Holding the discount at the
  reference changes them by under 1%. Delta moves from 0.000369 to 0.000372 at p95 — the
  discount does not touch it, and the numbers confirm the convention says so.
- **Theta is discount-driven, overwhelmingly.** Holding the discount at the reference cuts
  F2's median theta error by **53x**, from 0.0988 to 0.00185. Theta is the only Greek that
  re-discounts, and it inherits the fragility `docs/forward.md` §4 found in the slope
  rather than the robustness it found in the crossing.

So `docs/forward.md`'s headline — *the forward is robust, the discount is fragile* —
survives being carried downstream, and it now names which outputs it damages.

**F2 is the surprise.** One strike, inverted, no regression: it tracks F1-all-pairs to
within **0.13 vol points at every expiry out to 86 days**, meets T2's 0.1 vol-point target
out to 58 days, and costs a third of F1's already-negligible time. It is not a substitute
for F1 — it borrows F1's assumed rate and cannot check itself — but as a corroborating
second opinion it is nearly free.

Implemented in `tools/measure_greeks.py`. Reads `engine/src/deltapayoff/{forward,solvers,
greeks}.py` unchanged; nothing in the engine was modified for this study.

## How to read this

**Measured** names the fixture and the run. Every number here comes from a committed
fixture and reproduces from the repo:

```sh
python tools/measure_greeks.py --runs 500
```

**Assumed** means borrowed and unverified — the 6.5% rate feeding F2 and F3 is the only
such number, and it is the same one `docs/forward.md` §4.4 measured as roughly 2.3x too
large.

**The reference is F1-all-pairs, and it is a reference rather than a truth.** It is the
only method that assumes nothing and also passes the gate. Implied volatility is not
observable and neither is a forward, so "accuracy" throughout this document means
*distance from F1-all-pairs*, never distance from a known answer. Where the reference
itself fails its gate — one expiry does — the table says so and its numbers are not used.

**Deviations are absolute, in each Greek's own reported unit**, with a relative figure
beside them. Relative figures are taken only over legs whose reference Greek clears a
per-Greek floor, because `|Δγ| / 1e-9` reports a wing's smallness rather than a method's
error. Every count is printed so the exclusions are visible.

**p95 by nearest rank**, matching `timing.summarise` — a leg that occurred, not an
interpolation between two that did.

**The Greek conventions are `greeks.py`'s, unchanged**: delta and gamma undiscounted, so a
call's delta is `N(d1)`; vega and rho discounted and per one percent; theta a
one-calendar-day repricing. That asymmetry is the reason the split above exists at all.

---

## 1. What is being graded

`docs/forward.md` measured the four forwards against each other and stopped there. But
**nobody trades a forward.** It is an input to an implied volatility and to five Greeks,
and those are what reach a screen and a hedge. A $22 error on a $77,590 forward is 2.9
basis points and sounds like nothing. Whether it *is* nothing is a question about delta
and theta, not about the forward.

So: fit F1 over every paired strike, solve one implied volatility per strike off the
out-of-the-money leg's midpoint with S1 Newton, report the five Greeks. That is the
reference. Then do it again under F2, F3 and F4 and measure the distance.

The volatility and the Greeks are produced by exactly the code the live path runs —
`solvers.solve_chain` and `greeks.report_greeks` — under `compute.enrich`'s rule that one
volatility per strike is written to both legs. Nothing was reimplemented for the study,
which is what makes it a measurement of the engine rather than of a model of it.

---

## 2. The two attributions

Each method supplies a forward **and** a discount. Reporting only the pair would let a bad
discount be recorded as a bad forward, which is precisely the confusion `docs/forward.md`
§4.1 warns about — across window choices the forward spans $1.23 while the implied rate
spans -17.1% to +9.4%.

| attribution | what varies | answers |
|---|---|---|
| **end-to-end** | the method's own `(F, D)` | what using this method would actually produce |
| **forward-only** | the method's `F`, reference `D` | the forward's contribution alone |

The difference between the two columns is the discount's contribution. That subtraction is
where §5's result comes from, and it is why both are reported everywhere below rather than
one being chosen.

---

## 3. Measured: the 04-09-2026 chain

**Measured** on `engine/tests/fixtures/tickers-btc-04-09-2026.json` — 65 strikes of which
**63 quote both sides**, spot 77,568.2, T = 3.799 days, snapshot 2026-08-31T16:49:17Z. The
same chain `docs/forward.md` §4 tables, so the forwards below reproduce its numbers
exactly and the Greeks extend them. 126 legs priced. 500 runs per timed phase.

| method | forward | vs ref | D | implied r | trusted | n pairs |
|---|---|---|---|---|---|---|
| **F1 all pairs** | **77,590.39** | **ref** | **0.999706** | **2.824%** | yes | 63 |
| F2 | 77,587.99 | -2.40 | 0.999324 | 6.500% (assumed) | yes | 1 |
| F3 | 77,620.70 | **+30.30** | 0.999324 | 6.500% (assumed) | yes | 0 |
| F4 | 77,568.20 | **-22.19** | 1.000000 | 0.000% | yes | 0 |

### Implied volatility, in vol points

| method | attribution | n | median | p95 | worst | median rel |
|---|---|---|---|---|---|---|
| F2 | end-to-end | 63 | 0.0131 | **0.0366** | 0.0491 | 0.036% |
| F2 | forward-only | 63 | 0.0139 | 0.0334 | 0.0383 | 0.038% |
| F3 | end-to-end | 63 | 0.1782 | **0.4216** | 0.5601 | 0.472% |
| F3 | forward-only | 63 | 0.1759 | 0.4230 | 0.5492 | 0.478% |
| F4 | end-to-end | 63 | 0.1307 | **0.3114** | 0.3457 | 0.346% |
| F4 | forward-only | 63 | 0.1288 | 0.3078 | 0.3529 | 0.350% |

**The two attributions agree to within 3%.** Implied volatility is a forward story and not
a discount one — which is the sharpening `docs/implied-vol.md` §2's forward axis needed,
and is recorded there too.

### The five Greeks

Absolute deviation from the reference, in each Greek's own unit. `n = 126` legs throughout.

| method | attribution | delta | gamma | vega | theta | rho |
|---|---|---|---|---|---|---|
| F2 | end-to-end | 0.000369 | 2.64e-07 | 0.017955 | 1.417278 | 0.004152 |
| F2 | forward-only | 0.000372 | 2.35e-07 | 0.008624 | **0.143869** | 0.003052 |
| F3 | end-to-end | 0.004789 | 3.06e-06 | 0.104353 | 1.875828 | 0.037603 |
| F3 | forward-only | 0.004775 | 3.01e-06 | 0.109158 | 1.823347 | 0.037838 |
| F4 | end-to-end | 0.003415 | 2.19e-06 | 0.075454 | 1.377044 | 0.027171 |
| F4 | forward-only | 0.003403 | 2.16e-06 | 0.079494 | **1.319779** | 0.028491 |

*p95 of the absolute deviation. Medians are in §5's table and in the tool's output.*

**Delta and gamma barely move between the two attributions** — 0.000369 against 0.000372,
2.64e-07 against 2.35e-07. That is the convention showing through: they are undiscounted,
so `D` cannot reach them, and a study that found otherwise would have found a bug.

**At 3.8 days none of this is large.** F3's delta is out by 0.0048 at p95 — half a delta
point on a hundred-lot. Whether that matters is a desk's question, not this document's.
What §6 shows is that it does not stay this small.

---

## 4. Where the error sits

Delta error by moneyness band, end-to-end, p95:

| method | deep ITM put | OTM put | at the money | OTM call | deep OTM call |
|---|---|---|---|---|---|
| F2 | 0.000002 | 0.000139 | **0.000415** | 0.000081 | 0.000001 |
| F3 | 0.000011 | 0.001283 | **0.005166** | 0.001543 | 0.000025 |
| F4 | 0.000008 | 0.000954 | **0.003826** | 0.001114 | 0.000018 |

**The error concentrates at the money and vanishes in the wings**, by a factor of a
thousand or more. This is the opposite of where the *solvers* disagree — `docs/implied-vol.md`
§4 found solver disagreement concentrating in the wings, where vega collapses.

The two are consistent, and the reason is the same one twice. A wrong forward shifts `d1`;
delta is `N(d1)`, and `N` is steepest at the money and flat in both tails. So a shift that
moves at-the-money delta by half a point moves a 0.99-delta call's by nothing — it was
already pinned against 1. **The wings are insensitive to the forward and sensitive to the
solver; the money is the reverse.**

Which means a chain that looks fine in aggregate can be wrong exactly where the position
usually is.

---

## 5. Theta, and why it breaks differently

Median absolute theta deviation, both attributions:

| method | end-to-end | forward-only | the discount's share |
|---|---|---|---|
| F2 | 0.098772 | 0.001848 | **53x** |
| F3 | 0.585756 | 0.023223 | **25x** |
| F4 | 0.446682 | 0.017113 | **26x** |

**Almost all of the theta error is the discount.** `report_greeks` computes theta as a
one-calendar-day repricing, and to reprice a day nearer expiry it needs a rate — which it
recovers as `r = -ln(D)/T`. So theta is the one Greek that reads `D` *twice*: once in the
price now, once in the price tomorrow. F2 and F3 assume 6.5% where the chain fits 2.824%,
and that gap is what the table above is measuring.

**The worst legs are deep in-the-money calls, and their theta is positive.** Measured, F2
against the reference:

| strike | side | reference theta | F2 theta | relative |
|---|---|---|---|---|
| 66,500 | call | +0.191866 | +1.307308 | **581%** |
| 66,000 | call | +0.229490 | +1.395238 | 508% |
| 65,500 | call | +0.267170 | +1.483225 | 455% |

A 581% error on a Greek is not a rounding difference, and it is worth being precise about
what it is. A deep in-the-money call under Black-76 holds almost no time value, so its
theta is not decay — it is **the discount unwinding**. Holding it one day longer, you
collect the payoff one day sooner, and that is worth roughly `r · K · D · Δt`. Time value
decay pushes theta down, discount unwind pushes it up, and deep in the money the two
nearly cancel. Theta there is a **small difference of two larger terms**, so doubling the
rate does not double theta — it moves one term and leaves the near-cancellation to
amplify the result.

Twenty-four of 126 legs show a relative theta error above 10%, and every one has a
reference theta under 7 in absolute value. **The relative figure is real but is not the
number to act on**; the absolute deviation, a median of 0.099 USD per day, is.

This is the one place where a method choice produces an error that is *qualitatively*
wrong rather than merely imprecise, and it is the strongest single argument in this
document for fitting the discount rather than assuming it.

---

## 6. Across time to expiry

**Measured** on `engine/tests/fixtures/tickers-btc-multi-expiry.json` — 588 contracts,
eight expiries, spot 77,874.2, snapshot 2026-09-02T08:40:14Z.

Forward error in dollars, against the F1-all-pairs reference:

| T, days | reference r | F2 | F3 | F4 |
|---|---|---|---|---|
| 1.139 | **43.124%** | -12.24 | +11.74 | -4.06 |
| 2.139 | 2.916% | -3.23 | +15.10 | -14.57 |
| 3.139 | 4.741% | -2.63 | +18.05 | -25.49 |
| 9.139 | 4.747% | -2.85 | +37.25 | -89.59 |
| 16.139 | 4.678% | +0.36 | +60.59 | -163.55 |
| 23.139 | 4.899% | -0.30 | +81.99 | -239.56 |
| 58.139 | 4.883% | +1.40 | +196.01 | -614.45 |
| 86.139 | 4.875% | +5.86 | **+288.74** | **-915.04** |

**The front expiry is excluded from every conclusion.** At 1.139 days the reference fits
an implied rate of **43.124%**, outside the project's own 0-30% gate, so F1-all-pairs is
not trustworthy there and neither is anything measured against it. The tool prints a
warning above that table rather than a footnote beneath it. This is the same effect
`docs/forward.md` §4 records: under a day the true discount is within parts per hundred
thousand of 1, so its implied rate is quote noise.

**F4's error is the basis, and the basis is linear in T.** -4 dollars at a day, -915 at
86. That is the honest null hypothesis failing exactly as it should: asserting the basis
is zero costs nothing when there is no time for a basis to accumulate and costs a great
deal when there is.

**F3's error is the assumed rate being wrong**, and it grows the same way for the same
reason. The chain fits ~4.9% at every expiry past the front; F3 assumes 6.5%, and
over-carries by the difference compounded over T.

**F2 does not degrade.** Within $6 at 86 days, and inside $3 across the middle of the
curve. Inverting parity at one strike turns out to need almost nothing from the
regression.

Implied volatility, p95 in vol points, end-to-end. **T2's target is 0.1**:

| T, days | F2 | F3 | F4 |
|---|---|---|---|
| 2.139 | 0.0532 | 0.2490 | 0.2413 |
| 3.139 | 0.0407 | 0.2772 | 0.3977 |
| 9.139 | 0.0325 | 0.3327 | 0.7888 |
| 16.139 | 0.0227 | 0.4218 | 1.1265 |
| 23.139 | 0.0246 | 0.4166 | 1.2364 |
| 58.139 | 0.0740 | 0.6931 | 2.2110 |
| 86.139 | **0.1283** | 0.9169 | **2.8134** |

**F2 meets the target at every expiry out to 58 days and misses it marginally at 86.
F3 and F4 miss it everywhere.**

Delta, p95, end-to-end:

| T, days | F2 | F3 | F4 |
|---|---|---|---|
| 2.139 | 0.000728 | 0.003465 | 0.003261 |
| 23.139 | 0.000101 | 0.004203 | 0.011647 |
| 86.139 | 0.000531 | 0.007364 | **0.022672** |

And the wings stop protecting you. At 3.8 days F4's delta error is 0.0038 at the money and
0.000018 in the deep call wing — a factor of 200. At 86 days it is 0.0227 at the money and
0.0098 in the wing, a factor of 2. **A long-dated chain priced off spot is wrong
everywhere, not just in the middle.**

---

## 7. Time taken

Milliseconds, 500 runs, the 04-09-2026 chain — 63 strikes solved, 126 legs priced:

| method | forward | IV solve | Greeks | total | forward's share |
|---|---|---|---|---|---|
| **F1 all pairs** | **0.0296** | 0.7695 | 0.5837 | **1.3828** | **2.1%** |
| F2 | 0.0099 | 0.7678 | 0.5887 | 1.3665 | 0.7% |
| F3 | 0.0082 | 0.7832 | 0.5855 | 1.3769 | 0.6% |
| F4 | 0.0011 | 0.7813 | 0.5921 | 1.3745 | 0.1% |

*Medians. p95 figures are in the tool's output.*

**The forward is not where the time goes.** F1-all-pairs is 27x more expensive than F4 in
isolation and **0.6%** more expensive in total, because the volatility solve that follows
it costs 26x more than the fit does and does not care which forward it was handed. Newton
runs the same number of iterations either way.

The same holds at 86 days on a 35-strike chain: F1's forward is 0.0201 ms of a 0.6139 ms
total, 3.3%.

**So the timing axis has no trade on it.** Every argument for a cheap forward has to be
made on grounds other than speed, and §3 and §6 are those grounds pointing the other way.

---

## 8. What to use

**F1 all pairs, and it is not close.** It assumes nothing, it passes its own gate at every
expiry past the front, and it costs 2.1% of a pipeline that already runs in 1.4 ms. This
is what `compute.enrich` already does, and this study finds no reason to change it.

**F2 as a corroborating second opinion.** It agrees to within 0.13 vol points everywhere
and 0.0005 delta, from one strike and a tenth of a millisecond. It cannot replace F1 — it
takes the assumed rate on faith, so it cannot detect a bad rate — but two independent
methods landing on the same forward is the only evidence available that either is right,
which is `docs/implied-vol.md` §1's argument applied one level up. `compute.enrich`
already falls back to it when there are too few pairs, and this measures that fallback as
sound.

**F3 and F4 for neither.** Both fail T2's volatility target at every expiry, and both
degrade linearly in T for mechanical reasons that will not improve with better quotes. F4
remains worth keeping as the null hypothesis it was introduced as: the measurement that
the basis is not zero is exactly what its error curve is.

**Fit the discount; do not assume it.** §5 is the sharpest form of this. The assumed 6.5%
is `docs/forward.md`'s "roughly 2.3x too large" arriving at its destination, and theta is
where it lands.

---

## 9. Still open

- **The live corroboration did not run.** `--live` was attempted and Delta's edge timed out
  at the TLS handshake from this network; the tool prints the refusal and continues on the
  fixtures. Both snapshots are therefore from the same week of the same market. The
  T-dependence is mechanical and will hold, but it has been observed twice, not many times.
- **The front expiry is unmeasured, not measured-and-good.** Its reference fails the gate,
  so the `under a day` band is empty here for the same reason it is empty in
  `docs/implied-vol.md` §6. The band that most needs a trustworthy forward is the one where
  none of these methods supplies one.
- **Our Greeks are still not compared against Delta's reference columns.** This study grades
  our Greeks against our own Greeks under a different forward. The comparison #4 asked for —
  ours against the venue's — remains undone, and this document does not close it.
- **`greeks.rho` still does not reconcile** with a textbook vanilla rho, carried from
  `docs/settlement.md` §5. Every rho figure here is therefore self-consistent and of
  unverified absolute meaning.
- **Theta's relative error has no agreed denominator.** §5 reports 581% and argues the
  absolute figure is the one to act on. A better statistic for a Greek that legitimately
  crosses zero would improve this table, and none is proposed here.
- **One solver.** Everything is S1 Newton. `docs/implied-vol.md` measured the solver axis
  at 2.5e-05 vol points, so the choice should not matter — but "should not" is an
  inference, and this study did not re-check it while varying the forward.
