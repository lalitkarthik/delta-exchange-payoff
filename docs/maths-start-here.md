# How a number is made — forward, IV, Greeks

Print all three for a live chain, right now:

```bash
curl -s "http://127.0.0.1:8000/chain?underlying=BTC&expiry=04-09-2026" | python -m json.tool | head -40
```

The `forward`, `discount` and `years_to_expiry` are at the top. Every leg's `computed` block holds our IV and our five Greeks.

> **Reference doc.** What each formula is, which convention it follows, and the trap in each.
> Evidence lives elsewhere: [forward.md](forward.md) measures four forwards, [implied-vol.md](implied-vol.md) measures four solvers.
> Field-by-field contract: [chain-contract.md](chain-contract.md).
>
> Not to be confused with `payoff-project/docs/calculations.md`, which `forward.md` cites.
> That is the sibling project's file for the same subject under **its** conventions — the
> Greek conventions below are shared with it, the 365-day clock is not.

---

## The order is fixed — you cannot skip a step

```
1. FORWARD    ← from prices. Needs no volatility.
2. IV         ← needs the forward.
3. GREEKS     ← needs the forward AND the IV.
```

Each step consumes the one above. Get step 1 wrong and steps 2 and 3 are wrong with no error raised.

---

# 1 · The forward

## What it is

**The price the market says BTC will be at expiry.** Not spot. Recovered from option prices, never assumed.

## The identity it rests on

Put-call parity. For any strike `K`:

```
C - P = D · (F - K)
```

Read as a line in `K`, that is `y = D·F - D·K`. So:

- **slope** = `-D` → the discount factor
- **crosses zero at** `K = F` → the forward

Fitting one line recovers both, assuming neither.

## What we run — F1

**Ordinary least squares of `C - P` against `K`, across every paired strike.** No window.

```
slope     = (n·Σxy - Σx·Σy) / (n·Σxx - (Σx)²)
intercept = (Σy - slope·Σx) / n
D = -slope        F = -intercept / slope
```

## The trap — the two numbers have very different noise

| | is | noise |
|---|---|---|
| **F** | where the line crosses zero | **tiny** — an interpolation inside the strike range |
| **D** | the line's slope | **large** — a tilt measured across the range |

`measured`: across window choices the forward spans **$1.23 on $77,590** (1.6 bp) while the implied rate runs **−17.1% to +9.4%**.

Under a day to expiry the true discount sits within a few parts per hundred thousand of 1, so its implied rate is quote noise. `measured` one second apart on a live chain: `0.99997892 → 1.00001939`, taking the rate `+1.03% → −0.95%`.

**So a failed gate discredits the discount, not the forward.**

## What `forward_method` tells you

| value | meaning |
|---|---|
| `F1` | the regression fitted both, both passed |
| `F1+assumed-rate` | the regression's forward, **discount assumed at 6.5%** |
| `F2` | parity inverted at the money strike alone, discount assumed |

`F1+assumed-rate` is the common case near expiry and **is not a degraded answer**.

**Deeper:** [forward.md](forward.md)

---

# 2 · Implied volatility

## What it is

**A price quoted in a different unit.** Nobody can compare a BTC call at 1,240 with one at 890 — different strikes, different expiries, different time value.

Inverting the pricing formula asks instead: *what volatility makes this option worth exactly what the market is paying?*

That single number is comparable across the whole board. It is **the only field on the chain that is an opinion rather than an observation.**

## The model — Black-76

Prices from the **forward**, not from spot plus a rate.

```
spread = σ√T
d1 = [ ln(F/K) + spread²/2 ] / spread
d2 = d1 - spread

C = D · ( F·Φ(d1) - K·Φ(d2) )
P = D · ( K·Φ(-d2) - F·Φ(-d1) )
```

## Why we invert instead of reading Delta's

Two reasons, and the first is the whole project:

1. **Delta republishes its IV every 5,001 ms while the book moves every 508 ms** — up to **9.8x stale**.
2. **Delta fits its IV to its own mark price**, which is its model's output. Reading it back is reading Delta's opinion of Delta's opinion.

We invert the **bid/ask midpoint**, which asks the market.

## The solver — S1, Newton-Raphson

There is **no closed form**. You must search.

```
σ ← σ + (market price - model price) / vega
```

**The loop was never the fragile part — the seed is.** Two seeds, picked by moneyness:

| seed | formula | good where |
|---|---|---|
| Brenner-Subrahmanyam | `σ ≈ √(2π/T) · C/(D·F)` | at the money |
| Manaster-Koehler | `σ* = √(2·|ln(F/K)|/T)` | in the wings |

`measured`: all four solvers agree to **2.5e-05 vol points** — 4,000x inside target. **The solver choice does not matter.** Pick on cost.

## One volatility per strike, from the out-of-the-money leg

Calls above the forward, puts below.

**Why the OTM leg:** it holds no intrinsic value, so its whole price is time value and its **vega is largest**. The ITM leg prices the same number with most of its value insensitive to volatility — same answer, far worse conditioned.

Parity guarantees both legs share one volatility, so it is written to **both** legs of the row. `iv_leg` names the side it came from, so the repetition cannot be read as two independent solves.

## Three refusals, and none of them return a number

| situation | result |
|---|---|
| price below intrinsic | `iv: null` + reason — a broken quote, not a hard solve |
| no two-sided quote | `iv: null` + `NO_QUOTE` |
| vega too small to identify σ | `iv: null` |

**`iv` is `null` and never `0`.** A leg with no volatility carries **no Greeks either** — five plausible numbers at a default volatility describe nothing.

**Deeper:** [implied-vol.md](implied-vol.md)

---

# 3 · The Greeks

## What they are

The derivatives of the price. Once the volatility is recovered they are **closed-form** — no iteration, no search, just arithmetic.

**Which is why the expensive part is the volatility and the Greeks are nearly free.**

## The five

```
delta = Φ(d1)                     calls
        Φ(d1) - 1                 puts

gamma = φ(d1) / (F·σ·√T)

vega  = D·F·φ(d1)·√T   / 100

rho   =  D·K·T·Φ(d2)   / 100      calls
        -D·K·T·Φ(-d2)  / 100      puts

theta = price(T - 1/365) - price(T)
```

## The conventions are deliberately NOT all textbook

| Greek | convention |
|---|---|
| `delta`, `gamma` | **undiscounted** — no `D`, so delta is bounded by `[0,1]` not `[0,D]` |
| `vega`, `rho` | **discounted**, and **per one percent** (÷100) |
| `theta` | a **one-day repricing**, not the analytic derivative |

The asymmetry is the sibling platform's, not ours. Carried unchanged, because **a convention the desk does not use is one that has to be undone at every boundary.**

Graded against that platform to **2.2e-16** on delta and **1.1e-11** on theta.

## The trap — the clock, and it is the big one

The sibling runs a **252-trading-day year** in which nothing decays overnight or at weekends. Correct for an index that closes. **Wrong here** — crypto trades every day and this venue lists weekend expiries.

`measured` on the 25-09-2026 ATM call, 21.8 days out at 36.63%:

| step | theta |
|---|---|
| 1/252 | **−96.96 USD** |
| 1/365 | **−66.58 USD** |
| overstated by | **1.456x** |

Predicted 365/252 = 1.4484 — so the mechanism is understood, not guessed.

**Nothing crashes. Theta is just 46% wrong.** Black-76 does not care which calendar you use; it requires only that **time and volatility are quoted on the same one**.

Live cross-check: our theta's median **−76.05** against Delta's **−77.84** — agreeing to **0.6%**.

`tests/test_greeks.py` pins the **property** — that theta is the change over exactly one calendar day — not a stored value. A test recording today's theta to six decimals would pass just as happily on the wrong calendar.

## Two more things to know

**Delta is with respect to the FORWARD.** Delta's own published delta is with respect to **spot**. The two are recorded side by side and deliberately **not graded against each other**.

**The Greeks raise at expiry rather than returning zero.** At `T ≤ 0` gamma divides by a time scaling that has gone to zero, and theta is the change over a day that no longer exists. The *price* there is well defined; the exposures are not.

---

## Word list

| term | meaning |
|---|---|
| `F` | forward — the price the market implies for expiry |
| `K` | strike |
| `D` | discount factor — present value of 1 paid at expiry |
| `T` | years to expiry, **ACT/365** |
| `σ` | volatility, annualised, as a decimal (0.3663 = 36.63%) |
| `Φ` | standard normal **cumulative** distribution |
| `φ` | standard normal **density** |
| `d1`, `d2` | the two Black-76 intermediates |
| OTM | out of the money — no intrinsic value |
| ITM | in the money — has intrinsic value |

---

## The three rules that stop a wrong number looking right

1. **Never assume the forward.** `measured`: spot-as-forward disagrees with F1 by up to **1.626 vol points**.
2. **Time and volatility on the same calendar.** ACT/365 everywhere. A 1/252 step anywhere is a silent 46% error.
3. **Absent is `null`, never `0`.** No IV means no Greeks.

---

**Next action:** run the curl at the top and find one leg where `iv_reason` is not empty. That tells you what a refusal looks like in practice, and it takes about a minute.
