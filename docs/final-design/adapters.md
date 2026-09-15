# Adapters

**An adapter is the one class that knows a venue.** Everything it says outward is in our language
-- canonical `Instrument`s and catalogued `Event`s -- and everything it hears inward is in the
venue's: the socket, the REST calls, the symbol spelling, the channel names, the wire layout.
**Nothing downstream of an adapter ever sees venue JSON.**

In split mode `feed` owns the adapter exclusively; the api holds no venue socket and the store has
never had one.

## The protocol

Eight members in three groups. Implementing them is the whole of being a venue here.

| Group | Member | Contract |
|---|---|---|
| Describe | `venue` | The venue's short name, upper case: `DELTA`, later `NSE` |
| Describe | `underlyings` | What this adapter records. **Configuration, not a constant** |
| Feed | `instruments(underlying)` | Every listed contract, as canonical instruments, with `venue_symbol` riding along |
| Feed | `subscribe(instruments)` | Register for streaming. **Safe before anything is connected** |
| Feed | `on_connection` / `off_connection` | Register and unregister a socket-signal listener |
| Feed | `stream(publish)` | **Connect once and stream until the connection closes. Then return** |
| Feed | `stop()` | Ask `stream` to return. Synchronous, safe before `stream` runs |
| Read | `expiries`, `chain_snapshot` | The two venue REST reads the screens need. They are on the adapter because the venue client *is* part of knowing a venue |

**`stream` is push, not an async generator**, because the socket owner already publishes and
returns. `publish` is synchronous and must never block: the socket reader calls it between reads, so
anything that suspends there suspends the socket.

**Reconnect is not on the interface, and not below it either.** One call is one connection: dial,
replay, publish until the socket ends, return. Backoff, the budget and the decision to redial belong
to `controller.py` and `supervisor.py` above it -- the layer that can say *we have given up* out
loud is the layer that decides it. `stream` **returns rather than raises** on every ending, and a
`stop()` returns *without* reporting a close: a stop is not a drop, and the controller reads exactly
that difference.

**An adapter reports facts about its socket, never states.** `ConnectionSignal` has two members:
`OPENED` and `CLOSED`. What they mean -- `connected`, `degraded`, `reconnecting` -- is the
controller's to decide; an adapter that reported states would be a second state machine disagreeing
with the first. `OPENED` means the socket is up **and every subscription has been replayed**: a
fresh socket with no subscriptions delivers nothing, the failure-with-no-error this layer exists to
prevent.

**`off_connection` exists because a register with no way out is a leak with a voice**: a replaced
controller would keep driving a state machine nobody reads, off a socket it no longer owns.

**Conformance is structural, not nominal.** An explicit subclass of a `Protocol` inherits
`...`-bodied stubs returning `None`, so a missing `stream` would deliver silence instead of raising
`AttributeError`. The structural `isinstance` check does fail, and a test pins that it still can.

## The Delta adapter

| File | Holds |
|---|---|
| `adapters/base.py` | The `Adapter` protocol and the `Publish` callable |
| `adapters/delta.py` | `DeltaAdapter`, `instrument_from_symbol`, `VENUE` |
| `adapters/delta_socket.py` | `DeltaFeed`, `VenueMessage`, and **the two channel names** |
| `wire.py`, `delta_client.py` | The frame offsets and the REST reads, reached through the adapter and nothing else |
| `tests/fakes/scripted_adapter.py` | The scripted double |

The two channel strings appear **nowhere in `src/` outside this package**, and a test asserts it.
`delta_socket.py` decodes nothing: it publishes a `VenueMessage` -- the frame verbatim, with its
channel and arrival stamp -- and the decode happens one layer up.

### The three mappings

```
ob_l2  frame  ->  md.option_quote
ticker frame  ->  md.option_reference
ticker frame  ->  md.index_quote      one per frame
```

**One index quote per ticker frame, not one per change**, and the reason is the store rather than
the screen: the spot bars count observations, `spot_ticks` is the column that says whether the
ingester was running at all, and a deduplicated stream cannot say how long a price held.

### The symbol mapping

```
C-BTC-77600-040926   ->   DELTA-BTC-20260904-77600-C-USD
```

The venue's string is kept verbatim in `venue_symbol`. **A symbol that is not a contract returns
`None` and is counted, never raised**: `underlying` is a partition directory name, and a wrong guess
files quotes under an asset they did not happen in.

`quote_currency` and `settlement_currency` are set **explicitly** by the adapter rather than left to
a class default: the requirement is that the adapter states them. Neither is on the ticker frame --
the venue carries them on a different endpoint, one call per symbol, for a value that is today
always `USD`. The constants live in `chain.py` and are re-exported, so two files read one fact.

### `null` is not `0` -- the boundary lives here

Delta spells an absent quote three ways: `"0"`, `""` and `null`. All three become `None` on a price,
a size or an implied volatility, because rendering one as `0.0` claims somebody bid zero. A real
zero in open interest or a greek stays `0.0`. `convert.to_quote_number` and `convert.to_number` are
that split, applied field by field in `wire.py`.

**The events cannot enforce it** -- a string `"0"` handed to a pydantic `float` field is coerced to
`0.0` -- which is why the rule lives with the adapter, where the venue's spellings are known. A
fixture lifting those spellings verbatim pins it.

**A non-finite number is absent, counted, and takes its size with it.** The event model refuses
`NaN` outright, so the adapter converts to `None` first and increments `non_finite`; an absent price
drops its size, or the size would describe an order at no price. Reachable, not defensive:
`json.loads` accepts the bare tokens `NaN` and `Infinity`.

### Both stamps travel

`ts_venue` is the venue's own microsecond stamp, converted by integer arithmetic so the last digit
survives -- the store buckets on it. `ts_received` is our wall clock at the socket read. **Neither is
corrected against the other: the arrival lag is the data.**

## Failure modes

| What goes wrong | What happens |
|---|---|
| Frame is not JSON | `feed.malformed` grows; the read loop continues |
| Frame is JSON and makes no sense | `adapter.undecodable` grows; **nothing reaches the bus**. The **first** is logged and the counter carries the rest -- logging each would flood at 1,323 msg/s |
| The symbol is not an option symbol | `unparseable_symbols` grows; no events at all |
| A price is `NaN` or `Infinity` | Carried as `None`; `non_finite` grows |
| The venue gives no stamp | `ts_venue` is `null`; our clock is never substituted |
| Control traffic (`subscriptions`, `error`) | Dropped in the socket layer; never on a bus |
| Configuration names an asset the venue does not list | Dropped, logged at error; the rest still record |
| The venue is unreachable at start-up | `DeltaUnavailable`; REST serves, the socket says `waiting` |

## Building a new adapter

### 1. Satisfy the protocol, and nothing else

The protocol is the whole test of the abstraction: a venue with a different shape -- NSE spells the
same contract `NIFTY-20260908-25500CE`, in rupees, in lots, off a different calendar -- must fill it
**without the interface bending**. A member added for one venue means the design is wrong elsewhere.

### 2. Keep the venue's vocabulary inside the package

Channel names, endpoint paths, field offsets and symbol spellings live in your adapter package and
nowhere else. Write the test that greps `src/` for your channel strings; it is the cheapest
guarantee in this repository.

### 3. Emit the existing events. Do not invent one

The ten events are the contract. A venue carrying something none of them holds is a **catalogue**
change -- argued in [Events](events.md), under the version rule -- not a payload the adapter
smuggles through.

### 4. Hold the boundary rules at the boundary

`null` is not `0`; every decimal is a JSON number; non-finite is absent and counted; both timestamps
travel uncorrected. Nothing downstream can recover a distinction the adapter collapsed.

### 5. Follow the naming scheme

| Thing | Rule | Example |
|---|---|---|
| Venue name | Upper case, one word, no `-` | `DELTA`, `NSE` |
| Underlying | Upper case, no `-` | `BTC`, `NIFTY` |
| Adapter package | `adapters/<venue lower>.py`, socket in `<venue lower>_socket.py` | `adapters/nse.py` |
| `VENUE` constant | Module-level, upper case, the string every `Instrument` carries | `VENUE = "NSE"` |
| Canonical symbol | `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C\|P-CCY` | `NSE-NIFTY-20260908-25500-C-INR` |
| Option right | `C` and `P`. One spelling, no mapping table | |
| Stream name | `{event_type}:{VENUE}[:{UNDERLYING}]` | `md.option_quote:NSE:NIFTY` |
| Consumer group | The **service** name, lower case, one word -- never the venue | `store`, `api` |
| Consumer name | `{service}-{instance}` | `store-1` |
| Log `event` | `{area}.{noun}`, registered in `log_events.ALL` | `feed.transition` |
| Alert `code` | `{area}.{condition}`, short and stable | `store.flush_failed` |
| Environment variable | `DELTA_`-style prefix per venue, or a service prefix | `DELTA_LIVE_UNDERLYINGS` |
| Dataset root | `<noun>-bars`, hive-partitioned `underlying=/date=` | `quote-bars` |

**The venue appears in the stream key and the canonical symbol, and nowhere else.**

### 6. Write the scripted double before the socket

`ScriptedAdapter` implements the same protocol and does what it is told. Four verbs, walked in order
by `stream`:

| Verb | Means |
|---|---|
| `Frames(channel, frames)` | These arrived; their events are published |
| `Close(reason)` | Dropped and came back, **replaying every subscription** |
| `Silence(seconds)` | Nothing arrives for this long; nothing is published |
| `Resume()` | The feed returns with the book it left with -- the last `Frames` again |

So `[Frames(...), Close(), Silence(20.0), Resume()]` produces the events, then nothing, then the
events again. **`Silence` does not wait**: the clock is injected, so twenty seconds are free and
still assertable, which makes a 15-second staleness bound cheap to test. **It emits no
`feed.connection` events** -- a double that pre-empted the state machine would make the machine's
own tests assert against the double.

### 7. Expose the decode as a pure function

`events_from_frame(channel, frame, received_at)` -- three plain values, no socket, no bus, no clock.
Every captured fixture frame runs through it, so the boundary is asserted against real contracts on
both channels, and every consumer test decodes through it so none drifts from it.
## Related guides

[Events](events.md) | [Message bus](message-bus.md) | [Architecture](architecture.md)
