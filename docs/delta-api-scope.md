# What Delta's public API actually gives you

**Verdict: do not backtest options on this API.** Every historical option series is padded
with fabricated prices — bars that keep printing after the contract expired, after the
settlement that made it worthless, and (for live symbols) into dates that have not happened
yet. **Perp history is 978 daily bars, not ten years.** The blocker is not that options expire —
their lifetimes here are normal, up to 91 days. It is that **no historical quote surface exists**:
no endpoint returns the chain as it stood on a past date, only a trade log with the gaps filled
in. Delta's own API cannot sell you that surface, because it never stored it. A third-party
vendor who snapshotted the chain themselves might have it — untested, see section 8. Failing
that, the only trustworthy option history is history captured going forward.

## Do this next

1. **Do not build any options backtest against `/v2/history/candles`.** Nothing in this
   document makes it safe.
2. **Stand up a `/v2/tickers` snapshotter** on a schedule before anything else. It is the
   prerequisite for every options question the firm wants to answer. Not built here.
3. **If you need perps only**, `/v2/history/candles` is usable from 2023-12-29 onward once you
   drop zero-volume bars and never set `end` past now. Roughly a day's work.

## How to read this

Every claim is tagged. **Measured** means a request I made on 2026-09-01, and the request is
named. **Documented, not observed** means it comes from Delta's docs and I did not verify it.
The distinction matters: the docs state a 2000-candle cap and the real cap is 4000, so a
documented number here is a claim, not a fact.

All measurements come from one run of `tools/probe_api.py all`, 2026-09-01T17:42:55Z to
17:49:35Z, 116 requests, against `https://api.india.delta.exchange`. No API key.

---

## 1. How far back the data goes

**Measured** — `GET /v2/history/candles?resolution=1d&symbol=<sym>&start=<2014-01-01>&end=<now>`:

| Symbol | Kind | Daily bars | Oldest bar |
|---|---|---:|---|
| `BTCUSD` | perp trades | 978 | 2023-12-29 |
| `ETHUSD` | perp trades | 939 | 2024-02-06 |
| `SOLUSD` | perp trades | 877 | 2024-04-08 |
| `MARK:BTCUSD` | perp mark | 989 | 2023-12-18 |
| `MARK:ETHUSD` | perp mark | 940 | 2024-02-05 |
| `.DEXBTUSD` / `.DEETHUSD` | index | 989 | 2023-12-18 |
| `OI:BTCUSD` | open interest | 978 | 2023-12-29 |
| `FUNDING:BTCUSD` | funding rate | 989 | 2023-12-18 |

Depth is the same at every resolution — it is a start date, not a retention window. The
resolution only changes how many bars fit in one response (section 3).

**Dated futures: none exist.** **Measured**, `GET /v2/products?states=live&page_size=1000`:
1263 live products, of which 493 call options, 475 put options, 27 perpetual futures, 5
`move_options`. Zero `futures`. There is no dated-futures history to assess.

**Options: the contract lifetimes are normal. The problem is elsewhere.** An option series runs
from its listing date to its expiry, and that is true at every venue — a contract being born and
dying is how options work, not a defect. **Measured**, `GET /v2/products/{symbol}`:

| Contract | Listed | Settles | Life |
|---|---|---|---:|
| `C-BTC-98000-271126` | 2026-08-28 | 2026-11-27 | **91 days** |
| `C-BTC-90000-301026` | 2026-08-21 | 2026-10-30 | **70 days** |
| `C-BTC-77600-040926` | 2026-09-01 | 2026-09-04 | 3 days |
| `C-BTC-60000-270624` | 2024-06-24 | 2024-06-27 | 3 days |

Delta runs a weekly-and-monthly ladder: the near expiries live days, the far ones roughly three
months — the same shape as a NIFTY monthly. So "the series are too short" is **not** the finding,
and an earlier draft of this document said so wrongly. It reached that by confusing *this
contract is only seven days old right now* with *options here only live for days*.

**The real limitation is that Delta records trades, and options mostly do not trade.**
**Measured**: `C-BTC-60000-270624` existed for 3 days and contains **54 minutes with a real
trade out of 3636** — it traded in 1.25% of the minutes it was alive. That is ordinary for a
strike away from the money and is not Delta's fault.

But a backtest still has to know what those strikes were **worth** on the days nothing traded:
the mark and the implied vol are what a position is valued at, margined on, and closed at.
*Nobody traded it* is not *it had no value*. `/v2/history/candles` carries trades only, and then
fills the silence with a copy of the last trade without saying so — which is section 2.

**So the honest answer for options is not a number of days. It is that no historical quote
surface exists at all.** No endpoint returns the chain as it stood on a past date; there is only
a trade log with the gaps filled in. That is the difference between this API and a commercial
options dataset, which stores the whole chain per day — every strike, with mark, IV, Greeks and
open interest, whether or not it traded — because the quote surface *is* the product.

---

## 2. The defect: zero-volume carry-forward, and it never stops

This is the finding that decides the backtesting question.

**The mechanism (measured).** `/v2/history/candles` fills every bucket that contains no trade
by copying the last trade forward: `open = high = low = close = last traded price`, `volume = 0`.
It does this for gaps mid-life, for every bucket after the contract stops trading, and for
buckets dated in the future. It stops only when it reaches the `end` you asked for.

The full rule, which predicted every case I then tested:

> The effective window is `[max(start, end − 4000 × resolution), end]`. If that window contains
> no real trade, the result is empty. Otherwise the response runs from the first real trade in
> the window through `end`, with every empty bucket padded as above.

### Evidence

**`C-BTC-60000-270624` — expired 2024-06-27T12:00:00Z, still printing today.**
**Measured**, `resolution=1d&start=<2014-01-01>&end=<now>`: 800 bars, 2024-06-24 to 2026-09-01,
**796 of 800 with volume 0**. Four real trading days, then 796 days of this:

```
2024-06-26  o=1628.0 h=2034.0 l=931.0 c=1241.0                v=2715.0
2024-06-27  o=1154.0 h=1433.0 l=705.0 c=1237.558620689655     v=2316.0   <- settlement day
2026-08-31  o=1237.558620689655 ... c=1237.558620689655       v=0
2026-09-01  o=1237.558620689655 ... c=1237.558620689655       v=0
```

`GET /v2/products/C-BTC-60000-270624` gives `state: "expired"`,
`settlement_index_price: "61237.558620689655"`, strike 60000 — intrinsic 1237.5586, which is
what the settlement print booked at and what has been carried forward ever since.

**The carried value is the last trade, not the settlement value.** **Measured** on
`C-BTC-60000-310726` (expired 2026-07-31T12:00:00Z): the flat value is **3912.0**, which was
the close of the 10:00Z bar — the last bar with volume. Its `settlement_index_price` is
63847.3917, so the contract actually paid **3847.39**. The series is wrong by 64.61 for every
one of the 32 days since. At 1h resolution the boundary is visible: 10:00Z has `v=20`, and
every bar from 11:00Z onward is flat at 3912 with `v=0`, straight through the 12:00Z settlement.

**An option that expired worthless still quotes a price.** **Measured**: `C-BTC-70000-310726`
settled against index 63847.39 with a 70000 strike, so it paid zero. Its series returns
`close=0.1, volume=0` every day since, and is still doing so on 2026-09-01. Payoff 0, series 0.1.

**The same mechanism fabricates bars for dates in the future.** **Measured**,
`symbol=BTCUSD&resolution=1d&start=<now−5d>&end=<2027-03-01>`: 186 bars, **181 of them dated
after today**, all `o=h=l=c=77418.0, v=0`. A pipeline that sets `end = now + buffer` will
silently ingest six months of invented perp prices.

**It is not a catch-all fallback.** **Measured** controls: `C-BTC-60000-999999`, `NOT-A-SYMBOL`
and `C-BTC-99999999-040926` all return `n=0`. A window entirely in the future returns `n=0`.
The padding needs one real trade to seed from — that is all it needs.

**The corruption is invisible if you ask for a recent window.** **Measured** on
`C-BTC-60000-270624` at `resolution=1d`, varying only `start`:

| `start` | Rows |
|---|---:|
| 2014-01-01 | 800 |
| 2024-06-24 | 800 |
| 2024-06-25 | 799 |
| 2024-07-01 | **0** |
| 2025-01-01 | **0** |

Anyone spot-checking with a recent `start` sees an empty result and concludes the endpoint
handles expiry correctly. It does not.

### Three things that make this worse than it first looks

1. **Padding dominates the contract's own lifetime, not just its afterlife.** **Measured**:
   `C-BTC-60000-270624` at `resolution=1m` up to expiry+12h returns 3636 bars, of which
   **3582 (98.5%) have volume 0**. Filtering to real prints leaves 54 bars for the whole
   contract.
2. **`MARK:` series are corrupted identically and cannot be filtered.** **Measured**:
   `MARK:C-BTC-60000-270624` returns 800 daily bars through 2026-09-01, flat at 1238.58991071 —
   and its `volume` field is `null` on every bar, real or padded. The one discriminator that
   works on trade candles does not exist here. `OI:` series behave the same way.
3. **`volume == 0` is therefore the only filter available, and only on trade candles.** It is
   correct but expensive: it deletes most of every option series.

**The padding is generated at query time, not stored.** **Measured**: the same
`BTCUSD&end=2027-03-01` request run at 17:44Z returned future bars at `77418.0`; run again at
17:52Z it returned `77326.0`. The fabricated bars track the live last trade, so the same
historical request returns different history depending on when you ask it. Re-running a
backtest will not reproduce its own inputs.

**Mechanism established, boundary established.** The one thing I did not establish is whether
Delta considers this a bug. It does not change the conclusion.

---

## 3. Cap per response, and how to page past it

**The cap is 4000 bars, not the documented 2000.** **Measured**,
`symbol=BTCUSD&resolution=1m`, varying only the window:

| Requested window | Rows returned |
|---|---:|
| 1 day | 1440 |
| 2 days | 2880 |
| 3 days | **4000** |
| 5 / 10 / 40 / 200 days | **4000** |

Delta's docs say "it can return only upto 2000 candles maximum in a response"
(**documented, not observed** — and contradicted above). The cap is uniform across
resolutions: a 400-day window returns 4000 bars at `1m`, `5m`, `15m` and `1h`, and 2400 at
`4h`, 400 at `1d` — i.e. the window binds before the cap does at coarse resolutions.

**Truncation drops the oldest, never the newest.** The response is always anchored to `end`.
`start` is effectively ignored once it is further back than `end − 4000 × resolution`.

**To page, walk `end` backwards.** Set `end = oldest_returned_time − one interval` and repeat.
**Measured**: three pages of `1m` BTCUSD reassembled **12,002 unique minutes with 0 gaps**,
2026-08-24T09:42Z to 2026-09-01T17:43Z. Pages 2 and 3 returned 4001 rows, not 4000, because
`end` landed exactly on a bucket boundary — size your loop off the returned timestamps, not
off a constant.

**Valid resolutions (measured, from the 400 error body):** `5s, 1m, 3m, 5m, 15m, 30m, 1h, 2h,
4h, 6h, 1d, 1w`. `12h`, `3d`, `7d`, `1M` are rejected with `bad_schema`. Note `5s` is accepted
by the API but absent from the docs' enumerated list.

---

## 4. What the API gives you for an option

From `GET /v2/tickers?contract_types=call_options&underlying_asset_symbols=BTC`, sampled on
`C-BTC-98000-301026`. **Measured**, all of it.

**Read this first: every price, IV, Greek, open-interest and band value is a JSON string.**
Only `open/high/low/close`, `volume`, `turnover`, `turnover_usd` (floats) and
`product_id`/`size`/`leverage`/`timestamp` (ints) are numbers. Parse accordingly.

| Field | Type | Example |
|---|---|---|
| `quotes.best_bid` / `best_ask` | **str** | `"498"` / `"539"` |
| `quotes.bid_size` / `ask_size` | **str** | `"6143"` / `"4952"` |
| `quotes.bid_iv` / `ask_iv` / `mark_iv` | **str** | `"0.41168255"` / `"0.41967074"` / `"0.41569626"` |
| `quotes.impact_mid_price` | null | `null` |
| `mark_price` | **str** | `"518.61229452"` |
| `mark_vol` | **str** | `"0.41570447"` |
| `greeks.delta` / `gamma` / `theta` / `vega` / `rho` | **str** | `"0.09211620"`, `"0.00001279"`, `"-18.16064981"`, `"51.33736257"`, `"10.64975020"` |
| `greeks.spot` | **str** | `"77446"` |
| `spot_price` | **str** | `"77447.8"` |
| `oi` / `oi_value` | **str** | `"9.9080"` (BTC, per `oi_value_symbol`) |
| `oi_contracts` | **str** | `"9908"` |
| `oi_value_usd` | **str** | `"768478.3512"` |
| `oi_change_usd_6h` | **str** | `"11537.0800"` |
| `price_band.lower_limit` / `upper_limit` | **str** | `"5.08984471"` / `"4077.19278685"` |
| `tick_size` | **str** | `"0.100000000000000000"` |
| `contract_value` | **str** | `"0.001000000000000000"` |
| `strike_price` | **str** | `"98000"` |
| `close` / `open` / `high` / `low` | float | `496.0` / `579.0` / `650.0` / `496.0` |
| `volume` / `turnover` / `turnover_usd` | float | `0.508` / `39752.7658` / `39752.7658` |
| `timestamp` | int | `1788284909628171` (microseconds) |
| `product_trading_status` | str | `"operational"` |

Three IV fields (`bid_iv`, `ask_iv`, `mark_iv`), plus a fourth, `mark_vol`, which shadows
`mark_iv` without matching it — `"0.41570447"` against `"0.41569626"` in the same snapshot.
Five Greeks; no vanna, vomma or charm. `probe_api.py option` prints the complete 54-field
inventory.

**There is no expiry field.** **Measured**: `'expiry_date' in ticker` is `False` on every
ticker. Expiry exists only as the `DDMMYY` symbol suffix, so every consumer has to parse
`C-BTC-98000-301026` → 2026-10-30. `/v2/products/{symbol}` does carry `settlement_time`
(`"2026-10-30T12:00:00Z"`) and `launch_time`, at the cost of one request per symbol.

**`spot_price` and `greeks.spot` disagree, and the gap is real.** **Measured** across 582 BTC
option tickers in one response: **one** distinct `spot_price` (`"77447.8"`) against **18**
distinct `greeks.spot` values spanning 77435.3 to 77450.4 — a **15.10 USD spread inside a
single snapshot**. The Greeks are not computed against a common spot. Anything that recovers a
forward or nets Greeks across strikes inherits that inconsistency. (The 2026-09-01 brief
measured 15 distinct values over 128 tickers; same phenomenon, different snapshot.)

---

## 5. Live expiries

**Measured** — 8 expiries for both underlyings, identical dates, furthest **87 days out**.

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

Totals: **BTC 298 calls / 284 puts, ETH 160 calls / 156 puts.** Six of the eight expiries are
inside a month. There is no long-dated surface here.

---

## 6. Every timeout and limit

| Limit | Value | Status |
|---|---|---|
| Rate-limit window | fixed 5 minutes, quota resets to full | **measured** — `/v2/rate_limits/quota` returned `remaining_time_in_milliseconds: 65826` |
| Quota per window | 20000 | **documented, not observed** — see note |
| Weight, public market data | 3 | **measured** — tickers, candles, orderbook, trades, assets, indices, settings, sparklines, rate_limits all cost exactly 3 |
| Weight, order write / history / batch | 5 / 10 / 25 | **documented, not observed** (needs a key) |
| 429 reset header | `X-RATE-LIMIT-RESET`, milliseconds until retry | **documented, not observed** |
| Signature validity | 5 seconds from generation | **documented, not observed** |
| Websocket connections | 150 per IP per 5 minutes; 429 on breach, wait 5–10 min | **documented, not observed** |
| Websocket idle disconnect | disconnect "if there is no activity within 60 seconds after making connection" | **documented, not observed** |
| Matching engine | 500 operations per second per product | **documented, not observed** |
| `/v2/tickers/{symbol}` | max 10 comma-separated symbols | **documented**; 3 verified working |
| `User-Agent` header | required | **measured** — omitting it returns HTTP 403 from CloudFront, not from Delta |

Three notes, and they matter for pacing:

1. **The quota ceiling is unverified and the sources disagree.** Delta's docs today say
   "Default Quota is 20000 for a fixed 5 minute window". The 2026-09-01 brief says 10000.
   I did not trip a 429 to settle it: at weight 3, reaching even 10000 needs ~3300 requests,
   which is a poor trade for one number. Budget against 10000 and you are safe either way.
2. **`/v2/rate_limits/quota` answers without an API key**, though the docs list it under signed
   endpoints. **Measured**: returns `{"current_quota": 318, "remaining_time_in_milliseconds":
   65826}`. Meter yourself against it instead of discovering the ceiling by hitting it.
3. **CloudFront serves repeats free.** **Measured**: a second identical `/v2/tickers/BTCUSD`
   within the cache window cost **0 quota** (`Cache-Control: public, max-age=60`). Polling one
   symbol faster than 60s buys you nothing and costs you nothing.

**The websocket idle disconnect was not retested.** The earlier 75-second test that failed to
reproduce it ran on testnet; this exercise was scoped to production REST, so the row above
stays *documented, not observed*. Note the docs' wording is narrower than "idle timeout" — it
says *after making connection*, which may only govern connections that never subscribe.

---

## 7. What is free

**Measured** — every request below with no API key, no IP whitelisting, only a `User-Agent`:

| HTTP 200, no key | HTTP 401, key required |
|---|---|
| `/v2/products`, `/v2/products/{symbol}` | `/v2/wallet/balances` |
| `/v2/tickers`, `/v2/tickers/{symbol}` | `/v2/orders` |
| `/v2/history/candles`, `/v2/history/sparklines` | `/v2/positions/margined` |
| `/v2/l2orderbook/{symbol}`, `/v2/trades/{symbol}` | `/v2/fills` |
| `/v2/settings`, `/v2/assets`, `/v2/indices`, `/v2/rate_limits/quota` | `/v2/profile` |

**IP whitelisting: only trading keys need it.** Keys with Trading permission require whitelisted
IPs; read-only keys do not (**documented, not observed** — no key was created for this work).
Nothing in this document required a key at all.

**One quirk worth knowing.** **Measured**: `/v2/history/sparklines` ignores its `symbols`
parameter. `?symbols=BTCUSD` and `?symbols=ETHUSD,MARK:BTCUSD` both return all **1263** live
products. It is a cheap way to get a full-market snapshot and a useless way to get one symbol.

---

## 8. Is this data safe to backtest on?

**Perps and index: yes, with two guards.** Drop every bar where `volume == 0`, and never set
`end` beyond now. You get 978 days on `BTCUSD` from 2023-12-29 — enough to test a
short-horizon perp strategy, not enough for a regime study.

**Options: no.** Not with filters, not with care — and not because the contracts are short-lived.
Their lifetimes are normal (section 1). It is that **no historical quote surface exists**. No
endpoint returns the chain as it stood on a past date, so a backtest cannot establish what it
would have entered at. What does exist is a trade log that is 98.5% padding at intraday
resolution, keeps printing after expiry, keeps printing after a worthless settlement, and on the
`MARK:` feed carries no `volume` field to filter on at all. There is no subset of this data that
supports an options backtest.

Concretely, testing even a plain hold-to-expiry straddle needs three things: the chain on entry
day, a mark for each day held, and the settlement. Delta supplies the settlement cleanly from
`/v2/products`. It supplies the entry chain **only if you are standing there live**. The daily
marks come back padded.

**Can we get ten years? No.** The longest series of any kind on this API is 989 daily bars
beginning 2023-12-18. Ten years is 3,653. The data does not exist at this venue.

**Will we need to buy history?** For anything before 2023-12-18, or for any option, yes —
Delta cannot supply it. Whether a vendor sells clean Delta India option history is a question I
could not answer from the API and did not test.

**What follows, and it is the only path.** The only trustworthy options history is history
captured going forward, by snapshotting `/v2/tickers` on a schedule and storing every row with
its fetch timestamp. Every day that passes without it is a day of surface that cannot be
recovered later. **This document does not build that.** It is the prerequisite for every
options question after this one, and it should be scoped as its own piece of work.

---

## 9. Where I contradict the 2026-09-01 brief

Four corrections. Everything else in the brief reproduced exactly.

1. **The expired-option series is not an options bug.** The brief framed it as expired option
   symbols returning a series that should not exist. **Measured**: it is a generic zero-volume
   carry-forward in the candle endpoint that also fabricates future-dated bars for `BTCUSD`,
   pads live contracts mid-life, and corrupts `MARK:` and `OI:` series identically. Framing it
   as an options problem understates the blast radius — a perp pipeline with `end = now + 1d`
   is affected too.
2. **The flatline value is the last traded price, not the settlement price.** They coincided on
   `C-BTC-60000-270624` (1237.5586 = settlement intrinsic) which makes the mechanism look
   deliberate. **Measured** on `C-BTC-60000-310726`: flatline 3912.0 against a settlement value
   of 3847.39. It is a stale print, not a settlement record.
3. **The docs' 2000-candle cap: the brief is right and I initially doubted it.** Delta's docs
   do say "only upto 2000 candles maximum in a response". **Measured** cap is 4000, and 4001
   when `end` aligns to a bucket boundary.
4. **The rate-limit quota may be 20000, not 10000.** Delta's docs today state 20000 for a fixed
   5-minute window. Neither figure is verified. Budget against 10000.

Counts drift with the clock, not with disagreement: BTC live calls read 298 here against the
brief's 295, four hours later; the expiry dates are identical.

### A fifth correction, to this document

**Added 2026-09-01, after review.** The first version of section 1 claimed *"options have no
history worth the name"* and answered the depth question with *"days"*. That was wrong, and it
was wrong in a way that would have misled a reader into blaming the wrong thing.

**Measured**, `GET /v2/products/{symbol}`: `C-BTC-98000-271126` was listed 2026-08-28 and settles
2026-11-27 — a **91-day** life. `C-BTC-90000-301026` gets 70 days. Delta runs a weekly-and-monthly
ladder and its far expiries have roughly the lifetime of a NIFTY monthly.

The original claim came from reading *"this contract has 4 daily bars"* as a statement about
option lifetimes, when it was a statement about the contract being 7 days old at the time of
measurement. A contract expiring in 87 days had not yet lived its 91 days.

This matters because it changes the argument. Options expiring is not a defect — it is how
options work everywhere. The defect is narrower and worse: Delta records **trades**, options
mostly **do not trade** (`C-BTC-60000-270624`: 54 real minutes out of 3636), and the untraded
buckets are filled with fabricated prices rather than left empty. What a backtest needs on a
no-trade day is the **mark and the IV**, and that surface is never stored at all.

## Reproducing

```
python tools/probe_api.py all              # everything, ~3 minutes, 116 requests
python tools/probe_api.py expired          # just section 2
python tools/probe_api.py depth cap --fast # sections 1 and 3, 0.4s pacing
```

Standard library only, no API key, no dependencies, read-only. `--slow` (1.5s between
requests) is the default and will not get anyone rate-limited; the run above used
`--sleep 1.0` and consumed 318 of the window's quota.
