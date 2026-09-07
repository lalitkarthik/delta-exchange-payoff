# High-level design: the feed, end to end

**What the platform is: one engine that owns every venue connection, turns venue frames into
canonical events, and publishes them to consumers that never block each other.** A quote arrives
on a broker's socket; an adapter decodes it into an event carrying a canonical instrument; a
controller says whether that socket is healthy; a bus copies the event to whoever subscribed. It
becomes three things — the newest state of a ladder, our own implied volatility and Greeks, and a
sealed one-minute bar in Parquet. `docs/chain-contract.md` stays the authority on the page.

This document says **what the parts are and how they talk**, never how one is built inside —
that is a low-level design, written when the part lands ([lld/index.md](lld/index.md)). What
crosses between parts is [events.md](events.md); where the two disagree the catalogue wins.

**Status marks.** Present tense describes code that exists today; **will** describes work
specified in #33 and names the ticket that lands it. Where the two differ for one box, both are
written, because this is a map of a system in motion. It supersedes `docs/architecture.md`, whose
per-module detail is still accurate where this document is silent.

---

## 1. The shape of the system

```
       VENUES                 ENGINE FEED MANAGEMENT
  +----------------+     +------------------------------+
  | Delta Exchange |     |  +------------------------+  |
  | India ws + REST|====>|  | Delta adapter     #36  |  |
  +----------------+     |  +-----------+------------+  |
  +----------------+     |  | connection controller  |  |
  | NSE  ~~ = not  |~~~~>|  | #38  state machine     |  |
  | built yet      |     |  +-----------+------------+  |
  +----------------+     |  | supervisor        #39  |  |
  control.command  ----->|  +-----------+------------+  |
  pause/resume  (#41)    +--------------|---------------+
                                        | canonical events
                                 +------+-------+
                                 |   the bus    |  fanout.py
                                 +--+--------+--+
                      drop-oldest |        | lossless
                       +----------+-+   +--+-----------+
                       | chain cache|   |  bar writer  |
                       | stream.py  |   |  store.py    |
                       | + pricing  |   | -> Parquet x4|
                       +-----+------+   +-------+------+
                       +-----+------------------+-------+
                       |  main.py    FastAPI :8000      |
                       |  /expiries /chain /health      |
                       |  /ws/chain  + #45 #46 routes   |
                       +---------------+----------------+
                                       | ChainResponse + feed badge
                               +-------+--------+
                               |  web/  :3000   |
                               +----------------+
```

## 2. The boxes

### 2.1 Venues

Delta Exchange India, public market data, no API key: one websocket carrying `ob_l2` and
`ticker`, and a REST API for expiries and chain snapshots. NSE is named only because the adapter
interface exists to admit it — a different spelling, currency, lot size and calendar — and it is
out of scope.

### 2.2 Broker adapters

**An adapter owns everything venue-specific and nothing else:** the socket, the REST reads the
screens need, the symbol spelling, the channel names, the wire layout. It answers four
questions — describe yourself, list an underlying's instruments, subscribe to a set of them,
stream events until stopped — and emits canonical events carrying a canonical `Instrument`, so
nothing downstream sees venue JSON. **One boundary rule lives here and nowhere else:** the
venue's absent-quote spellings become `null`, and a real zero stays `0`.

#35 landed the instrument and the envelope; #36 moved the venue's three modules behind the
protocol and added a **scripted fake adapter**, the one new test seam; #37 retired the quote
record that used to carry a raw frame past the adapter, moved the socket owner into the
adapter package with it, and moved the two consumers onto the events. The `connect` factory
stays injectable, because that is the seam the feed tests already drive.

### 2.3 The controller and the supervisor

**One controller per adapter, and it is the only thing that may change a connection's state.** It
owns backoff, the lifetime reconnect budget, subscription replay and staleness detection, will
emit a `feed.connection` event and a log record on every transition (#38), and will accept
`control.command` (#41). **One supervisor holds every controller**, starts and stops them with the
application lifespan, and reports the **worst** state among them as the feed's state. `/health`
will grow from `{"status": "ok"}` into a report — that field preserved, plus per adapter its
state, last message time and age, reconnect count, budget remaining, and the pairs being solved
(#39, #44). Neither exists today: all of it lives inside the feed loop, where reconnect,
resubscribe and budget are correct but unreadable from outside, and `/health` reports process
liveness alone.

### 2.4 The bus

**One producer, many independent consumers, in one process.** `fanout.py` today; #37 puts it
behind an interface of `publish(event)` and `subscribe(name, maxsize, lossless)`, so a broker can
replace the implementation without touching a producer or a consumer. The socket handler
publishes and returns: if it blocks, the OS receive buffer fills and the venue closes us.

**Queue policy is per subscription and does not change.** The bar writer subscribes losslessly,
because drop-oldest under load systematically shaves the highs and lows bars exist to capture;
the chain cache drops the oldest, because it holds only the newest frame per contract anyway.
Every drop is counted. **No broker is deployed:** the interface is the seam, the fan-out fills it.

### 2.5 The consumers

- **Chain cache** — `stream.py`. Newest frame per contract, rebuilt into a ladder on demand, the
  recompute loop solving our IV and Greeks. Invalidation is by arrival, not by time, so the cache
  cannot serve a stale answer. Will consume events and publish `computed.chain` (#37), and solve
  only the `(underlying, expiry)` pairs a browser watches, plus a grace window (#44).
- **Bar writer and store** — `bars.py`, `store.py`. Ticks folded into sealed one-minute bars,
  written as hive-partitioned Parquet in four tables. **A minute with no arrivals produces no
  row** — not nulls, never the previous close — and that survives the refactor. Will consume
  events (#37) and record ETH beside BTC (#43).
- **Pricing core** — `forward.py`, `solvers.py`, `black76.py`, `black_scholes.py`, `greeks.py`,
  `compute.py`. Pure. The venue's IV and Greeks travel beside ours as reference columns, never
  as inputs.

### 2.6 The public surface and the screens

`main.py` serves `/expiries`, `/chain`, `/smile`, `/iv-vs-rv`, `/recording`, `/health` and
`/ws/chain`. The websocket sends three message types today — `chain`, `waiting`, `error` — and
will gain a fourth, `feed`, carrying the adapter's state on every transition and once on connect
(#40). Two REST routes will be added: a contract's minute bars for a date (#46), and the ladder at
one stored minute with the day's stored minutes beside it (#45).

`web/` renders and computes nothing. It will wear a badge on the ladder header whenever the feed
is not `connected`, clearing on recovery (#40); gain a chart panel of a contract's minute candles
opened by clicking a strike (#46); and gain a time slider whose right edge is live and whose left
is every stored minute of the day (#45).

## 3. The connection states

Five states, and a connection is in exactly one of them.

| State | Meaning |
|---|---|
| `connecting` | An attempt is open; no messages yet. |
| `connected` | The socket is up and messages are arriving. |
| `degraded` | The socket is up and nothing has arrived for the staleness interval. |
| `reconnecting` | The socket is gone or unusable; backoff is running. |
| `stopped` | Not running: paused by command, or the reconnect budget is spent. |

| From | To | Trigger |
|---|---|---|
| — | `connecting` | Adapter start, or a `resume` command. |
| `connecting` | `connected` | Socket open and every subscription replayed. |
| `connected` | `degraded` | No message for the staleness interval. |
| `degraded` | `connected` | The next message arrives. |
| `degraded` | `reconnecting` | Silence exceeds the second, longer bound. |
| any | `reconnecting` | Socket close, or a `reconnect` command. |
| `reconnecting` | `connecting` | Backoff elapses and an attempt is made. |
| `reconnecting` | `stopped` | Reconnect budget exhausted — plus an `alert` and an error log. |
| any | `stopped` | A `pause` command; no budget is spent. |

Every transition emits one `feed.connection` event and one log record; nothing else changes state.
#38 lands the machine, #39 the budget and the supervisor over it.

## 4. The flows, named by the event that carries them

| From | To | Event |
|---|---|---|
| Adapter | bus | `md.option_quote`, `md.option_reference`, `md.index_quote` |
| Bus | chain cache, bar writer | the three above |
| Chain cache | bus | `computed.chain` |
| Bar writer | bus | `md.option_bar`, on seal |
| Controller | bus | `feed.connection`, `heartbeat`, `alert` |
| Bus | websocket handler | `feed.connection`, sent to the browser as the `feed` message |
| Operator, over a route | controller | `control.command` — pause, resume, reconnect |
| Store | the two new REST routes | Parquet reads, carrying no event (#45, #46) |

Fields, emitters, consumers and timing are in [events.md](events.md).

## 5. Numbers, and where each came from

| Number | Tag | Run |
|---|---|---|
| `ob_l2` refreshes every 508 ms per contract, `ticker` every 5,001 ms; both channels on BTC alone carry 1,322.9 msg/s at 636.5 KB/s | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| Staleness before `degraded` 15 s (three ticker refreshes); grace after the last viewer leaves an expiry 30 s | `assumed` | #33. The staleness half is now measured against a live hour and stands — longest quiet gap 44.785 s, `design/quiet-gap.md`. The grace half is still untested |
| Store gap 2026-09-04 09:38Z to 2026-09-07 09:45Z, unnoticed | `measured` | store file timestamps |

**Two numbers are deliberately absent:** the live cost of one expiry's solve, which #33 requires
re-measured against the running engine before it may appear in any design document; and the feed's
rate and bandwidth with ETH enabled, which #43 measures and records here.
**One is contested.** #33 quotes the BTC-only feed at `measured` ~600 msg/s and ~300 KB/s, against
1,322.9 msg/s and 636.5 KB/s above for the same subscription. Not reconciled; #43 settles it.

## 6. Out of scope, and why

- **A message broker.** Undecided. The interface exists so one can be slotted in; the in-process
  fan-out stays. Crossing a process wall we do not have would buy nothing.
- **Order execution, positions, the sandbox** — the whiteboard's right half. The envelope is shaped
  so they can share it; nothing else here is designed for them.
- **An NSE adapter** and the instrument fields it needs — currency, lot size, tick size,
  settlement style, calendar — added with the adapter, against a real need.
- **The computed-bars rebuild tool**, specified as a batch job from quote bars; **backfilling the
  store's three-day hole**, not recoverable; **a web test runner**, where `typecheck` and `build`
  remain the gate; and **changing the four bar tables' schemas** beyond what the new routes read.
