# Implied volatility: two models, four solvers, four forwards

**Verdict: the solver does not matter and the forward does.** Across 295 solved strikes on
eight expiries, the four solvers agree to **2.5e-05 vol points** — 4,000x inside T2's 0.1
target — while the four forwards disagree by up to **3.9 vol points**. Every pair on the solver
axis passes; every pair on the forward axis fails.

**And the model axis is not an axis.** Black-Scholes on spot is Black-76 on the carry forward,
exactly: `BS(S, r) == B76(F = S·e^(rT), D = e^(-rT))`, agreeing to a ten-billionth of a cent
over twenty combinations. So M1-versus-M2 is the forward axis wearing different clothes, and it
already has a name in #2 — it is F3.

Companion to `docs/forward.md`, which recovers the forwards, and `docs/settlement.md`, which
establishes that no correction term is needed anywhere in either.

## How to read this

**Measured** names the fixture and the run. **Assumed** means borrowed and unverified — the
6.5% rate feeding F2, F3 and M2 is the only such number, and F1 exists so it can be checked.

`dIV` is **always method against method**. Delta publishes a `mark_iv`; it is not ground truth,
it is Delta's own inversion under Delta's own assumptions. `tests/test_no_delta_inputs.py`
corrupts every number Delta publishes — all three implied vols, all five Greeks, `mark_price`,
`mark_vol` — and asserts that not one forward or implied volatility downstream moves.

---

## 1. Why agreement is the only evidence

Implied volatility is **not observable**. It is inverted out of a price under assumptions, so
there is nothing to grade an answer against. That is not a gap in our data; it is what the
quantity is.

So the study is built on one substitute:

- Where two methods **agree**, the assumption separating them is not carrying risk, and the
  cheaper method wins.
- Where they **diverge**, the divergence names the assumption that is.

Everything below is that comparison, sliced so the answer is not an average over regions that
behave differently.

---

## 2. Measured: the eight-expiry capture

**Measured** on `engine/tests/fixtures/tickers-btc-multi-expiry.json` — a verbatim
`GET /v2/tickers` for BTC options with no expiry filter, captured 2026-09-02T08:40:14Z. 588
contracts, **303 strikes across eight expiries**, spot 77,874.2, 1.14 to 86.14 days out.
**295 of 303 solved** by every solver under every forward; the 8 misses quote only one side.

### The solver axis — forward held at F1

| pair | n | median | p95 | worst | target |
|---|---|---|---|---|---|
| S1 vs S3 | 295 | 6.77e-13 | 1.51e-09 | 3.37e-08 | pass |
| S1 vs S2 | 295 | 7.27e-07 | 2.09e-05 | 2.50e-05 | pass |
| S2 vs S3 | 295 | 7.27e-07 | 2.09e-05 | 2.50e-05 | pass |

**Every pair passes by four orders of magnitude.** Newton, Brent and the Householder iteration
share no arithmetic — one steps on vega, one shrinks a bracket, one iterates on normalised
Black — and they land on the same number to the eighth decimal place.

**So the solver choice is free, and reduces to cost.** From `docs/forward.md`'s harness on a
65-strike chain: S4 0.79 ms, S3 1.05 ms, S1 1.53 ms, S2 2.08 ms. S3 is the fastest that needs
nothing outside the standard library.

Keeping S2 anyway is worth defending. Brent is the slowest and the only one that **cannot
diverge** — it maintains a bracket and only ever shrinks it. It is the method to reach for when
a future chain misbehaves, and 2 ms is a cheap insurance premium.

### The forward axis — solver held at S3

| pair | n | median | p95 | worst | target |
|---|---|---|---|---|---|
| F1 vs F2 | 295 | 0.0268 | 0.1190 | 0.3114 | **fail at p95** |
| F1 vs F3 | 295 | 0.2244 | 0.6931 | 1.0382 | fail |
| F2 vs F3 | 295 | 0.2554 | 0.6933 | 0.9349 | fail |
| F1 vs F4 | 295 | 0.5076 | 2.2110 | 2.9804 | fail |
| F2 vs F4 | 295 | 0.5010 | 2.1628 | 3.1191 | fail |
| F3 vs F4 | 295 | 0.7124 | 2.7443 | 3.8973 | fail |

**Note F1 vs F2 fails here and passed on the single 3.8-day chain** (`docs/forward.md`: median
0.0131, p95 0.0366). The extra expiries are what changed, and §3 says why.

---

## 3. Sliced by time to expiry — the dominant effect

Median / p95, vol points.

| pair | 1 to 7 days | 7 to 30 days | over 30 days |
|---|---|---|---|
| F1 vs F2 | 0.031 / 0.151 | **0.012 / 0.029** | 0.048 / 0.119 |
| F1 vs F3 | 0.119 / 0.277 | 0.233 / 0.437 | **0.514 / 0.826** |
| F1 vs F4 | 0.083 / 0.340 | 0.627 / 1.196 | **1.626 / 2.661** |
| F2 vs F3 | 0.176 / 0.348 | 0.234 / 0.434 | 0.520 / 0.819 |
| F2 vs F4 | 0.085 / 0.308 | 0.624 / 1.187 | 1.604 / 2.633 |
| F3 vs F4 | 0.192 / 0.593 | 0.843 / 1.548 | **2.076 / 3.304** |

**Read the F1-vs-F4 row across: 0.083, 0.627, 1.626. Twenty-fold, monotone in time.**

The mechanism is not subtle. F4 asserts the basis is zero, and the basis is `F - S`, which grows
with `T`. At 3.8 days it is $22 and using spot is a small sin; at 86 days it is large enough to
dominate the implied volatility.

F1-vs-F3 grows the same way for the same reason: F3's error is `S·(0.065 - r_true)·T`, and a
rate error compounds with time.

**F1 vs F2 does not grow.** It is flat and small in every band — both recover the forward from
traded prices, and the only thing they differ on is how `D` is obtained. Its p95 of 0.151 in the
front band is the front expiry's small vega amplifying a tiny price gap, not a growing
disagreement about the forward.

**So the single-chain result understated the case.** `docs/forward.md` measured this on one
3.8-day expiry and concluded the forward matters. Across the term structure it matters roughly
twenty times more.

---

## 4. Sliced by moneyness — a secondary effect

Median / p95, vol points, `K/F` bands.

| pair | deep ITM put | OTM put | at the money | OTM call | deep OTM call |
|---|---|---|---|---|---|
| n | 67 | 60 | 72 | 53 | 43 |
| F1 vs F2 | 0.014/0.069 | 0.021/0.111 | 0.042/0.176 | 0.025/0.092 | 0.024/0.052 |
| F1 vs F3 | 0.173/0.553 | 0.172/0.782 | 0.252/0.743 | 0.220/0.683 | **0.340/0.544** |
| F1 vs F4 | 0.508/1.765 | 0.399/2.522 | 0.288/2.366 | 0.555/2.153 | **1.068/1.727** |
| F3 vs F4 | 0.681/2.318 | 0.564/3.304 | 0.518/1.908 | 0.788/2.836 | **1.408/2.270** |

**Moneyness moves the answer by about 3x; time moves it by 20x.** Time is the axis that matters.

Two things do show. Every F4 pair is **worst in the deep OTM call wing** — a mis-specified
forward shifts every strike's moneyness, and the wing is where a given shift changes the implied
volatility most, because vega is smallest there. And **F1 vs F2 is worst at the money**, which
reads oddly until you notice F2 *is* the money strike: it inverts one strike, so its answer is
most sensitive to that one quote, and at-the-money is where the two methods have least
independent information to average over.

---

## 5. What to use

```
forward   F1, all paired strikes        the only one that assumes nothing
solver    S3 Householder                fastest with no dependencies
          S2 Brent                      when a chain misbehaves; cannot diverge
          S4 vectorised                 for bulk history in #5, at NumPy + SciPy
model     M1 Black-76                   M2 is the same model and needs a rate we do not have
```

**F3 and F4 should not be used to price anything.** They remain in the codebase as the
measurement that says so, and `docs/forward.md` keeps them for the same reason.

---

## 6. Still open

- **One snapshot.** Eight expiries, one moment. The time-to-expiry pattern is strong and
  mechanical, but it is one observation of it.
- **Mid, not mark.** Every IV here inverts a bid/ask midpoint. `mark_price` is Delta's model
  output — fitting through it would recover Delta's volatility surface rather than the market's.
  How far apart the two answers are is **unmeasured**, and it is the largest remaining unknown
  in this ticket.
- **S3 is Jäckel's shape, not his method.** The reduction to `b(x, s)` and the third-order step
  are his; the rational initial guess and the reformulation avoiding cancellation are not. The
  cost is measured: at `K/F = 1.134` and sigma 15%, `b` evaluates to **-1.4e-17**, a negative
  price, and S3 declines there.
- **The `under a day` band is empty.** Delta lists a daily expiry but the nearest was 1.14 days
  out at capture. The front-expiry stress case is therefore untested at its extreme.
- **`greeks.rho` still does not reconcile** with a textbook vanilla rho, carried over from
  `docs/settlement.md` §5. Our own Greeks are not yet compared against Delta's as reference
  columns — that comparison belongs here and is not done.

---

## 7. Sources

**Peter Jäckel, "Let's Be Rational", Wilmott, 2015, pp. 40–53.**
DOI [10.1002/wilm.10395](https://doi.org/10.1002/wilm.10395) ·
reference C source at <https://www.jaeckel.org/LetsBeRational.7z> ·
Python port <https://github.com/vollib/lets_be_rational>

The method S3 is named for. Two things were taken from it. The **reduction to normalised
Black** — expressing price as `b(x, s)` with `x = ln(F/K)` and `s = sigma·sqrt(T)`, which turns
the solve into one equation in one unknown that behaves identically at every strike and expiry,
and which is what makes three derivatives short enough to write down. And the **higher-order
Householder step**, which the paper's own abstract specifies as **convergence order four** — the
third-order Householder step implemented here, since a Householder method of order `d` converges
at order `d + 1`.

**What was not taken**, and the paper is explicit that this is the substance of it: the initial
guess. Jäckel uses *four rational function branches selected on log-moneyness*, two of them
combined with nonlinear transformations of the input price, and it is that construction — not the
iteration — that delivers machine precision in **two** iterations for all possible inputs. S3
seeds from Manaster-Koehler instead and takes five on this chain. The paper's reformulation
avoiding cancellation in `b` is likewise not implemented, and §6 measures what that costs.

**This was implemented from the method's published structure, not from reading the C source.**
The description above was checked against the paper's abstract afterwards. Anyone extending S3
should read `LetsBeRational.7z` first — that is where the branches live.

**S. Manaster and G. Koehler, "The Calculation of Implied Variances from the Black-Scholes
Model: A Note", Journal of Finance 37(1), 1982, pp. 227–230.**
DOI [10.1111/j.1540-6261.1982.tb01105.x](https://doi.org/10.1111/j.1540-6261.1982.tb01105.x)

Where `sigma_0 = sqrt(2·|ln(F/K)| / T)` comes from — the volatility at which vega is maximised
for a given strike, which is what makes Newton's descent from it monotone. Adopting it took this
project from 19 of 65 strikes solved to 63 of 65; see `engine/src/deltapayoff/solvers.py`.

**M. Brenner and M. Subrahmanyam, "A Simple Formula to Compute the Implied Standard Deviation",
Financial Analysts Journal 44(5), 1988.**

`sigma ~ sqrt(2·pi/T)·C/(D·F)`. Exact at the money and useless away from it, which is precisely
the failure this project measured before adding Manaster-Koehler beside it.

**A. S. Householder, *The Numerical Treatment of a Single Nonlinear Equation*, McGraw-Hill, 1970.**

The family the order-three step belongs to.

**R. P. Brent, *Algorithms for Minimization without Derivatives*, Prentice-Hall, 1973, ch. 4.**

S2. Implemented directly rather than pulled from SciPy, so the engine keeps S1 through S3 on the
standard library alone.
