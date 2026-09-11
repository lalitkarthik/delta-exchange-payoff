# 0010 — The store as its own process, replaying from its last flush

**Question** what a worker must be told, exactly, to build I4 (#63) with no judgment left to
it. Seven decisions were unsettled after the read-only planning pass; an eighth was found
while settling them. **Decision** [../decisions/0010-store-replay.md](../decisions/0010-store-replay.md).
**Protocol, schemas and the build list** [0010a-store-replay-protocol.md](0010a-store-replay-protocol.md).
**Evidence** the code as it stands at `01710dd` (#77 landed), #64 in flight in the `i5-feed-state`
worktree, `docs/design/cloud/redis-hosting.md` §4–§5, records
[0001](../decisions/0001-stream-naming-and-payload-format.md),
[0002](../decisions/0002-redis-hosting.md), [0006](../decisions/0006-stream-names-without-environment.md),
and one measurement run of my own against `fakeredis` 2.38.0.

---

## 1. What the code does today, before anything is decided

| Fact | Where | Tag |
|---|---|---|
| `RedisSubscription.last_ids` is one id **per stream**; `subscribe(start_id=…)` takes **one** id for all of them | `redis_bus.py` | `measured` (read) |
| A lossless group is created at id `0` on every configured stream, `MKSTREAM` | `_ensure_group` | `measured` |
| Replay is `XREAD` from `start_id` to each group's `last-delivered-id`, then `>` | `_replay` | `measured` |
| Every table seals on the **wall clock** handed to `seal(now)`; `late` is a policy | `bars._Watermarked` | `measured` |
| A flush file is `{earliest-minute}-{flushes:06d}.parquet`; `flushes` restarts at 0 each process | `BarStore.flush` | `measured` |
| A flush writes straight to the final name — **not atomic** — which is why compaction skips the open day | `BarStore.flush`, `compact` | `measured` |
| `computed-bars` is **sampled** from the chain cache every 10 s, at each minute edge, and from #44's minute pass | `BarWriter._sample_computed`, `sample_chains` | `measured` |
| **Nothing publishes `computed.chain`.** The type exists in the catalogue and in tests only | `grep` over `engine/` | `measured` |
| `/recording` reads a local `BarWriter`; no writer is a 503, deliberately | `main.get_bar_writer` | `measured` |
| `ControlCommand` addresses an adapter; `pause`/`resume`/`reconnect` | `events/catalogue.py` | `measured` |
| Bus transit, publish → consumer receipt, at the chosen 50 ms batch: p50 195.2 ms, p99 847.8 ms, **max 1,156.8 ms** | #61 run | `measured` |
| Store subscribes to four event types, so its stream set is 4 × underlyings = **8 keys**, not 14 | derived from `redis_wire.stream_names` | `derived` |

## 2. The eighth decision, which nobody had named

**A restarted store seals on its own wall clock, and a backlog is not "now".** The writer's
loop drains what is queued, then calls `seal(clock())`. After a three-minute absence the
first drain pass holds one `XREADGROUP` batch — `DEFAULT_READ_COUNT` 500 entries per stream,
`derived` ≈ 0.5 s of `md.option_quote` at 1,000/s and ≈ 4 s of `md.index_quote` at 118/s —
and the seal that follows it uses the real clock, whose boundary is `now − grace − 60 s`.
Every remaining minute of that backlog is then `late` and refused. Replay would deliver the
bytes and the store would throw them away, silently except for one counter.

The same applies to the `(start_id, last-delivered]` replay itself and to any catch-up after
a stall, so it is not a corner case: **it is the mechanism the whole ticket rests on.**
Settled in the decision record as the *log clock* — while a stream is behind, the seal clock
is the timestamp inside the last stream id taken from it, and lateness is then measured
against the same instant the live store measured it against.

## 3. The seven, in one line each — options weighed, choice, and what was rejected

**1. Watermark shape.** One scalar vs one per stream. Chosen: **per stream**, and each entry
carries an id *and* a logical index. A scalar is not merely imprecise: a stream that has been
quiet for an hour would drag a scalar `min` an hour back, into the trimmed region, and raise a
loss alarm every time. The index is what turns "a saved id may have been trimmed" into an
exact count (§4). Rejected: scalar-min (false alarms, replays 30 minutes), scalar-max (skips).

**2. Checkpoint location and format.** Chosen: `<root>/_store-checkpoint.json`, a root-level
file beside the four dataset directories, written tmp→`fsync`→`os.replace`. `BarStore.scan()`
globs `<root>/<dataset>/**/*.parquet`, `partitions()` reads `underlying=*/date=*`, compaction
and `tools/migrate_store.py` only ever walk inside a dataset — so nothing in the store can see
it, by construction and not by luck. Rejected: inside a partition (compaction's manifest
recovery and the migration tool both treat a partition's contents as theirs), a `.parquet`
sidecar (`scan()` would read it as data), Redis (the pipe is not an archive: 0002 §2).

**3. Exactly-once across a crash.** Two exact protocols exist. **A** — checkpoint the open
aggregator state, replay from the last drained id. **B** — checkpoint a *replay-from* position
that precedes every event still folded into an open bar, plus each aggregator's sealed
watermark; rebuild the open bars by replaying them. **B chosen**: it serialises four integers
and eight ids instead of every open bucket of ~774 contracts × 4 tables, it has no second
encoding of a bar to drift from the first, and a replayed minute is folded by the same code as
a live one, which is what the ticket actually asks for. Over-replay is harmless under B: an
event for a minute already on disk is refused by the restored watermark, and an event refused
as `late` live is refused again. The crash window between Parquet and checkpoint is closed by
an intent record — the compaction manifest's own pattern, in the same module.

**4. A saved id Redis has already trimmed.** Chosen: **replay the retained suffix, with an
explicit loss signal** — an `alert` on the bus, an error-level `store.replay_gap` record
naming the stream, both bounds and the exact number of lost entries, and a counter in the
store's state. Refusing to start turns a bounded hole into an unbounded one: the store stays
down, the pipe keeps trimming, and nothing is recorded while somebody is found. The house rule
is already written in `store.py`'s compaction note — "a gap is visible and recoverable; a
silent doubling is invention" — and this is the same trade one layer up. What makes replay
acceptable is only that the loss is *counted*, not inferred.

**5. `computed.chain` cannot rebuild `computed-bars`.** `ChainStrike` carries one set of
Greeks per strike; the table stores a call row and a put row with their own symbols, option
types and deltas. Chosen: **per-leg blocks on the strike** (`call`/`put`), plus the chain's own
computation stamp, and `schema_version` 2 for that type. The alternative — keep the strike
shape and have the store synthesise two legs — cannot work: delta, theta and rho differ
between the legs and the venue symbol is per leg. Because **nothing has ever published this
event**, there is no compatibility to preserve; the bump follows `events.md`'s own rule that a
field changing meaning bumps the version. Who publishes: the api, on the writer's own sampling
schedule, so the store folds exactly the samples the monolith folded.

**6. Split-mode `/recording`.** Chosen: the store publishes a `store.state` event and the api
answers from a cache of it, with age — #64's remote-state-cache pattern, reused rather than
re-invented, including its staleness bound and its "past the bound, stop pretending" rule.
Rejected: the api reading the checkpoint file (it is five minutes stale by design, and the two
counters are process memory), an HTTP call to the store (#57 forbids service-to-service HTTP),
and answering from the last `control.command` the api itself sent (a command is a request, not
a state).

**7. The store command on the bus.** #63 settles that the toggle "becomes a `control.command`
the store consumes", so the choice made is `ControlCommand` plus an optional
`target: "feed" | "store"` defaulting to `"feed"`, which keeps the wire and the grammar
unchanged. **It carries one hazard and it must be built out:** #64's `dispatch_command` offers
every `ControlCommand` to every controller, and a store `pause` addressed to `DELTA` would
pause the venue connection. The filter belongs in `dispatch_command`, the single place any
path reaches a controller, with a test that a recording pause never moves a controller. A
separate event type on its own stream would make that impossible rather than merely tested —
see the decision record's owner list.

## 4. Numbers this research produced

| Number | Tag | Basis |
|---|---|---|
| Store stream set: 8 keys (4 types × 2 underlyings) | `derived` | `redis_wire.stream_names` with #64's `event_types` filter |
| One `XREADGROUP` batch ≈ 0.5 s of quotes and ≈ 4 s of index frames | `derived` | `DEFAULT_READ_COUNT` 500 against `measured` 1,000/s and 118/s (#37, `measure_feed.py`) |
| Table C grace in the split store: **2.0 s** | `derived` | 1.45 × `measured` max transit 1,156.8 ms = 1.68 s, rounded up; 1.45 is the same factor table A/B/D's 8.0 s used |
| Tables A/B/D grace stays 8.0 s | `derived` | `measured` max arrival lag 5,511 ms + `measured` max transit 1,157 ms = 6.67 s, still inside 8.0 s (1.2× rather than 1.45×) |
| Minute-pass ladders at risk without a grace: everything slower than the 500 ms lead | `derived` | `MINUTE_PASS_LEAD_SECONDS` 0.5 s against `measured` p99 transit 847.8 ms |
| `fakeredis` 2.38.0: after `XTRIM MINID`, `max-deleted-entry-id` stays `0-0`; `entries-added` − `length` = entries trimmed | `measured` | my run, 5 entries added, 3 trimmed → `entries-added` 5, `length` 2, `max-deleted-entry-id` `0-0` |
| `XGROUP CREATE … <id> MKSTREAM` at an explicit id works on `fakeredis` | `measured` | same run |
| Ack timeout for a recording command: 10.0 s | `assumed` | #64's 2.0 s feed bound plus one flush of ≤ 8 files; flush duration is logged (`store.flush`, `duration_seconds`) and is to be read off the live run |

## 5. What I learned

**An acknowledgement, a watermark and a durability boundary are three different facts, and
only the third is about disk.** The ack says a message was handed to this process; the id in
`last_ids` says a message was put on a queue; only "this minute is in a file that has been
replaced into place" says anything survived. This store keeps all three apart, which is why it
can ack on receipt and still promise no loss — and why the id it records is neither of the
first two, but a position chosen so that everything before it is already on disk.

**Replaying a log is not the same as living through it, and the difference is the clock.**
Every aggregator here decides lateness against a clock it is handed. Live, that clock and the
data move together. In a replay the data arrives in seconds and the clock does not move at
all, so a wall clock throws away almost everything a replay delivers. Any system that stores
time-bucketed data and expects to catch up has to carry its clock in the log — here, inside
the stream ids Redis already assigns — and to know when it has caught up so the wall clock can
take over again. This is the single idea most likely to be wrong in a system that looks
finished.

**Idempotence is cheaper than transactions when the data is keyed.** There is no way to write
Parquet files and a checkpoint atomically. There is a way to make replaying too much harmless:
bucket on the event's own clock, refuse anything for a minute already written, and let the
replay re-derive the rest. Then the only thing the crash protocol has to guarantee is that the
saved position is never *ahead* of the disk — a much weaker promise than exactly-once
delivery, and one a single atomic file replace can keep.

**A distributed pause is a position, not a moment.** In one process, "stop recording now" has
an obvious meaning. Across a bus it has none, because two processes disagree about now. What
does survive a restart is "the last message folded before the pause", per stream — so the
pause is recorded as a pair of positions and a replay reproduces it exactly, while a wall-clock
timestamp would have reproduced something nobody observed.

**The most useful output of a planning pass is the decision it refuses to invent.** Six of
these seven were answerable from the code in an hour; the seventh (the seal clock) was not on
anyone's list and would have been discovered as a mysteriously empty three minutes after the
first live kill. A worker told to "replay from the last flushed id" would have built exactly
that and it would have looked right.
