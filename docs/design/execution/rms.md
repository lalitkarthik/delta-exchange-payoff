# The risk checks

**What this page contains.** The six checks every order passes through before it is sent, what each
one compares against what, where its numbers come from, and what happens when a check fails.

**How to read it.** The principle first, then one section per check, in the order they run. The
configuration table at the end shows the shape of the file a person edits.

## The principle: yes or no, never "smaller"

The **RMS** (risk management system) is a module inside the OMS. Every order intent goes through it
before it becomes an order. A check either passes the intent or **rejects it outright**, publishing an
event that names the check and the numbers it compared. **No check ever changes an order**: it does not
shrink the quantity, move the price, or drop a leg. A scaled order silently changes what a strategy
meant, and a value that quietly stops meaning what it says is the defect this repository has learned to
fear most.

Two families of check exist, as the senior's diagram draws them:

- **Strategy-wise** checks compare the intent against limits set for that strategy.
- **Engine-wise** checks compare it against limits set for the whole strategy set.

A limit that is not configured is not checked, and the startup log says which checks are active.

## The six checks, in the order they run

The senior named these six as required. Each is written here as one sentence of arithmetic.

### 1. Order check

| Compares | Against | From |
|---|---|---|
| The intent's quantity | The strategy's maximum lots per order | configuration |
| The limit price the execution module would use | A band of ± X % around the **mid** of the contract's current quote | the latest quote on the bus; X from configuration |

Mid is the reference rather than the last traded price, because on a wing the last trade can be hours
old, and rather than our theoretical price, because that is only as good as the volatility fit behind
it. An intent whose contract has no two-sided quote fails this check.

### 2. Margin limit

| Compares | Against | From |
|---|---|---|
| Blocked margin now, plus this order's estimated margin | The engine's margin ceiling | the wallet's blocked-margin figures from the adapter; the estimate from a margin model in the adapter; the ceiling from configuration |

Delta offers no endpoint that estimates the margin a proposed order would need, so the estimate is
ours, kept in the adapter with the broker's published rules. The paper broker uses the same model.
After each fill the wallet's actual blocked margin is read back, so the estimate is checked against the
truth every time it is used.

### 3. Position limits

| Compares | Against | Scope |
|---|---|---|
| Lots held plus working plus this intent | A maximum, per **contract** | strategy-wise |
| The same, summed over an **underlying** | A maximum per underlying | strategy-wise |
| The same, summed over the **strategy** | A maximum per strategy | strategy-wise |
| The same, summed over the **whole engine** (Book 4 plus all working) | A maximum for the engine | engine-wise |

Each is one number in configuration. Any not set is not checked.

### 4. Instrument and underlying restrictions

| Compares | Against |
|---|---|
| The intent's contract | A configured **ban list** of contracts, underlyings or expiries |
| The intent's contract | The **illiquidity rule**: rejected if the contract has had no two-sided quote in the last 60 seconds |

The ban list is what a person edits when a venue announces a restriction. The illiquidity rule is the
only automatic one in this phase; open-interest and spread-width rules are later additions.

### 5. Profit and loss tracking

Not a check that rejects, but the input the sixth check reads. Per strategy and for the engine:

| Figure | How it is computed |
|---|---|
| **Realised** | From fills: what was received minus what was paid for closed lots, minus fees |
| **Unrealised** | Open lots marked at the current mid |
| **Day's total** | Realised since the day began plus unrealised now |

Published on the bus every second and stored every minute. Discord does not receive these.

### 6. Mark-to-market loss limits

| Compares | Against | Scope |
|---|---|---|
| The strategy's day's total | The strategy's daily loss limit | strategy-wise |
| The engine's day's total | The engine's daily loss limit | engine-wise |

**On breach: new opening orders are rejected, closing orders still pass, and an alert is raised.**
Nothing is flattened automatically. An automatic flatten is a burst of orders at the worst moment of
the day, and it is the kind of behaviour to add only after a person has watched the system run.

## What a rejection looks like

A rejected intent produces one event: which check, the strategy, the contract, the number compared and
the limit it broke, and the identifiers linking it to the target that caused it. The strategy sees no
fill arrive and stays where it was. The alert forwarder posts every rejection to Discord.

## The configuration file

One file, mounted into the OMS, read at start and on a `reload` command. Its shape:

```yaml
engine:
  max_lots_total: 40
  max_margin_usd: 5000
  daily_loss_limit_usd: 800
  ban: ["ETH"]
strategies:
  icbtc1:
    max_lots_per_order: 4
    price_band_pct: 3
    max_lots_per_contract: 4
    max_lots_per_underlying: 16
    max_lots_total: 16
    daily_loss_limit_usd: 300
```

A key that is absent switches that check off for that scope. The startup log lists what is on.

## Open questions

- Whether the price band should widen automatically on a fast market, or whether a rejection there is
  exactly what is wanted. Written: a fixed band; a rejection is loud.
- The margin model's rules for Delta's options, and how far the estimate may drift from the wallet's
  figure before that itself is an alert.
- Whether a net-delta or net-gamma limit belongs in this phase. Written: not until positions exist to
  measure it on.

## Where to go next

[paper-broker.md](paper-broker.md) is the broker these checks are first exercised against.
