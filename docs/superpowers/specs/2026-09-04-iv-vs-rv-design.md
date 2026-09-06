# Implied against realised: one chart, one slider

**The question the stack was built to ask.** We now solve an implied volatility for every
strike every minute and fold the index price into one-minute bars beside it. Both halves of
the oldest question in options trading — *is the market's forecast of movement bigger than the
movement that actually arrived?* — are sitting in the same store, and nothing puts them on the
same axis.

**This is a measurement instrument, not a signal.** The chart's job is to make a gap visible
and honest, including where it cannot be computed. Every decision below was taken to stop a
plausible-looking number appearing where a real one does not exist.

---

## Problem Statement

Implied volatility and realised volatility are not comparable by default, and four separate
things have to be got right before putting them on one axis means anything.

**They describe different periods.** IV at time *t* forecasts the window *[t, t+N]*. Trailing
RV at time *t* measures *[t−N, t]*. Plotted raw against a shared x-axis, the two lines at any
point describe windows that do not overlap at all — 2N days apart end to end. This is how most
published IV-vs-RV charts are drawn and it is why the premium they appear to show is not the
premium.

**They are quoted on different clocks.** IV is a Black-76 parameter and is annualised by
construction, because the model only balances when σ and T share a calendar. RV is a sample
statistic over whatever bars you fed it. Comparing them requires an explicit choice of common
units, and the natural-seeming one — annualise both — hides the lookback inside a constant.

**RV is not one number.** Close-to-close, Parkinson, Garman-Klass and Rogers-Satchell estimate
the same quantity under assumptions that this asset violates in different ways. They will
disagree, and the disagreement is informative rather than a defect to average away.

**And the venue publishes neither series.** Delta has no volatility index — no DVOL
equivalent — so the IV side must be constructed. Its history carries no implied volatility and
no bid/ask at all (`docs/delta-api-scope.md` §7), so the IV side cannot be backfilled as IV;
only the index price can be backfilled.

## Solution

**One chart, two series, one control that drives both.**

A route in the web app plots a constant-maturity ATM implied volatility against rolling
realised volatility of the index. A single lookback **N** sets the RV lookback *and* the IV
index's tenor, so the two lines are always asking about the same length of time. A button
switches between contemporaneous and lag-aligned. A checkbox per estimator decides what is
drawn.

The instrument is honest about its own limits: it breaks the line rather than drawing across a
gap, bounds its own slider by the history that exists, and labels the sampling interval it
chose so the reader can see the decision rather than inherit it.

---

## The decisions

### 1. The IV series is ATM, constant-maturity, tenor N

At each timestamp, take the ATM implied volatility for every listed expiry and interpolate the
term structure to N.

**ATM only, not a variance-swap index.** DVOL and VIX integrate option prices across the whole
strike range weighted by `1/K²`, which captures the distribution rather than its centre and
rises when skew steepens even with ATM flat. Ours will track the level and be blind to shape.
That is a deliberate trade for an instrument whose subject is the level, and it carries one
obligation: **this number must never be labelled a DVOL equivalent**, because it will disagree
with a published DVOL print and the reason will not be a bug.

**Interpolated in total variance (σ²T), never in volatility.** Variance is additive across
time; volatility is not. A straight line drawn between two volatilities is a curve in the
quantity that actually accumulates, and it can imply negative forward variance between two
expiries — a term structure that can be arbitraged against itself. CBOE's VIX and Deribit's
DVOL both interpolate in total variance for this reason.

**ATM interpolated between the bracketing strikes, not snapped to the nearest.** Delta lists
strikes on a fixed grid and the forward almost never sits on one. Taking the nearest strike
makes the series step discretely every time the forward crosses a strike midpoint — not
because volatility moved but because a different contract started being read. On a smile with
curvature that is a visible sawtooth of pure measurement artifact. Reading both neighbours and
weighting by the forward's position between them costs one extra contract and removes it.

**The expiry roll is weighted to zero.** As time passes, the near expiry leaves the bracket. If
the weights are continuous, the near expiry's contribution reaches zero exactly as it exits and
there is no step. Rolling early or late produces one. **No expiry under 7 days enters the
index** under any circumstance: near expiry an option's implied volatility becomes extremely
noisy, and this repo has already measured the front expiry's own forward fit implying a 43.1%
rate at 1.139 days — outside its own 0–30% gate. VIX excludes options under a week for exactly
this reason. With a bracket around N ≥ some floor the rule should never bind, so it is written
as an assertion: if it ever binds we find out rather than charting noise.

### 2. The RV series is five estimators on the index price

**On `spot_price`**, which is Delta's index — the series the options settle against. RV of a
perpetual or of one spot venue would measure a different asset and call the difference a risk
premium.

**Five estimators, each independently selectable**: simple-return and log-return rolling
standard deviation, Parkinson, Garman-Klass, Rogers-Satchell. Formulas from
`docs/realised-volatility.md`.

**Yang-Zhang is excluded.** Its entire contribution is the overnight term
`σ²_overnight = Var(ln(Oᵢ/Cᵢ₋₁))`, which requires a market that closes. Crypto trades
continuously, so that term collapses toward zero and the estimator degenerates into a more
expensive Rogers-Satchell. With no overnight gaps, Rogers-Satchell is already the appropriate
choice and Yang-Zhang buys nothing.

**GARCH is excluded, and for a different reason.** The five above are *estimators* — they
summarise bars that already happened, with no parameters to fit. GARCH(1,1) is a *model* with
three parameters fitted by maximum likelihood that produces a forecast. That makes it the same
kind of object as IV, not the same kind as RV, and putting it on this chart would be a
model-versus-market comparison wearing the clothes of a market-versus-realisation one. It is
the obvious next line and it is not this ticket.

**The mean is subtracted**, following `docs/realised-volatility.md`. This departs from
variance-swap convention and the departure is deliberate — see Open Questions.

### 3. Units: both expressed over the lookback window

Neither series is annualised. IV is scaled down from its annualised solve by `√(N/365)`; RV is
the window standard deviation with no scaling up. The chart reads *"the market expected an
11.5% move over N days; it delivered 9%."*

**Why not annualise.** Annualising is the convention, but it hides N inside a constant — two
charts at different lookbacks produce identical-looking axes describing different questions.
Expressing over the window puts the control's value into the numbers.

**Why 365 and not 252** wherever a year appears: crypto trades weekends and this venue lists
weekend expiries. This repo measured a 1/252 year overstating theta by **1.456x**. Deribit's
DVOL agrees, dividing by 19 ≈ √365.

**One scaling function, taking the interval as an argument, shared by every estimator.** The
252 hardcoded in `docs/realised-volatility.md` is wrong for this asset class, and a literal
anywhere lets two lines on one chart silently disagree about the calendar.

### 4. The controls

| Control | Behaviour |
|---|---|
| **Lookback N** | Slider/textbox. Drives the RV lookback *and* the IV tenor. Bounded dynamically |
| **Sampling interval** | A choice of valid intervals *for the current N*, not one derived value |
| **Estimator checkboxes** | One per RV estimator plus one for IV. Up to six lines |
| **Alignment** | Button toggling contemporaneous against lag-aligned |

**N's bounds are computed, not fixed.** Lower by the observation-count floor — below some N no
sampling interval yields enough returns for a stable estimate. Upper by the smaller of the
listed term structure (~86 days; beyond it the interpolation becomes extrapolation and is
refused) and *the history actually held*. In the first month the third constraint binds, and
the UI must say so — otherwise an empty chart reads as a bug every time.

**The sampling interval is offered, not imposed.** RV from 1-minute returns is biased upward by
microstructure noise; the classic result (Andersen et al. 2001, Bandi–Russell 2005,
Hansen–Lunde 2005) puts the bias/variance optimum near 5 minutes for equities. Our series is a
*computed index* rather than a traded price, so its noise profile may differ and the number
should be measured here rather than inherited. The chosen interval and the resulting count are
displayed — *"30d window, 5m sampling, 8,640 returns"* — so an invisible decision becomes a
visible one.

### 5. Alignment

**Contemporaneous by default, lag-aligned behind the button.** Contemporaneous is what traders
read and what every vendor draws. But **any panel reporting the premium as a number must use
the lag-aligned pair**, because the contemporaneous spread is between two non-overlapping
windows. Deribit's own study shifts IV back before measuring, finding 30-day IV exceeding
subsequent 30-day RV about 70% of the time and by ~15 points in contango.

In lag-aligned mode the most recent N days have no RV to compare against — the window has not
finished happening. That is rendered as an honest blank, deliberately, not as a crash.

### 6. Computed on demand

No new store table. The engine reads `spot-bars` and the IV inputs and computes at whatever N
is asked for.

The performance argument for precomputing does not survive the numbers: a full 63-strike
forward fit, IV solve and Greeks pass measures at **1.38 ms** (`docs/greeks.md` §7), and RV
over a window is one pass of arithmetic that Polars does in low milliseconds. Precomputing
would also mean a fixed set of lookbacks, which makes the slider a three-position switch, and
would freeze a methodology that is still moving.

---

## Open questions, carried deliberately

- **RV subtracts the mean; variance swaps do not.** We follow
  `docs/realised-volatility.md` and use mean-subtracted sample standard deviation. IV is the
  market's price for `E[∫σ²dt]`, which variance swaps settle **without** mean subtraction. The
  gap between our two lines therefore contains a drift term that is not a risk premium —
  negligible at short windows, growing with the window and with trending markets, which is
  precisely when the chart is interesting. Chosen knowingly; filed as its own issue.

- **The index price history is unverified.** `docs/delta-api-scope.md` line 13 removed
  perpetuals and index series from its scope, so nobody has checked what
  `/v2/history/candles` returns for an index symbol — and that endpoint is the one that pads
  empty buckets with the last trade without saying so. Three symbol spellings were tried on
  2026-09-04 and all timed out. **If no index candle series exists, RV is limited to
  `spot-bars` going forward** and the chart's range is bounded by when the engine started.

- **`spot_price` versus `settlement_index_price` is unconfirmed.** `docs/settlement.md` does
  not establish they are the same series. If they differ, RV measures a slightly different
  asset than IV implies.

- **Historical IV is assumed available and is not.** Delta's history carries no IV and no
  bid/ask. Reconstructing it means pulling `MARK:` per contract and inverting it — which
  recovers Delta's model surface, not the market's, the unknown `docs/implied-vol.md` §6 calls
  the largest in that ticket. Parked by decision; the live series accumulates from
  2026-09-03 regardless.

- **The chart draws nothing across a gap.** Never-forward-fill means a missing minute produces
  no row, so the line breaks with no indication whether the hole is one minute or one hour, and
  no signal when a window was computed from partial coverage. Filed as its own issue.

## Out of scope

Skew and term-structure surfaces; the variance-swap index; GARCH and any fitted forecast;
trading signals or backtests over the spread; ETH, which is not on the live feed or in the
store.

## Tickets

Parent: **#24**.

| | | | Landed as |
|---|---|---|---|
| **R1** | [#25](https://github.com/lalitkarthik/delta-exchange-payoff/issues/25) | Probe the index price history, and write down what it actually serves | `tools/probe_index_history.py`, `docs/index-history.md` — probe built, **venue unreachable from this machine, recorded as unreachable and not as absent** |
| **R2** | [#26](https://github.com/lalitkarthik/delta-exchange-payoff/issues/26) | The five estimators, pure and test-first | `engine/src/deltapayoff/realised_vol.py` |
| **R3** | [#27](https://github.com/lalitkarthik/delta-exchange-payoff/issues/27) | The constant-maturity ATM IV index | `engine/src/deltapayoff/iv_index.py` |
| **R4** | [#28](https://github.com/lalitkarthik/delta-exchange-payoff/issues/28) | The endpoint that serves both series at one N | `engine/src/deltapayoff/volatility.py`, `GET /volatility` and `/volatility/bounds` |
| **R5** | [#29](https://github.com/lalitkarthik/delta-exchange-payoff/issues/29) | The screen: one chart, one slider, six checkboxes | `web/app/volatility/page.tsx`, `web/components/VolatilityChart.tsx` |

Findings from building them: [`docs/iv-vs-rv.md`](../../iv-vs-rv.md).

**Two decisions the build changed.** The design said nothing about how the endpoint would
compute a series of windows; recomputing each one from scratch costs ~15 ms a point over a
14-day window of minutes, so a chart would take 27 seconds and never draw, and the estimators
gained a prefix-sum rolling form. And the design's units rule — IV scaled by `sqrt(N/365)` —
was missed in the first implementation and caught by looking at the built chart, where a 40%
implied line sat above a 7% realised one.

Carried as open questions rather than tickets: **#30** (RV subtracts the mean) and **#31**
(the chart draws nothing across a gap).

R1 blocks nothing but changes R2's data source; R2 and R3 are independent; R4 needs both;
R5 needs R4.
