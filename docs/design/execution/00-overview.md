# The execution half: overview

**What this page contains.** A picture of the whole system once it can trade, a description of each
new part, and the rules that every other page in this folder follows. The pages after this one each
take one part and explain it fully.

**How to read it.** Read the first two sections, then follow the reading order at the bottom. Words
in **bold** on first use are defined where they appear, and again in [Glossary](../../final-design/glossary.md)
once they land there. Everything here is a **decision, not a description**: none of it is built yet.
[Architecture](../../final-design/architecture.md) describes what is built.

**One rule shaped every decision here: build the broad machine first.** Every part below is the
simplest version that lets one strategy trade end to end for several clients in a sandbox, with every
step traceable to what caused it. The intricate behaviours (partial fills, pooling orders across
strategies, copy trading) are named in each page's *Open questions* and left for later, on purpose.

## The whole machine, in one picture

The feed, the message bus, the store and the API exist in the feed engine. Everything else is what
this folder designs: the parts that decide what to hold, work out what to order, check it, send it,
and keep a record of it.

```mermaid
flowchart LR
  V["Delta Exchange<br/>(the venue)"]
  F["feed<br/>data adapters"]
  BUS[("message bus<br/>Redis Streams")]
  ST["store"]
  S3[("S3<br/>Parquet: market data")]
  API["api"]
  S1["strategy ic-btc-1"]
  S2["strategy ..."]
  OA["oms-acme<br/>books · risk checks · execution"]
  OB["oms-paper1<br/>books · risk checks · execution"]
  BA["Delta adapter<br/>(acme's account)"]
  PB["paper broker"]
  PS["persistence service"]
  PG[("Postgres<br/>orders, fills, books,<br/>checkpoints, signal cards")]
  DC["alert forwarder<br/>(Discord)"]
  V --> F --> BUS
  BUS --> ST --> S3
  BUS --> API
  BUS --> S1 & S2
  S1 & S2 -- "target in units,<br/>checkpoints, signal cards" --> BUS
  BUS -- "targets" --> OA & OB
  OA --> BA --> V
  OB --> PB
  OA & OB -- "orders, fills, books, PnL<br/>per client" --> BUS
  BUS --> PS --> PG
  PS -- "restore replies" --> BUS
  BUS --> DC
  classDef new fill:#e6f5e6,stroke:#2e7d32,stroke-width:2px
  class S1,S2,OA,OB,BA,PB,PS,PG new
```

Green boxes are new. The feed, the bus, the store, the API and the alert forwarder change only where
[message-bus.md](message-bus.md) and [data-feed.md](data-feed.md) say.

## The new parts, one at a time

| Part | What it does | What it deliberately does not do |
|---|---|---|
| **Strategy** | Decides what position it wants to hold, in **units**, and publishes that whole position every time its mind changes. One running program per strategy, shared by every client. | Never names an order, a price, an order type, the sequence legs are sent in, or a client. |
| **Client** | A person or entity whose capital is traded: one broker account, a list of **subscriptions** (strategy → units), and its own limits, in one client file ([clients.md](clients.md)). | Never shares an OMS, a book or a risk limit with another client. |
| **OMS** (order management system) | **One instance per client.** Keeps that client's books, works out the difference between what its subscribed strategies want **as a whole** and what is held, checks each order against the client's risk rules, and sends it to the client's broker. | Never decides *whether* to trade. That is the strategy's job. |
| **Risk checks** (RMS, risk management system) | A module inside the OMS. Every order passes through six checks before it is sent. A failed check rejects the order outright. | Never shrinks or reshapes an order. It says yes or no. |
| **Broker adapter** | The one part that sends orders to a broker and reads back fills, positions and the wallet. Two are built: the **paper broker**, which fills orders against the live prices without touching the venue, and the Delta adapter. | Never brings in market data. That is a **data adapter**'s job, in the feed ([data-feed.md](data-feed.md)). |
| **Persistence service** | The only program that connects to Postgres. It reads records off the bus and writes them, and it answers restore requests from programs that restart. | Never decides anything. It writes what it is sent and reads back what it is asked for. |
| **Postgres** | The database holding orders, fills, book snapshots, strategy checkpoints and signal cards: every record that must outlive a process. | Never holds market data: prices go to Parquet files on S3 through the store. Not a log sink: the event trace lives in the log files. |

## The vocabulary the design uses

Three words are overloaded elsewhere and are pinned here.

| Word | Meaning in this folder | Where it means something else |
|---|---|---|
| **Book** | A table of *signed lots per contract*: how many of each contract someone wants, or holds. Negative is short. There are six, explained in [books.md](books.md). | Nowhere else in this repository. |
| **Strategy** | One running program that decides on a position. | On Convex Hedge's terminal a strategy is a backtestable rule; in `payoff-project` it is a set of option legs. |
| **Strategy set** | Every strategy program currently running. | *Portfolio* means a set of backtest runs on the terminal, so it is not used here. |
| **Unit** | One copy of a strategy's leg ratio: a straddle's unit is one call and one put. Clients hold strategies in units. | A lot is one contract's trading size; a unit is several lots across legs. |

## The rules every page follows

1. **A strategy publishes a position in units, never an order.** Everything about *how* to trade lives in the OMS, and a strategy's position reaches the order path only through each client's **pooled** desired book, so two strategies that want opposite things trade nothing. [books.md](books.md).
2. **One OMS per client, and nothing crosses clients.** Each client's books, limits and orders are its own; a strategy's target reaches a client only through the client's subscriptions. [clients.md](clients.md).
3. **The broker is the truth about what is held.** The engine keeps its own record and checks it against the broker's, contract by contract, every ten seconds. When they disagree, the engine adopts the broker's record and repairs the gap with new orders, up to a fixed number of times; past that, the contract is frozen and a person is told.
4. **Every event says who caused it.** Three identifiers on every message — `decision_id`, `parent_event_id`, `actor` — make any fill traceable back to the decision that started it. [events-and-tracing.md](events-and-tracing.md).
5. **A risk check says no, or nothing.** It never changes an order.
6. **The sandbox is the same software.** A sandbox is one or more **paper clients**: clients whose broker is the paper broker, reading the same live prices and the same targets as real clients.
7. **Postgres is reached only through the bus.** No program but the persistence service holds a database connection. A program that needs its records back after a restart asks for them over the bus. [message-bus.md](message-bus.md).
8. **No real money in this phase.** The paper broker runs the sample strategy first; the venue's testnet is next; live trading is a decision taken later, by a person.

## Reading order

1. [books.md](books.md) — the six books, working orders, how an order is derived, and the check that catches and repairs a double fire
2. [strategy-worker.md](strategy-worker.md) — what a strategy is, its model position, checkpoints and signal cards, and the sample iron condor as a state machine
3. [clients.md](clients.md) — clients, the client file, subscriptions and units, joining and leaving, paper clients
4. [oms.md](oms.md) — from desired position to sent order: the order lifecycle, legging, and the tables
5. [rms.md](rms.md) — the six risk checks and what happens when one fails
6. [paper-broker.md](paper-broker.md) — how a fill is made up, and the door for manual trades
7. [events-and-tracing.md](events-and-tracing.md) — the new events, the three identifiers, one traced day
8. [message-bus.md](message-bus.md) — retention, the store's flush, and the persistence service
9. [data-feed.md](data-feed.md) — market hours, instrument lists, subscriptions, and the two kinds of adapter
10. [operations.md](operations.md) — commands, the kill switch, what a restart loses, and the build order

## To review

Three decisions are taken for now and flagged to be looked at again before a real client trades:

- A strategy's model position and a client's real position can differ ([strategy-worker.md](strategy-worker.md)).
- Every client's records share one database, told apart by `client_id` ([clients.md](clients.md)).
- Copy trading is not designed; manual trades stay in the account they were placed in ([clients.md](clients.md)).

## What is out of scope, on purpose

- Splitting one pooled order across the strategies that caused it. Orders **are** pooled, and each carries the tag of the strategy whose change caused it; when two strategies move one contract at once, that tag is approximate. [books.md](books.md) says what breaks and when.
- Any execution method beyond "limit at the touch, then cancel and replace".
- A dashboard page. The API gains read-only routes; a page is a later ticket.
- Any real broker beyond Delta. The adapter interface allows one to be added; which broker serves Indian indices and SPX is an open decision in [operations.md](operations.md).
- Automatic flattening on a loss limit. A breach blocks new opening orders and tells a person.
- A risk limit across clients. Each client's OMS checks only its own account.
