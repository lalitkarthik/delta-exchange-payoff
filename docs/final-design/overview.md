# Overview

**delta-exchange-payoff** is an option chain, volatility and payoff workbench for
[Delta Exchange India](https://www.delta.exchange/) crypto options. It holds one websocket to the
venue, solves implied volatility and Greeks from the order book rather than reading the venue's,
streams the result to a browser once a second, and folds the same stream into a permanent
one-minute Parquet record.

It is a **learning and research system**, not an execution system. There is no order path, no API
key and no private endpoint: everything here runs on public market data.

## The thesis

**The two venue channels are not interchangeable, and that gap is the whole point.**

| Channel | Carries | Refresh |
|---|---|---|
| `ob_l2` | top of book | `measured` **508 ms** per contract |
| `ticker` | spot, open interest, and the venue's own IV and Greeks | `measured` **5,001 ms** per contract |

The venue's implied volatility is fitted to prices that are, on average, **9.8x** staler than the
book we can see. So Delta's IV and Greeks are carried as **reference columns only and never
consumed as inputs** -- `tests/test_no_delta_inputs.py` pins that -- and the system recovers its
own forward, its own volatility and its own Greeks from the book, then stores both side by side so
the difference is measurable rather than asserted.

## What it does

### Live option chain

Every listed expiry for BTC and ETH, one at a time, calls left and puts right, streamed over
`/ws/chain`. The websocket sends the identical object `/chain` returns, so one renderer serves
both. A feed badge on the ladder header appears whenever the connection is not `connected` and
clears on recovery.

### Implied volatility, solved here

**IV is a property of the strike, not of the leg.** It is recovered by inverting the
**out-of-the-money** leg's bid/ask midpoint -- calls above the forward, puts below -- where the
whole price is time value and vega is largest, then written to both legs with `iv_leg` naming the
source. A leg with no volatility carries **no Greeks**: reporting them at a default sigma would put
five plausible numbers on screen that describe nothing.

**Four solvers and four forwards, built to be compared.** S1 Newton, S2 Brent, S3 Jaeckel-shaped,
S4 vectorised; F1-F4 for the forward. `agreement.py` measures where they disagree. They exist
because "which method" is a question this project wants answered with data.

### Greeks

Delta, gamma, vega, theta and rho, in the sibling project's conventions rather than the textbook's:
delta and gamma undiscounted, vega and rho discounted and per one percent, theta a one-calendar-day
repricing. Delta India's options are **vanilla, linear and USD-settled**, so textbook
Black-Scholes and put-call parity apply with no correction term.

### The volatility surface and the smile

`/smile` serves the stored surface for one expiry; the browser renders it with the fitted curve
and the points it was fitted from. Implied against realised volatility (`/volatility`) puts our
solved IV beside a realised series computed from the index, over a chosen window.

### Structures

Every straddle and strangle on one expiry, priced on one grid, from the same live ladder.

### History, with no invention

Four read paths answer from the Parquet store: a whole ladder at a past minute (`/chain/at`), the
minutes that exist (`/chain/minutes`), one contract's day of candles (`/bars`), and the volatility
series. A time slider on the ladder has "live" at its right edge and every stored minute of the day
to its left.

**Never forward-fill.** A minute with no arrivals produces **no row** -- not nulls, and never the
previous close. This is the system's moral as well as its rule: Delta's own
`/v2/history/candles` pads with the last trade and does not say so. `C-BTC-60000-270624` returns
801 daily bars of which **797 are fabricated**; with `end` set to the contract's
`settlement_time` the same request returns 4 bars, all real.

### The recording

Five hive-partitioned dataset roots -- `quote-bars`, `reference-bars`, `spot-bars`,
`computed-bars` and `index-bars` -- written by a lossless consumer so that a dropped message is
never a permanent hole. What the book did, what the venue said, and what we made of it, each in its
own table, joinable on `date` and `underlying`. See [Data store](data-store.md).

### Alerts

An `alert` event is anything a person should see: a nearly spent reconnect budget, a stale
connection, a failed flush, an empty generation while recording. A dedicated consumer posts them to
a Discord webhook and never opens the feed, store or api to do it.

## What it is not

- **Not an execution system.** No orders, no positions, no API key. The event envelope is shaped so
  an order path could share it, and that is all.
- **Not a backtester**, though the store is written so one can be built on it in any language --
  the same tree answers Polars, DuckDB and Athena with no export step.
- **Not a fork of the NIFTY sibling.** The architecture carries over; the maths does not.

## The shape of the code

```
engine/   FastAPI. One venue socket, the pure pricing core, the store writer, every route.
web/      Next.js. Renders. Computes nothing and calls parseFloat nowhere.
tools/    Probes. Every number in the docs came from one of these.
docs/     Contracts, design records and measured findings.
```

**The pure core** -- `chain.py`, `wire.py`, `convert.py`, `compute.py`, `forward.py`,
`solvers.py`, `black76.py`, `black_scholes.py`, `greeks.py`, `bars.py` -- takes data in and returns
data out: no socket, no clock, no filesystem. Only `delta_client.py` talks to Delta and only
`store.py` touches a file. That is why the suite can be both large and fast.

## Related guides

- [Getting started](getting-started.md) -- install and run it
- [Architecture](architecture.md) -- the processes, the topologies and the AWS shape
- [Events](events.md) -- the ten events everything crosses on
