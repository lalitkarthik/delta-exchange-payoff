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

`streams` is per stream: `id` is the exclusive replay-from position and `index` is its
logical `entries-added` position. `sealed_through_us` is each aggregator watermark,
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
is behind while replaying or after a full read batch. The clock follows the log so replayed
events are judged against the same time live sealing used; once no stream is behind it is
the wall clock again. The flush interval still uses the wall clock.

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
5. If trimmed, replay the retained suffix and signal both bounds and the exact count. If
   the named group is missing, signal the loss with `lost: null`. Neither condition refuses
   start-up. Publish `store.state` once, then run.

## 5. Failure modes and the test seam

| What goes wrong | What happens |
|---|---|
| Unparseable checkpoint | `store_main.py` refuses start-up and names the checkpoint path |
| Checkpoint position was trimmed | retained suffix replays; gap alert, error record and counter carry both bounds and exact `lost` |
| Flush fails | generation is cleaned up, bars remain buffered, `flush_errors` increments, and the next interval retries |
| Group is missing for a checkpoint stream | replay continues with the retained suffix and reports `lost: null`; start-up is not refused |
| Two stores use one root | unsupported: both can corrupt Parquet and the checkpoint; one store per root is required |

The seam is `engine/tests/test_store_process.py`, including the flush-stage parametrisation
over `FLUSH_STAGES`. It drives the crash boundary, replay, gap arithmetic, restored
watermark and state publication without a network.

## 6. Numbers

| Figure | Tag | Basis |
|---|---|---|
| Table C grace: 2.0 s | `derived` | 1.45 x `measured` 1,156.8 ms maximum transit (`#61` run) = 1.68 s, rounded up |
| Recording-command ack timeout: 10.0 s | `assumed` | #64's 2.0 s bound plus one flush of at most 8 files |
| `STORE_STATE_STALE_SECONDS`: 25.0 s | `derived` | #64's feed-state bound: ten-second publish interval, two missed publishes and five seconds of slack |
