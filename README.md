# delta-exchange-payoff

Option chain and payoff analysis for **Delta Exchange** crypto options — the sibling of
[`convex-hedge-payoff`](https://github.com/lalitkarthik/convex-hedge-payoff), which does the same
job for NIFTY.

**Goal one is the chain on screen.** BTC and ETH, one expiry at a time, calls left and puts
right, implied vol and Greeks straight off the venue. No calculation of our own yet.

| | |
|---|---|
| The engine/web interface | [`docs/chain-contract.md`](./docs/chain-contract.md) |
| What the API actually gives you | `docs/delta-api-scope.md` *(in progress)* |

State lives in the issues, not in files. If an issue and a file disagree, the issue wins.

## Layout

```
engine/   FastAPI. Fetches from Delta, pivots tickers into a chain, computes nothing.
web/      Next.js. Renders the ladder. Does no arithmetic.
tools/    Probes that measure the API and regenerate the numbers in the docs.
docs/     Contracts and measured findings.
```

## Why it is not a fork of the NIFTY project

The architecture carries over; the maths does not. Crypto options here are **inverse-settled** —
quoted in USD, margined and settled in the underlying — and Delta **supplies** implied vol and
Greeks rather than making you recover a forward from the chain. `convex-hedge-payoff` exists
largely to reimplement Black-76 and recover a forward that no venue publishes. Neither problem
is the same one, so the code starts fresh and the conventions carry.

## Data caveats

Before using any historical series from this venue for a backtest, read
[`docs/delta-api-scope.md`](./docs/delta-api-scope.md). The finding that shapes everything:

- **Set `end` to the contract's `settlement_time`, never to `now`.** With `end = now`,
  `C-BTC-60000-270624` returns 801 daily bars of which **797 are fabricated** — the last trade
  copied forward for two years past expiry. With `end = settlement_time` the same request
  returns 4 bars, all real.
- **Under that one rule the history is clean and usable**, back at least to June 2024: traded
  OHLCV, a mark price series, and open interest, per contract.
- **What history does not carry** is bid/ask and implied vol. Those exist only on the live
  ticker, which is why a `/v2/tickers` snapshotter is still worth standing up.
