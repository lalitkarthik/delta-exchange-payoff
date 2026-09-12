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
3. **Amazon EC2**, one `c7g.xlarge` in `ap-south-1`, is the box it shares with them.
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
history: `store` replays from its checkpoint and `api` refills its cache from live frames in a
`measured` 508 ms. That is [0002](../decisions/0002-redis-hosting.md), and the rules it implies are
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

`control.command` is the one inbound type: a screen command enters the bus and the addressed service
reads it. Its `target` field says which service, and the feed supervisor drops anything not addressed
to it ([0010](../decisions/0010-store-replay.md) R8).

## 4. Acknowledgement, trimming and persistence

**These are the three policies that decide whether a restart loses data.** Read all three before you
change any one of them.

### 4.1 Acknowledgement

| Rule | What it says |
|---|---|
| A1 | Every consumer acks a batch **on receipt**, before the work, never after it. |
| A2 | The durability boundary is the **store flush**, not the ack. Five minutes of bars sit in memory until a Parquet file lands. |
| A3 | `store` restarts from its **checkpoint**, never from the pending list and never from `0`. |
| A4 | `api` joins at `$` and **never replays**. Its cache refills from live frames. |
| A5 | A **trimmed position** replays the retained suffix, reports both bounds and an exact count, and never refuses start-up. |

**A3 is what [0010](../decisions/0010-store-replay.md) changed, and it supersedes
[0002](../decisions/0002-redis-hosting.md).** The watermark is per stream and carries an id and a
logical index, so "how much did Redis trim" is an exact number. Starting at `0` instead would
re-record up to thirty minutes the old writer already wrote, as duplicates.

**Acking on receipt is deliberate, and it is not the textbook pattern.** The textbook acks after the
work and recovers from the pending list with `XAUTOCLAIM`. A per-message ack tells us nothing about
what reached a file, so the store records the id it last flushed and reads forward from there.

### 4.2 Trimming

| Rule | Value |
|---|---|
| T1 | Retention is **thirty minutes**, `assumed`, by age and never by count. |
| T2 | The command is `XTRIM <stream> MINID ~ <now − 1800s>`. |
| T3 | The trim rides **in the same pipeline as that batch's `XADD`s**, on every batch write. |
| T4 | `XACK` frees no stream memory; only `XTRIM` does (`measured`, #69). |

**Age, not `MAXLEN`, because a count is a guess about rate.** Thirty minutes is the promise made to a
restarting `store`. A count would hold hours in a quiet market and four minutes in a loud one.

**The trim is free at our shape.** `measured` 2026-09-09, `tools/measure_redis_hosting.py`, 100 ms
batches of 185 entries: 31.8 µs an entry with the trim and 31.8 µs without. A separate trim timer
would be one more thing that can stop.

### 4.3 Persistence

| Rule | Value | Why |
|---|---|---|
| P1 | `appendonly no` | the Parquet store is the archive |
| P2 | `save ""` | `redis:7-alpine` ships `save 3600 1 300 100 60 10000` (`measured`, 2026-09-09), which at our rate forks every minute |
| P3 | no volume, no backup | a container with no volume leaves no file behind |
| P4 | `maxmemory 2gb` | 2x the `derived` memory at thirty minutes |
| P5 | `maxmemory-policy noeviction` | at the ceiling Redis raises an error **at the publisher** |

**P5 is a data-loss decision and not a tuning one.** Under `allkeys-lru` Redis evicts whole keys, and
one of our keys is one stream: `md.option_quote:DELTA:BTC` would stop existing with nothing raised.
A managed node must pin this in its parameter group, because ElastiCache defaults to `volatile-lru`.

## 5. The numbers, and what nobody has measured

**Quote a number from this table, never from a sentence elsewhere.**

| Number | Tag | Run behind it |
|---|---|---|
| Retention thirty minutes; `maxmemory 2gb` | `assumed` | chosen in [0002](../decisions/0002-redis-hosting.md); see the judged tags in #72 |
| 1,849.8 events/s on the bus | `derived` | #58, from `measured` per-entry sizes and 1,693.6 frames/s |
| 1,835–1,854 events/s, 3,330,235 events | `measured` | `tools/measure_bus_live.py`, 2026-09-09, 774 live BTC+ETH contracts |
| 1,056.4 MiB after thirty continuous minutes | `measured` | same run, `INFO memory` `used_memory` 1,107,735,240 B |
| 1,051.5 MB at thirty minutes, forecast | `derived` | #58, before any of it was built; the run came in 0.5% above |
| Batch interval **50 ms**, achieved period 96.4 ms | `measured` / `derived` | same run; 10 ms is not honoured, 100 ms costs 45 ms of period |
| Publish to consumer receipt, p50 195.2 ms, p99 847.8 ms | `measured` | same run, 50 ms phase, ~55,000 samples |
| Publisher cost 29.96 points of a core at 50 ms | `derived` | same run, 70.73% against control's 40.77% |
| `XADD` pipelined at 100 ms: 31.8 µs an entry, 5.88% of a core | `measured` | `tools/measure_redis_hosting.py`, 2026-09-09, loopback, isolated |
| Outbox ceiling 200,000 entries, about 108 s of traffic | `assumed` / `derived` | `redis_bus.py`; the seconds are `derived` at 1,849.8 events/s |
| Dropped, skipped, undecodable, failed batches: 0 | `measured` | `tools/measure_bus_live.py`, whole run |
| Latency to a managed endpoint | **unmeasured** | no AWS account; [services.md](services.md) §5 |

**Every loopback figure passes through Docker Desktop's WSL2 port forward.** It is an upper bound for
a co-located container and a floor for anything with a network hop.

**Two figures in this table disagree with each other and both are `measured`.** 5.88% of a core is
the publisher alone, on loopback, at 100 ms. 29.96 points is the same publisher inside the feed
process at the chosen 50 ms. Size `feed` from the second ([0007](../decisions/0007-load-profile.md)),
and read the first as Redis's own cost.
