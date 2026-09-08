# The index price series, and where realised volatility can come from

**Verdict: Delta serves it, and it does not pad.** `.DEXBTUSD` returns 1-minute index candles
back to **2023-12-20**, complete, with no fabricated buckets. Realised volatility can be
backfilled for roughly two and three-quarter years rather than accumulated forward from the day
the engine started.

This is the R1 findings document ([#25](https://github.com/lalitkarthik/delta-exchange-payoff/issues/25)),
under the parent study [#24](https://github.com/lalitkarthik/delta-exchange-payoff/issues/24).

## Why this document exists

Realised volatility needs a price series reaching back as far as the chart's lookback. The whole
IV-vs-RV design assumes Delta serves 1-minute index history "easily". **Nobody had checked.**
`docs/delta-api-scope.md` line 13 is explicit — *"Perpetuals, index and funding series are out of
scope and were removed"* — so the entirety of that document measures **options**, and the one
series RV depends on is the one series this repo had never probed.

And it is the same endpoint that produced this project's founding lesson. `/v2/history/candles`
pads every bucket containing no trade with the last trade, silently: `C-BTC-60000-270624` returns
801 daily bars of which **797 are fabricated** (`docs/delta-api-scope.md` §2). An index is
computed continuously and may never have an empty bucket — or it may pad exactly like an option
and look perfectly smooth while doing it.

**The direction of that error is the point.** A padded bar has zero return. Zero returns
*suppress* realised volatility. So padding would make RV read systematically low, the chart would
show a fat volatility premium everywhere, and the conclusion would be wrong in precisely the
direction that looks most interesting — the one nobody checks.

That is why this ticket exists, and the answer turned out to be the good one.

## How to read this

Every claim is tagged. **Measured** names the run that produced it. **Not measured** says so and
says why. There is no third category, and in particular there is nothing here inferred from
Delta's documentation — the docs already state a 2000-candle cap where the real one is 4000.

All figures below are **measured** by `tools/probe_index_history.py all --fast`, run
**2026-09-08 06:59Z** against production, unless a row says otherwise.

---

## 1. The probe

`tools/probe_index_history.py`. Standard library only for the venue half, no API key, production
only, read-only. Five sections:

| Section | Question |
|---|---|
| `symbol` | Enumerate `/v2/indices`, then try every plausible candle spelling |
| `resolutions` | Which of the twelve the index actually accepts |
| `depth` | How far back it goes, and what one response covers |
| `padding` | Whether it repeats a price where nothing happened |
| `compare` | The venue's minutes against our own `spot-bars`, offline |

A transport failure is reported as status `0` and is deliberately distinguishable from a `404`,
because for eight weeks this probe could not reach the venue at all and the difference between
*unreachable* and *absent* was the entire finding. See §6.

## 2. The symbol

**`.DEXBTUSD`** — *"Bitcoin spot price index quoted in US Dollar"*, from `/v2/indices`:

| Field | Value |
|---|---|
| `index_type` | `spot_pair` |
| `price_method` | **`orderbook`** |
| `is_composite` | `true` |
| `constituent_indices` | `.DEXBTUSDT × .DEUSDTUSD` |
| `tick_size` | 0.5 |

`price_method: orderbook` is the single most useful field on this page, and it answers §4 before
§4 runs. This index is computed from resting order books, not struck from trades. A book has a
price at every instant whether or not anyone trades, so there is no empty bucket for the endpoint
to pad. The option candles that started this whole line of enquiry are trade-driven, which is
exactly why they had holes worth filling in.

It is also **composite** — BTC/USDT multiplied by USDT/USD — so it carries a stablecoin peg
inside it. At the fourth decimal that matters; for volatility over minutes it does not. Recorded
rather than acted on.

### The nine spellings, and the trap in the result

| Symbol | Status | Bars | |
|---|---:|---:|---|
| `.DEXBTUSD` | 200 | 60 | **serves** |
| `.DEXBTUSDT` | 200 | 60 | serves |
| `BTCUSD` | 200 | 60 | serves |
| `MARK:BTCUSD` | 200 | 60 | serves |
| `DEXBTUSD` | 200 | **0** | 200 with an empty array |
| `BTCUSDT` | 200 | 0 | 200 with an empty array |
| `INDEX:BTCUSD` | 200 | 0 | 200 with an empty array |
| `SPOT:BTCUSD` | 200 | 0 | 200 with an empty array |
| `.BTCUSD` | 200 | 0 | 200 with an empty array |

**A wrong symbol is not an error here.** Five of the nine return `HTTP 200` with `result: []` —
the same shape a real symbol returns for a window in which nothing happened. Anything reading this
endpoint must treat an empty result as *unknown*, never as *quiet*, or a typo in a symbol becomes
a flat stretch on a volatility chart. This is the padding lesson wearing a different hat.

Two bugs in the probe were found by running it, both now fixed:

- It enumerated `/v2/products?contract_types=spot_index`, which **400s** — `spot_index` is not in
  Delta's `contract_types` enum, and indices are not products. `/v2/indices` is the route, and it
  returns 461 of them.
- Once that was fixed, the enumeration's own order chose the default symbol, and the first BTC-ish
  entry is `.DEXBTINR` — the rupee-quoted index. The `compare` section duly reported a price
  discrepancy of 98x, which is the USD/INR rate and a nice accidental proof that the comparison
  works. The list also contains `.DEAIXBTUSD`, a **different asset** whose ticker merely contains
  the letters `BT`. The documented candidate now leads and the enumeration follows.

## 3. Resolutions and depth

**Eleven of the twelve** resolutions serve the index. Only `5s` fails, with `bad_schema`.

| | Measured |
|---|---|
| Resolutions served | `1m 3m 5m 15m 30m 1h 2h 4h 6h 1d 1w` |
| Refused | `5s` (HTTP 400) |
| Bars per response | **4,000** — 66.7 hours at 1m |
| Daily history | **996 bars, oldest 2023-12-18** |
| 1-minute history | **reaches ~993 days, to about 2023-12-20** |

The 1-minute depth is the number the design turns on, and one page cannot measure it: a request
spanning a year returns the newest 4,000 bars and tells you nothing about the far end. It was
measured instead by asking for **narrow two-hour windows at increasing age** and binary-searching
the boundary:

| Window starts | Bars returned |
|---|---|
| 1, 7, 30, 90, 180 days ago | 120 of 120 |
| 365 days ago | 120 of 120 |
| 730 days ago | 120 of 120 |
| 991 days ago | 120 of 120 |
| 995 days ago | **0** |

Every window in that sweep came back **complete** — 120 buckets of a possible 120, at every age
including two years back. The 1-minute series therefore begins within about two days of the daily
series, and there is no thinning with age.

**This changes R4's bound.** The endpoint today refuses every lookback because our own store holds
about three days against a seven-day expiry floor. That constraint is now a choice rather than a
fact: the realised side can be backfilled from the venue for ~2.7 years. The implied side cannot —
Delta's history carries no IV and no bid/ask at all — so the chart would gain a long realised line
against a short implied one. Worth doing, deliberately **not** done here; it is a change to R4's
data source, not to R1's question. Filed as the next step in §7.

## 4. Padding: it does not

Six hours of 1-minute bars, the section this ticket was really written for:

| | Measured |
|---|---:|
| Bars returned | 360 |
| Buckets in the span | 360 |
| **Gaps** | **0** |
| Distinct closes | 353 |
| Repeated closes | 2 |
| **Longest flat run** | **2 bars** |
| Bars with `open = high = low = close` | 1 |
| `volume` | `null` on every bar |

**No padding.** The test is *runs* of identical closes, not flat bars counted alone — a single
flat bar on a fast-moving index is a minute in which the price happened to return to where it
started, and proves nothing. A run of them is padding's signature. The longest run here is two.

Compare the option series that motivated the check: 797 of 801 daily bars fabricated, in runs
hundreds long. These are not the same endpoint behaving differently on a whim; they are a
trade-driven series and a book-driven one, and §2's `price_method` predicted it.

`volume` being `null` on every bar is the corroborating detail. There is no volume because there
are no trades — nothing is traded *as* the index. A series with null volume and no gaps is
computed, and that is what we want under a realised-volatility estimator.

## 5. Their bars against ours

The offline half, **measured** 2026-09-08 against `data/spot-bars`. Sixteen minutes are covered
by both sources.

**The prices agree.** Relative differences, median and worst:

| | Median | Max |
|---|---:|---:|
| `close` | 8.818e-06 | 1.813e-04 |
| `open` | 3.777e-06 | 4.725e-04 |
| `high` | 1.825e-05 | 4.523e-04 |
| `low` | 1.574e-05 | 1.498e-04 |

Agreement to a part in 10⁵ on the close. Whatever `spot_price` is on the ticker channel, it is the
same series `.DEXBTUSD` serves — which incidentally retires half of the open question the previous
version of this document left in §5.

**The ranges do not, and they fail in the direction nobody predicted.**

| | Median range / close |
|---|---:|
| Ours, from the ticker channel | 1.341e-04 |
| Theirs, from `.DEXBTUSD` candles | **1.769e-04** |
| Minutes where ours is wider | **0 of 16** |

The ticket predicted the opposite. `spot-bars` folds a high and low out of ~7,600 ticker frames a
minute; the reasoning was that watching a path that often must catch extremes a candle would miss.
Ours is narrower on **every single minute**, by about a third at the median.

That is a direct corroboration of the hypothesis already recorded in `docs/iv-vs-rv.md` §4: the
frame count is not the observation count. Every one of ~588 contracts' ticker frames carries the
same top-level `sp`, so a minute holding 7,600 frames may hold only a few dozen *distinct* spot
observations. A range estimator reads the extremes it can see, and a coarsely sampled path never
reaches as far as a finely sampled one — the same −0.63/√n discretisation bias that made
Parkinson, Garman-Klass and Rogers-Satchell read about half the close-to-close pair on real data.

**So the range estimators should read `.DEXBTUSD`, not `spot-bars`.** Sixteen minutes is not
enough to act on, and it is enough to know what to measure next: the two sources give materially
different answers to Parkinson and Garman-Klass, and the venue's is the less biased of the two.

### Our store, for the record

**Measured** 2026-09-08 against the whole of `data/spot-bars`:

| | |
|---|---:|
| Rows | 46 |
| First / last minute | 2026-09-04 05:41Z / 2026-09-07 10:58Z |
| Span | 4,637 minutes |
| **Minutes with no row** | **4,592** |
| Ticks per minute | 643 min, 7,568 median, 8,944 max |
| Range / close | 2.745e-04 median, 8.389e-04 max |
| Bars with `open = high = low = close` | 1 |
| 1-minute log-return standard deviation | 3.785e-04 over 42 adjacent returns |

Forty-six rows across three days. The writer has not run continuously, and the 4,592 absent
minutes are absent *correctly* — no row, not a null and not a repeat, exactly as the
never-forward-fill rule requires. This is why R5's line-breaking and R2's gap-dropping are not
hypothetical robustness: they fire on the only data this project currently holds.

The 3.785e-04 return standard deviation is an **assumed** guide to scale over 42 returns, not an
estimate of anything — the number against which an estimator returning something wildly different
is wrong rather than interesting.

## 6. Why this took eight weeks, and it was never Delta

The previous version of this document recorded the venue as **unreachable**: TCP connected in
47 ms and the TLS handshake never completed, on IPv4 and IPv6, with and without SNI, sandboxed and
not. That was recorded as a network path problem between this machine and Delta's edge, and it was
correct not to record it as a finding about Delta.

It was not a path to Delta. It was **this machine's MTU**, and it broke TLS to everything.

| Check | Result |
|---|---|
| `eth0` MTU | 1400 |
| Largest packet that survives, DF set | **1280** |
| 1284-byte packet | 100% loss, no ICMP reply |
| TLS to `api.india.delta.exchange` | hangs after ClientHello |
| TLS to `api.github.com` | **hangs identically** |
| Plain HTTP to either, port 80 | **answers in 0.3 s** (`example.com` 200, Delta 301) |
| TLS with MSS clamped to 1240 | **TLSv1.3 in 0.77 s** |

A classic path-MTU black hole. The interface advertises 1400, the real path carries 1280, and no
`fragmentation needed` comes back — so nothing lowers its estimate. TCP's own handshake is small
and completes, which is why the connection looked healthy. The server's ServerHello and
certificate chain are full-size, and vanish. Hence: connects, then hangs, always at the same
place.

The earlier diagnosis missed it because `api.github.com` answered in 138 ms that day, which made
the failure look Delta-specific. It was intermittent because the path was.

Both fixes work; the first is the real one:

```sh
sudo ip link set dev eth0 mtu 1280      # persist in /etc/wsl.conf or the VPN profile
```

This run used the second: a `sitecustomize.py` on `PYTHONPATH` clamping `TCP_MAXSEG` to 1240
before connect, which asks the far end to stay under the path MTU. Scaffolding for one probe run,
not committed.

**The lesson is the one the probe was already built for.** Eight weeks of "the venue is
unreachable" was a true statement about a symptom and a wrong one about a cause, and the only
reason it cost nothing is that the finding was filed as *unreachable* rather than as *absent*. Had
this document said "Delta does not serve index history", R4 would have been built against
`spot-bars` forever.

## 7. What to do next

1. ~~**Backfill RV from `.DEXBTUSD`.**~~ **Done** — #54, `tools/backfill_index_bars.py`,
   writing table E `index-bars`. 30 days fetched in 11 pages: 43,200 rows for 43,200 minutes,
   no gap and no duplicate, and a re-run over the same range adds only the minute that ticked
   over while it ran. `/volatility/bounds` reports `usable: true` for the first time since the
   screen shipped, `max_days` 30.00 against `min_days` 7.24.
2. ~~**Point the range estimators at the venue's bars, not ours.**~~ **Done, and the hypothesis
   holds.** Measured over the window both sources cover: the spread across the four estimators
   falls from **99% to 33%**, and the movement is concentrated exactly where discretisation
   predicts — Parkinson 1.58x, Garman-Klass 1.70x, Rogers-Satchell 1.72x, close-to-close 1.14x.
   The estimators that read extremes were the ones being starved. A third of a spread survives
   and is now the open question; see `docs/iv-vs-rv.md` §4.
3. **Fix the MTU** (§6) before anything else needs the network. Still outstanding: the backfill
   above was run with a `TCP_MAXSEG` clamp rather than a corrected interface.
4. **Grow the implied side.** It is now the binding constraint and cannot be backfilled at all —
   Delta's history carries no IV and no bid/ask. 481 realised points against **1** implied one on
   a ten-day lookback. Only engine uptime moves that number.

## 8. Open, and deliberately not closed here

**Whether `.DEXBTUSD` is the settlement index.** §5 establishes that our `spot_price` and
`.DEXBTUSD` are the same series to a part in 10⁵. It does **not** establish that either is what
the options settle against — `docs/settlement.md` reports `spot_index = .DEXBTUSD` on a settled
product record, which is suggestive and is not a measurement of the settlement price itself. If
they differ, RV measures a slightly different asset than IV implies, and the wedge sits inside the
premium unlabelled.

**The stablecoin leg.** `.DEXBTUSD` is `.DEXBTUSDT × .DEUSDTUSD` (§2). A peg wobble enters the
index as a price move and would be measured as realised volatility. **Not measured** — the size of
that contribution is a subtraction away, `.DEUSDTUSD` being separately available, and nobody has
done it.

**Whether the 4,000-bar page truncates the oldest or the newest.** `docs/delta-api-scope.md` says
oldest. Not re-verified here for the index, and it decides whether a backfill loop walks forward
or backward.
