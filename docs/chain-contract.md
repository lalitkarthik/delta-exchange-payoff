# The `/chain` contract

The interface between the engine and the web app. Both are built against this file, so it is
fixed before either exists. Change it by changing this file first.

Delta has **no option-chain endpoint**. A chain is `GET /v2/tickers` filtered by underlying and
expiry, with the rows pivoted so a call and a put sharing a strike land on one line. The engine
does that pivot. The web app does no arithmetic at all.

## `GET /expiries?underlying=BTC`

Populates the expiry dropdown. Underlying is `BTC` or `ETH`.

```json
{
  "underlying": "BTC",
  "expiries": ["02-09-2026", "03-09-2026", "04-09-2026", "11-09-2026"]
}
```

Ascending by date. Format is `DD-MM-YYYY`, matching what Delta's `expiry_date` filter expects —
no reformatting anywhere in the stack.

## `GET /chain?underlying=BTC&expiry=04-09-2026`

```json
{
  "underlying": "BTC",
  "expiry": "04-09-2026",
  "spot": 77543.0,
  "atm_strike": 77500.0,
  "fetched_at": "2026-09-01T09:21:04Z",
  "forward": 77609.4,
  "discount": 0.99961,
  "years_to_expiry": 0.008522,
  "forward_method": "F1",
  "rows": [
    { "strike": 77000.0, "call": { }, "put": { } }
  ]
}
```

`rows` is ascending by strike. `atm_strike` is the listed strike closest to `spot` — a lookup,
not a model. Either side of a row may be `null` when only one of the pair is listed.

`forward` is **recovered from prices**, not assumed: an ordinary least-squares fit of `C - P`
against `K` across every paired strike, whose slope is `-D` and whose zero crossing is `F`.
`forward_method` names it — `F1` is that regression. `years_to_expiry` is ACT/365, and it is
the clock both the volatility and the Greeks below are quoted on.

`forward_method` takes one of three values, and it matters which:

| `F1` | The regression fitted both the forward and the discount, and both passed the gate. |
| `F1+assumed-rate` | The regression's forward, with the **discount assumed at 6.5%**. |
| `F2` | Parity inverted at the money strike alone, discount assumed. Used on chains too sparse to fit a line. |

**`F1+assumed-rate` is the common case near expiry, and it is not a degraded answer.** The
regression recovers the forward and the discount from *different features of one line* — the
forward is where `C - P` crosses zero, the discount is the line's slope — and those have very
different noise. `docs/forward.md` §4 measured it: across window choices the forward spans
$1.23 on a $77,590 number while the implied rate runs −17.1% to +9.4%. A crossing is an
interpolation inside the strike range and noise barely moves it; a slope is a tilt measured
across that range, where a small error becomes a large one in `D`.

Under a day to expiry the true discount is within a few parts per hundred thousand of 1, so
its implied rate is quote noise — measured on the live 04-09-2026 chain a second apart, the
fitted discount moved between 0.99997892 and 1.00001939, taking the rate from +1.03% to
−0.95%. So a failed gate discredits the **discount**, not the forward: the forward stands and
the discount is assumed. The 6.5% is borrowed from `payoff-project`, not measured — there is
no risk-free rate for BTC — and the method name says so rather than passing it off as a fit.

**All four fields are `null` together only when no method works at all**, and then no leg
carries a computed volatility either. That takes a chain both too sparse to fit a line and
unquoted at the money. Pricing against a forward we do not believe would produce a ladder of
plausible, uniformly wrong numbers with nothing to signal it.

### A leg

```json
{
  "symbol": "C-BTC-77000-040926",
  "product_id": 138742,
  "bid": 1234.5,
  "ask": 1240.0,
  "mark": 1237.0,
  "bid_iv": 0.3701,
  "ask_iv": 0.3760,
  "mark_iv": 0.3730,
  "delta": 0.52,
  "gamma": 0.000031,
  "theta": -88.4,
  "vega": 41.2,
  "rho": 12.9,
  "oi": 148.0,
  "oi_value_usd": 1150400.0,
  "tick_size": 0.5,
  "computed": {
    "iv": 0.3712,
    "iv_leg": "call",
    "iv_reason": "",
    "delta": 0.5231,
    "gamma": 0.0000312,
    "vega": 0.4118,
    "theta": -66.58,
    "rho": 0.1290
  }
}
```

Everything outside `computed` is **Delta's own figure, passed through untouched**. Everything
inside `computed` is ours. The two sit side by side on purpose: Delta republishes its
volatility every 5,001 ms while the book beneath it moves every 508 ms, so ours is up to 9.8x
fresher, and the comparison is only possible while both are present. **Ours are added, never
substituted.**

### `computed`

`iv` is a property of the **strike**, not of the leg. Put-call parity gives both legs one
volatility, and it is recovered by inverting the **out-of-the-money** leg's bid/ask midpoint —
calls above the forward, puts below — because that leg holds no intrinsic value, so its whole
price is time value and its vega is largest. The same number therefore appears on both legs of
a row, and `iv_leg` names the side it came from so the repetition cannot be read as two
independent solves.

`iv_reason` is empty when solved and otherwise says why not. **`iv` is `null` and never `0`**,
and a leg with no volatility carries no Greeks either: reporting Greeks at some default
volatility would put five plausible numbers on screen that describe nothing.

**The Greek conventions are not all textbook.** They are carried unchanged from the sibling
project's implementation, which is graded against its platform's own Greeks to 2.2e-16 on delta:

| `delta`, `gamma` | **undiscounted** — delta is bounded by `[0, 1]`, not `[0, D]` |
| `vega`, `rho` | discounted, and quoted **per one percent** |
| `theta` | a **one-calendar-day repricing**, not the analytic derivative |

The asymmetry is the sibling platform's rather than ours, and it is kept so one implementation
serves both projects. `delta` is with respect to the **forward**; Delta's own `delta` above is
with respect to **spot**, so the two are recorded side by side and not graded against each
other.

**Theta is one calendar day.** The sibling runs a 252-trading-day year in which nothing decays
at weekends, which is right for an index that closes and wrong here — crypto trades every day
and this venue lists weekend expiries. Measured: a 1/252 step overstates theta by **1.456x**.

## Rules that are not negotiable

**Every decimal is a JSON number or `null`. Never a string.** Delta sends decimals as strings to
preserve precision. The engine converts once, at the boundary. The web app never calls
`parseFloat`.

**Absent means `null`, and `null` is not zero.** A missing `best_bid` means nobody is bidding.
Rendering that as `0.0` claims someone bid zero. Delta also returns the string `"0"` for absent
quotes on some fields — that is still `null` here. `impact_mid_price` is genuinely `null` on
illiquid strikes and is deliberately not in the contract.

**IV is a decimal fraction, not a percentage.** `mark_iv: 0.3730` is 37.30%. The web app
formats; the engine never multiplies by 100.

**`spot` is Delta's top-level `spot_price`.** A ticker also carries `greeks.spot`, and the two
disagree — 78111.9 against 78112.5 on a measurement taken 2026-08-31. That is not rounding. This
project uses `spot_price` everywhere and does not expose `greeks.spot`.

**Greeks are Delta's numbers, passed through unchanged.** We are displaying them, not yet
checking them. When payoff computation lands they become a value to verify against our own, not
an oracle.

**Production market data only, and no API key.** Base URL `https://api.india.delta.exchange`.
Public endpoints need no authentication. Testnet lists strike ladders that no longer span spot
and only 62.6% of its options carry a bid, so nothing is computed on it.

## Errors

FastAPI default shape, `{"detail": "..."}`.

| Status | When |
|---|---|
| 400 | `underlying` is not `BTC` or `ETH`, or `expiry` is not `DD-MM-YYYY` |
| 404 | Delta returned no contracts for that underlying and expiry |
| 502 | Delta was unreachable, timed out, or returned `success: false` |

Upstream requests carry a `User-Agent` — Delta returns 4XX without one — and a 10-second
timeout.
