# What Delta's public API gives you for options

**Verdict: options history is obtainable, and it is clean — if you set `end` to the contract's
settlement time.** Set `end = now` instead, which is the obvious thing to write, and the same
request returns a series padded with fabricated prices. That one parameter is the difference
between usable data and garbage, and nothing in the API warns you.

Six months is reachable. So is more. Expired contracts are enumerable back beyond 3.5 months
with the cursor still running, and per contract you get **daily or hourly traded OHLCV, a mark
price series, and an open-interest series** — all real, all the way back to contracts that
expired in June 2024.

Scope: options only. Perpetuals, index and funding series are out of scope and were removed
from this document.

## Do this next

1. **Never set `end = now` on a candles request for an expired contract.** Use its
   `settlement_time` from `/v2/products/{symbol}`. This is the whole finding.
2. **Build the history loader.** Enumerate `/v2/products?states=expired`, then for each symbol
   pull the trade, `MARK:` and `OI:` series with `end = settlement_time`. Costing is in
   section 3. Roughly a few unattended hours for six months.
3. **Start the `/v2/tickers` snapshotter anyway.** History gives you mark, not bid/ask, and not
   implied vol. Section 7 says what is still missing and why the snapshotter remains worth it.

## How to read this

Every claim is tagged. **Measured** means a request made on 2026-09-01, and the request is
named. **Documented, not observed** means it comes from Delta's docs and was not verified. The
distinction earns its keep: the docs state a 2000-candle cap and the real cap is 4000.

Base URL `https://api.india.delta.exchange`. No API key was used anywhere in this document.

---

## 1. Contract lifetimes are normal

An option runs from its listing date to its expiry. That is true at every venue — a contract
being born and dying is how options work, not a defect.

**Measured**, `GET /v2/products/{symbol}`:

| Contract | Listed | Settles | Life |
|---|---|---|---:|
| `C-BTC-98000-271126` | 2026-08-28 | 2026-11-27 | **91 days** |
| `C-BTC-90000-301026` | 2026-08-21 | 2026-10-30 | **70 days** |
| `C-BTC-77600-040926` | 2026-09-01 | 2026-09-04 | 3 days |
| `C-BTC-60000-270624` | 2024-06-24 | 2024-06-27 | 3 days |

Delta runs a weekly-and-monthly ladder: near expiries live days, far ones roughly three months —
the same shape as a NIFTY monthly. **"The series are too short" is not a finding**, and an
earlier draft of this document wrongly said it was, by reading *this contract is seven days old
right now* as *options here only live for days*.

**`/v2/products/{symbol}` also carries the settlement value**, as `settlement_index_price`. That
is what the contract actually paid, and it is the correct terminal value for a backtest — not
the last traded price. **Measured**: `C-BTC-60000-310726` settled at index 63847.3917 against a
60000 strike, so it paid **3847.39**, while its last trade printed **3912.0**.

---

## 2. How far back you can enumerate

**Measured** — `GET /v2/products?states=expired&page_size=1000&contract_types=call_options,put_options`,
following the `meta.after` cursor:

| Pages walked | Products | Settlement dates reached |
|---:|---:|---|
| 14 | **14,000** | 2026-05-16 … 2026-09-01 |

The cursor had not run out. **3.5 months came back in 14 requests**, so six months is roughly 25
requests of enumeration — trivial.

Two traps in this endpoint:

- **`meta.total_count` reports 10000 and is wrong.** The walk above returned 14,000 products.
  Page until the cursor is absent; do not trust the count.
- **Results are not ordered by settlement date.** Page 5 of the walk returned a contract settling
  2023-12-28 in among August 2026 contracts — the ordering is by internal id. **You cannot stop
  paging when you reach your target date**; you must exhaust the cursor and filter afterwards.

`states=settled` returns zero products. `expired` is the only state that matters.

---

## 3. The padding trap, and the one-parameter fix

This is the finding that decides everything, so it is worth stating precisely.

**The mechanism (measured).** `/v2/history/candles` fills every bucket containing no trade by
copying the last trade forward — `open = high = low = close = last traded price`, `volume = 0` —
and continues doing so until it reaches the `end` you asked for. The full rule:

> The effective window is `[max(start, end − 4000 × resolution), end]`. If it contains no real
> trade, the result is empty. Otherwise the response runs from the first real trade in the
> window through `end`, padding every empty bucket.

Read that rule carefully and the fix falls out of it: **the padding lives between the contract's
last trade and your `end`. Move `end` back to settlement and there is no room for it.**

**Measured**, same symbols, `resolution=1d`, varying only `end`:

| Symbol | `end` | Bars | Real | Padded |
|---|---|---:|---:|---:|
| `C-BTC-60000-270624` | now | 801 | 4 | **797** |
| `C-BTC-60000-270624` | settlement | 4 | 4 | **0** |
| `C-BTC-60000-310726` | now | 73 | 40 | **33** |
| `C-BTC-60000-310726` | settlement | 40 | 40 | **0** |
| `C-BTC-70000-310726` | now | 73 | 40 | **33** |
| `C-BTC-70000-310726` | settlement | 40 | 40 | **0** |

Zero padding in every case. The 2024 contract included.

**It is not a sampling fluke.** **Measured** across 18 randomly sampled expired BTC and ETH
options settling 1–15 July 2026, each queried with `end = settlement_time`: **58 real daily bars
out of 58 returned, 100%**. Every contract in the sample traded on every day it existed.

### Why this went unnoticed

`end = now` is the natural thing to write, and it is what every earlier probe of this API used.
The corruption it produces is also invisible from the other direction — **measured** on
`C-BTC-60000-270624`, varying only `start`:

| `start` | Rows |
|---|---:|
| 2024-06-24 | 800 |
| 2024-07-01 | **0** |

So a spot check with a recent `start` returns empty and looks correct, while a spot check with
`end = now` returns two years of flat prices and looks broken. Neither reveals that the data
underneath is fine.

### What the padding still costs you

- **Live contracts.** There is no settlement time to anchor to yet, so use `end = now` and drop
  `volume == 0`. The tail padding is unavoidable while a contract is trading.
- **Intraday gaps mid-life are real.** At `1m` a contract genuinely does not trade most minutes —
  `C-BTC-60000-270624` has 54 real minutes out of 3636. That is ordinary illiquidity, correctly
  reported once you filter, not a defect.
- **A worthless option still prints its last trade.** `C-BTC-70000-310726` settled worthless, and
  its final bar is its last trade, not zero. Take the terminal value from
  `settlement_index_price`, never from the last candle.

---

## 4. What you get per contract

Three separate series, all clean under the `end = settlement` rule. **Measured** on
`C-BTC-60000-270624` (expired 2024-06-27), `resolution=1h` across its full life:

| Series | Bars | Distinct closes | Range |
|---|---:|---:|---|
| `C-BTC-60000-270624` (trades) | 62 | 21 | 705.0 … 2000.0 |
| `MARK:C-BTC-60000-270624` | **64** | **64** | 729.67 … 2313.45 |
| `OI:C-BTC-60000-270624` | 64 | 16 | 0 … 2.051 |

**`MARK:` is the important one.** 64 bars, 64 distinct values — a continuous mark-price series
across the contract's entire life, from a contract that expired over two years ago. This is what
lets you mark a position on a day it did not trade, which is the thing a backtest actually needs.

`MARK:` and `OI:` carry `volume: null` on every bar, so `volume == 0` cannot filter them. Under
the `end = settlement` rule they do not need filtering. Under `end = now` they are unfilterable
and unusable — **measured**, `MARK:C-BTC-60000-310726` with `end = now` returns 73 bars whose
most recent 33 are flat at 3846.83.

**Valid resolutions (measured, from the 400 error body):** `5s, 1m, 3m, 5m, 15m, 30m, 1h, 2h,
4h, 6h, 1d, 1w`. `12h`, `3d`, `7d` and `1M` are rejected. `5s` works but is absent from the
docs' list.

**Cap: 4000 bars per response**, not the documented 2000, and 4001 when `end` lands on a bucket
boundary. Truncation drops the oldest, never the newest — the response anchors to `end`. To page,
set `end = oldest_returned − one interval` and repeat; size the loop off returned timestamps, not
a constant. No option series in this venue's history approaches 4000 daily bars, so the cap only
binds at intraday resolutions.

### Costing a six-month pull

Enumeration is ~25 requests. Roughly 24,000 expired contracts over six months, at three series
each, is ~72,000 requests. Public market data costs weight 3 against a budget of at least 10,000
per fixed five-minute window (section 6), so the floor is about **1.8 hours of pure quota**, and
a few unattended hours with sane pacing. Pull `MARK:` first — it is the series that carries the
most information per request.

---

## 5. The live chain

What `/v2/tickers` gives you right now, which is what the app renders.

**Measured** — 8 expiries for both underlyings, identical dates, furthest **87 days out**:

| Expiry | Days out | BTC C / P | ETH C / P |
|---|---:|---|---|
| 2026-09-02 | +1 | 28 / 28 | 25 / 24 |
| 2026-09-03 | +2 | 25 / 24 | 15 / 15 |
| 2026-09-04 | +3 | 63 / 65 | 33 / 34 |
| 2026-09-11 | +10 | 30 / 28 | 14 / 13 |
| 2026-09-18 | +17 | 24 / 22 | 13 / 13 |
| 2026-09-25 | +24 | 46 / 43 | 24 / 23 |
| 2026-10-30 | +59 | 47 / 42 | 20 / 18 |
| 2026-11-27 | +87 | 35 / 32 | 16 / 16 |

Totals: **BTC 298 calls / 284 puts, ETH 160 calls / 156 puts.** Six of eight expiries are inside
a month; there is no long-dated surface.

**Read this before parsing: every price, IV, Greek and open-interest value is a JSON string.**
Only `open/high/low/close`, `volume`, `turnover`, `turnover_usd` (floats) and
`product_id`/`size`/`leverage`/`timestamp` (ints) are numbers.

| Field | Type | Example |
|---|---|---|
| `quotes.best_bid` / `best_ask` | **str** | `"498"` / `"539"` |
| `quotes.bid_iv` / `ask_iv` / `mark_iv` | **str** | `"0.41168255"` / `"0.41967074"` / `"0.41569626"` |
| `quotes.impact_mid_price` | null | `null` |
| `mark_price` / `mark_vol` | **str** | `"518.61229452"` / `"0.41570447"` |
| `greeks.delta` / `gamma` / `theta` / `vega` / `rho` | **str** | `"0.09211620"`, `"0.00001279"`, `"-18.16064981"`, `"51.33736257"`, `"10.64975020"` |
| `greeks.spot` vs `spot_price` | **str** | `"77446"` vs `"77447.8"` — see below |
| `oi` / `oi_contracts` / `oi_value_usd` | **str** | `"9.9080"` / `"9908"` / `"768478.3512"` |
| `price_band.lower_limit` / `upper_limit` | **str** | `"5.08984471"` / `"4077.19278685"` |
| `strike_price` / `tick_size` | **str** | `"98000"` / `"0.100000000000000000"` |
| `timestamp` | int | microseconds |

Three IV fields plus a fourth, `mark_vol`, which shadows `mark_iv` without matching it —
`"0.41570447"` against `"0.41569626"` in one snapshot. Five Greeks; no vanna, vomma or charm.

**There is no expiry field.** **Measured**: `'expiry_date' in ticker` is `False` on every ticker.
Expiry exists only as the `DDMMYY` symbol suffix, so `C-BTC-98000-301026` must be parsed to
2026-10-30. Sort parsed dates, never the strings — as text, `30-10-2026` sorts after `27-11-2026`.

**`spot_price` and `greeks.spot` disagree, and the gap is real.** **Measured** across 582 BTC
option tickers in one response: **one** distinct `spot_price` against **18** distinct
`greeks.spot` values spanning **15.10 USD**. The Greeks are not computed against a common spot.
Anything netting Greeks across strikes or recovering a forward inherits that inconsistency. Use
`spot_price`.

---

## 6. Timeouts and limits

| Limit | Value | Status |
|---|---|---|
| Rate-limit window | fixed 5 minutes, quota resets whole | **measured** — `/v2/rate_limits/quota` |
| Quota per window | 20000 | **documented, not observed** — see note |
| Weight, public market data | 3 | **measured** — tickers, candles, products, orderbook, trades all cost 3 |
| Weight, order write / history / batch | 5 / 10 / 25 | **documented, not observed** |
| 429 reset header | `X-RATE-LIMIT-RESET`, ms until retry | **documented, not observed** |
| Signature validity | 5 seconds | **documented, not observed** |
| Websocket connections | 150 per IP per 5 min | **documented, not observed** |
| Websocket idle disconnect | 60s "after making connection" | **documented, not observed** |
| Matching engine | 500 ops/sec/product | **documented, not observed** |
| `/v2/tickers/{symbol}` | max 10 comma-separated symbols | **documented**; 3 verified |
| `User-Agent` header | required | **measured** — omitting it returns 403 from CloudFront, as HTML, before the request reaches Delta |

Three notes that matter for pacing a history pull:

1. **The quota ceiling is unverified and the sources disagree.** Delta's docs say 20000 per
   window; an earlier note said 10000. Settling it means firing ~3300 requests to trip a 429 —
   a poor trade. **Budget against 10000 and you are safe either way.**
2. **`/v2/rate_limits/quota` answers without a key**, though documented as signed. **Measured**:
   returns `current_quota` and `remaining_time_in_milliseconds`. Meter against it rather than
   discovering the ceiling by hitting it. A long history pull should read it between batches.
3. **CloudFront serves repeats free.** **Measured**: an identical request within the 60s cache
   window cost **0 quota**. Retries of an identical request are free.

---

## 7. What is still missing

The history is real, but it is not the whole chain. Three gaps, and they are the reason the
snapshotter still matters.

**No bid/ask history.** `MARK:` gives one price per bucket. There is no historical spread, so
nothing here supports a transaction-cost model. A backtest using mark as its fill price is
assuming a spread it cannot measure.

**No implied vol or Greeks history.** Those exist only on the live ticker. For history you must
compute them yourself from mark, spot, strike and time to expiry — which is precisely what
`payoff-project` already does for NIFTY, so the capability exists. Note the inverse settlement
difference before reusing that code.

**Untested: whether `MARK:` exists for a strike that never traded.** Every contract sampled here
had trades. If Delta only publishes a mark series for contracts that traded, then the reconstructed
chain is restricted to traded strikes and the surface has holes at the wings. **This is the single
most valuable open question in this document** — it decides whether a reconstructed historical
chain is complete or partial. One afternoon's work to answer.

**What is free** — **measured**, every endpoint below with no API key, only a `User-Agent`:
`/v2/products`, `/v2/products/{symbol}`, `/v2/tickers`, `/v2/tickers/{symbol}`,
`/v2/history/candles`, `/v2/history/sparklines`, `/v2/l2orderbook/{symbol}`,
`/v2/trades/{symbol}`, `/v2/settings`, `/v2/assets`, `/v2/indices`, `/v2/rate_limits/quota`.
Wallet, orders, positions, fills and profile return 401. **Nothing in this document required a
key.** IP whitelisting applies only to trading keys (**documented, not observed**).

**One quirk.** **Measured**: `/v2/history/sparklines` ignores its `symbols` parameter and returns
all 1263 live products regardless. A cheap full-market snapshot; a useless single-symbol lookup.

---

## 8. Is this data safe to backtest on?

**Yes, with discipline.** Four rules, and all four are load-bearing:

1. **`end = settlement_time`** for expired contracts, from `/v2/products/{symbol}`. Never `now`.
2. **Terminal value from `settlement_index_price`**, never from the last candle — a worthless
   option still prints its last trade, and the gap was 64.61 on the contract measured here.
3. **Drop `volume == 0`** on trade candles anyway, as belt and braces. It is a no-op under rule
   1 and it saves you when rule 1 is missed.
4. **Page the expired-product cursor to exhaustion.** It is not ordered by date and its
   `total_count` under-reports.

**How far back: at least back to June 2024**, on the evidence here — a contract that expired
2024-06-27 returns a complete hourly mark series. Enumeration was verified to 2026-05-16 with
the cursor still running, so the practical limit was not found. **Six months is comfortably
inside it.**

**What you get:** per contract, per day or hour — traded OHLCV, a mark price, and open interest.
Plus a true settlement value. That supports entry and exit at traded prices, daily marking, and
correct expiry accounting.

**What you cannot do:** model spreads or slippage, use historical IV or Greeks without computing
them, or — pending the open question in section 7 — rely on the wings of the reconstructed chain
being populated.

**Should we buy history?** Less clear-cut than it looked. Delta supplies mark, OI and trades for
free, back beyond two years. A vendor would be adding bid/ask, IV and completeness at the wings.
Answer section 7's open question before paying for anything, because it decides how large the
gap actually is.

**Does the snapshotter still matter?** Yes, for the bid/ask and IV that history does not carry,
and because the clock only runs forward on those. But it is no longer the *only* path to an
options backtest, which is what an earlier version of this document claimed.

---

## 9. Corrections

**This document's verdict was reversed on 2026-09-01.** An earlier version opened with *"do not
backtest options on this API"* and stated the corruption could not be filtered out. That was
wrong. The mechanism it described was correct; the conclusion drawn from it was not.

The rule as written — *the response runs from the first real trade through `end`* — already
implies that moving `end` back to settlement leaves no room for padding. Nobody drew that
inference, because every probe had `end = now` hard-coded, including the verification. The
measurements above were run specifically to test the inference and it held on every contract,
including one from 2024.

**Earlier claims, corrected:**

1. **"Options have no history worth the name — days."** Wrong. Lifetimes run to 91 days;
   `C-BTC-98000-271126` was listed 2026-08-28 for a 2026-11-27 settlement. This came from
   reading *this contract is seven days old* as *options only live for days*.
2. **"Not with filters, not with care."** Wrong. One parameter fixes it entirely.
3. **"`MARK:` is corrupted identically and cannot be filtered."** True only under `end = now`.
   Under `end = settlement` it is the cleanest series available — 64 of 64 bars distinct.
4. **"The flatline is the settlement price."** It is the last traded price. They coincided on
   the first contract examined, which made the mechanism look deliberate.
5. **"Buying history will not fix options."** Overstated, and it contradicted this document's own
   section 8. Delta cannot sell a bid/ask or IV surface it never stored; a vendor who captured
   the chain themselves might have one. Untested.

Counts drift with the clock, not with disagreement: BTC live calls read 298 here against 295 in
an earlier brief four hours prior; the expiry dates are identical.

## Reproducing

```
python tools/probe_api.py all              # everything, ~3 minutes, 116 requests
python tools/probe_api.py expired          # the padding sections
python tools/probe_api.py depth cap --fast # 0.4s pacing
```

Standard library only, no API key, read-only. `--slow` (1.5s) is the default and will not get
anyone rate-limited.

**Note:** `probe_api.py` predates the `end = settlement` finding and still probes with
`end = now`. Its measurements are correct for what they measure, but it does not yet demonstrate
the fix. Updating it is the obvious next change.
