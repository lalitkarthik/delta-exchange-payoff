# The bars contract

The interface between the engine and the contract chart. Sibling of
[chain-contract.md](chain-contract.md) and [historical-chain-contract.md](historical-chain-contract.md),
which read the same four stored tables for the ladder; this route reads two of them for
one contract's whole day instead. Split into its own file for the reason
`historical-chain-contract.md` was: `chain-contract.md` is already past this project's
200-line bound at 222, and a third read path is a third subject, not an appendix to the
first two.

## `GET /bars?instrument=DELTA-BTC-20260904-77600-C-USD&date=2026-09-04`

One contract's minute bars for one date, addressed by the **canonical instrument
string** `docs/design/lld/events.md` defines — not by `underlying`/`expiry`/`strike` as
separate parameters, the way every other route here is addressed. The chart panel opens
from a strike the reader clicked, so the one string the URL already needs to carry is the
one string this route needs to be asked with.

```json
{
  "instrument": "DELTA-BTC-20260904-77600-C-USD",
  "underlying": "BTC",
  "expiry": "04-09-2026",
  "date": "2026-09-04",
  "bars": [
    {
      "minute": "2026-09-04T09:00:00Z",
      "bid_open": 100.0, "bid_high": 105.0, "bid_low": 95.0, "bid_close": 101.0,
      "ask_open": 102.0, "ask_high": 107.0, "ask_low": 97.0, "ask_close": 103.0,
      "mid_open": 101.0, "mid_high": 106.0, "mid_low": 96.0, "mid_close": 102.0,
      "ltp_open": 102.0, "ltp_high": 102.0, "ltp_low": 102.0, "ltp_close": 102.0
    }
  ]
}
```

`instrument` echoes the canonical string the route was asked with — the underlying
normalised, everything else unchanged — rather than reconstructing one from the parts
below, so a client that parsed the URL and a client that parsed the response never
disagree about which contract answered. `expiry` is `DD-MM-YYYY`, matching `/chain` and
`/chain/at`; `date` is `YYYY-MM-DD`, the store's own partition spelling, matching
`/chain/minutes`. The two are deliberately different formats for the reason
`historical-chain-contract.md` gives: a contract's expiry and the day being asked about
are two different questions, and a same-day (0DTE) contract makes them look identical
often enough that conflating the formats would hide the one day they are not.

**`bars` ascends by minute, and a minute nobody quoted is absent — never a null row.**
`quote-bars` gates what "stored" means here, exactly as it gates `/chain/minutes` and
`/chain/at`: a minute is in the list exactly when `quote-bars` holds a row for it.
`engine/tests/test_contract_bars.py`'s first test is this rule, verified red-green: three
minutes written, the middle one empty, asserted absent rather than present with nulls.

### Where each field comes from

| Field | Table | Meaning |
|---|---|---|
| `bid_*`, `ask_*`, `mid_*` | `quote-bars` | The minute's own OHLC — what the book quoted, and their midpoint, computed per tick and aggregated (`store.py`'s module docstring). |
| `ltp_*` | `reference-bars` | The venue's last-traded price, as an OHLC of a field that mostly repeats itself — see *The LTP is not what it looks like*, below. |

**Never stored, and therefore never on this route: `mark_*`.** The chart draws mid,
bid, ask and last-trade — never mark — so `reference-bars`' `mark_*` columns are read by
`/chain/at` and not by this route.

**A table with nothing for this minute makes its fields `null`, never a default.** A
minute `reference-bars` never fed — the book was busy and the ticker channel's grace
window (`store.QUOTE_GRACE_SECONDS`) had not closed the bar the way `quote-bars` had —
carries `ltp_*: null` and the bid/ask/mid columns unaffected. A **one-sided tick minute**
carries `mid_*: null` while `bid_*` or `ask_*` still answers — `bars.QuoteBar`'s own rule,
inherited rather than reproduced: a tick with a bid and no ask advances only the bid
series.

### The LTP is not what it looks like

`reference-bars`' `ltp_*` is not "the OHLC of trades this minute." `store.py`'s module
docstring is explicit: Delta's `ltp` field is the **close of the venue's rolling 24-hour
candle**, republished on every ticker frame regardless of whether a trade happened in the
minute just closed. So the stored OHLC of it is very often flat — the same number,
open through close, minute after minute — and moves only when a trade actually clears.
**This is not a defect in the route; it is the fact the chart panel's toggle exists to
show.** The mid series moves continuously with the book; the last-trade series steps,
sometimes for hours, on an option far from the money. See the LLD's *What to notice* for
a measured instance of the gap between the two.

## No forward-fill

The same rule `historical-chain-contract.md` states for the ladder, applied to one
contract's day instead of one minute's ladder: a minute the store never quoted is absent
from `bars`, never filled from the minute either side of it. There is no code path in
`contract_bars.py` that reads a minute other than the ones `quote-bars` actually holds.

## Cost

`docs/design/lld/bars-read-path.md` carries the measured number and the run that
produced it, beside `/chain/at`'s 15.3 ms and `/smile`'s 6.8 ms.

## Errors

| Status | When |
|---|---|
| 400 | `instrument` is not a valid canonical string, or names an `underlying` outside BTC/ETH, or `date` is not `YYYY-MM-DD` |
| 422 | a parameter is absent altogether — FastAPI's own validation |

No 404 and no 502: this route reads the local store and never calls Delta, exactly as
`/smile` and the two historical routes do. A contract this store never recorded and a day
nobody has lived through both answer 200 with an empty `bars`.

**A canonical string naming a venue other than the one this store happens to hold is not
an error either.** `store.py`'s schemas carry no venue column — see the LLD for why —
so a request for `NSE-BTC-20260904-77600-C` reads the same rows a `DELTA-` request would
and answers honestly with whatever `underlying`/`expiry`/`strike`/`right` matched, or
with nothing at all if none did.
