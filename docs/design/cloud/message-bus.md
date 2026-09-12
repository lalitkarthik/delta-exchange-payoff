# The message bus

**Read this to learn what the bus is, where it runs, what travels on it, and what it is allowed to
keep.** It is assembled from the closed research and its decision records, and it links to each one
where it uses it. Every term resolves to [../../../CONTEXT.md](../../../CONTEXT.md). The engine that
feeds it is [data-feed-engine.md](data-feed-engine.md).

Nothing in `prod` is built. The bus itself runs today, in the local stack and in the split engine.

---

## 1. The AWS services the bus uses

**Check [services.md](services.md) before you name a service here.** In five lines:

1. **No AWS message service carries the bus.** Redis Streams does, in a container.
2. **Amazon ECS on EC2** runs that container beside `feed`, `store` and `api`.
3. **Amazon EC2**, one `c7g.xlarge` in `ap-south-1`, is the instance it shares with them
   ([0008](../decisions/0008-topology.md)).
4. **`host` network mode** keeps the publisher on `127.0.0.1`, with no hop to Redis.
5. **ElastiCache for Valkey is the named fallback**, `derived` $47.30 a month, one endpoint string.

The full list, the fallbacks and the bill are [services.md](services.md).

## 2. Where Redis runs

**Start Redis with this command, in both environments.** It is the whole hosting policy in one line:

```
redis-server --save "" --appendonly no --maxmemory 2gb --maxmemory-policy noeviction
```

| | `dev` | `prod` |
|---|---|---|
| Where | Compose on a laptop | the same image in the same task as `feed`, `store` and `api` |
| Reachable from | the Compose network | the instance's own loopback; never a public address |
| Ceiling | whatever the laptop has | `maxmemory 2gb` |

**A container, not a managed cache, because the bus is a pipe.** Losing it costs a restart and not
history: `store` replays from its checkpoint, and `api` refills its cache from the live events that
follow, inside one book refresh — `measured` 508 ms a contract (`tools/measure_feed.py`,
2026-09-03). That is [0002](../decisions/0002-redis-hosting.md), and the rules it implies are
[redis-hosting.md](redis-hosting.md).

## 3. The names and the format on the wire

**Build a key from this grammar, and never from the keyspace.** In five lines:

1. **A stream is named `{event_type}:{VENUE}[:{UNDERLYING}]`** — `md.option_quote:DELTA:BTC`.
2. **No environment section**, because there is one Redis on a laptop and one in prod
   ([0006](../decisions/0006-stream-names-without-environment.md)).
3. **One entry per event**, with the envelope flat across Redis fields and the type's own keys as
   one JSON object in `payload` ([0001](../decisions/0001-stream-naming-and-payload-format.md)).
4. **Absent is omitted, never spelled**, and inside `payload` a missing number is JSON `null`.
5. **A reader builds its key list from configuration.** No `KEYS`, no `SCAN`, no pattern.

The grammar, the per-event stream list, the field table and the consumer-group rules are
[nomenclature.md](nomenclature.md). What each event means is [../events.md](../events.md), which
wins on names and directions.

**Inputs, events and outputs, in one table.** Every row is an event; the direction says which way it
crosses the bus.

| Direction | Stream | Written by | Read by |
|---|---|---|---|
| input to the bus | `md.option_quote`, `md.option_reference`, `md.index_quote` | `feed` | `store`, `api` |
| input to the bus | `feed.connection`, `heartbeat` | `feed` | `api` |
| input to the bus | `computed.chain` | `api` | `store` |
| input to the bus | `md.option_bar` | `store` | `api` |
| input to the bus | `store.state` | `store` | `api` |
| input to the bus | `alert` | any service | the Discord alert consumer |
| output of the bus | `control.command` | `api` | `feed`, `store` |

`control.command` is the one inbound type: `api` publishes one `command` from the screen, and the
addressed service reads it. Its `target` field names that service, and the feed supervisor drops
anything not addressed to it ([0010](../decisions/0010-store-replay.md) R8).

## 4. Acknowledgement, trimming and persistence

**These are the three policies that decide whether a restart loses data.** Read all three before you
change any one of them.

### 4.1 Acknowledgement

**Read A5 and A6 before you trust a `store` that has been running for hours.** The five rules
above them describe a restart; those two describe what the code does and does not watch.

| Rule | What it says |
|---|---|
| A1 | Every consumer acks a batch **on receipt**, before the work, never after it. |
| A2 | The durability boundary is the **store flush**, not the ack. `FLUSH_SECONDS` is 300 s (`assumed`, `deltapayoff.store`), so up to five minutes of bars sit in memory until a Parquet file lands. |
| A3 | `store` restarts from its **checkpoint**, never from the pending list and never from `0`. |
| A4 | `api` joins at `$` and **never replays**. Its cache refills from the events that arrive next. |
| A5 | A **trimmed position** is a **replay gap**: `store` replays the retained suffix, reports both bounds and an exact count, alerts, and never refuses start-up ([0010](../decisions/0010-store-replay.md) R5). **The check is continuous, on the `store.state` cadence, not a start-up step** ([0010](../decisions/0010-store-replay.md) R5a, #103). |
| A6 | While any stream is behind, the **seal clock** is `min(wall clock, the time inside the last stream id of any stream still behind)` ([0010](../decisions/0010-store-replay.md) R4). |

**A3 is what [0010](../decisions/0010-store-replay.md) changed, and it supersedes
[0002](../decisions/0002-redis-hosting.md).** The watermark is per stream and carries an id and a
logical index, so "how much did Redis trim" is an exact number. Starting at `0` instead would
re-record up to thirty minutes the old writer already wrote, as duplicates.

**A6 is what makes a replay produce the bars a live run would have produced.** Without it the
first drain pass after any absence seals the whole backlog as late, and `store` throws away the
bytes it just replayed. With no stream behind, the seal clock is the wall clock.

**A5 protects a running `store`, not only a starting one — corrected here by #105, verified
against `store_main.py`.** Before #103, `store_main.py` counted the gap once, inside
`_prepare_process`, against the checkpoint it had just read, and never again: a `store` that
kept running and stopped reading raised nothing.
[#103](https://github.com/lalitkarthik/delta-exchange-payoff/issues/103) records a live store
that lost **97 minutes** of market data while its own `/health` reported
`replay_gap_entries: 0` throughout, because its bus reader had died and no restart ever ran the
check. `store_main.py`'s `poll_bus` now asks Redis where the store's consumer group stands
every `STORE_BUS_MONITOR_INTERVAL_SECONDS` — the same ten-second cadence `store.state` already
publishes on, one loop instead of two — and a trimmed position raises the alert whether or not
anything has restarted since.

**Acking on receipt is deliberate, and it is not the textbook pattern.** The textbook acks after the
work and recovers from the pending list with `XAUTOCLAIM`. A per-message ack tells us nothing about
what reached a file, so the store records the id it last flushed and reads forward from there.

### 4.2 Trimming

**Set the retention window before you size Redis: the two are one decision.**

| Rule | Value |
|---|---|
| T1 | Retention is **thirty minutes**, `assumed`, by age and never by count. |
| T2 | The command is `XTRIM <stream> MINID ~ <now − 1800s>`. |
| T3 | The trim rides **in the same pipeline as that batch's `XADD`s**, on every batch write. |
| T4 | `XACK` frees no stream memory; only `XTRIM` does — `measured`, `tools/measure_redis_hosting.py`, 2026-09-09 (#69): 5,000 entries acked, `XLEN` still 5,000, `MEMORY USAGE` unchanged at 1.81 MB. |

**Trim by age and never by `MAXLEN`, because a count is a guess about rate.** Thirty minutes is
what the bus promises a restarting `store`. A count would hold hours in a quiet market and four
minutes in a loud one.

**The trim is free at our shape.** `measured` 2026-09-09, `tools/measure_redis_hosting.py`, 100 ms
batches of 185 entries: 31.8 µs an entry with the trim and 31.8 µs without. A separate trim timer
would be one more thing that can stop.

### 4.3 Persistence

**Start Redis with all five of these, or the bus buys a disk and a fork nobody asked for.**

| Rule | Value | Why |
|---|---|---|
| P1 | `appendonly no` | the Parquet store is the archive |
| P2 | `save ""` | `redis:7-alpine` ships `save 3600 1 300 100 60 10000` (`measured`, 2026-09-09), which at our rate forks every minute |
| P3 | no volume, no backup | a container with no volume leaves no file behind |
| P4 | `maxmemory 2gb` | 2x the `derived` memory at thirty minutes |
| P5 | `maxmemory-policy noeviction` | at the ceiling Redis raises an error **at the publisher** |

**P5 is a data-loss decision and not a tuning one.** Under `allkeys-lru` Redis evicts whole keys, and
one of our keys is one stream: `md.option_quote:DELTA:BTC` would stop existing with nothing raised.
A managed cache must pin this in its parameter group, because ElastiCache defaults to `volatile-lru`.

## 5. The numbers, and what nobody has measured

**Moved to [message-bus-numbers.md](message-bus-numbers.md)** by #72, this file being close to the
200-line bound: every figure with its tag and its run, the two publisher measurements side by side,
and the one line nobody has measured. **Quote it from there, never from a sentence here.**
