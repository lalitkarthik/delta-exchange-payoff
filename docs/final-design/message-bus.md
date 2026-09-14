# Message bus

**One producer, many independent consumers.** The bus is two methods --
`publish(event)` and `subscribe(name, maxsize, lossless)` -- and every design choice below exists
to keep those two methods honest across an in-process queue and a network broker.

What travels on it is [Events](events.md), which wins on names and directions.

## The interface, and why it is two methods

`Bus` is a `runtime_checkable` `Protocol` naming `publish` and `subscribe`, and **it defines no
behaviour**. Queue policy belongs to the implementation that argues it.

**Conformance is structural, not nominal.** `class FanOut(Bus)` was written first and reverted: a
`Protocol`'s methods are not abstract, so a subclass implementing *neither* still constructs and
inherits `...`-bodied stubs returning `None`. A missing `publish` would stop raising
`AttributeError` and start **dropping messages silently**, and an `isinstance` test against a
nominal subclass is `True` whatever the class contains. The structural check fails when a method
goes missing; that is the whole reason it is the one used.

**The publisher never blocks.** The socket handler publishes and returns; if it blocked, the OS
receive buffer would fill and the venue would close us. That rule is why the bus exists at all,
rather than the socket reader calling the consumers directly.

## Queue policy is per subscription, and does not change

| Consumer | Policy | Why |
|---|---|---|
| Bar writer | **lossless** | A dropped message is a permanent hole in the record. Drop-oldest under load systematically shaves the highs and lows bars exist to capture |
| Chain cache | **drop-oldest** | It holds only the newest frame per contract anyway, and a four-second-old quote is worthless to a screen |
| Feed/store state caches | drop-oldest | Only the latest state matters |
| Alert consumer | lossless | An alert nobody sees is the failure it was raised about |

**Every drop is counted, because a silent drop is a lie.** On Redis, so is every entry a reader
skipped: Redis trims silently and this does not.

For a lossless subscription `maxsize` is a **watermark**, not a ceiling -- an over-capacity offer
is counted and delivered. A genuine lossless drop is logged at **error**, because under the current
construction it should be impossible.

## Two implementations behind one seam

| | `fanout.py` | `redis_bus.py` |
|---|---|---|
| Selected by | `DELTA_BUS` unset (the default) | `DELTA_BUS=redis` |
| Transport | asyncio queues in one process | Redis Streams |
| Scope | the monolith | the four-process split |
| Delivery | direct | pipelined batches, `XADD` |

A producer and a consumer are opened by neither choice. Redis is **mandatory** in split mode: an
unreachable Redis raises `BusUnavailable` at startup, before the feed opens the venue socket.

## Why Redis Streams, and not a managed service

**No AWS message service carries this bus.** Redis Streams does, in a container beside the
services, on the instance's own loopback under `host` network mode.

| Rejected | Why |
|---|---|
| ElastiCache for Valkey | `derived` $47.30/month against a container's $0. **The named fallback** if the hosting criteria move -- one endpoint string |
| ElastiCache Serverless | It has no `maxmemory`, so a trim that stops working is a bill and not an error |
| MemoryDB | Durability is the product and cannot be turned off, for frames the venue can re-tell |
| ZeroMQ | Settled against in-process fan-out early; do not reopen |

**A container, not a managed cache, because the bus is a pipe.** Losing it costs a restart and not
history: the store replays from its checkpoint, and the api refills its cache from the live events
that follow, inside one book refresh -- `measured` 508 ms a contract.

## Stream names

```
{event_type}:{VENUE}[:{UNDERLYING}]
md.option_quote:DELTA:BTC
```

`:` separates sections and `.` lives inside one, which is Redis's own convention. The event type is
the first section, dots included.

| Arity | Streams |
|---|---|
| Per venue and underlying | `md.option_quote`, `md.option_reference`, `md.index_quote`, `md.option_bar`, `computed.chain` |
| Per venue | `store.state`, `feed.connection`, `heartbeat`, `control.command` |
| Global | `alert` -- `adapter` is nullable and no reader wants a subset |

**No environment section.** There is one Redis on a laptop and one in prod, and they never share
one. **Numbered databases are not used and `SELECT` is never called**: Redis Cluster supports
database zero only, so a layout depending on database numbers cannot move to a managed Redis.

`computed.chain` keeps the expiry in the payload rather than the key, because expiries list and
settle daily and **a key that appears daily is a key a reader misses**. `md.option_bar` keeps its
`table` discriminator in the payload, because four names for one envelope would be four registries.

### Discovery is forbidden

**A reader builds its key list from configuration and never from the keyspace.** No `KEYS`, no
`SCAN`, no pattern. `XREAD` and `XREADGROUP` take an explicit list and have no wildcard, so a
stream discovered late is a stream that was silently not read -- which is exactly the failure that
once left contracts listed after start-up unsubscribed, damaging only the history.

## The envelope on the wire

One stream entry per event: `XADD <stream> * field value ...`. The envelope is **flat**, one Redis
field per key; the type's own keys are **one nested JSON object** in `payload`.

| Redis field | Present |
|---|---|
| `type`, `event_id`, `schema_version`, `source`, `ts_received`, `payload` | always |
| `ts_venue` | omitted when the venue gave none |
| `instrument` | omitted when the event is not about one contract |
| `venue_symbol` | omitted when the instrument carries none |

1. **Absent is omitted, never spelled.** No field carries `""`, `"None"`, `"null"` or `0` to mean
   absent. A Redis stream field is a binary string with no null in it, which is why absent values
   live inside `payload`, where JSON has one.
2. **`null` is not `0`**, and inside `payload` it is a JSON `null`.
3. **`payload` plus the envelope fields is the whole event.** Reassembling them and calling
   `parse_event` is the only decode there is, so `UnknownEventType`, `UnknownSchemaVersion`,
   `extra="forbid"` and the non-finite refusal all keep working over the wire with nothing new
   written to hold the line.
4. **Field order is as tabled**, because a stream whose entries repeat one field set in one order
   is the case Redis's listpack encoding compresses against.
5. **A `type` that disagrees with the stream it arrived on is an error**, not a preference.

## Consumer groups

| Thing | Rule |
|---|---|
| Group name | the service name, lower case, one word: `store`, `api` |
| Consumer name | `{service}-{instance}` |
| Creation | `XGROUP CREATE <stream> <group> <id> MKSTREAM` |
| Start id, unstated | `$`. `0` replays everything still held and has to be typed |
| Start id, `store` | its checkpoint id for that stream; on first start the head; **never `0`** |
| Start id, `api` | `$` -- it never replays |

**No environment and no venue in a group name**: the stream key already carries the venue.
**One group per service, never one per instance** -- that is what makes the store and the screen
independent readers of one stream rather than competitors for one message.

## Acknowledgement

| Rule | What it says |
|---|---|
| A1 | Every consumer acks a batch **on receipt**, before the work, never after it |
| A2 | The durability boundary is the **store flush**, not the ack |
| A3 | The store restarts from its **checkpoint**, never from the pending list and never from `0` |
| A4 | The api joins at `$` and never replays |
| A5 | A trimmed position is a **replay gap**: replay the retained suffix, report both bounds and an exact count, alert, and never refuse start-up. **Checked continuously**, on the ten-second `store.state` cadence |
| A6 | While any stream is behind, the seal clock is `min(wall clock, the time inside the last id of any stream still behind)` |
| A7 | A pass that fails **after** `XREADGROUP` returned leaves its batch in the pending list, and the next pass reads it back with `XREADGROUP ... 0` before it reads `>` |

**Acking on receipt is deliberate, and it is not the textbook pattern.** The textbook acks after the
work and recovers with `XAUTOCLAIM`. A per-message ack tells us nothing about what reached a file,
so the store records the id it last flushed and reads forward from there. A1 is also what makes A7
exact: acked on receipt, the pending list means *handed over and not delivered* and nothing else, so
reading it delivers each entry exactly once. Acked after delivery it would hold delivered entries
too, and recovery could not tell them apart.

**A5 protects a running store, not only a starting one.** A live store once lost **97 minutes** of
market data while its own `/health` reported `replay_gap_entries: 0` throughout, because its bus
reader had died and the gap check only ever ran at start-up. It now runs on the same loop that
publishes `store.state`.

**A6 is what makes a replay produce the bars a live run would have produced.** Without it the first
drain pass after any absence seals the whole backlog as late and discards what it just replayed.

## Trimming and persistence

| Rule | Value |
|---|---|
| Retention | **thirty minutes**, `assumed`, by age and never by count |
| Command | `XTRIM <stream> MINID ~ <now - 1800s>` |
| When | in the **same pipeline as that batch's `XADD`s**, on every batch write |
| `XACK` | frees no stream memory -- `measured`: 5,000 entries acked, `XLEN` still 5,000 |

**Trim by age and never by `MAXLEN`, because a count is a guess about rate**: it would hold hours in
a quiet market and four minutes in a loud one. The trim is free at our shape -- `measured` 31.8 us
an entry with it and 31.8 us without -- and a separate trim timer would be one more thing that can
stop.

```
redis-server --save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

| Flag | Why |
|---|---|
| `appendonly no` | The Parquet store is the archive |
| `save ""` | The stock image ships `save 3600 1 300 100 60 10000`, which at our rate forks every minute |
| no volume, no backup | A container with no volume leaves no file behind |
| `maxmemory 2gb` | 2x the `derived` memory at thirty minutes; `measured` 1,056.4 MiB for BTC+ETH |
| `maxmemory-policy noeviction` | **A data-loss decision, not a tuning one** |

**`noeviction` is load-bearing.** Under `allkeys-lru` Redis evicts whole keys, and one of our keys
is one stream: `md.option_quote:DELTA:BTC` would stop existing with nothing raised. At the ceiling
we want an error **at the publisher** instead. A managed cache must pin this in its parameter
group, because ElastiCache defaults to `volatile-lru`.

## Related guides

- [Events](events.md), [Architecture](architecture.md), [Configuration](configuration.md)
