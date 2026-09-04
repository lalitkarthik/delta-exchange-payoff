# The `/smile` contract

The interface between the engine and the volatility screen. Sibling of
[chain-contract.md](chain-contract.md), which is the authority for `/expiries`, `/chain` and
`/ws/chain`. Both files are changed **first**, before the response models and before
`web/lib/contract.ts`.

**Why a second file rather than a section in the first.** [#15](https://github.com/lalitkarthik/delta-exchange-payoff/issues/15)
preferred one document and carried a caveat: split if the smile section runs long. It ran
long — request, response, the per-minute/per-point split, the buffer rule, the null rules and
the empty-series rules come to well over sixty lines, and `chain-contract.md` was already at
208, past this project's 200-line bound. Two documents are only two authorities when they
describe the same thing; these describe different endpoints and each is the sole authority for
its own. Cross-linked in both directions so neither can be read without finding the other.

`/chain` serves **now**, from Delta, and computes as it answers. `/smile` serves **what was
already computed and stored** — `computed-bars`, table C of `docs/storage-start-here.md`. It
never solves a volatility and never calls Delta.

## `GET /smile?underlying=BTC&expiry=04-09-2026`

Every stored minute for one expiry, in one response.

```json
{
  "underlying": "BTC",
  "expiry": "04-09-2026",
  "model_versions": ["F1+assumed-6.5 / S1-newton / ACT365 / mid-OTM"],
  "minutes": [
    {
      "minute": "2026-09-04T09:00:00Z",
      "forward": 77590.43,
      "discount": 0.99997892,
      "years_to_expiry": 0.00114155,
      "forward_method": "F1+assumed-rate",
      "model_version": "F1+assumed-6.5 / S1-newton / ACT365 / mid-OTM",
      "points": [
        { "strike": 77000.0, "iv": 0.4321, "iv_leg": "put", "iv_reason": null },
        { "strike": 77500.0, "iv": null, "iv_leg": null, "iv_reason": "NO_QUOTE" }
      ]
    }
  ]
}
```

`minutes` ascends by minute; `points` ascends by strike. Both may be empty.

### The day, not the minute

**One request returns every stored minute for that expiry.** `measured`, three runs, minimum:
one minute for one expiry reads in 4.5 ms; the whole store for one expiry — 540 minutes,
18,676 rows — reads in 6.8 ms. Parquet prunes by column and by partition directory, so the
fixed cost dominates and the day costs 2.3 ms more than the minute.

A per-minute endpoint would therefore buy nothing and put a network round trip inside every
scrubber drag. Scrubbing is an array index into `minutes`, and an overlay is a second index
into the same array — no second request, no client-side arithmetic.

> That 6.8 ms was measured against hourly flush files. [#16](https://github.com/lalitkarthik/delta-exchange-payoff/issues/16)
> took the interval to five minutes, so the current uncompacted day is twelve times as many
> files. The re-measurement is owed and is `derived` at roughly 88 ms until it is taken.

### Parquet **and** the buffer

**The response is the union of what is on disk and what is still in the writer's buffer.**
The store flushes every five minutes, so a parquet-only read hands the screen a right edge up
to a full interval behind the live curve — `measured` this session at 07:38Z flushed against a
clock of 08:05Z, a 27-minute hole. The buffer already lives in the process that owns this
endpoint, so the union costs a concatenation.

This is the guard that matters most here, because a regression to a parquet-only read looks
correct in every other test: the shape is right, the fields are right, and only the newest few
minutes are missing. `engine/tests/test_smile.py` asserts a buffered minute reaches the wire
with nothing flushed at all.

The **open** minute — sampled but not yet sealed by the writer's watermark — is deliberately
not included. It is at most one minute wide, and a bar that has not sealed is not yet a bar.

### The response carries data, not a rendering

Per point: the minute it belongs to, the strike, the implied volatility, the leg that produced
it, and the reason when there is none. Per minute: the forward, the discount, the years to
expiry, the forward method and the model stamp. Nothing is bucketed, smoothed, interpolated or
dropped, and no field is a display string.

The client has **no test runner**. Anything computed only there ships unverified, so anything
the client could get wrong is decided here, over HTTP, where a test can assert it.

## The fields

| field | meaning |
|---|---|
| `underlying` | `BTC` or `ETH`, normalised — the request may be lower case |
| `expiry` | `DD-MM-YYYY`, exactly as `/chain` and Delta spell it |
| `model_versions` | **every** distinct model stamp in this response, ascending |
| `minutes[].minute` | ISO 8601 UTC, second precision, `Z`-suffixed — `2026-09-04T09:00:00Z` |
| `minutes[].forward` | the forward this minute's volatilities were solved against |
| `minutes[].discount` | the discount factor fitted alongside it |
| `minutes[].years_to_expiry` | ACT/365, the clock the volatility is quoted on |
| `minutes[].forward_method` | `F1`, `F1+assumed-rate` or `F2` — see the chain contract |
| `minutes[].model_version` | the stamp on this minute's rows |
| `points[].strike` | the listed strike |
| `points[].iv` | decimal fraction. `0.4321` is 43.21%. `null` when unsolved |
| `points[].iv_leg` | `"call"` or `"put"` — the out-of-the-money side it was solved on |
| `points[].iv_reason` | `null` when solved; otherwise the solver's own account |

### One point per strike, not per leg

Table C stores a row per **contract**, so a paired strike holds two rows carrying the same
volatility — that is what `iv_leg` is for. The smile plots volatility against strike, so those
two rows become one point. The de-duplication is by `(minute, strike)` and it is not a choice
between two numbers: put-call parity gives the strike one volatility and `compute.enrich`
writes that one number to both legs.

### A null is a fact, not a gap

**An unsolved strike travels as a point whose `iv` is `null`, carrying its `iv_reason`. It is
never dropped.** The screen has to tell a strike that was not solved from a strike that does
not exist, and it can only do that if the null arrives. 1.2% of the store is such rows.

`iv_reason` is `null` when the strike solved — the store's spelling, not `/chain`'s `""`. A
column holding both spellings for one fact is a column every reader has to guess at.

`iv_leg` is `null` exactly when `iv` is: there is no side a number came from when there is no
number. The Greeks are stored beside these rows and are **not** carried here — the smile plots
volatility, and five figures nothing on this screen reads would be five more chances to drift.

### Two model stamps in one response

`model_versions` is a **list** because the model can change mid-day and the stored rows say so
per row. A response spanning two stamps reports both rather than picking one; the header then
warns that the curves on screen were not all computed the same way. The forward convention
alone is worth up to 3.9 vol points, and this screen plots nothing but vol points.

A response with one stamp still sends a one-element list. A response with no rows sends an
empty one.

## Absence is 200 and empty, not an error

**An underlying with no stored partitions returns `{"minutes": [], "model_versions": []}`.**
So does an expiry that was never stored, and so does one whose rows exist but hold no computed
volatility at all. Nothing has gone wrong: the store is answering "nothing yet", which is the
same discipline as a minute with no bar.

A 404 here would make the screen render an error page for the ordinary case of ETH before ETH
is being collected, and the client cannot distinguish "the engine is broken" from "you asked
for a day we have not lived through".

## Errors

FastAPI default shape, `{"detail": "..."}`, and the same table as `/chain` minus the 404 —
a malformed request is still a malformed request.

| Status | When |
|---|---|
| 400 | `underlying` is not `BTC` or `ETH`, or `expiry` is not `DD-MM-YYYY` |
| 422 | a parameter is absent altogether — FastAPI's own validation |

No 502: this endpoint reads the local store and never calls Delta, so there is no upstream to
be unavailable.
