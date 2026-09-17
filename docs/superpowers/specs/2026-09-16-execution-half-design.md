# The execution half: strategy worker, order management, risk checks, paper broker

> **Not yet published.** This file is the spec as agreed on 2026-09-16, kept local until the senior
> has reviewed the design pages under `docs/design/execution/`. Once he has, it becomes one GitHub
> epic and is cut into tickets. Until then the design pages are the authority and this file is
> their contract form.

**What this file is.** The design pages explain each part for a reader who has never seen the
project. This spec restates the same decisions as one problem, one solution, the things a person
must be able to do, the decisions a builder must not re-open, and how it will be tested. It adds
nothing the pages do not say; where they disagree, the pages win and this file is wrong.

---

## Problem Statement

The engine can see the market and cannot act on it. It holds one connection to Delta Exchange,
computes its own implied volatility and Greeks, shows them on a page and records every minute to
Parquet. It places no orders, holds no position, and has no idea of what position it would want.

The senior's architecture has a right half the engine lacks: strategies that decide on a position,
an order management system that works out what to send, risk checks that can say no, and a broker
that fills. His own version of that half has a gap he named himself: every process keeps its state
in memory, and a restart loses it.

Three things make building this half harder than drawing it:

1. **A person trades on the same account by hand.** The broker reports one net position per contract
   and says nothing about who opened it. If the engine cannot tell its own trades from a person's, it
   will either fire again to "fix" a position it did not cause, or refuse to trade because the numbers
   never match. Both are wrong, and the second is silent.
2. **Nothing today says who caused what.** Every event has an id, and no event names the decision that
   led to it. When a fill appears at 11:42, there is no query that returns the reason.
3. **The rest of the design does not exist yet either**, and building it in the wrong order produces a
   strategy that knows about orders, or an order path that cannot be tested without a live account.

The senior's instruction is to build the broad machine first, with the tracing to backtrack any
event, and to fix intricate behaviours once the machine runs. This spec is that broad machine.

## Solution

Four new parts, all on the existing bus, all runnable on a laptop without an account:

- **A strategy** is one running program per strategy. It reads our computed chain and its own actual
  position, and publishes the whole position it wants every time its mind changes. It never names an
  order.
- **An order management system (OMS)** keeps six position books, works out the gap between what the
  strategy set as a whole wants and what is held, runs six risk checks on each order, sends what
  passes through a broker adapter, and records everything in Postgres. Risk checks and execution are
  modules inside it; working orders live in the execution module's memory and nowhere else.
- **A paper broker** fills orders against the live quotes, keeps its own positions, and accepts a
  manual order through a test hook, so that a person's trade and the engine's can be told apart and
  tested locally. The sandbox is a second OMS instance with the paper broker inside it.
- **Three identifiers on every event** (`decision_id`, `parent_event_id`, `actor`) carried on every
  log line, so that one filter by the decision's id returns everything that followed from it.

The engine's trades are marked with the client order id Delta echoes on every order and fill. A fill
without one is a person's. The OMS rebuilds its own book from the broker's marked fills every ten
seconds and compares it with the book it built as fills arrived. A contract where the two disagree is
frozen and a person is told. Nothing is ever flattened automatically, and no real money is used in
this phase.

## User Stories

**Strategy authors**

1. As a strategy author, I want to publish the whole position I want, so that I never have to think about orders, prices or leg order.
2. As a strategy author, I want my strategy to run as its own process, so that adding or restarting it disturbs no other strategy.
3. As a strategy author, I want to read our own Greeks from the computed chain, so that strike selection uses the numbers the repository trusts.
4. As a strategy author, I want to read my own actual position from the OMS, so that I know whether I am in without talking to any broker.
5. As a strategy author, I want to ask a clock rather than read one, so that the same strategy could later run against recorded data.
6. As a strategy author, I want my counters and entry credit to survive a restart, so that a re-entry limit still holds after a crash.
7. As a strategy author, I want a restarted strategy that finds legs it does not recognise to stop and alert, so that it never trades against a position it does not understand.
8. As a strategy author, I want to set lots, times, delta targets and limits as parameters, so that one strategy body serves many instances.
9. As a strategy author, I want the sample iron condor to enter at 08:00 IST, take profit at 60%, stop a side at 0.45 delta, exit at 17:00 IST, and re-enter within its limits, so that the description I was given is exactly what runs.
10. As a strategy author, I want to see my strategy's state transitions as events, so that "why did it not enter" has an answer.

**The OMS and its books**

11. As an operator, I want six books with the names desired per strategy, desired pooled, actual per strategy, actual engine, actual broker and discretionary, so that everyone means the same thing by "the book".
12. As an operator, I want the broker's reported position to be the truth that is never overridden, so that a disagreement is always resolved in the broker's favour and shown to me.
13. As an operator, I want working orders subtracted from the gap and held only in the execution module's memory, so that an order in flight is never sent twice and no table can disagree with the broker about what is open.
14. As an operator, I want orders worked out from the pooled desired book against the engine's own position, so that two strategies wanting opposite things in one contract pay no spread at all.
15. As an operator, I want a Book 2 change to wait a short settling window before becoming orders, so that targets published moments apart net instead of firing and un-firing.
16. As an operator, I want Book 3 updated directly when Book 1 moves and Book 2 does not, so that a reallocation between strategies happens without an order and every strategy still knows what it holds.
17. As an operator, I want every engine order to carry a client order id that names the strategy whose change caused it, so that a fill's origin can be read off it and Book 3 derives from what the broker confirms.
18. As an operator, I want a fill without our client order id treated as discretionary, so that a person's trade lands in the discretionary book and is never "corrected" by the engine.
19. As an operator, I want reconciliation every ten seconds and on every fill, so that a mismatch is found within seconds and not at the end of the day.
20. As an operator, I want a mismatched contract frozen and named in an alert, with both numbers, so that I understand before I unfreeze it.
21. As an operator, I want other contracts to keep trading while one is frozen, so that one puzzle does not stop the day.
22. As an operator, I want orders to move through named states and nothing else, so that any order's history reads the same way.
23. As an operator, I want limit orders at the touch with cancel-and-replace after a set time and a set number of tries, so that a thin market never gets a market order from us.
24. As an operator, I want buy legs sent first and sell legs only after the buys fill, so that margin is lower and a half-filled spread is a known shape.
25. As an operator, I want a legging timeout to cancel everything and tell me exactly which legs are held, so that I know when I am holding a long strangle instead of a condor.
26. As an operator, I want every order, fill, book snapshot, reallocation and reconciliation result in Postgres, so that any book can be read back for any moment.
27. As an operator, I want working orders recovered from the broker's open orders on a restart, so that the OMS never trusts a stale table about what is live at the venue.
28. As an operator, I want the OMS to refuse to start without Postgres, so that it never runs while forgetting.

**Risk checks**

29. As a risk owner, I want every order checked before it is sent, in a fixed order of six checks, so that no path bypasses them.
30. As a risk owner, I want a failed check to reject the order outright and publish which check, which number and which limit, so that a rejection is never silent.
31. As a risk owner, I want no check ever to shrink, reprice or reshape an order, so that what was sent is what the strategy meant.
32. As a risk owner, I want a maximum quantity per order and a price band around the mid, so that a bad quote or a bad parameter cannot send a wild order.
33. As a risk owner, I want the margin ceiling checked against an estimate of the **resulting portfolio's** margin rather than the order's own, so that a spread whose long leg is already held is not rejected for a cost it does not have.
34. As a risk owner, I want the margin check to alert rather than reject until its model has been measured against the wallet's blocked margin, so that the engine never stops trading on a number nobody has verified.
35. As a risk owner, I want position limits per contract, per underlying, per strategy and for the engine, so that size is bounded at every level.
36. As a risk owner, I want a ban list and an illiquidity rule, so that restricted or dead contracts are never traded.
37. As a risk owner, I want realised and unrealised profit and loss per strategy and for the engine, published every second and stored every minute, so that the loss limits have something to read.
38. As a risk owner, I want a daily loss limit per strategy and for the engine that blocks new opening orders, allows closes, and alerts, so that a bad day stops growing without a burst of market orders.
39. As a risk owner, I want the active checks and their limits printed at startup, so that a check silently switched off is visible.
40. As a risk owner, I want to edit limits in one file and reload without a restart, so that a limit change is a small act.
41. As a risk owner, I want a pooled order to pass the strategy-wise limits of every strategy that contributed to it, so that pairing two strategies cannot step over either one's ceiling.

**The paper broker and the sandbox**

42. As a developer, I want a broker that needs no account and fills against the live quotes, so that the whole order path runs on my laptop.
43. As a developer, I want fills at the touch, whole quantity, and rejections where there is no two-sided quote, so that paper results do not flatter a strategy.
44. As a developer, I want fee and slippage parameters that default to zero, so that a later run can ask "what if execution were worse".
45. As a developer, I want the paper broker to keep its own positions and answer the same adapter calls as Delta, so that the sandbox OMS is the live OMS with one setting changed.
46. As a developer, I want a test to place an order at the paper broker with no client order id, so that the discretionary book and the reconciliation can be tested without a person, a venue or an operator command.
47. As a developer, I want the sandbox to be a second OMS instance on venue `PAPER`, so that live and sandbox traffic never mix even on one bus.

**Tracing**

48. As anyone debugging, I want every event to carry `decision_id`, `parent_event_id` and `actor`, so that "what caused this" and "who started this" are each one lookup and neither field's name needs explaining.
49. As anyone debugging, I want one filter by a decision's id over the day's log to return the whole chain in order, so that a fill at 11:42 explains itself with no second store to keep in step.
50. As anyone debugging, I want the emitter carried inside every `event_id`, so that the parent's actor is readable from `parent_event_id` without a lookup and without a fourth field to drift.
51. As anyone debugging, I want market ticks not to start chains, so that the trace is decisions and not noise.

**Operations**

52. As an operator, I want a kill switch that cancels every working order and refuses new ones without closing anything, so that I can stop the engine in one command and still see what it holds.
53. As an operator, I want freeze and unfreeze per contract, so that the reconciliation's stop and mine are the same mechanism.
54. As an operator, I want every command I send to carry my name as its actor, so that a person's action is as traceable as the engine's.
55. As an operator, I want fills and risk rejections posted to Discord, so that a paper-trading day can be watched from a phone.
56. As an operator, I want read-only API routes for books, working orders, orders, fills, profit and loss and frozen contracts, so that I can look without SQL.
57. As an operator, I want a table that says what each process loses on a restart and how it recovers, so that the first restart in anger is not a surprise.
58. As an operator, I want the live OMS present in the compose file but not started, so that the shape is visible and real trading remains a deliberate act.

## Implementation Decisions

Every decision below is explained at length in the design pages; the page is named so the reason can be found.

**Boundaries and processes**

- A strategy is one compose service per instance, all from one image, configured by environment: which strategy, its instance id, its venue, its underlying, its parameters. (strategy worker)
- The OMS is one process. Risk checks and execution are modules inside it with one entry point each, so either can be lifted out later without changing what it does. (OMS)
- Two OMS instances from one image: live with the Delta adapter on venue `DELTA`, sandbox with the paper adapter on venue `PAPER`. Stream names already carry the venue. (OMS)
- The paper broker is a module inside the sandbox OMS, not its own process. Its manual door is a command on the bus. (paper broker)
- The alert forwarder subscribes to fills and risk rejections in addition to alerts. (events and tracing)
- All of this joins the existing single machine. The earlier decision to add a second instance when the OMS places its first order stands as a go-live decision. (operations)

**The strategy contract**

- A strategy publishes one event type: its whole desired position, signed lots per contract, on every change. No event a strategy can publish carries an order. (strategy worker)
- Inputs: the computed chain for its underlying, its own actual book from the OMS, a clock object, its parameters. Our delta selects strikes; the venue's Greeks are never inputs. (strategy worker)
- One Postgres row per strategy holds its state name, counters, entry credit, intended legs and day. Position is never stored by the strategy. (strategy worker)
- The sample condor is a state machine with the states Idle, Selecting, Entering, InPosition, Exiting (profit, stop, time), Flat, Done. A stop closes that side, body and wing; a stop re-entry rebuilds that side; a profit re-entry opens a fresh condor; counters reset with each day's first entry. Sizing in lots is a strategy parameter. (strategy worker)

**Books and orders**

- Six books, named `desired:<strategy>`, `desired:pooled`, `actual:<strategy>`, `actual:engine`, `actual:broker`, `actual:discretionary`, plus `working`, which is **not** a book: it is an in-memory structure in the execution module, stored nowhere, rebuilt on restart from the broker's open orders. (books)
- The gap is pooled: `Book 2 − Book 4 − working`, per contract. Book 1 reaches the order path only through Book 2, so two strategies wanting opposite things in one contract produce no order. A Book 2 change waits a short settling window so that targets published moments apart net rather than firing twice. (books)
- When Book 1 moves and Book 2 does not, no order is sent and Book 3 is rewritten directly as a reallocation, recorded as its own kind of entry, with `Σ Book 3 = Book 4` still holding. (books)
- Book 3 is derived from Book 4 using the strategy tag the broker echoes — our client order id. Two known limits, both accepted for this phase: a broker that echoes no such tag would leave Book 3 an unconfirmed engine-side allocation, and one pooled order serving two strategies can only name the strategy whose change caused it. Neither bites with one strategy running. (books)
- The engine's mark is the client order id, format `E.<strategy id>.<sequence>`, at most 32 characters, unique among open orders. The adapter marks each fill engine or manual by its presence. (books, OMS)
- The discretionary book is derived as broker minus engine, never entered. (books)
- Reconciliation every ten seconds and on every fill rebuilds the engine book from the broker's marked fills and checks two identities per contract. A mismatch freezes that contract, cancels its working orders and alerts. (books)
- Order states: Intent, RiskRejected, Sent, Acked, BrokerRejected, PartiallyFilled, Filled, Replaced, Cancelled. Working means Sent, Acked or PartiallyFilled. (OMS)
- One execution method: limit at the touch, cancel and replace at the new touch after N seconds, give up after M tries with an alert. N and M are OMS parameters. No market orders. (OMS)
- Legging: all buy legs first, wait for their fills, then all sell legs; a timeout cancels everything and names the legs held. (OMS)

**Risk checks**

- Six checks in a fixed order: order check (max lots per order, price band as a percentage around mid), margin limit (the estimated margin of the resulting portfolio — Book 4 plus working plus the intent — against a ceiling, because margin is not additive per order), position limits (per contract, underlying, strategy, engine), restrictions (ban list, no two-sided quote in 60 seconds), profit and loss tracking (realised from fills, unrealised at mid, per strategy and engine), mark-to-market loss limits (per strategy and engine, per day). (risk checks)
- A check passes or rejects. It never changes an order. A rejection publishes the check, the number and the limit. (risk checks)
- A loss-limit breach blocks new opening orders, allows closes, alerts. No automatic flatten. (risk checks)
- One configuration file, keys absent switch that check off for that scope, startup logs what is active, a reload command re-reads it. (risk checks)
- The margin estimate lives in the adapter; Delta offers no pre-trade estimate for an order or a portfolio; the paper broker uses the same model. Because the model starts unvalidated, check 2 **alerts and lets the order through** until a week of paper fills has measured it against the wallet's blocked margin; turning it into a rejection is a configuration change and a deliberate act. (risk checks)
- A pooled order must pass the strategy-wise checks of every strategy that contributed to it, not only the one its client order id names. (risk checks)

**Paper broker**

- A buy fills at the ask and a sell at the bid when the limit crosses, whole quantity, immediately; otherwise it stays working. Since execution places its limit at the touch, the sandbox comes out instant-fill with no branch saying so, and no code path exists that the live system does not run. Size is ignored: the `ob_l2` depth is on the bus and discarded. No two-sided quote in 60 seconds rejects with reason `no_quote`. Fee and slippage parameters default to zero. No partial fills yet; every fill still carries a quantity. (paper broker)
- It keeps its own broker book and wallet and answers the same adapter calls as Delta. (paper broker)

**Events and tracing**

- `decision_id` is set once at a strategy's decision and copied unchanged; `parent_event_id` is the event id of the direct cause; `actor` is one of `strategy:<id>`, `oms`, `rms`, `execution`, `broker:delta`, `broker:paper`, `operator:<name>`. Market ticks start no chain. (events and tracing)
- New event types: strategy target and `strategy.transition`, named apart from the `strategy_state` table on purpose; order intent; risk verdict; order sent, acked, replaced, cancelled, rejected; order fill with an origin of engine or manual; book snapshot; profit-and-loss snapshot; reconciliation result; the existing alert and control command with new targets. (events and tracing)
- Three envelope fields, spelled out rather than borrowed: `decision_id` and `parent_event_id` in place of the literature's `correlation_id` and `causation_id`, and `actor`. The emitter is carried inside `event_id`, so no fourth field naming the causing actor is added. (events and tracing)
- **No event-log table.** Every event already carries the three identifiers on its log line, and a chain is filtered out of the day's file with `jq` or DuckDB. A table copying the same events into Postgres was written into the first draft and dropped as a second record to keep in step. No OpenTelemetry and no log server in this phase. (events and tracing)

**Storage and operations**

- Postgres, one schema: orders, fills, book snapshots, reallocations, strategy state, profit-and-loss snapshots, reconciliation results. No working-orders table and no event log; the broker and the log files are the better records of those two. Redis stays a pipe. (OMS)
- Commands on the existing control-command event: for the OMS freeze, unfreeze, kill, resume, reload; for a strategy pause and resume. Each carries the operator's name. **No command places an order**: manual trading happens on the broker's own app, and the paper broker's manual door is a test hook. (operations)
- Read-only API routes for books, working orders, orders, fills, profit and loss, frozen contracts. No page in this phase. (OMS)
- Build order: identifiers on the envelope and the log lines; paper broker; strategy; OMS without checks; reconciliation and the manual door; the six checks; API routes and the trace query; a week in the sandbox, then the Delta adapter against the testnet. (operations)

## Testing Decisions

**What makes a good test here.** A test publishes events into the bus and asserts on the events that come out, or drives an adapter with a script and asserts on the books. It never reaches into a module's memory. A test that reads a counter the code sets is not a test; a test that would fail if the guard were deleted is. The repository's standing rule applies: mutate, do not read.

**Three seams, from the highest down.** These are the seams to confirm with the senior.

1. **The bus seam**, the highest and the main one. The in-process bus already used by the feed and store tests carries a scripted computed chain and quotes in; a strategy, the sandbox OMS and the paper broker subscribe; the test asserts on the targets, intents, verdicts, orders, fills and book snapshots that come out, and on their three identifiers. Most stories above are tested here and nowhere else. Prior art: the bus contract tests, the computed-chain publish tests, the recording-split tests.
2. **The broker adapter seam.** A scripted broker adapter, in the style of the scripted feed adapter that already exists, returns positions and fills from a script. Reconciliation, the engine-versus-manual marking, the frozen contract and the Delta adapter's mapping of Delta's own fields are tested here without a venue. The paper broker is tested at this seam as well: given these quotes and this order, this fill.
3. **The API seam.** The read-only routes through the existing test client, as the health and history routes are tested today.

**Two fixtures.** A fake clock, as the controller tests already use, drives every time rule in the sample strategy including the IST conversion. A Postgres test session, in the style of the Redis test session that skips when Docker is absent, holds the tables; the strategy row, the order lifecycle and the reallocation entries are asserted by reading them back after the bus seam has run. The trace is asserted on the events themselves, not on a table.

**What is tested by module.** The strategy's state machine, exhaustively, at the bus seam with the fake clock. The pooled gap and the working-order subtraction, as a pure function, including the case where two strategies' targets cancel and no order is produced. The reallocation path: Book 1 moves, Book 2 does not, no intent is published, and both strategies' Book 3 snapshots change. The order lifecycle, at the bus seam with the scripted broker. Each of the six risk checks, one rejection each, with the rejection event's fields asserted. The paper broker's fill rule, every row of its table. Reconciliation, including the worked case in the books page: a manual fill moves the broker book, the engine book does not move, no intent is published. Restart recovery of working orders from a scripted broker's open orders, at the adapter seam. The identifier rules, on every event a chain produces. The nomenclature test extends to the new terms.

**One live check that is not a unit test.** A week of the sample strategy in the sandbox, with the fill record and the trace query read every evening. Its acceptance is that every fill in the week traces to a decision and every book identity held at every reconciliation.

## Out of Scope

- Splitting one pooled order across the strategies that caused it. Pooling itself is in; the tag names the causing strategy, and a pro-rata allocation is the later refinement.
- Any execution method beyond limit at the touch with cancel and replace; walking from mid; legging priorities beyond buys first.
- Partial fills in the paper broker, and proportional sell waves after a partial buy wave.
- Automatic flattening on a loss limit, net-delta or net-gamma limits, open-interest or spread-width illiquidity rules.
- A dashboard page. The API routes are in; the page is a later ticket.
- OpenTelemetry, a log server, log retention policy, and any table copying events into Postgres.
- A second real broker. The adapter interface allows IBKR; only Delta and the paper broker are built, and the Delta adapter's order path is the last step, against the testnet.
- Live trading on real money. A decision taken later, by a person, after the sandbox week and the testnet run.
- Running a strategy against recorded Parquet data. The clock object leaves the door open; nothing walks through it.

## Further Notes

- **Venue facts the design relies on.** Delta's daily options settle at 12:00 UTC, 17:30 IST, and the venue closes open positions at settlement; the sample strategy's 17:00 exit is thirty minutes before that. Delta's order API accepts a client order id of up to 32 characters, unique among open orders, echoed on orders and on the private fills channel; there is no order-source field anywhere; positions carry no lineage; there is no pre-trade margin estimate; the India testnet has its own base address and its own keys.
- **Open questions carried from the design pages, for the senior:** the allocation rule when two strategies want one contract, and what Book 3 becomes on a broker that echoes no tag; the settling window's length; whether a one-sided stop re-entry rebuilds that side or re-enters a fresh condor; whether premium profit is marked at mid or at the closing price; the margin model for Delta's options and the drift at which the estimate itself alerts; whether `actor` should carry a process instance id; whether the live OMS sits dormant in the compose file before a key exists.
- **The senior's standing instructions that shaped this.** Build the broad machine first and fix intricate behaviour later; make every event traceable to what caused it and who started it; write for a reader who has never seen the project; converge on the design and its diagrams before the spec, and on the spec before tickets.
- **Where this goes next.** Review of the design pages with the senior; this file is refined from that review; then it is published as one epic with the `ready-for-agent` label and cut into tracer-bullet tickets in the build order above.
