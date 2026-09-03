# Four forwards, and what the chain says about them

**Verdict: the forward is solid and the discount is not.** On the captured BTC chain, four
window widths put the forward inside a $1.23 band and the implied rate inside a
26.5-percentage-point one. Same fits, same data, same line — read at two different places.

**Use every paired strike, not a window.** Each narrow window implies a rate the gate rejects.
The unwindowed fit passes, and the rate it recovers — 2.824% — independently matches the
2.749% implied by the basis. It costs 0.065 ms against 0.055 ms.

A second finding, unexpected and worth #4's attention early: **assuming a 6.5% carry rate is
worse than assuming no carry at all.** F3 misses the parity forward by $30.30, F4 by $22.19.
The borrowed constant is 2.4x the rate this chain actually implies.

Companion to `docs/settlement.md`, which establishes that no correction terms are needed
anywhere in what follows. Implemented in `engine/src/deltapayoff/forward.py`, tested in
`engine/tests/test_forward.py`.

## How to read this

**Measured** means the run is named and reproducible from a committed fixture. **Assumed**
means borrowed and unverified — `r = 6.5%` is the only such number here, and F1 exists so it
can be checked rather than trusted.

---

## 1. The identity, and why it is not a model

For European options on one strike and expiry:

```
C - P = D (F - K)
```

No volatility appears. No distribution, no Black-Scholes. It holds by arbitrage: a long call
and a short put at the same strike *is* a forward, so if the equality broke you could assemble
the synthetic, trade the real forward against it, and take the difference risklessly.

Hold the expiry fixed and let `K` vary. Then `y = C(K) - P(K)` against `x = K` is a straight
line `y = a + mK` with `a = D·F` and `m = -D`, so

```
D = -m            the slope
F = -a / m        where the line crosses zero
```

One ordinary least squares fit recovers both, assuming neither. That is F1.

**Only strikes quoting both a call and a put may enter.** Parity needs a pair; a one-sided
strike says nothing about the forward. Letting them in with a missing leg read as zero moved
the recovered forward from 78,400 to 124,404 on a planted chain — the mutation is in
`test_one_sided_strikes_are_excluded_from_the_fit`.

## 2. The four methods

| | Method | Assumes | Reads option prices |
|---|---|---|---|
| `F1` | parity OLS over ATM +/- x, x in {3,5,7,9, all} | nothing | yes, up to 2x+1 strikes |
| `F2` | parity inverted at the money strike | an `r`, for `D` only | yes, one strike |
| `F3` | carry, `F = S·e^(rT)` at r = 6.5% | an `r`, and that carry holds | no |
| `F4` | spot, `F = S` | that the basis is zero | no |

These stay **side by side, not stacked**. `payoff-project` wires the same three tiers as a
fallback ladder and takes the first that passes its gate. Here they stay independent, because
#4 exists to measure whether the parity fit was worth the trouble and a ladder would have
already answered that.

## 3. The gate

OLS returns a number whether the input deserves one or not. It reports no error on a line
fitted to noise. Carried over from `payoff-project/docs/calculations.md` §1:

```
n >= 5                    at least five paired strikes
0 < r < 30%               where r = -ln(D) / T
```

`D > 1` means being paid to wait. `r > 30%` means the line has tilted absurdly.

**The gate is really a discount gate.** It is the slope that goes wrong; the crossing rarely
does. So a failed gate is a verdict travelling with the answer, not a refusal to answer — the
forward is usually still worth having.

## 4. Measured: the captured chain

**Measured** on `engine/tests/fixtures/tickers-btc-04-09-2026.json` — the 04-09-2026 BTC
chain, snapshot 2026-08-31T16:49:17Z, spot 77,568.2, 65 strikes of which **63 quote both
sides**, T = 3.799 days. 500 runs per method. Distance is measured against the unwindowed
fit, which is the reference because it is the only one assuming nothing that also passes.

| method | n | forward | vs ref | basis | D | implied r | trusted | median | p95 |
|---|---|---|---|---|---|---|---|---|---|
| F1 ATM+/-3 | 7 | 77,589.96 | -0.43 | +21.76 | 1.000428 | **-4.111%** | no | 0.055 ms | 0.070 ms |
| F1 ATM+/-5 | 11 | 77,590.60 | +0.21 | +22.40 | 1.001265 | **-12.147%** | no | 0.059 ms | 0.084 ms |
| F1 ATM+/-7 | 15 | 77,591.19 | +0.80 | +22.99 | 1.001786 | **-17.144%** | no | 0.066 ms | 0.133 ms |
| F1 ATM+/-9 | 19 | 77,590.60 | +0.20 | +22.40 | 0.999026 | +9.363% | yes | 0.058 ms | 0.082 ms |
| **F1 all pairs** | **63** | **77,590.39** | **ref** | **+22.19** | **0.999706** | **+2.824%** | **yes** | **0.065 ms** | **0.089 ms** |
| F2 | 1 | 77,587.99 | -2.40 | +19.79 | 0.999324 | 6.500% (assumed) | yes | 0.019 ms | 0.021 ms |
| F3 | 0 | 77,620.70 | **+30.30** | +52.50 | 0.999324 | 6.500% (assumed) | yes | 0.015 ms | 0.016 ms |
| F4 | 0 | 77,568.20 | **-22.19** | 0.00 | 1.000000 | 0.000% | yes | 0.002 ms | 0.002 ms |

### The forward is robust; the discount is fragile

Across every window the forward spans **77,589.96 to 77,591.19 — $1.23**, or 1.6 basis points
of a $77,590 number. Over the same four fits the implied rate runs **-17.1% to +9.4%**.

They come from different features of one line. `F` is where it **crosses zero**, an
interpolation sitting inside the strike range, and noise barely moves an interpolation. `D` is
its **slope**, measured as a tilt across the range, and a small tilt error is a large `D` error.
Three of the four windows imply a negative rate. Nothing about those fits looks broken.

This reproduces `payoff-project`'s result on an entirely different asset class, which is the
strongest evidence available that it is a property of the method rather than of NIFTY.

### Wider is better, which inverts the prior art's instinct

The narrow windows fail and the wide ones pass — the opposite of the usual "trim the noisy
wings" reflex. Read the strike span against the verdict:

| window | strike range | span as % of spot | implied rate | gate |
|---|---|---|---|---|
| ATM+/-3 | 77200–78200 | 1.3% | -4.111% | rejected |
| ATM+/-5 | 76800–78500 | 2.2% | -12.147% | rejected |
| ATM+/-7 | 76500–78800 | 3.0% | -17.144% | rejected |
| ATM+/-9 | 75500–79200 | 4.8% | +9.363% | passed |
| all pairs | 58000–88000 | **38.7%** | **+2.824%** | passed |

Over 3.8 days the true tilt is `D ~ 0.9997`. Across a $1,000 range that tilt is a rounding
error, and the fit reads quote noise as slope instead. The prior art measured the same thing
from the other side: its rejected minutes spanned a median 850 strike points against 1,300 for
accepted ones.

**The unwindowed fit is corroborated independently.** Its 2.824% is not graded against
anything in the regression — but the basis it produces, +$22.19 on a spot of 77,568.2 over
3.799 days, implies **2.749%** by direct arithmetic. Two routes through different numbers,
agreeing to 8 basis points. None of the rejected windows can say that.

**For the discount, reach wider. For the forward, it does not matter.** `SWEEP_WIDTHS` carries
`None` as a fifth entry for exactly this reason.

### F2 corroborates F1 independently

F2 lands **$2.40** from the unwindowed fit, and within **$3.20** of every F1 window, while
using a completely different construction — one strike inverted, no regression, an assumed `D`.
Two methods sharing no arithmetic agreeing to four parts in a hundred thousand is about as
close to independent confirmation as a single chain snapshot can offer.

### The assumed rate is roughly 2.3x too large

The parity basis is **+$22.19** over 3.799 days, an implied **2.749%** annualised. The borrowed
`r = 6.5%` produces +$52.50 instead.

So on this chain:

```
F3 error vs F1    +$30.30       carry at 6.5%
F4 error vs F1    -$22.19       no carry at all
```

**Ignoring carry beats assuming the wrong carry, by 37%.** That is one snapshot and not a
finding yet, but it is the cheapest thing #4 can test and it points at deleting work rather
than adding it. If it holds across expiries, F3 should be dropped rather than tuned — a rate
fitted to make F3 match F1 would just be F1 with extra steps.

### Timing

Every method is **under 0.1 ms median**. F4 is 0.002 ms and F1 at its widest is 0.058 ms — a
30x spread that is irrelevant at this scale. #6's latency budget will not be spent here, and
choosing between these methods is a question of accuracy alone.

## 5. Still open

- **One snapshot, one expiry.** Every number in §4 comes from a single captured chain. The
  robust/fragile split is corroborated by `payoff-project` on NIFTY, but the 2.774% implied
  rate and the F3-vs-F4 ordering are one observation each.
- **Mid, not mark.** Prices here are the midpoint of best bid and ask. Delta's `mark_price` is
  its own model output, so fitting through marks would recover *Delta's* forward rather than the
  market's. Untested: how far apart the two answers are. Worth a line in #4.
- **The window clips rather than reflecting** when the money strike sits near the end of the
  chain, so ATM+/-9 can return fewer than 19 pairs. Measured behaviour, deliberate, but it means
  `n_pairs` must be read alongside `width` rather than inferred from it.
