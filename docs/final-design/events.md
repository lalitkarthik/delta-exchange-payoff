# Events

**An event is the contract between two parts that never call each other.** If a producer and a
consumer agree on the events, each can be built, tested and replaced without the other being
opened. This page is that agreement: the envelope, the canonical instrument, the ten events, and
the rule for changing one.

Where this page and a wire contract disagree about what reaches the browser,
`docs/chain-contract.md` wins, because it is the engine-to-web authority.

## The envelope

Seven fields, on every event. Frozen once built, extra fields forbidden, registered by `type`.

| Field | Meaning |
|---|---|
| `type` | The event's name, one of the ten below. Parsing an unregistered type raises |
| `event_id` | Unique per event -- what a consumer deduplicates on and a log record joins on |
| `schema_version` | Integer, starts at `1` |
| `source` | Who built it: the adapter's venue name, or the component that emitted it |
| `ts_venue` | The venue's own stamp, as the venue gave it. `null` where the venue gives none |
| `ts_received` | Our wall clock at arrival |
| `instrument` | The canonical `Instrument`, or `null` for events not about one contract |

**Neither timestamp is a latency clock.** `ts_received` comes from `time.time()`, which can step
backwards under an NTP correction; elapsed time is measured on the monotonic clock. The
arrival-lag column is `ts_received - ts_venue`, and it is data rather than an error to correct:
`measured` p50 212.6 ms, p99 365.3 ms, max 510.3 ms on `ob_l2` against a median 3,176 ms on
`ticker` -- which is why the two seal on different graces.

## The canonical instrument

A frozen record: `venue`, `underlying`, `expiry` as a calendar date, `strike` as a `Decimal`,
`right` as `C` or `P`, `venue_symbol` kept verbatim, and `quote_currency` and
`settlement_currency` as ISO 4217 strings.

```
VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY
DELTA-BTC-20260627-60000-C-USD
```

- **The string is derived and never stored as truth.** It exists so a log line, a cache key, a URL
  and a stream field agree.
- **The last token is the *quote* currency.** `settlement_currency` does not travel in it: a string
  naming two currencies with no marker for which is which would be a worse ambiguity than the one
  it closes.
- **`strike` is a `Decimal`, never a float**, and carries no trailing zero and no exponent.
  `60000`, not `60000.0` and not `6E+4` -- an exponent in a cache key is a bug waiting a year.
- **`venue_symbol` is not in the string**, so two venues listing the same contract produce the same
  string. `from_canonical` takes it back as a keyword argument.
- **`venue` and `underlying` may not contain `-`**, or the string would stop being its own inverse.

## The ten events

Nine travel outbound from a producer; one travels inbound.

### `md.option_quote` -- top of book

Emitted by the broker adapter from the venue's book channel; consumed by the chain cache and the
bar writer. `measured` every 508 ms per contract.

**Payload** `bid`, `bid_size`, `ask`, `ask_size`, `lts`. Absent quotes are `null`, including where
the venue spells absence `"0"`. The mid is computed per tick by the consumer, never from separately
aggregated bid and ask. `lts` is the venue's last-trade stamp: carried, **never bucketed on** --
`ts_venue` alone decides a tick's minute.

### `md.option_reference` -- the venue's own view of a contract

Emitted by the adapter from the venue's ticker channel; consumed by the chain cache and the bar
writer. `measured` every 5,001 ms per contract.

**Payload** `mark`, `last_price`, `oi` (contracts), `oi_value_usd`, `oi_change_usd_6h`,
`tick_size`, `turnover`, `product_id`, `bid`, `ask`, and the venue's own `bid_iv`, `ask_iv`,
`mark_iv`, `delta`, `gamma`, `theta`, `vega`, `rho`.

**These are reference columns and never inputs**, pinned by `tests/test_no_delta_inputs.py`. The
`bid` and `ask` here are the **fallback quote** when the book stays silent for a whole minute --
not a second opinion but the same quantity slower, so a consumer holding both takes the book's.

### `md.index_quote` -- the underlying's spot

Emitted off the same ticker frames, **one per frame and not one per change**: the spot bars count
observations, and `spot_ticks` is the column that says whether the ingester was running at all.
`instrument` is `null`, because spot is a property of the underlying and not of a contract.

**Payload** `underlying`, `spot`.

### `md.option_bar` -- a sealed minute bar

Emitted by the bar writer at seal, **in the store process only**; consumed by the api's
`BarBuffer`. **A minute with no arrivals produces no event**, as it produces no row.

**Payload** `table` (quote, reference, spot or computed), `underlying`, `minute`, and that table's
columns. The four store schemas are not re-declared on the event -- `store.py` is their authority,
and a second copy is exactly the drift this catalogue prevents.

### `computed.chain` -- our IV and Greeks for one expiry

Emitted by the chain cache's recompute pass; consumed by the bar writer's computed table.

**Payload** `underlying`, `expiry`, `forward`, `discount`, `forward_method`, `years_to_expiry`,
`fetched_at`, `model_version`, `solver`, and per strike `strike`, `symbol`, `iv`, `iv_leg`,
`iv_reason`, and the five Greeks. `instrument` is `null` -- the event is about an expiry.
**`iv` is `null` and never `0`**; a solved volatility of zero is not a thing, and the model
rejects it.

### `store.state` -- the store's venue-scoped state

Emitted by the store every ten seconds and on every change of recording, committed generation or
replay-gap counter. **Payload** `recording`, `buffered_rows`, `rows_written`,
`replay_gap_entries`, `already_flushed`, `flush_errors`, `generation`.

### `feed.connection` -- a state transition

Emitted by the connection controller, once per transition; consumed by the supervisor, `/health`,
the websocket handler (which forwards it to every browser) and the logger. The websocket also sends
the state once on connect, so a browser that joined mid-stream is not blind.

**Payload** `adapter`, `from_state`, `to_state`, `reason`.

### `heartbeat` -- the feed is alive

Emitted per adapter on a timer, whatever the state. **A heartbeat is not a message from the
venue**: it says the controller is running, and carries the age of the last real message so that a
quiet market and a dead socket are distinguishable.

**Payload** `adapter`, `state`, `last_message_age_seconds`.

### `alert` -- something a person should see

Emitted by any component; in practice the controller and the store. **Payload** `severity`,
`code`, `detail`, and `adapter` where one applies.

| Code | Raised when |
|---|---|
| `connection_silent` | Silence past `reconnect_after` |
| `poll_failing` | The controller's watchdog polls are raising |
| `store.flush_failed` | A Parquet flush raised |
| `store.replay_gap` | A saved position needs entries the bus has trimmed |
| `store.consumer_lag` | The store's group is behind past the threshold |
| `store.empty_generation` | A generation committed zero rows across all four tables while recording |
| `bus.reader_stopped` | A bus reader reached its retry bound and gave up |

### `control.command` -- the one inbound event

Raised by an operator over `POST /feed/{adapter}/{command}`. **Payload** `adapter`, `command`
(`pause`, `resume` or `reconnect`), `target`. `pause` reaches `stopped` **spending no reconnect
budget**; `resume` restores the budget in full; `reconnect` cuts the socket and lets ordinary close
handling take over. `target` defaults to `feed`, and a service drops anything not addressed to it.

## The events carry no channel

A channel is a venue's word, and the point of an adapter is that no consumer learns one. That left
one question: the quote bars store `from_book`, which says whether a minute's prices came from the
book or from the slower channel standing in for a silent one.

**The event type is the provenance.** A tick built from `md.option_quote` is a book tick; one built
from `md.option_reference`'s `bid`/`ask` is a fallback tick. Nothing is added to a payload. The
rejected alternative was a `channel` string, which would put a venue's vocabulary back on the bus
to redraw a line two type names already draw.

## Versioning

`schema_version` starts at `1` for every type and is **bumped when a field changes meaning, not
when one is added with a default.** Adding an optional field is compatible: an old consumer ignores
it and nothing true stops being true. Renaming a field, changing its units, or changing when it is
`null` is not.

The version is per type, so bumping one leaves the others at `1`. **A consumer receiving a version
it does not know fails loudly** -- `UnknownSchemaVersion`, naming the type and both versions --
checked at **parse** and not at construction, because "do I understand this?" only arises for a
payload that crossed a boundary.

Three units are fixed and are **not** a versioning matter, because changing one is a bug: IV is a
decimal fraction on the wire; every decimal is a JSON number or `null`; `oi` is contracts and
`oi_value_usd` is a notional, and they are different quantities.

## Failure modes

| What goes wrong | What happens |
|---|---|
| Type not registered, or absent | `UnknownEventType`, carrying the name |
| Payload has an undeclared field | `ValidationError` -- `extra="forbid"` |
| Two classes claim one type name | `ValueError` at import |
| A consumer mutates an event | `ValidationError` -- frozen |
| A price is `NaN` or `Infinity` | `ValidationError`; it would serialise to `null` and read as absent |
| Canonical string malformed, or five-part | `InstrumentParseError`, naming the part that was wrong |
| A `schema_version` nobody knows | `UnknownSchemaVersion` at parse |

## Related guides

- [Message bus](message-bus.md) -- how these travel, and what is kept
- [Adapters](adapters.md) -- who produces them
- [Data store](data-store.md) -- where the market-data ones are folded
