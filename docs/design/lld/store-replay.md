# The store's replay and flush commit

This is the built crash protocol for `deltapayoff.store_main:app`, the process that owns
the `store` consumer and the four-table writer. The rulings are
[0010](../decisions/0010-store-replay.md); the schemas and walk below are the compact
implementation record from [0010a](../research/0010a-store-replay-protocol.md). Table
definitions, grace periods and partitions remain in [store.md](store.md).

## 1. The two files

Both files are at the **store root**, beside the four dataset directories. Neither is a
`.parquet`. Atomic writes are tmp -> `fsync` -> `os.replace`; stray root `*.tmp` files are
removed at start-up.

`<root>/_store-checkpoint.json`

```json
{ "format": "deltapayoff.store-checkpoint", "version": 1, "generation": 17,
  "written_at": "2026-09-12T10:05:00.123456+00:00", "group": "store", "recording": true,
  "streams": { "md.option_quote:DELTA:BTC": { "id": "1789163972987-4", "index": 9123456 } },
  "sealed_through_us": { "quote-bars": 1789163880000000, "reference-bars": 1789163880000000,
                         "spot-bars": 1789163880000000, "computed-bars": 1789163940000000 },
  "pauses": [ { "from": { "md.option_quote:DELTA:BTC": "…-0" }, "to": null } ] }
```

`streams` is per stream: `id` is the **exclusive** replay-from position — a stream read
from an id never returns that id — and `index` is its logical `entries-added` position. `sealed_through_us` is each aggregator watermark,
verbatim and not minute-rounded. `pauses` are per-stream spans; `to: null` means the store
was paused at the checkpoint. An absent file means first start. An unparseable or unknown
format/version refuses start-up and names the path.

`<root>/_store-flush-intent.json`

```json
{ "format": "deltapayoff.store-flush-intent", "version": 1, "generation": 18,
  "files": ["quote-bars/underlying=BTC/date=2026-09-12/20260912T100000Z-g00000018.parquet"] }
```

`files` contains relative POSIX paths for exactly the files in generation 18 that are
about to be published. `BarStore.scan()` globs `**/*.parquet`; compaction and migration
own only partition contents, so neither root file is data or a partition sidecar.

## 2. A flush is a five-step commit

The writer flushes at a drained point: the queue was emptied by `get_nowait`, so its
positions describe everything ingested so far.

1. Seal with the log clock; take replay-from positions, four watermarks, pause spans and
   buffered bars. Set `g` to committed generation plus one.
2. Plan `{earliest-minute}-g{g:08d}.parquet` per dataset/underlying/date and write intent.
3. For every path, write `<path>.flushing`, `fsync`, and `os.replace` onto the final name.
4. Write checkpoint generation `g`; this is the commit point.
5. Delete intent, then drop written bars and prune origins.

The exported `FLUSH_STAGES` seam covers each failure row below. A failure during steps two
through four is all-or-nothing: generation `g` files and intent are removed and bars stay
buffered. A caught failure increments `flush_errors`, logs at error and publishes `alert`
with `code="store.flush_failed"`.

| Flush stage | On disk | Recovery and result |
|---|---|---|
| `before-intent` (before step 2) | checkpoint `g-1` | nothing to undo; replay from `R_{g-1}` |
| `after-intent` (during intent write) | `...intent.json.tmp` | delete the tmp file; replay from `R_{g-1}` |
| `during-files` (during step 3) | intent `g`, some files and maybe `.flushing` | intent `g` > checkpoint `g-1`: delete every listed final and `.flushing` path; replay from `R_{g-1}` |
| `after-files` (after step 3, before checkpoint) | intent `g`, all files | intent `g` > checkpoint `g-1`: delete generation `g`; replay from `R_{g-1}` |
| `after-checkpoint` (before step 5) | intent `g`, files and checkpoint `g` | intent `g` <= checkpoint `g`: delete intent only; files stand; replay from `R_g` |
| `after-intent-delete` (after step 5) | clean | nothing to recover; replay from `R_g` |

Over-replay is safe: the restored watermark refuses an already flushed minute and counts
it in `already_flushed`, not `late`. A live-late event is refused as late again.

## 3. Why the protocol has three mechanisms

**Replay-from position.** `prev_positions` records the subscription positions at each
drained pass. `origins[minute_us]` is set when a minute is first folded, and `R` is the
element-wise per-stream minimum across all still-open minute origins. With no open minute,
`R` is the drained position. This saves a small boundary instead of serialising every open
bar, while ensuring replay includes every event that may still be in memory.

**Log clock.** `seal_now = min(wall_clock, id_seconds(position[s]) for s behind)`. A stream
is behind while replaying, after a full read batch, **and whenever its reader is not running
or has stopped completing passes** (#103) -- `behind` is derived, not a flag the read loop
caches, because a loop that dies freezes the flag at "caught up" and the clock then advances
over data nobody read, sealing those minutes empty. [bus-reader.md](bus-reader.md) §4 has the
derivation. The clock follows the log so replayed events are judged against the same time
live sealing used; once no stream is behind it is the wall clock again. The flush interval
still uses the wall clock.

> **History.** `_stream_id_seconds` unpacked three names from `value.split("-", 1)`, which
> yields two, so from #63 (`83120d6`) it raised `ValueError` on every stream id and
> `seal_clock`'s `except (TypeError, ValueError): continue` swallowed it: R4 never ran.
> #103 (`720036d`) changed it to `partition`, matching `redis_bus._id_parts`.
> `test_store_health.py::test_store_stream_id_seconds_parses_well_formed_ids` pins the fix.

**Pause spans.** Commands apply at a drained point, so a pause boundary is a position, not
an ambiguous wall-clock moment. The store checkpoints `recording: false` and the open span;
the reader drops events inside the span on their own stream and counts them. Resume closes
the span. A null end means through the dead process's replay target.

## 4. Start-up and gaps

1. Read the checkpoint. Absent means first start: create groups at the head (`$` resolved
   once), write generation zero once every reader is positioned, and log first start.
2. Recover intent and delete stray root tmp files.
3. Restore aggregator watermarks and subscribe losslessly with `start_ids` and pause spans;
   use drop-oldest `store-control` for `control.command`.
4. Use the checkpoint id for a named stream, otherwise the head, and run one `XINFO STREAM`
   per stream in one pipeline. Compute `trimmed = entries-added - length` and
   `lost = max(0, trimmed - index)`. `max-deleted-entry-id` is `measured` to remain `0-0`
   after `XTRIM`, so it is unusable.
5. If trimmed, replay the retained suffix and signal both bounds and the exact count. The
   rebase **keeps the saved id and moves only the index**. The id is a read cursor and the
   cursor is exclusive, so setting it to `first_retained_id` reads past the one entry that
   survived the trim at the boundary — the #85 defect. The saved id is already gone from
   the stream and a read from a trimmed id returns the whole retained suffix, so it is the
   correct cursor; the index moves onto `trimmed` so that the first entry delivered carries
   its true `entries-added` ordinal. If the named group is missing, signal the loss with
   `lost: null`. Neither condition refuses start-up. Publish `store.state` once, then run.

### 4.1 Where the replay stops, and why it has to

Replay is `XREAD` from the saved id. Live delivery is the group's `>` from its
`last-delivered-id`. `XREAD` takes a start and no end, so one batch can run past that id —
by up to `read_count` (500) entries per stream per restart. **The replay drops everything
past the group's `last-delivered-id`** and leaves it to the `>` read. That bound is what
makes the two halves meet exactly: `(start_id, last-delivered]` from the replay,
everything after `last-delivered` from the group.

Without the bound the overlap is delivered twice, which is #84. `measured` 2026-09-12, the
`test_a_restarted_reader_replays_from_the_id_it_last_flushed` scenario — ten distinct
entries across the seam, run with the bound removed and with it in place:

| Replay | Bus | Delivered | Distinct | Position index | `entries-added` |
|---|---|---|---|---|---|
| unbounded (#84) | fakeredis | 15 | 10 | 20 | 15 |
| unbounded (#84) | Docker Redis 7 | 14 | 10 | 19 | 15 |
| bounded (now) | fakeredis | 10 | 10 | 15 | 15 |
| bounded (now) | Docker Redis 7 | 10 | 10 | 15 | 15 |

The two buses disagree on the surplus because the group's `last-delivered-id` sits one
entry further along on real Redis; neither number is a property worth pinning, and the
tests assert that the surplus is empty rather than what it was. The store does not
deduplicate — `BarWriter.ingest` folds every tick and an unsealed minute accepts any tick —
so the surplus inflates `bid_ticks` on the restart minute and leaves the saved index past
the end of the stream, which under-reports the next restart's `lost`.

## 4.2 The gap check is continuous, and health can say no

Both are #103's and both live in [store-health.md](store-health.md): the replay-gap test runs
on a timer rather than only at start-up, because a store that never restarts never ran step 4;
and `GET /health` consults reader liveness and consumer lag instead of returning a hardcoded
`ok`.

## 5. Failure modes and the test seam

| What goes wrong | What happens |
|---|---|
| Unparseable checkpoint | `store_main.py` refuses start-up and names the checkpoint path |
| Checkpoint position was trimmed | the **whole** retained suffix replays, its first entry included; gap alert, error record and counter carry both bounds and exact `lost` |
| Store restarts while the feed publishes | the replay stops at the group's `last-delivered-id` and the `>` read takes it from there; each entry after the last flush is folded exactly once |
| Flush fails | generation is cleaned up, bars remain buffered, `flush_errors` increments, and the next interval retries |
| Group is missing for a checkpoint stream | replay continues with the retained suffix and reports `lost: null`; start-up is not refused |
| Two stores use one root | unsupported: both can corrupt Parquet and the checkpoint; one store per root is required |

The seam is `engine/tests/test_store_process.py`, including the flush-stage parametrisation
over `FLUSH_STAGES`. It drives the crash boundary, replay, gap arithmetic, restored
watermark and state publication without a network.
`test_kill_and_restart_replays_an_uncommitted_minute_once` publishes **between** the kill
and the restart, so the replay has something to over-read, and asserts the seam minute's
tick count rather than only key uniqueness — a doubled tick folds into the same bar and
leaves the key set untouched. `test_a_trimmed_replay_folds_the_first_retained_entry` seeds
a trim past the saved position and asserts the boundary entry is folded.

The bus half is parametrised over fakeredis and a real `redis:7-alpine`:
`test_a_restarted_reader_replays_from_the_id_it_last_flushed` drains the subscription to
empty rather than taking the count it expects, and
`test_a_replay_from_a_trimmed_id_delivers_the_whole_retained_suffix` pins the exclusive
cursor both ways — the saved id yields the suffix whole, a cursor on `first_retained_id`
is one entry short.

## 6. Numbers

| Figure | Tag | Basis |
|---|---|---|
| Table C grace: 2.0 s | `derived` | 1.45 x `measured` 1,156.8 ms maximum transit (`#61` run) = 1.68 s, rounded up |
| Recording-command ack timeout: 10.0 s | `assumed` | #64's 2.0 s bound plus one flush of at most 8 files |
| `STORE_STATE_STALE_SECONDS`: 25.0 s | `derived` | #64's feed-state bound: ten-second publish interval, two missed publishes and five seconds of slack |
| Replay over-read ceiling: 500 entries per stream per restart | `measured` | `read_count`, the `XREAD` `COUNT` a replay batch uses; it was the whole of the #84 surplus before the bound |
| That ceiling as time on the live feed: about 0.27 s | `derived` | 500 / `derived` 1,849.8 events/s ([research/0001](../research/0001-stream-naming-and-payload-format.md) §5, #58) |
| Seam surplus before the bound: 5 of 15 on fakeredis, 4 of 14 on Docker Redis 7 | `measured` 2026-09-12 | §4.1, the ten-entry restart scenario run both ways |
