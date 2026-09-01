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
`docs/delta-api-scope.md`. Two findings from 2026-09-01 that shape everything:

- **Perp history goes back to 2023-12-29, not ten years.** `BTCUSD` daily returns 978 candles.
- **Expired option series keep reporting prices after expiry.** `C-BTC-60000-270624` expired
  27 June 2024 and still returns candles dated today. Options history from this endpoint is not
  safe to backtest on.
