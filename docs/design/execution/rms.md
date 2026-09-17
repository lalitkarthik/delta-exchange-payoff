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

**Orders are pooled, so one order can belong to two strategies.** When it does, it must pass the
strategy-wise checks of **every** strategy whose desire contributed to it, not only the one named in
its client order id. A limit that could be stepped over by pairing with another strategy is not a
limit. With one strategy running the two readings are the same; the rule is written now so that the
second strategy does not quietly widen the first one's ceiling.

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
| The estimated margin of the **resulting position** — Book 4 plus working plus this intent | The engine's margin ceiling | a margin model in the adapter; the ceiling from configuration; the wallet's blocked margin as the check on the model |

**Margin is not additive per order, and an earlier draft of this check assumed it was.** A short option
is far cheaper to hold once the long option that caps its loss is already held — that is the whole
reason [oms.md](oms.md) sends buy legs first. A check that priced each order on its own would put a
standalone short's margin against the ceiling and reject the second half of every spread the legging
rule was designed to make cheap. So the model is applied to the **portfolio the order would produce**,
and the number compared against the ceiling is that portfolio's margin, not the order's.

Delta offers no endpoint that estimates the margin of a proposed order or portfolio, so the model is
ours, kept in the adapter with the broker's published rules, and the paper broker uses the same one.
**It starts unvalidated, so it starts as an alert and not a rejection.** After each fill the wallet's
actual blocked margin is read back and compared with what the model predicted; until a week of paper
fills says how far the two drift, a breach of this check raises an alert and lets the order through.
Turning it into a rejection is a one-line configuration change and a deliberate act. Rejecting on a
number with no measurements behind it is how a system stops trading for a reason nobody can explain.

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
- The margin model's rules for Delta's options, the drift at which the estimate itself is an alert,
  and the evidence that turns check 2 from an alert into a rejection.
- Whether a net-delta or net-gamma limit belongs in this phase. Written: not until positions exist to
  measure it on.

## Where to go next

[paper-broker.md](paper-broker.md) is the broker these checks are first exercised against.
