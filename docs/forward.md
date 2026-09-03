# Four forwards, and what the chain says about them

**Verdict: the forward is solid and the discount is not.** On the captured BTC chain, four
window widths put the forward inside a $1.23 band and the implied rate inside a
26.5-percentage-point one. Same fits, same data, same line — read at two different places.

A second finding, unexpected and worth #4's attention early: **assuming a 6.5% carry rate is
worse than assuming no carry at all.** F3 misses the parity forward by $30.10, F4 by $22.40.
The borrowed constant is 2.3x the rate this chain actually implies.

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
| `F1` | parity OLS over ATM +/- x, x in {3,5,7,9} | nothing | yes, up to 2x+1 strikes |
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
sides**, T = 3.799 days. 200 runs per method.

| method | n | forward | basis | D | implied r | trusted | median | p95 |
|---|---|---|---|---|---|---|---|---|
| F1 ATM+/-3 | 7 | 77,589.96 | +21.76 | 1.000428 | **-4.111%** | no | 0.057 ms | 0.067 ms |
| F1 ATM+/-5 | 11 | 77,590.60 | +22.40 | 1.001265 | **-12.147%** | no | 0.055 ms | 0.079 ms |
| F1 ATM+/-7 | 15 | 77,591.19 | +22.99 | 1.001786 | **-17.144%** | no | 0.056 ms | 0.088 ms |
| F1 ATM+/-9 | 19 | 77,590.60 | +22.40 | 0.999026 | +9.363% | yes | 0.058 ms | 0.093 ms |
| F2 | 1 | 77,587.99 | +19.79 | 0.999324 | 6.500% (assumed) | yes | 0.019 ms | 0.020 ms |
| F3 | 0 | 77,620.70 | +52.50 | 0.999324 | 6.500% (assumed) | yes | 0.015 ms | 0.017 ms |
| F4 | 0 | 77,568.20 | 0.00 | 1.000000 | 0.000% | yes | 0.002 ms | 0.002 ms |

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
wings" reflex. ATM+/-3 spans $1,000 on a $77,600 underlying, 1.3%, and over 3.8 days the true
tilt is `D ~ 0.9997`. There is almost no signal in that range for a slope to sit on. The prior
art measured the same thing from the other side: its rejected minutes spanned a median 850
strike points against 1,300 for accepted ones.

**For the discount, reach wider. For the forward, it does not matter.**

### F2 corroborates F1 independently

F2 lands within **$2.61** of every F1 window while using a completely different construction —
one strike inverted, no regression, an assumed `D`. Two methods sharing no arithmetic agreeing
to three decimal places of a percent is about as close to independent confirmation as a single
chain snapshot can offer.

### The assumed rate is roughly 2.3x too large

The parity basis is **+$22.40** over 3.799 days, an implied **2.774%** annualised. The borrowed
`r = 6.5%` produces +$52.50.

So on this chain:

```
F3 error vs F1    +$30.10       carry at 6.5%
F4 error vs F1    -$22.40       no carry at all
```

**Ignoring carry beats assuming the wrong carry, by 34%.** That is one snapshot and not a
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
