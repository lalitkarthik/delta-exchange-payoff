# 0010 — The store's watermark, its crash protocol, and the two events the split needs

**Status** decided, #63 (I4), 2026-09-12. **Supersedes in part**
[0002](0002-redis-hosting.md): its `store` group start id. **Extends**
[0001](0001-stream-naming-and-payload-format.md) and [0006](0006-stream-names-without-environment.md)
by one stream key, without touching the grammar. **Evidence and alternatives**
[../research/0010-store-replay.md](../research/0010-store-replay.md); protocol, schemas and the
build list [../research/0010a-store-replay-protocol.md](../research/0010a-store-replay-protocol.md).
**Changes** [../lld/store.md](../lld/store.md), [../lld/redis-bus.md](../lld/redis-bus.md),
[../events.md](../events.md), [../../recording-contract.md](../../recording-contract.md),
[../cloud/redis-hosting.md](../cloud/redis-hosting.md) §5, and #64's `dispatch_command`.

## The question

`store` becomes its own process and replays from its last flush. Seven decisions were left
open by the planning pass, and settling them found an eighth. This record answers all eight so
that no worker has to invent one.

## The decisions

**R1 — The watermark is per stream, and carries an index.** `{stream: {id, index}}` for each
of the store's eight streams. `index` is the entry's logical position (`entries-added`
counting), which makes "how much did Redis trim" an exact number rather than a suspicion.
`RedisBus.subscribe` takes `start_ids`, `group_start` and `skip`; the scalar `start_id` is
removed.

**R2 — The checkpoint is `<root>/_store-checkpoint.json`**, root-level, not a `.parquet`,
never inside a partition, written tmp → `fsync` → `os.replace`. Schema in 0010a §1. It records
the replay-from position per stream, each aggregator's `sealed_through_us` verbatim, the
recording flag and any open pause span. Unparseable refuses start-up; absent is the first ever
start.

**R3 — Exactly-once is a replay-from position plus a restored watermark, committed by an
intent record.** The saved position precedes every event still folded into an open bar; every
minute already on disk is refused on replay by the restored watermark; the crash window
between the Parquet files and the checkpoint is closed by `<root>/_store-flush-intent.json`,
written before the files and deleted after the checkpoint — the compaction manifest's own
pattern. Flush files gain the generation in their name, so no two processes can collide on
one. A failed store-mode flush keeps its bars. 0010a §2 walks every failure point.

**R3a — The pending list a restart inherits belongs to the replay, not to the reader** (#111).
A lossless pass that fails after `XREADGROUP` returned strands its batch in the consumer group's
pending list, and the reader now reads that list back before it reads `>`. At a restart the two
overlap: every pending entry has an id at or below the group's `last-delivered-id`, so for a
stream `store` holds a watermark for it lies inside `(watermark, last-delivered]` — exactly what
R3's replay re-reads from the raw stream. **So `store` acks its inherited pending list and does
not deliver it**, counted as `deferred`; delivering it as well would fold it twice and R3 would
no longer be exactly-once. A subscription with no watermark has no replay, and its inherited
list is delivered instead. `message-bus.md` A3 is unchanged: `store` still restarts from its
checkpoint and never from the pending list.

**R4 — The seal clock follows the log while a reader is behind.** `min(wall clock, the time
inside the last stream id of any stream still behind)`. With nothing behind it is the wall
clock, unchanged. Without this the first drain pass after any absence seals the whole backlog
as late and replay delivers bytes the store throws away.

**R5 — A trimmed position replays the retained suffix and says what was lost.** An `alert`, an
error-level `store.replay_gap` record with both bounds and an exact count, and a counter in
the store's state. Start-up is never refused for this.
**When: continuously, on the `store.state` cadence, and at start-up as well — never at
start-up alone.** The condition becomes true the moment retention passes the watermark, not
when a process restarts. Counted every poll, alerted once per episode. This sentence is
#106's: the silence where it stands is what [../cloud/message-bus.md](../cloud/message-bus.md)
A5 read as "at start-up only", and R5a below is the amendment (#103) that settled it.
**"Exact count" is qualified.** The count is exact whenever Redis can answer. When the stream
is absent or its group is gone the count and the retained bound are `null`, because what was
in it is unknowable — and `null` is not `0` ([../../../CONTEXT.md](../../../CONTEXT.md) §7).
A record for that case must read *stream absent or empty*, never *lost None entries*.

**R5a (#103) — and the check is continuous, not a start-up step.** A store that never restarts
never ran R5, so a 97-minute hole reported `replay_gap_entries: 0` for two hours. The test is
exact and needs no threshold: the group's `last-delivered-id` older than the stream's oldest
surviving entry means data has been lost. It is two `XINFO` calls, and it runs on the
`store.state` cadence. See [store-replay.md](../lld/store-replay.md) §4.2.

**R4a (#103) — "behind" is derived from the reader, not cached by it.** R4's rule was right and
its input was a flag the read loop wrote. A loop that died froze it at "caught up" and the
clock advanced over unread data. See [bus-reader.md](../lld/bus-reader.md) §4 -- and the note
in [store-replay.md](../lld/store-replay.md) §3, because R4 has never actually run.

**R4b (#113) — R4a's closing clause was wrong the moment it was written.** `720036d`, the
#103 commit that wrote R4a, is the same commit that changed `_stream_id_seconds` from
`split` to `partition` -- the one call `behind`'s pin still depended on. R4 has run since
that commit. See [store-replay.md](../lld/store-replay.md) §3 and
`test_store_health.py::test_store_stream_id_seconds_parses_well_formed_ids`.

**R6 — `computed.chain` gains per-leg blocks and a computation stamp, at `schema_version` 2.**
`ChainStrike` becomes `{strike, call, put}` with `ChainLeg` carrying the venue symbol, `iv`,
`iv_leg`, `iv_reason` and the five Greeks; `ComputedChain` gains `fetched_at` and makes
`forward`, `discount`, `years_to_expiry` and `forward_method` nullable. **The api publishes it,
on the writer's own sampling schedule** — every ten seconds, at each minute edge, and from
#44's minute pass — so the store folds exactly the samples the monolith folded. Table C's
grace in the split store is **2.0 s**, `derived` from the measured maximum bus transit; the
monolith keeps 0.0 s and keeps sampling its own cache.

**R7 — Split-mode `/recording` is served from a cached `store.state` event.** The store
publishes it every ten seconds and on every change; the api caches the newest with its age,
exactly as #64 caches feed state. Fresh → 200 with the three fields and a nullable
`state_age_seconds`. Never seen, or older than `STORE_STATE_STALE_SECONDS` → 503 naming the
age. `POST` publishes one command and waits for a later `store.state` carrying the requested
value; no acknowledgement → 504.

**R8 — The store command is a `ControlCommand` with `target: "feed" | "store"`, defaulting to
`"feed"`.** The wire, the stream and the grammar are unchanged, and `schema_version` stays 1.
`FeedSupervisor.dispatch_command` must drop anything not targeted at the feed — without it a
recording pause pauses the venue connection.

## Why, in the criteria's order

**1. Invariants.** Every ruling is chosen so a failure is loud rather than invisible. R3
guarantees a minute is never sealed twice and never lost while Redis still holds it; R4 keeps
"replayed exactly as live" true instead of nominal; R5 converts an unavoidable hole into a
counted one; R2 keeps the checkpoint where no other code in the store can touch it. The one
place this record accepts a runtime check instead of a structural guarantee is R8, and it says
so.

**2. Operations burden.** Two small JSON files at the store root, both readable by a person
during an incident and both safe to delete (deleting them costs a gap, never a duplicate). No
new service, no database, no lock file. The store's own state reaches the screen through the
bus that already exists.

**3. Cost.** One extra `store.state` key and about one event every ten seconds; `derived`
negligible against the `derived` 1,849.8 events/s already on the bus. `computed.chain`
published on the sampling schedule rather than on every recompute is `derived` well under
1 KiB/s. One `fsync` per Parquet file per five minutes.

**4. Path to OMS, NSE, more consumers.** The watermark, the intent and the log clock belong to
any lossless consumer of this bus, not to this store — a second durable consumer builds on the
same three. `target` on `ControlCommand` gives the next controllable service a place to stand.

**5. Latency.** Unchanged on every live path. The log clock only ever *slows* sealing, and
only while a reader is behind.

## Rejected, and why

| Option | Why not |
|---|---|
| One scalar watermark | A stream quiet for an hour drags the minimum into the trimmed region and raises a false loss alarm every start-up; the maximum skips entries on slower streams |
| Checkpoint inside a partition, or as a `.parquet` | `scan()` globs `**/*.parquet`; compaction and `tools/migrate_store.py` treat a partition's contents as theirs |
| Checkpoint the open aggregator state | Exact, but it is a second encoding of a bar that can drift from the first, and it serialises thousands of open buckets every five minutes to avoid replaying ninety seconds of a pipe that already holds thirty minutes |
| Deterministic, idempotent flush file names instead of an intent record | The set of bars in a flush is not reproducible after a restart — replay seals in different batches — so "the same name" would not hold the same rows |
| Refuse start-up on a trimmed watermark | Turns a bounded hole into an unbounded one: the store stays down, the pipe keeps trimming, nothing is recorded until a person intervenes |
| Keep `ChainStrike`'s single Greek set and have the store synthesise the two legs | Delta, theta and rho differ between call and put, and the venue symbol is per leg. The store would invent the difference |
| Publish `computed.chain` on every recompute | `derived` roughly 10 events/s per watched pair against 6 samples a minute, for rows no table would keep |
| The api reads the checkpoint file for `/recording` | Five minutes stale by design, and the two counters are process memory |
| HTTP between api and store | #57: no service calls another over HTTP |
| A separate `store.command` event type | Better by construction — no consumer could ever misroute it — but #63 settles that the toggle becomes a `control.command`. Named in the owner list below rather than decided here |

## What the repository owner should decide, not this record

1. **Acceptance criterion 1 for table C.** Tables A, B and D can be compared column for column
   against the in-process recording. Table C cannot: two processes recompute on their own
   timing, so the values differ below the second even when the minute keys match. Say what the
   proof is — key sets equal, or a single-process comparison against the event stream.
2. **The read paths' right edge in split mode.** With no writer in the api, `/smile`,
   `/history` and contract bars lose `BarStore.pending()` and read only what is on disk: up to
   one flush interval plus the open minute behind. Accept it, or open a ticket to feed the api
   the `md.option_bar` events the catalogue already defines.
3. **R8, once more.** A separate event type removes the hazard structurally; the settled
   wording says `control.command`. If the wording is reopened, everything else in this record
   stands unchanged.
4. **The store's graceful stop no longer writes partial open-minute bars** — it checkpoints
   them instead, so a restart within the retention window completes the minute rather than
   sealing a truncated one. A stop longer than thirty minutes loses those minutes, and the gap
   signal reports it. This changes a rule stated in `lld/store.md` §4 for the store process.
5. **The cutover from I3 to I4.** The first start has no checkpoint and begins at the head, so
   the seconds between stopping the old writer and starting `store` are not recorded. The
   alternative — starting at `0` per 0002 — would re-record up to thirty minutes the engine had
   already written, as duplicates.

## The owner's answers, 2026-09-12

1. **Table C is compared numerically, within a stated tolerance** — not by key sets alone.
   The tolerance is justified by what actually differs, two processes recomputing against a
   moving book, and is measured rather than chosen to pass. #63 carries it as a named
   constant.
2. **The read paths' right edge is not accepted as lost.** [#81](https://github.com/lalitkarthik/delta-exchange-payoff/issues/81)
   gives `/smile`, `/history` and the contract-bar routes their live edge back by folding
   `md.option_bar` in the api. #63 does not fix it, and the store LLD states the lag as
   current behaviour until #81 lands.
3. **R8 stands**: `control.command` with `target`, and the feed supervisor drops what is not
   addressed to it.
4. **R4's graceful stop stands**: the store checkpoints partial open-minute bars rather than
   writing a truncated minute.
5. **The I3 → I4 cutover's few unrecorded seconds are accepted**; re-recording thirty
   minutes as duplicates is refused.

## What would change this decision

- **A consumer that needs per-message delivery guarantees.** That is a change to
  `redis-hosting.md` §5's acknowledgement policy before it is a change here.
- **A flush interval short enough that the open minute is the whole buffer.** R3's replay-from
  position would then be within noise of the drained position and the origin tracking could go.
- **Two stores writing one root.** Nothing here defends against it; `lld/store.md` §4 already
  forbids it, and the checkpoint would be the second thing they corrupt.
