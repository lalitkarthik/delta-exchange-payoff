# Implied against realised: what was built, and what it measured

The instrument for the oldest question in options trading — *is the market's forecast of
movement bigger than the movement that actually arrived?* — and the numbers that came out of
building it.

Parent study [#24](https://github.com/lalitkarthik/delta-exchange-payoff/issues/24). R1's
findings are in [`index-history.md`](index-history.md); this document covers R2 through R5.

Every claim is tagged. **Measured** names the run. **Assumed** says so. There is no third
category, and in particular no figure here comes from a paper without being re-derived
against this venue's data.

## Do this next

1. **Run `probe_index_history.py` from a machine that can reach Delta.** Everything about
   the realised side's *range* rests on it, and it has never been run successfully.
2. **Check the effective spot sample count per minute.** §4 below finds the range estimators
   reading roughly half the close-to-close ones on real data, and the leading hypothesis is
   that a minute holds about **14** distinct spot observations rather than the 8,256 frames
   `spot_ticks` counts. If that is right, Parkinson, Garman-Klass and Rogers-Satchell are
   biased low on this series by a knowable amount.
3. **Leave the engine running.** The upper bound on `N` is the history held, and on
   2026-09-06 that is thirty minutes.

---

## 1. What was built

| | Module | What it is |
|---|---|---|
| R2 | `engine/src/deltapayoff/realised_vol.py` | Five estimators, the window scaler, the roll-up, and a rolling form |
| R3 | `engine/src/deltapayoff/iv_index.py` | Constant-maturity ATM implied volatility |
| R4 | `engine/src/deltapayoff/volatility.py` | The two series joined, plus `GET /volatility` and `/volatility/bounds` |
| R5 | `web/components/IvRvPanel.tsx` | The volatility section's second tab: one chart, one slider, six checkboxes |

**It is a tab, not a screen of its own.** The rail's VOL entry already carried a disabled
`IV vs RV — soon` tab beside `Smile`; this fills it. The two are views of one subject and
the underlying picker lives once, in `VolatilityHeader`, so they cannot disagree about
which series is on screen. The tab is in the URL as `?tab=iv-rv`, because a tab is part of
what someone means when they send a link.

All of R2, R3 and R4's core are pure — data in, data out, no socket, no clock, no
filesystem — in the manner of `forward.py` and `bars.py`. `store.py` gained the read path
and `main.py` the two routes, which are the only two modules allowed outside.

## 2. The estimators agree, and the tolerance is 3.85%

**Measured**, `tests/test_realised_vol.py::test_all_five_agree_on_a_series_of_known_volatility`:
1,440 bars of a geometric Brownian path at a planted 0.05% per bar, sub-sampled 2,000 times
inside each bar, over eight seeds.

| | |
|---|---:|
| Worst relative error of any estimator against the planted volatility | **3.85%** |
| Widest spread between the highest and lowest of the five on one path | **5.05%** |
| Assertion in the suite | 6% |

The 6% is the measured figure with headroom for the seed lottery, and it is tight enough that
an estimator wrong by a factor would fail. It is written down rather than guessed, and the
test re-measures it on every run.

## 3. Range estimators are biased low, and the bias is a property of the *sampling*

This is the most useful thing the build produced, and it was not what the ticket expected.

A range estimator reads the high and low it can **see**. A finitely sampled path never
reaches the extremes a continuous one does, so Parkinson, Garman-Klass and Rogers-Satchell
come in low — by an amount that has nothing to do with the estimator and everything to do
with how many observations went into the bar.

**Measured**, 720 bars, 20 seeds, varying only the sub-samples per bar. Mean relative error:

| Samples per bar | Simple | Log | Parkinson | Garman-Klass | Rogers-Satchell |
|---:|---:|---:|---:|---:|---:|
| 1 | −0.9% | −0.9% | **−40.4%** | −66.6% | **−100.0%** |
| 5 | +1.0% | +1.0% | −24.1% | −36.5% | −39.2% |
| 20 | +0.7% | +0.7% | −13.4% | −19.5% | −20.2% |
| 60 | +0.1% | +0.1% | −8.1% | −11.5% | −11.7% |
| 240 | +0.3% | +0.3% | −4.1% | −5.8% | −6.0% |
| 1,000 | +0.1% | +0.1% | −2.2% | −3.1% | −3.2% |
| 4,000 | −0.6% | −0.6% | −1.4% | −1.7% | −1.6% |

The two close-to-close estimators are unaffected — they only ever read the close, which is
exact at any sampling rate. The three range estimators converge on the truth as the sampling
becomes continuous, and the bias goes as roughly **−0.63 / √n** for Parkinson.

At one sample a bar Rogers-Satchell reads exactly **zero**, because every one of its four
logarithms collapses. That is the estimator behaving correctly on a bar that carries no range
at all, and it is the clearest possible statement of what these three actually measure.

**Why this matters far beyond the test.** It means a range estimator's answer depends on
*which source built the bar*. That is exactly the question `index-history.md` §4 asks about
Delta's candles: a candle built from trades might hold a few dozen observations a minute
where our `spot-bars` hold thousands, and the same formula on the same minute would then
read 10% lower on one source than the other. That would appear on the chart as a difference
in volatility, and it would not be one.

## 4. On real data the range estimators read *half* the close-to-close ones

**Measured** 2026-09-06 against `data/spot-bars` — all thirty minutes of it, 1-minute
sampling, expressed over the 31-minute window:

| Estimator | Per bar | Over the window | Observations | Coverage |
|---|---:|---:|---:|---:|
| Simple returns | 4.277e-04 | 0.002382 | 28 | 0.903 |
| Log returns | 4.278e-04 | 0.002382 | 28 | 0.903 |
| Parkinson | 2.480e-04 | 0.001381 | 30 | 0.938 |
| Garman-Klass | 2.080e-04 | 0.001158 | 30 | 0.938 |
| Rogers-Satchell | 2.064e-04 | 0.001149 | 30 | 0.938 |

Two things to read off it.

**Simple and log agree to 2.55e-05 relative** — `ln(1+x) ≈ x` for small `x`, exactly as the
ticket predicted, and it is free reassurance that the bars are sane rather than a redundancy.

**The spread between the highest and lowest is 107%**, with all three range estimators
sitting at roughly half the close-to-close pair. That is well outside the ±47% the simulation
gives at 30 bars, so it is unlikely to be sampling noise alone.

**Leading hypothesis, assumed and not measured: a minute holds about 14 distinct spot
observations, not 8,256.** `spot_ticks` counts *ticker frames*, and every one of ~588
contracts' frames carries the **same** `sp` — so 8,256 frames a minute is roughly `8256/588
= 14.04` distinct prices. At 14 samples a bar the table in §3 predicts a Parkinson bias near
−17%, which is the right sign and a good part of the size. The remainder would be 30-bar
sampling noise.

This is falsifiable and worth falsifying: count the distinct `sp` values arriving in a minute
off the raw frames. If it holds, the three range lines on the chart are biased low by a
knowable amount on this data source, and the fix is either to correct them or to say so on
screen. **It is not a reason to drop them** — the bias is a constant factor, so the *shape*
they carry is still the shape, and the disagreement between them is still informative.

## 5. The strike interpolation earns its keep only at the front

R3 interpolates the ATM level between the two strikes bracketing the forward rather than
snapping to the nearest. **Measured** against `data/computed-bars` — 31 minutes, 8 expiries,
193 `(minute, expiry)` pairs that solved:

| | |
|---|---:|
| Mean absolute difference, interpolated against nearest-strike | 0.001058 |
| As a share of the ATM level | **0.279%** mean, **2.49%** max |
| ATM IV level over the sample | 0.345 … 0.434 |

So the two methods barely differ in **level**. The difference is in the **jitter** — the
sawtooth interpolation exists to remove. Mean absolute minute-to-minute change in the ATM
level, per expiry:

| Expiry | Days out | Interpolated | Nearest strike | Nearest ÷ interp |
|---|---:|---:|---:|---:|
| 04-09-2026 | 0.26 | 0.002044 | 0.004477 | **2.19** |
| 05-09-2026 | 1.26 | 0.000919 | 0.001742 | **1.90** |
| 06-09-2026 | 2.26 | 0.000650 | 0.001133 | **1.74** |
| 11-09-2026 | 7.26 | 0.000248 | 0.000145 | 0.58 |
| 18-09-2026 | 14.26 | 0.000166 | 0.000081 | 0.49 |
| 25-09-2026 | 21.26 | 0.000216 | 0.000162 | 0.75 |
| 30-10-2026 | 56.26 | 0.000062 | 0.000132 | 2.14 |
| 27-11-2026 | 84.26 | 0.000123 | 0.000131 | 1.07 |

**Interpolation halves the jitter at the front and roughly doubles it in the belly.** At
0.26–2.26 days the forward moves fast across the strike grid and nearest-strike sawtooths
exactly as predicted. At 7–21 days the forward barely crosses a strike, so nearest-strike sits
still on one contract while interpolation picks up its neighbour's noise as the weight moves.

**And the front expiries are the ones the seven-day floor already excludes.** So the
sawtooth interpolation was designed to fix is largest precisely where the index never looks,
and in the range the index does use it is the *noisier* of the two choices.

That is an argument against the interpolation on this evidence, and it is deliberately not
being acted on: **31 minutes and 23–28 points per expiry is not enough to decide it.** It is
recorded here so that it is re-measured on a real span rather than rediscovered. What is not
in doubt is the level agreement — whichever is chosen, the index moves by well under a
volatility point.

## 6. Total variance, not volatility, and the roll is continuous

Two decisions pinned by test rather than by comment.

**Interpolating in total variance rather than volatility changes the answer by 9%** on the
worked example: 14 days at 60% and 42 days at 40%, asked at 28 days, gives **0.4583** in
total variance and **0.5000** as a straight line between the two volatilities. Variance is
additive across time and volatility is not, so the naive line is a curve in the quantity that
accumulates, and between two expiries it can imply *negative forward variance* — a term
structure that can be arbitraged against itself. VIX and DVOL both interpolate in total
variance.

**The expiry roll moves the index by 7.5e-05 across the hand-over.** A ladder at 16, ~30 and
58 days asked at 30: just before the middle expiry crosses the tenor it is the far bracket
and the 16-day expiry carries a weight of **0.00071**; just after, the middle expiry is the
near bracket carrying **0.99964** and the 16-day expiry has gone. The departing expiry's
contribution reaches zero exactly as it exits, so the roll is a hand-over and not a step.

## 7. Recomputing every window is the wrong algorithm, by a factor of eighteen

**Measured** on a 20-day synthetic store, 1-minute bars:

| | |
|---|---:|
| Cost of one point, 14-day window, four estimators, recomputed | **~15 ms** |
| Implied cost of a 1,728-point chart | **~27 s** |
| Same chart with prefix sums | **1.5 s** |

Every one of the five estimators is a sum over its window, so a rolling form reduces each
window to a subtraction of prefix sums — O(1) per point instead of O(window). Without it the
screen simply never draws at the default settings, which makes this a correctness problem
wearing a performance problem's clothes.

It is a second implementation of five formulas and therefore a second thing that can be wrong
about them, so `test_rolling_agrees_exactly_with_the_window_at_a_time_functions` asserts the
two paths equal — for every estimator, at every timestamp, on a series with two holes in it.
The tolerance is `1e-12` relative, which is the reassociation of the arithmetic and nothing
else.

**The response-size question the ticket raised is answered by computing at fewer timestamps,
not by discarding points.** Each point still rests on its own full window, so this is a
coarser *reading* of the same rolling estimate rather than a downsampling of it — which the
ticket rightly flagged would not be the same thing. The step used is reported as
`step_seconds` and shown on screen, because a cap nobody is told about reads as having
covered everything.

## 8. The units, and why both series are on the same axis at all

Implied volatility is a Black-76 parameter and is **annualised by construction** — the model
only balances when σ and T share a calendar. Realised volatility is a window standard
deviation. Serving the first raw beside the second puts a 40% line above a 7% line and
invites the whole gap to be read as a risk premium when all of it is a unit mismatch. This
was caught by looking at the built chart, not by the tests, and the fix is one line:
`σ × √(N/365)`.

**Neither series is annualised.** The chart reads *"the market expected an 11.5% move over N
days; it delivered 9%"*. Annualising is the convention and it hides `N` inside a constant, so
two charts at different lookbacks would carry identical-looking axes while answering
different questions.

**A year is 365 days.** Crypto trades weekends and this venue lists weekend expiries; a
1/252 year was measured overstating theta by **1.456x** (`greeks.md`). Using 252 here would
inflate every implied point by `√(365/252)` = **1.204** — a 20% premium conjured out of a
calendar. `test_a_year_is_365_days_here_and_not_252` pins it, and
`test_no_calendar_literal_lives_in_the_estimator_module` pins that neither number may be
spelled inside `realised_vol.py` at all: the interval is an argument to `scale_to_window`,
which is the only place time enters.

## 9. What the instrument refuses, and why that is the feature

| Situation | What happens |
|---|---|
| `N` above the history held | 400 naming the bound and the binding constraint |
| `N` below the shortest listed expiry | 400 — there is no implied volatility to compare with |
| `N` past the longest listed expiry | Refused; the index would be extrapolating |
| No `N` works at all | `/volatility/bounds` answers 200 with `usable: false` and prose |
| An expiry under 7 days | Excluded, and the count is reported in `excluded_under_floor` |
| A minute with no bars | No row; the window loses observations and `coverage` says so |
| A window with no observations at all | `null`, never `0` |
| Lag-aligned, trailing `N` | Key present, value `null`, point not omitted |
| A gap on screen | The line breaks. It does not join and does not interpolate |

**As it stands on 2026-09-06 the real store answers 400 to everything**, and the answer is:

> no lookback works yet at 1m sampling: the lower bound is 7.24 days and the upper is 0.02.
> limited by available history: 0.02 days of bars are held, against a term structure reaching
> 84.26 days. Lower bound set by the shortest listed expiry, 7.24 days.

That is the correct behaviour and it is the whole design working. The screen prints it rather
than rendering an empty chart.

## 10. Still open

- **[#30](https://github.com/lalitkarthik/delta-exchange-payoff/issues/30) — RV subtracts the
  mean; variance swaps do not.** We follow `realised-volatility.md`. The gap between the two
  chart lines therefore contains a drift term that is not a risk premium: negligible at short
  windows, growing with the window and with trending markets, which is precisely when the
  chart is interesting. Chosen knowingly.
- **[#31](https://github.com/lalitkarthik/delta-exchange-payoff/issues/31) — the chart says
  nothing about coverage.** The payload carries it per point per estimator; the screen does
  not yet draw it. A window computed from 60% coverage currently looks identical to one from
  100%.
- **The index price history is verified, and argues these estimators read the wrong source.**
  [`index-history.md`](index-history.md) — `.DEXBTUSD` does not pad and reaches back ~2.7
  years, but its per-minute range is wider than ours on **16 of 16** overlapping minutes.
  Feeding the range estimators the venue's bars instead of `spot-bars` is the cheapest test
  of the discretisation hypothesis in §4.
- **`spot_price` versus `settlement_index_price` is unconfirmed.** If they differ, realised
  volatility measures a slightly different asset than implied volatility implies, and the
  wedge sits inside the premium unlabelled.
- **Historical IV cannot be backfilled.** Delta's history carries no IV and no bid/ask, so
  the implied side accumulates forward from 2026-09-03 and no earlier.
- **The chart is hand-rolled SVG while the smile screen uses `recharts`.** Two charting
  approaches in one app is a real cost. This one was finished before `recharts` landed, and
  the argument that justified it — that a library would join across a gap — does not hold:
  recharts defaults `connectNulls` to false. Porting it is worth doing.
- **The optimal sampling interval has not been measured here.** The literature puts the
  bias/variance optimum near five minutes for equities; our series is a computed index rather
  than a traded price, so the number should be measured on this data. Until it is, the
  interval is offered rather than imposed and the screen says which was chosen.
