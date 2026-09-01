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
  "rows": [
    { "strike": 77000.0, "call": { }, "put": { } }
  ]
}
```

`rows` is ascending by strike. `atm_strike` is the listed strike closest to `spot` — a lookup,
not a model. Either side of a row may be `null` when only one of the pair is listed.

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
  "tick_size": 0.5
}
```

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
