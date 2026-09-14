# The `/analyse` contract

The interface between the engine and the payoff screen. Both are built against this file, so
it is fixed before either exists. Change it by changing this file first.

> `docs/chain-contract.md` is the ladder's equivalent and the precedent for everything below:
> the leg vocabulary, the "every decimal is a JSON number" rule and the Greek conventions all
> come from there and are not restated in full. `web/lib/payoff.ts` mirrors this file field
> for field; if the two disagree, the mirror is wrong.

`POST /analyse` takes an ordered list of legs and returns **everything** about that strategy in
one response — the curve, the metrics, the per-leg and total Greeks, the payoff table, and the
forward, discount and spot it was all priced against. It is deliberately fat. Splitting it
would mean several round trips carrying the same legs and recomputing the same curve, and the
trader would watch the numbers arrive after the chart they belong to.

Nothing here is stored. A payoff is a view over data already recorded: no new table, no event.

## A leg

```json
{
  "instrument": "DELTA-BTC-20260904-77000-C-USD",
  "direction": 1,
  "quantity": 2,
  "entry_price": 1240.0
}
```

**`instrument` is the canonical string**, `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY`.
`events.instrument.Instrument.from_canonical` is the **single validator**; a malformed string
fails at the boundary naming the part that was wrong. The currency is required and is not
optional (#60/I1). The web side builds one with `canonicalInstrument` in `web/lib/instrument.ts`
and there is no second builder.

A strike and a side do not name a contract — 77000 C trades in every series at once — which is
why the leg carries the whole string rather than a strike and a right.

**`direction` is `+1` (bought) or `-1` (sold), and `quantity` is a positive integer.** The two
are separate fields, never one signed quantity: they produce the same curve today and disagree
the first time anything sums or renders a quantity.

**`entry_price` is optional and is USD per one unit of the underlying**, on the same scale as
the ladder's `bid` and `ask`. Absent, the engine fills it from the book and crosses the spread —
**buy at the ask, sell at the bid**. Never the mid, which is a trade nobody can make; never
`ltp`, which on this venue is the close of a rolling 24-hour candle republished on every frame;
never the mark, which is Delta's own model output. A leg with nothing on its side and no
`entry_price` supplied refuses the whole analysis (below) rather than being priced from the
other side.

## The request

```json
{
  "legs": [ { "instrument": "...", "direction": 1, "quantity": 1 } ],
  "as_of": "2026-09-04T09:21:00Z"
}
```

`legs` is **ordered** — the order the trader built them in, which is the order the per-leg
Greeks table is read in — and must hold at least one leg.

**`as_of` is optional. Absent means live**: the strategy is priced against the chain cache as it
stands. Present, it is a stored minute, ISO 8601 UTC with second precision and a `Z` suffix, the
exact spelling `/chain/minutes` publishes and `/chain/at` accepts. A link naming a minute is a
fixed set of numbers and never re-asks; a link naming none re-asks on a timer.

Unknown fields are **rejected, not ignored** — on the request, on each leg, and on every
shape of the response below. Inbound, a client's typo must not be quietly discarded into a
default; outbound, the payoff core builds these objects directly, so a mistyped keyword there
would drop a whole section and still answer 200.

## The response

```json
{
  "underlying": "BTC",
  "expiry": "04-09-2026",
  "as_of": "2026-09-04T09:21:00Z",
  "spot": 77543.0,
  "forward": 77609.4,
  "discount": 0.99961,
  "contract_value": 0.001,
  "legs": [
    {
      "instrument": "DELTA-BTC-20260904-77000-C-USD",
      "direction": 1,
      "quantity": 1,
      "entry_price": 1240.0,
      "iv": 0.3712,
      "greeks": { "delta": 0.5231, "gamma": 0.0000312, "vega": 0.4118,
                  "theta": -66.58, "rho": 0.129 }
    }
  ],
  "total_greeks": { "delta": 0.5231, "gamma": 0.0000312, "vega": 0.4118,
                    "theta": -66.58, "rho": 0.129 },
  "curve": {
    "corners": [ { "price": 74000.0, "pnl": -1240.0 },
                 { "price": 77000.0, "pnl": -1240.0 },
                 { "price": 81000.0, "pnl": 2760.0 } ],
    "slope_left": 0.0,
    "slope_right": 1.0,
    "window": { "low": 74000.0, "high": 81000.0 }
  },
  "metrics": {
    "max_profit": null,
    "max_loss": -1240.0,
    "breakevens": [78240.0],
    "net_premium": 1240.0,
    "reward_risk": null
  },
  "table": [ { "price": 74000.0, "pnl": -1240.0 } ]
}
```

`underlying`, `expiry` and `as_of` are echoed: `expiry` in `/chain`'s own `DD-MM-YYYY`, `as_of`
always populated — on a live request it is the minute the ladder that answered was stamped
with, so a client that sent none is still told which one it got.

**`spot`, `forward` and `discount` are what the analysis was priced against**, carried for the
reason `/chain` carries them: a delta is meaningless without the underlying it is a slope
against, and the basis is not zero. `spot` is Delta's top-level `spot_price`; `greeks.spot` is
never exposed. All three are `number | null`, and `forward` and `discount` are `null` together
exactly when the chain could not be fitted — in which case **no leg carries an `iv` or any
Greeks either**, exactly as on `/chain`. The curve and the metrics survive that: a P&L at expiry
is intrinsic value and a subtraction, and needs no model at all.

### `legs`

One row per requested leg, **in the order they were sent**. The first four fields are the
request echoed with `entry_price` filled in, so the screen can show what a leg was actually
priced at without holding on to what it asked for.

`iv` is a decimal fraction — `0.3712` is 37.12% — and is the volatility this leg's Greeks were
computed at. It is a property of the **strike**, not the leg: it is recovered by inverting the
out-of-the-money leg's midpoint and written to both legs of the pair.

`greeks` is `null` exactly when `iv` is. A leg with no volatility carries **no Greeks**;
reporting them at some default sigma would put five plausible numbers on screen that describe
nothing.

**The Greek conventions are `docs/chain-contract.md`'s and are not all textbook**: `delta` and
`gamma` undiscounted and with respect to the **forward**; `vega` and `rho` discounted and per
one percent; `theta` a **one-calendar-day** repricing on ACT/365 — not a trading-day year, which
overstates it by 1.456x here because crypto trades weekends. Per-leg rows are signed by
`direction` and scaled by `quantity`, because that is what a per-leg exposure means to whoever
reads it beside the legs. **`entry_price` is not**: it is what one unit cost, not what the leg
cost, so it stays comparable with the `bid` and `ask` on the ladder it was taken from.

`total_greeks` is the strategy's exposure, the same shape summed across the legs — and it is
published **only when every leg carries Greeks**. Not a sum over the legs that happened to
solve: that describes a different position from the one on screen, and nothing on the screen
would say so.

### `curve`

**The curve travels as its corner points, not as samples.** A single-expiry payoff is piecewise
linear — straight everywhere, kinked only at a strike — so the engine sends the kinks plus the
two end slopes and the browser draws straight segments between them. That is exact rather than
sampled, smaller on the wire, and it is what makes zooming out free. Drawing a straight line
between two given points is rendering, not arithmetic, so the web app's rule that it computes
nothing holds.

`corners` is a list of `{price, pnl}` objects — **not two parallel arrays**, which draw slightly
wrong rather than raising when they disagree — **strictly ascending by `price`**, with at least
one corner. `price` is the underlying's price at expiry, USD per one unit; `pnl` is the
strategy's profit or loss there, same units.

`slope_left` and `slope_right` are the P&L per unit of underlying **outside** the first and last
corner, so the two rays are exact however far the reader zooms out. A capped structure has zero
on both.

`window` is the range the engine suggests opening on: **±3 standard deviations** from the
at-the-money implied volatility and the time to expiry, widened to include every strike carrying
a leg — the **anchor** being the fitted forward where there is one and spot otherwise. The edges
are `anchor · e^(±3σ√t)`, three deviations of the **log** price, which is the quantity Black-76
models as normal: the additive reading `anchor · (1 ± 3σ√t)` puts the low edge below zero once
`σ√t` passes a third — 70% volatility a quarter out, and 296 days at the 37% below.
`low < high`. A fixed percentage does not transfer from an index either — at 37% volatility ±6%
is 1.6 sigma on a 3.8-day expiry and 0.33 sigma at 86 days. It is a suggestion, not a clamp: the
corners and the two slopes describe the curve everywhere.

**One line only — P&L at expiry.** It depends on the strikes and what was paid, neither of which
changes, so a live tab moves the forward, spot, the Greeks and the metrics while the curve sits
still. A "value today" line is genuinely curved, would have to be sampled, and would forfeit the
corner points; it is not built.

### `metrics`

| field | meaning |
|---|---|
| `max_profit` | The most the strategy can make. `null` when unbounded. |
| `max_loss` | The **worst outcome, as a P&L** on the same axis as `pnl`, so it is read straight off the chart rather than sign-flipped in the reader's head. Negative on almost everything, and zero or positive on a structure that cannot lose. `null` when unbounded. |
| `breakevens` | Every price at which P&L crosses zero, ascending. Empty when it never does. |
| `net_premium` | Positive is **paid out** (a debit), negative is **received** (a credit). |
| `reward_risk` | `max_profit` over the magnitude of `max_loss`. |

`reward_risk` is `null` when either side is unbounded or when there is no loss to divide by. A
ratio against unlimited has no meaning, and publishing a large number instead would read as a
good trade.

### `table`

The same quantity as `curve.corners` — `{price, pnl}`, strictly ascending, **never empty** — on
the readable grid a trader takes exact figures off rather than inferring them from the picture.
The same type as the corners deliberately: it is one quantity sampled twice, not two
quantities, and they must agree wherever they share a price. A response holding three corners
and an empty table is exactly the drift that shared type exists to prevent.

## Units

**Every number in this response is per one unit of the underlying.** Delta India's options are
vanilla, linear and USD-settled, every price on the venue is USD per 1 BTC, and `contract_value`
is `0.001` — so one contract of a 1,240-quoted call costs $1.24.

**`contract_value` is echoed so the screen can multiply, and the engine never does.**
`docs/settlement.md` is explicit that the multiplier never enters a pricing calculation and is
applied at the very end. The screen carries the toggle — on by default, showing dollars per
contract, money and Greeks together, with the unit in the column header. A consequence worth
stating: with the toggle on, our Greeks read 1,000x smaller than Delta's own in the ladder
beside them.

**Unbounded is `null`** — never an infinity, never a large sentinel, never a string. `null` is
also not `0`: a `max_loss` of `0` is a strategy that cannot lose, and a `max_loss` of `null` is
one that can lose everything.

**Every decimal is a JSON number or `null`, never a string,** and no non-finite float ever
reaches the wire. The engine converts once, at the boundary; the web app never calls
`parseFloat` and raises `ContractViolationError` if this is breached.

**That rule binds the response.** The request is parsed **leniently**: `{"quantity": "3"}` and
`{"entry_price": "1240.0"}` are coerced rather than refused. Strict request types would also
refuse an honest `3` where a caller — or the pure core building a request in a test — naturally
writes one, and the guarantee this contract exists to make is about what the engine *emits*,
which is the half a browser cannot defend itself against.

## Refusals

**Two envelopes, because there are two kinds of wrong.** Seven of the eight rows below are
**semantic** — the request is well formed and the engine will not answer it — and they travel
as FastAPI's default `{"detail": "..."}`, raised by the route exactly as every other refusal
in `main.py` is. The seventh, marked **schema**, is caught by `AnalyseRequest` itself before
the route is entered, so it arrives in FastAPI's request-validation envelope instead —
`{"detail": [{"type": ..., "loc": ["body", "legs"], "msg": ...}]}`, a **list** rather than a
string. That rule lives on the type on purpose: the pure core builds these models directly,
so the type is what stops a core-side bug producing an empty strategy, and moving the check
into the route to unify the envelope would take it off the type. A client reading `detail`
must therefore expect either shape.

| Status | When | The message names |
|---|---|---|
| 400 | A leg's `instrument` is not a canonical string | the string, and which of its six parts was wrong |
| 404 | A leg's instrument is not listed on the chain being analysed | the instrument string |
| 404 | An `as_of` minute the store does not hold | the underlying, the expiry and the minute |
| 503 | No live ladder yet — the chain cache has not warmed | the underlying and the expiry |
| 422 | `legs` is empty — **schema**, see above | that `legs` must hold at least one leg |
| 422 | The legs span more than one expiry | **both** expiries |
| 422 | The legs span more than one underlying | **both** underlyings |
| 422 | A leg has no quote on its side and no `entry_price` was supplied | the instrument, and which side was empty |

**One expiry per strategy.** With two, there is no date on which every leg has finished, so the
surviving leg has a price rather than a payoff and the line could only be drawn by assuming a
volatility. Calendar and diagonal spreads are #2.

**One underlying per strategy**, and this one is not merely out of scope. The ladder is fetched
for one underlying, so a leg naming another is looked up on a chain that does not list it — and
the only thing between that and a silently wrong answer is that BTC strikes are around 77,000
and ETH's around 4,000. On a collision the response would carry one `underlying` and one
`contract_value` for legs whose lot sizes differ by a factor of ten, and the screen would
multiply some of them by the wrong number with nothing on the page saying so. An accident of
arithmetic is not a refusal, so the refusal is written down.

**A leg nobody is quoting is not disabled, it is asked about.** "What if I were filled at 900"
is exactly the question an unquoted wing invites, so the refusal names the leg and the side and
the trader types a price. Nothing is ever inferred from the other side of the strike.

**Nothing to price against is two different facts, and they get two different codes.**

A **stored minute the store does not hold** is a **404**, the code this engine already uses for
a thing that does not exist — `/feeds/{adapter}/{command}` answers 404 for an adapter this
process does not run, `/expiries` for an underlying the venue lists nothing for. Under the
never-forward-fill rule a minute with no arrivals produces **no row at all**, so the minute
genuinely does not exist and 404 is the honest code rather than a borrowed one.

An **unwarmed live cache** is a **503**. The answer exists; it does not exist *yet*.
`get_bar_writer` in `main.py` reasons exactly this for `/recording`: a process with no writer
is not a process that is paused, it is one where the question has no answer, and answering with
a default would state something untrue.

**`/analyse` deliberately does not adopt `/chain/at`'s 200-with-`waiting`,** which is the
disposition `/chain/at`, `/smile` and `/bars` all take for their own kind of nothing-yet. The
difference is what the caller does with the answer. `/chain/at` feeds a screen that must render
*something* while it waits, so a `waiting` envelope beside a `chain` envelope is what lets one
component draw either. `/analyse` is a one-shot POST whose only product is an analysis: a
waiting envelope would make every consumer branch on a union before it could draw anything, to
represent a state in which there is nothing to draw. A status code says that once, at the
transport, and the client's error path already exists.

**No 502.** This route reads the chain cache or the local store and never calls Delta.

**And no code at all for a response that breaches this contract.** A `forward` published with
no `discount` is refused by the models and leaves as a **500** — the honest answer, because
half a fit reported as a whole one is our bug rather than a market condition, and inventing a
refusal that meant "our own code is wrong" would let a real defect leave the building disguised
as something the venue did.

**A partly solved leg is not that, and is not a 500.** A strike carrying an `iv` with fewer
than five Greeks beside it is a shape the store can hold — table C's Greek columns are nullable
independently of `iv` — so it is a condition in the data, not a defect in us. That leg is
published as **unfitted**: `iv` and `greeks` both `null`, and no `total_greeks` on the response,
exactly as for a strike that never solved. **Dropping a number is not fabricating one** — the
leg still says "no volatility here", which is true of what can be reported — whereas answering
500 would be the inversion this document refuses, and publishing four Greeks and a `null` would
put a missing exposure on screen where a reader skimming the column would read a zero.
