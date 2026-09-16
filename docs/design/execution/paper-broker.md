# The paper broker

**What this page contains.** What the paper broker is, why it is the first broker the system trades
against, exactly how it decides a fill, how it keeps its own positions, and the door through which a
person can place a manual trade so that the discretionary book can be tested on a laptop.

**How to read it.** The first section is the idea. The fill rule is the part to argue with. The manual
door is what makes the reconciliation in [books.md](books.md) testable without any keys.

## What it is

The **broker adapter** is the one part of the OMS that speaks a broker's own API: place, cancel, list
orders, list fills, list positions, read the wallet. The **paper broker** is an adapter that speaks no
external API at all. It reads the live prices already on the bus and answers as a broker would, in
memory, inside the sandbox OMS process. Its venue name is `PAPER`.

**Why first.** It needs no account and no keys. Its fills are deterministic, so a test can say what
should have happened. It runs on a laptop with the rest of the stack. And it is the research basis for
paper trading later: a strategy that has run for a week against the paper broker has produced a fill
record, a profit-and-loss history and a trace, all in the same shape the live system will produce.

**What it does not test.** Whether our Delta adapter speaks Delta's order API correctly. That is what
Delta's India testnet is for, once a test key exists. The testnet uses its own base address and its own
keys; production keys do not work against it and the reverse.

## The fill rule

The rule is deliberately unflattering.

| Order | Fills at | When |
|---|---|---|
| A **buy** limit at or above the current **ask** | the ask | immediately, whole quantity |
| A **sell** limit at or below the current **bid** | the bid | immediately, whole quantity |
| A limit that does not cross | nothing | it stays working until it crosses, is cancelled, or is replaced |
| Any order on a contract with no two-sided quote in the last 60 seconds | rejected | with the reason `no_quote` |

Two parameters, both zero by default: **fee** as a percentage of notional, and **slippage** in ticks
added against us on every fill. They exist so a later run can ask "what if execution were worse".

Filling at the touch rather than at mid is the whole point. On a 0dte wing the spread *is* the cost of
the trade; a paper broker that fills at mid would show every strategy an edge it does not have.

Partial fills are not simulated in this phase. Every fill is recorded with a quantity, so the record's
shape already allows them.

## The positions it keeps

The paper broker keeps its own Book 5, net signed lots per contract, updated by every fill it makes.
The OMS reads it through the same adapter calls it would use on Delta: list positions, list fills since
a time, read the wallet. The sandbox OMS therefore runs the same reconciliation, on the same schedule,
as the live one. The paper wallet holds a configured starting balance and blocks margin using the same
margin model the risk checks use.

## The manual door

A person can place an order at the paper broker directly, bypassing every strategy and the OMS, through
a `control.command` with target `paper` and command `manual-order`, sent the same way the feed is
paused today: an HTTP request to the API, which publishes it. The order names a contract, a side and a
quantity, and fills by the rule above.

**A manual fill carries no client order id.** The adapter marks it *manual*, exactly as it would a
Delta fill with no id. It appears in Book 5 and not in Book 4, so Book 6 becomes non-zero, and the
reconciliation must not fire an order because of it. This is the test of the whole differentiate-then-
check idea, runnable on a laptop before any account exists.

```mermaid
sequenceDiagram
  participant P as person
  participant API as api
  participant BUS as bus
  participant OMS as sandbox oms
  participant PB as paper broker (inside oms)
  P->>API: POST /paper/manual-order {contract, side, lots}
  API->>BUS: control.command target=paper
  BUS->>OMS: control.command
  OMS->>PB: manual order
  PB->>PB: fill at touch, no client order id
  PB-->>OMS: fill marked MANUAL, position updated
  OMS->>OMS: reconcile: Book 5 moved, Book 4 did not, Book 6 = difference
  OMS-->>BUS: book.snapshot (Book 6 non-zero), no order intent
```

## Open questions

- Whether the paper broker should apply Delta's actual fee schedule by default rather than zero.
  Zero is written so the first runs measure the strategy and nothing else.
- A latency model: today a crossing order fills in the same tick it arrives. A configurable delay is a
  small later addition.

## Where to go next

[events-and-tracing.md](events-and-tracing.md) lists what every fill, manual or not, publishes.
