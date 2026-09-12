# The Redis Streams bus — how the two policies survive a broker

**The seam does not move.** `events/bus.py` names `publish(event)` and
`subscribe(name, maxsize, lossless)`; `fanout.py` still fills it by default. `redis_bus.py` is
the second implementation behind the same methods, selected by `DELTA_BUS=redis`, landed by
#61 (I2). In split mode, `feed` is publisher-only; `store` owns its lossless subscription and
the engine owns `chain-stream` and `feed-state`. With `DELTA_BUS` unset — the default — FanOut
is the unchanged in-process monolith. The Redis subscription **subclasses** that class rather
than imitating it.

Names and encoding are [../cloud/nomenclature.md](../cloud/nomenclature.md), decided in #58.
The ack, trim and persistence policy is [../cloud/redis-hosting.md](../cloud/redis-hosting.md)
§4–§5, decided in #69. This document is only how the engine implements them.

---

## 1. Selecting it

| Variable | Default | What it does |
|---|---|---|
| `DELTA_BUS` | `fanout` | `redis` and nothing else selects Redis. **A typo is the default**, because a mistyped value must not silently pick a broker |
| `DELTA_REDIS_URL` | `redis://127.0.0.1:6379` | where |
| `DELTA_BUS_BATCH_MS` | `50` | the publisher's batch period. §7 |
| `DELTA_BUS_RETENTION_SECONDS` | `1800` | the trim floor's age |
| `DELTA_BUS_INSTANCE` | `1` | the `{instance}` half of `{service}-{instance}` |

`main.build_bus()` reads them at start-up, never at import, and logs `bus.selected` with the
URL, the interval and the retention. That line is the only moment anybody can
tell the two implementations apart from outside, which is why it exists: a process pointed
at the wrong Redis is otherwise a silent misconfiguration.

**The stream list comes from configuration and is never discovered.** `BusConfig.streams()`
is `stream_names(venues, underlyings)` — fifteen keys (`derived`) for one venue and two underlyings,
including venue-scoped `store.state:DELTA`, built from the same `--underlyings` set the feed is
given. `XREAD` has no
wildcard, so a stream found late is a stream that was silently not read: that is #51
restaged, and #51 cost three days of history.

Stream keys are `{event_type}:{VENUE}[:{UNDERLYING}]`; issue #74 removed the environment
section. Streams under old `dev:` and `prod:` names may still exist locally; they are not
migrated, because the pipe holds thirty minutes and they age out on their own.

## 2. Lossless — per-stream positions and a consumer group

The built signature is `subscribe(..., start_ids: Mapping[str, Position] | None,
group_start: "0" | "$" = "$", skip: Sequence[Span] = ())`. Each `Position` carries an id and
logical index. **The unstated `group_start` is `"$"`; `"0"` is deliberate** (#97). It resolves
once to a concrete id when a group is created, is never repeatedly passed as the literal `$`,
and positions a group only at creation (#86). `skip` contains pause spans that a replaying
reader drops and counts. **`behind` is derived per stream from the reader's own liveness**,
never a cached flag (#103): see [bus-reader.md](bus-reader.md) §4.

Lossless subscriptions create `XGROUP CREATE <stream> <name> <group_start> MKSTREAM`, read
`XREADGROUP`, and ack each batch before the work. The group name is the service name. The
durability boundary is the store flush, not the ack; `XACK` frees no stream memory (`measured`,
#69). Acking first also makes a pending list mean one thing: handed over, not delivered (#111).

`last_ids` is the last entry put on the queue per stream. A store checkpoint supplies the
per-stream `start_ids`, so replay covers the recorded suffix and then live `>` delivery.
`feed-state` still hydrates the API cache from `feed.connection` and `heartbeat`.

### 2.1 Gap check

At start-up, one `XINFO STREAM` per stream runs in one pipeline. For a saved position,
`trimmed = entries-added - length` and `lost = max(0, trimmed - index)`. The
`max-deleted-entry-id` is `measured` to remain `0-0` after `XTRIM`, so it is unusable.
A trimmed position replays the retained suffix, reports both bounds and the exact loss, and
never refuses start-up.

## 3. Drop-oldest — a reader outside every group, and it jumps

A reader that reads `>` in a group never drops. It accumulates a pending list and falls
further and further behind while reporting nothing, which is precisely wrong for a screen
where a four-second-old quote is worthless. So a drop-oldest subscription:

1. **positions at `$` once, as a concrete id.** Literal `$` is resolved by Redis per call,
   so a reader passing it every time would silently skip whatever arrived between two reads.
   Taken once it means "from now", and the `entries-added` counter read beside it is the
   baseline every later skip is measured from;
2. **offers everything it reads to the queue**, where `Subscription.offer` evicts the oldest
   and counts it — the same code, and therefore the same behaviour, as the fan-out;
3. **checks its lag when a read comes back full**, and when more than a queue's worth is
   still waiting, jumps to the newest `capacity` entries with `XREVRANGE ... COUNT` and adds
   the difference to `skipped`. Not to the very head: the head is where nothing is, and the
   last few quotes are exactly what the queue would have held.

The arithmetic is exact to the round trip, because `entries-added` and `last-generated-id`
come out of one `XINFO STREAM`: the stream added `added − baseline`, this reader took
`delivered` of them, and the rest it did not get — whether it jumped over them or **Redis
trimmed them away before it arrived**. That second case is the one nothing else would
notice. Redis trims silently; this does not. Each jump is a `bus.selected` record at
warning, naming the stream and the count.

## 4. The publisher — an outbox, a batch, and the trim riding with it

In split mode, `feed` is the publisher-only caller: `publish` encodes the event, resolves its
key and appends to an in-memory outbox. **It never
awaits.** It is called between two reads of the venue's socket, so an `await` in it would
put a network round trip inside the receive path, fill the OS receive buffer and get the
connection closed — the failure `fanout.py` exists to prevent. Nothing raises out of it
either: an event that cannot be keyed or encoded is logged, counted in `unroutable` and
dropped, because one lost event is a much smaller failure than a dead socket reader.

A flusher task writes one batch every `batch_ms` — **a period, not a pause**: the wait is the
interval minus what the last flush took, so 100 ms means a batch every 100 ms rather than
every 100 ms plus however long Redis and the event loop needed. When a flush takes longer
than the interval the next starts at once, and the interval has become a floor the process
cannot meet. §7 is where that stops being hypothetical.

Each batch is one pipeline: its `XADD`s, then one `XTRIM <key> MINID ~ <now − retention>` per
stream the batch touched. The trim rides with the write because `measured` (#69) it is free
at our shape — 31.8 µs an entry with it and 31.8 µs without — and because a separate trim
timer would be one more thing that can stop.

**A failed batch is put back at the head of the outbox and retried**, and the outbox is
bounded at `max_outbox` (200,000 entries, `derived` about 108 s at 1,849.8 events/s). Past
that the oldest go and are counted, for the same reason the fan-out counts a drop: an
unbounded buffer is a memory leak with good manners.

## 5. Starting, and failing to

`RedisBus.start()` dials, `PING`s inside the connect timeout, creates the groups, positions
the `$` readers and starts the flusher. It then **waits for every reader to be in position**
before returning. Both socket timeouts are bounded: an unbounded dial is not a failure but a
process that never reports one. **A started reader is supervised and survives a transient**: its retry policy, its death and its counters are [bus-reader.md](bus-reader.md).

When Redis does not answer, `start()` raises `BusUnavailable` naming the URL, the reason and
the variable to unset. `main.lifespan` does not catch it — unlike `DeltaUnavailable`, which
is deliberately survivable — so uvicorn prints it and the process exits within the connect
timeout. In split mode, `feed_main` fails before opening the venue. That is #57's user story 22:
a misconfiguration is visible at once rather than
buffering into an outbox nobody is draining.

## 6. What it counts

`stats()` keeps the fan-out's six per subscription — `offered`, `dropped`, `queued`,
`lossless`, `over_capacity`, `backlog_peak` — and adds broker counters `skipped` (§3),
`span_dropped`, `resyncs`, `undecodable` (an entry that would not decode, logged at error and
never fatal to the reader), and #111's `recovered`, `stranded` and `deferred`. Those three,
`readers()` and `consumer_lag()` are [bus-reader.md](bus-reader.md) §5 and §5a.
`publisher()` carries the write side: `published`, `written`, `batches`, `trims`, `failures`,
`unroutable`, `outbox`, `outbox_dropped`, and the flush timings §7 is read off.

## 7. Numbers

`measured` 2026-09-09, `tools/measure_bus_live.py` against 774 live BTC+ETH contracts: four
ten-minute phases in one continuous session, 3,330,235 events, publisher and consumer in
separate processes. The run, its caveats and the reasoning are
[../research/0061-batch-interval.md](../research/0061-batch-interval.md).

| Phase | Publisher CPU | Batches | Entries/batch | Achieved period | p50 | p99 |
|---|---|---|---|---|---|---|
| control — no bus | 40.77% of a core | — | — | — | — | — |
| 10 ms | 65.10% | 8,611 | 129.1 | 69.7 ms | 230.1 ms | 842.4 ms |
| **50 ms — chosen** | 70.73% | 6,233 | 177.1 | 96.4 ms | 195.2 ms | 847.8 ms |
| 100 ms | 70.33% | 4,239 | 262.5 | 141.7 ms | 235.3 ms | 830.6 ms |

**50 ms, and the reason is the period column rather than the latency one.** The three
latencies are indistinguishable and not even ordered by interval, because at this rate the
interval is not the dominant term — the feed's own decode work is. What orders cleanly is
that the process cannot flush faster than `derived` 69.7 ms whatever it is asked for, so
**10 ms is a number the system does not honour**; 100 ms costs a real 45 ms of period and
bought nothing back. 50 ms is the largest request still within about 1.4x of what the
process achieves, at the 177-entry batch shape #69 `measured` in isolation at 37 µs an entry.

**The bus costs the publisher `derived` 29.96 points of a core at the chosen 50 ms** (70.73%
against control's 40.77%; 24.33 at 10 ms, which is not the row to size from) for encoding, the pipeline and the trim over 1,850 events a second. Flush wall time
— `measured` 254–302 µs an entry against #69's isolated 31.8 µs — is the event loop, not
Redis: `pipe.execute()` yields and the socket decode runs inside that await.

**Memory: `measured` 1,107,735,240 B — 1,056.4 MiB — after thirty continuous minutes**,
`INFO memory` `used_memory`, 1,831,703 entries on the busiest of sixteen keys. #58 `derived`
1,051.5 MiB before any of it was built; the run came in 0.5% above. The container was started
at `--maxmemory 1gb` and raised to 1.5gb at minute 18, which is the finding rather than a
workaround: **1 GiB is not enough for thirty minutes of BTC+ETH and #69's 2gb is.**

Across the whole run: **0 dropped, 0 skipped, 0 undecodable, 0 failed batches, 0 unroutable
events**, deepest consumer backlog 1,668. **Start-up with no Redis: `measured` 3.70 s** from
launch to exit, of which 2.0 s is the connect timeout.

**A blocked reader on this host wakes late** — `measured` about 24 ms, against 1.1 ms for a
poll — so the measurement's consumer polled and says so. The default stays `BLOCK 500`
because prod is Linux beside the Redis; `DEFAULT_READ_BLOCK_MS` carries the same note.

## 8. What is not here

**The default remains in-process.** With `DELTA_BUS` unset, FanOut keeps the engine and feed
composition unchanged. With `DELTA_BUS=redis`, `feed` publishes, `store` owns its lossless
group, and the engine owns the screen subscriptions. Store replay from per-stream recorded
`Position`s is landed in I4 (#63): it replays the retained suffix, preserves the log clock,
and reports trimmed loss rather than refusing start-up. Engine I5 (#64) adds feed-state health.

**No `XAUTOCLAIM` and no dead-letter — but a pending list is read now** (#111). A lossless
pass that fails *after* `XREADGROUP` returned strands its batch where the fixed `>` cursor can
never reach it, so the next pass reads the consumer's own list with `XREADGROUP ... 0`:
[bus-reader.md](bus-reader.md) §5a. Acking stays on receipt, and §2 says what that buys.

**No Redis Cluster.** The key grammar would need a hashtag so one venue's streams landed in
one slot — an addition to the nomenclature, not a rewrite of this.
