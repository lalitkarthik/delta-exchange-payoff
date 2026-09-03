# Settlement: what inverse would have changed, and why it does not apply

**Verdict: Delta India's options are vanilla — linear, USD-quoted, USD-settled. They are not
inverse.** Textbook Black-Scholes and textbook put-call parity apply with no correction term
anywhere. This was the project's highest-risk unknown; it is now measured, not assumed.

The BTC in a Delta option contract is the *unit of size*, not the *unit of payout*. Those are
two different things, and conflating them is the whole trap.

## How to read this

Every claim is tagged. **Measured** names the request that produced it, made on 2026-09-03
against `https://api.india.delta.exchange` with no API key. **Assumed** means it is not yet
verified and should not be built on.

---

## 1. The two settlement styles

An expiring option must say who pays whom, how much, and **in what currency**. The third part
is the one that splits the world in two.

**Vanilla (linear).** A BTC call struck at 60,000 settles at 63,847. The holder is owed 3,847
**dollars** per unit of underlying. Payoff is a straight line in the underlying price. Every
equity and index option works this way, NIFTY included.

**Inverse.** The exchange never touches dollars. It owes 3,847 dollars *worth of BTC*, valued
at the settlement price: `3847 / 63847 = 0.0603 BTC`. Payoff in the currency actually received
is `max(S-K, 0) / S` — a concave curve, not a line. Deribit's classic BTC options work this
way.

| | Vanilla | Inverse |
|---|---|---|
| Payoff | `max(S-K, 0)` USD | `max(S-K, 0) / S` coin |
| Shape in `S` | straight line | concave curve |
| Black-Scholes | applies directly | needs a change of numeraire |
| Put-call parity | `C - P = D(F - K)` | a different identity |
| Greeks | textbook | every one carries a correction term |

**The failure mode is silent.** An inverse contract priced as vanilla does not crash, does not
throw, and does not produce absurd numbers. It produces plausible ones that are wrong. That is
why this note exists and why it was written before any pricing code.

---

## 2. What inverse would have cost us

Recorded so the dodge is legible, and so it is obvious what to redo if we ever add a venue that
is genuinely inverse.

- **The parity identity changes.** `C - P = D(F - K)` is derived by replicating a forward with
  a call and a put, and it assumes both legs pay in the same currency the forward is struck in.
  Under inverse settlement both legs pay in coin and the replication no longer produces a
  linear-in-`K` relationship. **T1's entire `F1`/`F2` construction — the OLS line through
  `C - P` against `K` — would have had no straight line to fit.**
- **Greeks would all need correction terms**, because the quantity being differentiated is
  `V/S`, not `V`. Every one of delta, gamma, vega, rho picks up extra terms from the quotient
  rule.
- **Implied volatility would be solving a different equation**, which puts T2's whole
  method-comparison on a different footing.

None of this applies. It is written down only so nobody re-derives the fear later.

---

## 3. The evidence

Three independent confirmations, strongest last.

### 3.1 The contract metadata says vanilla

**Measured**: `GET /v2/products/P-BTC-90000-040926`

```
notional_type          = vanilla
settling_asset         = USD
quoting_asset          = USD
is_quanto              = false
contract_unit_currency = BTC        <- size is in BTC; payout is not
contract_value         = 0.001
underlying_asset       = BTC
spot_index             = .DEXBTUSD
```

`notional_type: vanilla` is Delta's own field for exactly this distinction. `is_quanto: false`
rules out the third possibility — a contract paying a foreign-currency amount at a fixed rate,
which would carry its own correction.

### 3.2 It holds for every product on the venue

**Measured**: full cursor walk of `/v2/products?page_size=500`, 1,255 products returned,
1,031 of them options.

```
notional_type:   {'vanilla': 1255}
settling_asset:  {'USD': 1251, None: 4}
is_quanto:       {False: 1255}
```

The four products with no `settling_asset` are the INR spot pairs — `BTC_INR`, `ETH_INR`,
`SOL_INR`, `XRP_INR` — not derivatives. Restricted to options alone, all 1,031 are
`vanilla` and settle in `USD`, with no exceptions.

Zero inverse contracts exist on Delta India. Option contract sizes:
BTC 0.001 (594 contracts), ETH 0.01 (318), XAUT 0.001 (119).

This matters more than 3.1 on its own: it means the finding is a property of the venue, not of
the one symbol I happened to pick.

### 3.3 Settled prices prove it arithmetically

The strongest evidence, because it is the exchange's own money changing hands rather than a
label in a JSON document.

The 31 Jul 2026 12:00 UTC expiry settled against index **63,847.391666666656**.

**Measured**: `GET /v2/products/{symbol}` on three expired contracts, reading
`settlement_price`.

| Contract | Delta's `settlement_price` | Vanilla predicts | Inverse would predict |
|---|---|---|---|
| `C-BTC-60000-310726` | 3847.391666666656 | **3847.391666666656** | 0.06025918 |
| `C-BTC-70000-310726` | 0 | **0** | 0 |
| `P-BTC-90000-310726` | 26152.608333333344 | **26152.608333333344** | 0.40961 |

Exact to the last digit under `max(S-K, 0)`. Wrong by three orders of magnitude under
`max(S-K, 0)/S`. There is no reading of this that leaves the question open.

### 3.4 A fourth corroboration, cheap

**Measured**: fixture `engine/tests/fixtures/tickers-btc-04-09-2026.json`, symbol
`P-BTC-90000-040926`, spot 77,568.2. Delta reports `greeks.delta = -1.00000000`. That is the
vanilla answer for a put that deep in the money. An inverse put's delta at that strike is
nowhere near `-1`.

---

## 4. What the numbers are actually denominated in

The part that is genuinely easy to get wrong, now that inverse is off the table.

- **`mark_price`, `best_bid`, `best_ask`, `strike_price`, `settlement_price` are all USD per
  one unit of the underlying** — per 1 BTC, not per contract.
- **`contract_value` converts to a contract.** Premium per contract =
  `mark_price * contract_value` USD. **Measured**: `P-BTC-90000-040926` marks at 12,431.8, so
  one contract costs 12.4318 USD.
- **`oi` and `oi_value` are in the underlying** (BTC); `oi_value_usd` is that notional in USD.
  **Measured**: `oi_contracts = 2700`, `oi_value = 2.7` BTC = 2700 * 0.001.
- **`turnover` is USD** (`turnover_symbol: USD`); `volume` is in the underlying.

**So `contract_value` never enters a pricing or parity calculation.** It is a lot-size
multiplier applied at the very end, when converting a per-unit price into money. Put-call
parity, implied vol, and every forward in T1 operate on the per-unit USD prices exactly as
quoted. Multiplying by `contract_value` inside a solver would be a bug.

One consequence worth stating plainly for T1: because prices arrive per-unit and in USD, the
`C - P` versus `K` regression is in consistent units with no conversion at all. The chain
snapshot goes straight into the fit.

---

## 5. Still open

- **What "USD" is as a settling asset on Delta India.** `settling_asset` is a USD balance with
  `minimum_precision: 2`. The venue also lists INR spot pairs (§3.2), so the book underneath is
  plausibly INR-funded with a USD-denominated derivatives ledger on top — but that is
  **assumed**, not measured. It does not change the payoff maths, which is linear in USD either
  way, but it would matter for a funding or collateral model.
- **Delta's global exchange is a different venue.** Everything here is
  `api.india.delta.exchange`. If we ever point at another Delta host, re-run section 3.2 before
  assuming it carries over.
- **`greeks.rho` scaling does not obviously reconcile** with a textbook vanilla rho on the
  fixture. That is a units/convention question, not a settlement question, and belongs to T2's
  agreement matrix. Flagged here so it is not mistaken later for evidence of inverse settlement.
