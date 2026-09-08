# The events catalogue

**An event is the contract between two parts that never call each other.** If a producer and a
consumer agree on the events, each can be built, tested and replaced without the other being
opened. This document is that agreement: the envelope, the nine events, and the rule for changing
one. It is the authority on names and directions; where it and [hld.md](hld.md) disagree it wins,
and where it and `docs/chain-contract.md` disagree about what reaches the browser that contract
wins, because it is the engine↔web authority.

**Built.** #35 landed the envelope and the registry, #36 the adapter that produces the market-data
events, #37 the consumers that take them. The `Quote` record that carried a raw `frame` dict with
the venue's channel name on it is **gone**; nothing outside `adapters/` names a channel. Field
spellings are the store's and `chain-contract.md`'s, so the two agree by construction.

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
`timing.time_it` does. **The arrival-lag column is `ts_received − ts_venue`**, the number the
store carries as a watermark: `measured` p50 212.6 ms, p99 365.3 ms, max 510.3 ms on `ob_l2`
(`tools/measure_arrival_lag.py`, 2026-09-04, 61,648 frames over 45 s) against a median 3,176 ms
on `ticker`, which is why the two seal on different graces.

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
- **When** every book frame — `measured` 508 ms per contract (`tools/measure_feed.py`).
- **Payload** `bid`, `bid_size`, `ask`, `ask_size`, `lts`. Absent quotes are `null`, including
  where the venue spells absence `"0"`; a real zero stays `0`. The mid is computed per tick by
  the consumer, never derived from separately aggregated bid and ask.
- **`lts` is the venue's own last-trade stamp**, added by #37 for the quote bars' `last_lts`
  column. Carried, **never bucketed on**: `ts_venue` alone decides a tick's minute.

### `md.option_reference` — the venue's own view of a contract

- **Direction** outbound. **Emitted by** the broker adapter, from the venue's ticker channel.
- **Consumed by** the chain cache and the bar writer.
- **When** every ticker frame — `measured` 5,001 ms per contract (`tools/measure_feed.py`).
- **Payload** `mark`, `last_price`, `oi` (contracts), `oi_value_usd`, `oi_change_usd_6h`,
  `tick_size`, `turnover`, `product_id`, `bid`, `ask`, and the venue's own `bid_iv`, `ask_iv`,
  `mark_iv`, `delta`, `gamma`, `theta`, `vega`, `rho`.
- **Four added by #37**, `schema_version` left at `1`: `turnover` and `product_id` for a stored
  column and a browser field, and `bid`/`ask` because this channel publishes its own top of book
  and it is the **fallback quote** when the book stays silent for a whole minute. Not a second
  opinion but the same quantity slower, so a consumer holding both takes the book's **wholesale**.
- **These are reference columns and never inputs**, pinned by `tests/test_no_delta_inputs.py`.
  The prices added are quotes rather than opinions; IV, Greeks and `mark` stay reference-only.

### `md.index_quote` — the underlying's spot

- **Direction** outbound. **Emitted by** the broker adapter, off the same ticker frames.
- **Consumed by** the chain cache and the bar writer.
- **When** every ticker frame. `instrument` is `null`; the payload names the underlying instead,
  because spot is a property of the underlying and not of a contract.
- **Payload** `underlying`, `spot`.
- **One per ticker frame, not one per change.** #36 emitted this only when the value moved; #37
  removed that suppression, because the spot bars count observations and `spot_ticks` says
  whether the ingester was running at all. `derived` ≈118 extra events a second against a
  `measured` 1,322.9 msg/s feed; the price columns are unaffected, since suppression only ever
  dropped a value identical to the one before it. An absent spot, or one spelled `"0"`, is
  absent rather than a price of zero.

### `md.option_bar` — a sealed minute bar

- **Direction** outbound. **Emitted by** the bar writer at seal.
- **Consumed by** nothing today; published so a later consumer needs no second aggregation. Not
  yet emitted either: the writer's four aggregators still hand their bars straight to the store.
- **When** a minute closes and its grace elapses. **A minute with no arrivals produces no
  event**, as it produces no row.
- **Payload** `table` (quote, reference, spot or computed), `minute`, and that table's columns.

### `computed.chain` — our IV and Greeks for one expiry

- **Direction** outbound. **Emitted by** the chain cache's recompute pass.
- **Consumed by** the bar writer's computed table, and any later consumer wanting our numbers.
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
- **When** on every transition; the websocket also sends the state once on connect, so a browser
  that joined mid-stream is not blind.
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
  `reconnect_after` and forces `-> reconnecting`; and `poll_failing`, when the watchdog's own
  polls keep raising. **`degraded` does not alert, and nor does a `pause`** — fifteen quiet
  seconds is already a badge and a heartbeat, a pause is something a person just did, and an
  alert on either is the flood an alert exists to stand out from. Budget codes are #39's.

### `control.command` — the one inbound event

- **Direction** inbound. **By** an operator, over `POST /feed/{adapter}/{command}` (#41).
- **Consumed by** `supervisor.FeedSupervisor`, which publishes it and then offers it to every
  controller; the one whose adapter it names takes it. **Delivered synchronously inside that
  publish, not through a queue** — `docs/design/lld/commands.md` §3 says why.
- **When** on demand. Effects: `pause` → `stopped`, reason `paused`, **spending no reconnect
  budget**; `resume` → `connecting`, budget restored in full; `reconnect` → cut the socket
  and let ordinary close handling reach `reconnecting`.
- **Payload** `adapter`, `command` (one of `pause`, `resume`, `reconnect`).

---

## A note on provenance

**The events carry no channel**, because a channel is a venue's word and the point of an adapter
is that no consumer learns one. That left one question when the old quote record was retired: the
quote bars store `from_book`, which says whether a minute's prices came from the venue's order
book or from the slower channel standing in for a silent one, and a tick count cannot answer it
— twelve samples could be a quiet book or no book at all.

**The event type is the provenance.** A tick built from `md.option_quote` is a book tick; one
built from `md.option_reference`'s `bid`/`ask` is a fallback tick. Nothing is added to a payload.
The rejected alternative was a `channel` string on `md.option_quote`, which would put a venue's
vocabulary back on the bus to redraw a line the catalogue already draws with two names.

---

## Versioning

`schema_version` starts at `1` for every type and is **bumped when a field changes meaning, not
when one is added with a default.** Adding an optional field is compatible; renaming one,
changing its units, or changing when it is `null` is not. The nine fields #37 added to
`md.option_quote` and `md.option_reference` are all optional with defaults, so every type is
still at `1`.

The version is per type, so bumping `md.option_reference` leaves the other eight at `1`. A
consumer that does not know a version it receives must fail loudly rather than guess. The
registry is the enumeration: if a type is not in it, parsing raises.

**What a consumer knows is the version its own class declares.** `parse_event` refuses any other
with `UnknownSchemaVersion`, naming the type and both versions, so a newer producer's event is an
error at the boundary rather than a record whose fields this build quietly reads with the wrong
meaning. Refused at **parse**, not at construction: a producer inside this process builds at the
version it was compiled with, and "do I understand this?" only arises for a payload that crossed
a boundary.

**Three units are fixed and are not a versioning matter, because changing one is a bug.** IV is a
decimal fraction on the wire and a percentage only on screen — the engine never multiplies by 100.
Every decimal is a JSON number or `null`, converted once at the adapter boundary. Open interest
`oi` is contracts, `oi_value_usd` is notional, and they are different quantities.

## The bus these travel on

`publish(event)` and `subscribe(name, maxsize, lossless)`, implemented today by `fanout.py` and
replaceable by a broker without touching a producer or a consumer. **One bus since #37**, where
two ran side by side. Subscription semantics are unchanged: the bar writer subscribes with
`lossless=True`, where `maxsize` becomes a watermark and every over-capacity offer is counted;
the chain cache subscribes bounded and drops the oldest, because a quote from four seconds ago is
worthless to a screen. Every drop is counted, because a silent drop is a lie.

## What is deliberately not an event

Parquet reads. The two new REST routes (#45, #46) read the store directly and publish nothing —
history is a query, not a stream. Nothing on the right half of the whiteboard is catalogued here;
the envelope is shaped so order execution can share it, and that is all.
