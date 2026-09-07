# The events catalogue

**An event is the contract between two parts that never call each other.** If a producer and a
consumer agree on the events, each can be built, tested and replaced without the other being
opened. This document is that agreement: the envelope every event carries, the nine events, and
the rule for changing one.

It is the authority on names and directions. Where it and [hld.md](hld.md) disagree, this
document wins; where it and `docs/chain-contract.md` disagree about what reaches the browser, the
contract document wins, because that is the engine↔web authority.

**Nothing here is built yet.** #35 lands the envelope and the registry, #37 makes the producers
and consumers use them. Today `feed.py` publishes a `Quote` record carrying a raw `frame` dict,
and the channel name travels with it; that record is retired by #37. Field spellings below are
the ones the store and `chain-contract.md` already use, so the two agree by construction; #35
fixes them in code.

---

## The envelope

Seven fields, on every event. Frozen once built, extra fields forbidden, registered by `type`.

| Field | Meaning |
|---|---|
| `type` | The event's name, one of the nine below. Registered; parsing an unregistered type raises. |
| `event_id` | Unique per event. What lets a consumer deduplicate, and what a log record joins on. |
| `schema_version` | Integer, starts at `1`. See *Versioning*. |
| `source` | Who built it — the adapter's venue name, or the engine component that emitted it. |
| `ts_venue` | The venue's own stamp, as the venue gave it. `null` where the venue gives none. |
| `ts_received` | Our wall clock at arrival. |
| `instrument` | The canonical `Instrument`, or `null` for events that are not about one contract. |

**Neither timestamp is a latency clock.** `ts_received` comes from `time.time()`, which can step
backwards under an NTP correction; elapsed time is measured on the monotonic clock, as
`timing.time_it` does today. **The arrival-lag column is `ts_received − ts_venue`** and is the
number the store already carries as a watermark: `measured` p50 212.6 ms, p99 365.3 ms, max
510.3 ms on `ob_l2` (`tools/measure_arrival_lag.py`, 2026-09-04, 61,648 frames over 45 s), and a
median 3,176 ms on `ticker` in the same run, which is why the two channels seal on different
graces.

**The instrument** is a frozen record — `venue`, `underlying`, `expiry` as a calendar date,
`strike` as a decimal, `right` as call or put, and `venue_symbol` kept verbatim. Its canonical
string is derived and never stored as truth: `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P`, e.g.
`DELTA-BTC-20260627-60000-C`.

---

## The nine events

Eight travel outbound from a producer to the bus. One travels inbound.

### `md.option_quote` — top of book

- **Direction** outbound. **Emitted by** the broker adapter, from the venue's book channel.
- **Consumed by** the chain cache and the bar writer.
- **When** every book frame — `measured` 508 ms per contract on Delta's `ob_l2`
  (`tools/measure_feed.py`).
- **Payload** `bid`, `bid_size`, `ask`, `ask_size`. Absent quotes are `null`, including where the
  venue spells absence `"0"`; a real zero stays `0`. The mid is computed per tick by the
  consumer, never derived from separately aggregated bid and ask.

### `md.option_reference` — the venue's own view of a contract

- **Direction** outbound. **Emitted by** the broker adapter, from the venue's ticker channel.
- **Consumed by** the chain cache and the bar writer.
- **When** every ticker frame — `measured` 5,001 ms per contract (`tools/measure_feed.py`).
- **Payload** `mark`, `last_price`, `oi` (contracts), `oi_value_usd`, `oi_change_usd_6h`,
  `tick_size`, and the venue's own `bid_iv`, `ask_iv`, `mark_iv`, `delta`, `gamma`, `theta`,
  `vega`, `rho`.
- **These are reference columns and never inputs.** Nothing computes on them.
  `tests/test_no_delta_inputs.py` pins it and must keep passing with this event in place.

### `md.index_quote` — the underlying's spot

- **Direction** outbound. **Emitted by** the broker adapter, off the same ticker frames.
- **Consumed by** the chain cache and the bar writer.
- **When** every ticker frame. `instrument` is `null`; the payload names the underlying instead,
  because spot is a property of the underlying and not of a contract.
- **Payload** `underlying`, `spot`.

### `md.option_bar` — a sealed minute bar

- **Direction** outbound. **Emitted by** the bar writer at seal.
- **Consumed by** nothing today; published so a later consumer needs no second aggregation.
- **When** a minute closes and its grace elapses — `derived` 2.0 s on the book channel, about
  3.9x the maximum arrival lag measured by `tools/measure_arrival_lag.py` on 2026-09-04.
  **A minute with no arrivals produces no event**, as it produces no row.
- **Payload** `table` (one of quote, reference, spot, computed), `minute`, and that table's
  columns. One event per table per contract per minute.

### `computed.chain` — our IV and Greeks for one expiry

- **Direction** outbound. **Emitted by** the chain cache's recompute pass.
- **Consumed by** the bar writer's computed table, and any later consumer wanting our numbers
  without reaching into the cache.
- **When** a pair is recomputed: the live pass for watched pairs, and the minute-cadence pass for
  every expiry with a frame since its last pass (#44).
- **Payload** `underlying`, `expiry`, `forward`, `discount`, `forward_method`, `years_to_expiry`,
  and per strike our `iv`, `iv_leg`, `iv_reason`, `delta`, `gamma`, `vega`, `theta`, `rho`,
  stamped with `model_version` and the solver that produced it. `instrument` is `null` — the
  event is about an expiry, not a contract. **`iv` is `null` and never `0`.**

### `feed.connection` — a state transition

- **Direction** outbound. **Emitted by** the connection controller, once per transition (#38).
- **Consumed by** the supervisor and `/health` (#39), the websocket handler, which forwards it to
  every open browser as the `feed` message (#40), and the logger (#42).
- **When** on every transition and nowhere else; the websocket also sends the current state once
  on connect, so a browser that joined mid-stream is not blind.
- **Payload** `adapter`, `from_state`, `to_state`, `reason`. `instrument` is `null`. The five
  states and every transition are tabled in [hld.md](hld.md) §3.

### `heartbeat` — the feed is alive

- **Direction** outbound. **Emitted by** the connection controller, per adapter, on a timer.
- **Consumed by** `/health` and the logger.
- **When** periodically, whatever the state. **A heartbeat is not a message from the venue** — it
  says the controller is running, and carries the age of the last real message so that a quiet
  market and a dead socket are distinguishable.
- **Payload** `adapter`, `state`, `last_message_age_seconds`.

### `alert` — something a person should see

- **Direction** outbound. **Emitted by** any component; in practice the controller and the store.
- **Consumed by** the logger at warning level, and `/health`.
- **When** the reconnect budget is nearly or fully spent, a connection has gone stale, a flush
  failed, or a lossless queue dropped a message — which should be impossible and is logged at
  error.
- **Payload** `severity`, `code` (a short stable name), `detail`, and `adapter` where one applies.
- **Codes emitted so far.** The controller (#38): `connection_silent`, when silence passes
  `reconnect_after` and forces `-> reconnecting`; and `poll_failing`, when the staleness
  watchdog's own polls keep raising. **`degraded` does not alert** — fifteen quiet seconds is
  already a badge and a heartbeat, and an alert on every quiet minute is the flood an alert
  exists to stand out from. The budget codes are #39's, which owns the budget.

### `control.command` — the one inbound event

- **Direction** inbound. **Emitted by** an operator, over a route (#41).
- **Consumed by** the supervisor, which hands it to the named controller.
- **When** on demand. Effects: `pause` → `stopped` **without spending reconnect budget**;
  `resume` → `connecting`; `reconnect` → close the socket and enter `reconnecting`.
- **Payload** `adapter`, `command` (one of `pause`, `resume`, `reconnect`).

---

## Versioning

`schema_version` starts at `1` for every type and is **bumped when a field changes meaning, not
when one is added with a default.** Adding an optional field is compatible; renaming one,
changing its units, or changing when it is `null` is not.

The version is per type, so bumping `md.option_reference` leaves the other eight at `1`. A
consumer that does not know a version it receives must fail loudly rather than guess. The
registry is the enumeration: if a type is not in it, parsing raises.

**Three units are fixed and are not a versioning matter, because changing one is a bug.** IV is a
decimal fraction on the wire and a percentage only on screen — the engine never multiplies by
100. Every decimal is a JSON number or `null`, never a string, converted once at the adapter
boundary. Open interest `oi` is contracts, `oi_value_usd` is notional, and they are different
quantities.

## The bus these travel on

`publish(event)` and `subscribe(name, maxsize, lossless)`, implemented today by `fanout.py` and
replaceable by a broker without touching a producer or a consumer (#37).

**Subscription semantics do not change.** The bar writer subscribes with `lossless=True`, where
`maxsize` stops being a ceiling and becomes a watermark and every over-capacity offer is counted;
the chain cache subscribes bounded and drops the oldest, because a quote from four seconds ago is
worthless to a screen. Every drop is counted, because a silent drop is a lie.

## What is deliberately not an event

Parquet reads. The two new REST routes (#45, #46) read the store directly and publish nothing —
history is a query, not a stream. Nothing on the right half of the whiteboard is catalogued here
either; the envelope is shaped so order execution can share it, and that is all.
