# High-level design: the feed, end to end

**What the platform is:** in split mode, `feed` owns the venue connection, turns venue frames
into canonical events, and publishes them to Redis; `store` folds the lossless events into sealed
one-minute Parquet bars while the engine consumes its screen events into the newest ladder, our
implied volatility and Greeks. `docs/chain-contract.md` stays the authority on the page.

This document says **what the parts are and how they talk**, never how one is built inside —
that is a low-level design, written when the part lands ([lld/index.md](lld/index.md)). What
crosses between parts is [events.md](events.md); where the two disagree the catalogue wins.

**Modes.** `DELTA_BUS=redis` selects the split composition of three apps:
`deltapayoff.feed_main:app`, `deltapayoff.store_main:app` and `deltapayoff.main:app`.
With `DELTA_BUS` unset — the default — the engine remains the existing in-process FanOut
monolith, exactly as before. No service calls the other over HTTP. This supersedes
`docs/architecture.md`, whose per-module detail is still accurate where this document is silent.

---

## 1. The shape of the system

```
                         DELTA EXCHANGE
                        websocket + REST
                               |
                 +-------------v--------------+
                 | feed: deltapayoff.feed_main|
                 | DeltaClient + DeltaAdapter  |
                 | controller + FeedSupervisor |
                 | relisting + Redis publisher |
                 +-------------+--------------+
                               | canonical events
                        +--+--------+--+
                        | Redis Streams |
                        +--+--------+--+
                  canonical |        | canonical (lossless)
              +--------------v+      +-v----------------+
              | ChainStream    |      | store            |
              | chain-stream   |      | store_main:app   |
              | drop-oldest    |      | BarWriter        |
              +-------+--------+      | -> Parquet       |
                      |               +------------------+
                      +-----------------------+
                                 |
              +------------------v------------------+
              | engine: deltapayoff.main:app        |
              | feed-state -> FeedConnectionCache   |
              | (feed-state is drop-oldest)          |
              | REST /expiries /chain /health       |
              | /ws/chain                            |
              +-------------------------------------+
```

Canonical stream names are `{event_type}:{VENUE}[:{UNDERLYING}]`; issue #74 removed the
environment section. Only `feed` contacts Delta, and neither service calls the other over HTTP.

## 2. The boxes

### 2.1 Venues

Delta Exchange India, public market data, no API key: one websocket carrying `ob_l2` and
`ticker`, and a REST API for expiries and chain snapshots. NSE is named only because the adapter
interface exists to admit it — a different spelling, currency, lot size and calendar — and it is
out of scope.

### 2.2 The feed process

`deltapayoff.feed_main:app` owns the Delta REST client and websocket adapter, the connection
controller, `FeedSupervisor`, instrument relisting through shared `feed_runtime.py`, and the
Redis publisher — and nothing else. **An adapter owns everything venue-specific and nothing
else:** the socket, REST reads, symbol spelling, channel names and wire layout. It emits
canonical events carrying a canonical `Instrument`, so nothing downstream sees venue JSON.
**Only feed contacts Delta.**

#35 landed the instrument and the envelope; #36 moved the venue's three modules behind the
protocol and added a **scripted fake adapter**, the one new test seam; #37 retired the quote
record that used to carry a raw frame past the adapter, moved the socket owner into the
adapter package with it, and moved the two consumers onto the events. The `connect` factory
stays injectable, because that is the seam the feed tests already drive. Redis is mandatory in
split mode: an unreachable Redis fails startup with the existing `BusUnavailable` error before
the feed opens the venue.

### 2.3 The engine process

`deltapayoff.main:app` owns `ChainStream`, Parquet reads, the REST routes and `/ws/chain`. In
split mode it opens no Delta socket, owns no writer and performs no Parquet writes:
`ChainStream` consumes `chain-stream` and `FeedConnectionCache` consumes `feed-state` with
drop-oldest semantics. `/chain` and `/expiries` answer from `ChainStream` in that mode.

#### The store process

`deltapayoff.store_main:app` owns the lossless `store` consumer group, its checkpoint and
replay from the last flush, and is the only writer of the four tables. It publishes `store.state`;
the protocol is [store-replay.md](lld/store-replay.md). Its one route, `GET /health`, is **readiness, not liveness** — 503 once its reader stops, its group is trimmed past, or its lag passes the threshold ([store-health.md](lld/store-health.md)).

### 2.4 The bus

**One producer, many independent consumers.** `publish(event)` and
`subscribe(name, maxsize, lossless)` since #37, so that a broker could replace the implementation
without touching a producer or a consumer. The socket handler publishes and returns: if it blocks,
the OS receive buffer fills and the venue closes us.

**Queue policy is per subscription and does not change.** The bar writer subscribes losslessly,
because drop-oldest under load systematically shaves the highs and lows bars exist to capture;
the chain cache drops the oldest, because it holds only the newest frame per contract anyway.
Every drop is counted.

**Two implementations behind that seam since #61.** `fanout.py` remains the default when
`DELTA_BUS` is unset: the unchanged in-process monolith. `redis_bus.py` is selected by
`DELTA_BUS=redis`; feed publishes in pipelined batches and trims by age, while the consumers
own their independent subscriptions. Lossless remains acked and replayed from a recorded id;
drop-oldest still jumps to the newest entries and **counts what it skipped**. Details are
[lld/redis-bus.md](lld/redis-bus.md); names and encoding are
[cloud/nomenclature.md](cloud/nomenclature.md); ack, trim and persistence are
[cloud/message-bus.md](cloud/message-bus.md) §4.

### 2.5 The consumers

**ChainStream** (`stream.py`) is the live chain cache: newest event per contract, rebuilt into a
ladder on demand, with the recompute loop solving our IV and Greeks. In split mode it receives
canonical market events only from Redis and drives `/chain`, `/expiries` and `/ws/chain`.
**BarWriter** (`bars.py`, `store.py`) lives in `store` in split mode and folds the lossless
stream into four Parquet tables; a minute with no arrivals produces no row. In that mode it
folds `computed.chain` published by the api rather than sampling a cache. The pricing core
remains pure, and venue IV and Greeks remain reference columns, never inputs.
**Discord alerts** (`alert_main.py`) are the sixth container: they subscribe only to the
`alert` stream in their own consumer group and post to Discord through a configured webhook;
they never open `feed`, `store` or `api` to do this.

### 2.6 The public surface and the screens

In split mode, feed has `GET /health`, which is its local `FeedSupervisor.report`. The engine
serves `/expiries`, `/chain`, `/smile`, `/iv-vs-rv`, `/recording`, `/health` and `/ws/chain`,
while store has `GET /health`. Its `/health` is authoritative about remote feed state from the
`feed.connection` and `heartbeat` observations in `FeedConnectionCache`, while retaining process
liveness and watched pairs. The websocket badge is derived from that same projection, including
its heartbeat-silence rule. Screen commands enter through the existing
`POST /feed/{adapter}/{command}` route: the API publishes `control.command` and the feed returns
state through the bus. No service-to-service HTTP is added. The default no-`DELTA_BUS` engine
keeps the existing combined surface and synchronous command path.

`web/` renders and computes nothing. It will wear a badge on the ladder header whenever the feed
is not `connected`, clearing on recovery (#40); gain a chart panel of a contract's minute candles
opened by clicking a strike (#46); and gain a time slider whose right edge is live and whose left
is every stored minute of the day (#45).

## 3. The connection states

**The five states, and what each one means, are
[cloud/data-feed-engine.md](cloud/data-feed-engine.md) §4** — one table, in one place, beside the
rules that move a connection between them. A connection is in exactly one of them at every moment.
These are the transitions:

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
| Feed adapter | Redis publisher | `md.option_quote`, `md.option_reference`, `md.index_quote` |
| Redis | ChainStream, store/BarWriter | the three canonical market events |
| Redis | FeedConnectionCache | `feed.connection`, `heartbeat` |
| api | Redis -> store | `computed.chain` (split composition only) |
| store | Redis -> api | `store.state` |
| BarWriter | Redis | `md.option_bar` (store process only) |
| Redis | BarBuffer | `md.option_bar` (api process only) |
| ChainStream | REST and websocket handlers | ladders from the live cache |
| store/BarWriter | Parquet store | sealed `md.option_bar` data |
| Store | the REST routes | Parquet reads, carrying no event |

Fields, emitters, consumers and timing are in [events.md](events.md).

## 5. Evidence and boundaries

[HLD evidence](hld-evidence.md) contains every measured, assumed, and derived number, the run
and caveat behind each, and the explicit out-of-scope decisions.

## 6. Where it runs, and the words it uses

The bus is [cloud/message-bus.md](cloud/message-bus.md) and the engine is
[cloud/data-feed-engine.md](cloud/data-feed-engine.md). Both resolve their terms to [../../CONTEXT.md](../../CONTEXT.md).
