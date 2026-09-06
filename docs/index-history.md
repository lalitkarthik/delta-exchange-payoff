# The index price series, and where realised volatility can come from

**Verdict: not established. The venue was unreachable from the machine that ran the probe, and
that is written down as unreachable rather than as absent.** The probe exists, it refuses to
draw a conclusion it did not earn, and the one thing that *was* measurable — our own stored
spot series — is measured below.

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
direction that looks most interesting.

## How to read this

Every claim is tagged. **Measured** names the run that produced it. **Not measured** says so and
says why. There is no third category, and in particular there is nothing here inferred from
Delta's documentation — the docs already state a 2000-candle cap where the real one is 4000.

---

## 1. The probe

`tools/probe_index_history.py`. Standard library only for the venue half, no API key, production
only, read-only. Five sections:

| Section | Question |
|---|---|
| `symbol` | Enumerate `/v2/products?contract_types=spot_index`, then try every plausible candle spelling |
| `resolutions` | Which of the twelve valid resolutions the index serves |
| `depth` | How far back `1d` reaches, and what one `1m` page covers |
| `padding` | Whether the series repeats a price where nothing happened |
| `compare` | The venue's minutes against our `spot-bars`, for minutes both cover |

```sh
engine/.venv/bin/python tools/probe_index_history.py all
engine/.venv/bin/python tools/probe_index_history.py symbol --timeout 6
```

**It distinguishes "the venue refused us" from "we could not ask."** A transport failure returns
status `0` and prints `UNREACHABLE`, and the run exits without probing anything downstream rather
than reporting nine empty results that would read like nine negative findings. That distinction
is the whole reason the probe is worth committing even from a machine that cannot run it.

## 2. What the run found: nothing, and why

**Measured**, `probe_index_history.py all --timeout 6 --sleep 0.2`, run 2026-09-06 13:20:06Z:

```
  enumerate spot indices from /v2/products
  !! The venue was not reached. This is NOT evidence about the endpoint.
  !! transport error: URLError: <urlopen error _ssl.c:983: The handshake operation timed out>

    symbol           status   bars  note
    .DEXBTUSD             -      -  UNREACHABLE: handshake operation timed out
    .DEXBTUSDT            -      -  UNREACHABLE: handshake operation timed out
    DEXBTUSD              -      -  UNREACHABLE: handshake operation timed out
    BTCUSD                -      -  UNREACHABLE: handshake operation timed out
    BTCUSDT               -      -  UNREACHABLE: handshake operation timed out
    MARK:BTCUSD           -      -  UNREACHABLE: handshake operation timed out
    INDEX:BTCUSD          -      -  UNREACHABLE: handshake operation timed out
    SPOT:BTCUSD           -      -  UNREACHABLE: handshake operation timed out
    .BTCUSD               -      -  UNREACHABLE: handshake operation timed out

    serving symbols: (none)
```

`.DEXBTUSD` heads the candidate list because it is not a guess: `docs/settlement.md` line 82
records `spot_index = .DEXBTUSD` on a settled option's product record. The remaining eight are
the shapes this venue uses elsewhere, tried so that a failure can be attributed to the symbol
rather than to the endpoint.

### The failure is the network path, not Delta and not the code

**Measured** on the same machine, same minute:

| Check | Result |
|---|---|
| `getent hosts api.india.delta.exchange` | resolves, 8 AAAA records via `d15zy4kc8a63om.cloudfront.net` |
| TCP connect to `:443` | **succeeds in 0.047 s** (IPv6) and 0.094 s (`curl -4`) |
| TLS handshake | **never completes**, on IPv4 and IPv6 alike, with and without SNI |
| `curl https://api.github.com` | **HTTP 200, TLS in 0.138 s** |
| Sandbox disabled | identical failure |

So: DNS works, the SYN-ACK comes back from CloudFront, and the ClientHello goes unanswered.
General HTTPS from this host is healthy. This is a path problem between this machine and Delta's
edge — the same intermittent unreachability the ticket anticipated — and it is **an environment
problem, not a scope problem**.

**What is therefore still unknown, and must not be assumed either way:**

- whether any symbol serves BTC index candles, and which spelling
- which resolutions it accepts
- how far back it reaches
- **whether it pads** — the finding that actually decides whether RV can use it

## 3. What was measurable: our own `spot-bars`

The offline half needs no venue. **Measured** 2026-09-06 against `data/spot-bars`, the whole
store as it stands:

| | |
|---|---:|
| Rows | **30** |
| First minute | 2026-09-04 05:41:00Z |
| Last minute | 2026-09-04 06:12:00Z |
| Span | 32 minutes |
| **Minutes missing** | **2** |
| Ticks per minute | 643 min, **8,256 median**, 8,944 max |
| Range / close | 3.137e-04 median, 8.389e-04 max |
| Bars with `open = high = low = close` | 1 |
| 1-minute log-return standard deviation | **5.225e-04** over 29 returns |

Four things follow, and all four matter downstream.

**The store holds half an hour, not a day.** The design assumed a day's IV history would exist by
2026-09-04 EOD. It does not — the writer ran for roughly 32 minutes. Until it runs continuously,
R4's upper bound on `N` is bounded by *history held*, and that is the constraint that will bind
for the first month. R5 must say so on screen; an empty chart otherwise reads as a bug.

**The gap is already here.** One 180-second step where a 60-second one belongs: two minutes with
no arrivals produced **no rows**, exactly as the never-forward-fill rule requires. So R2's
gap handling and R5's line-breaking are not hypothetical robustness — they fire on the only data
this project currently has.

**The flat bar is real, not padding.** One bar in thirty has all four prices equal. On a series
sampled from 8,000-odd ticker frames a minute that is a minute in which the index genuinely did
not move, and it is the reason the padding test in §1 reads *runs* of identical closes rather
than counting flat bars alone — a single flat bar proves nothing, a run of them proves padding.

**A 1-minute return standard deviation of 5.2e-04** is the order of magnitude R2's tests should
expect from this asset, and the number against which any estimator returning something wildly
different is wrong rather than interesting. Note it is computed over 29 returns and is an
**assumed** guide to the scale rather than a stable estimate of anything.

## 4. What to do when the venue is reachable

1. Run `probe_index_history.py all` from a machine that can reach the edge, and replace §2.
2. If a symbol serves candles: read the `padding` section first. Long runs of identical closes
   mean the series is padded and **must not** be fed to RV unfiltered.
3. Run `compare`. Our `spot_high`/`spot_low` come from ~8,256 ticker frames a minute; a candle's
   come from trades. If the ranges differ materially, Parkinson and Garman-Klass will give
   different answers depending on which source they read — a finding worth having before it
   appears as an unexplained line on a chart.
4. If no index candle series exists, RV is limited to `spot-bars` going forward, and the chart's
   range is bounded by when the engine started. Write that down as plainly as the alternative.

## 5. Open, and deliberately not closed here

**`spot_price` versus `settlement_index_price` is unconfirmed.** `docs/settlement.md` does not
establish they are the same series. Our `spot-bars` are folded from the ticker's top-level
`spot_price`; the options settle against the settlement index. If the two differ, RV measures a
slightly different asset than IV implies, and the wedge would sit inside the premium unlabelled.
Not measurable without the venue either.
