# 0010a — The store's crash protocol, its files, and what a worker builds

Companion to [0010-store-replay.md](0010-store-replay.md). The rulings are
[../decisions/0010-store-replay.md](../decisions/0010-store-replay.md); this file is the part
a micro-ticket copies: exact files, exact fields, exact edge cases.

---

## 1. The two files, and their schemas

Both live at the **store root**, beside the four dataset directories, and neither is a
`.parquet`. Written tmp → `fsync` → `os.replace`; any `*.tmp` left at the root is deleted at
start-up.

`<root>/_store-checkpoint.json`

```json
{ "format": "deltapayoff.store-checkpoint", "version": 1, "generation": 17,
  "written_at": "2026-09-12T10:05:00.123456+00:00", "group": "store", "recording": true,
  "streams": { "md.option_quote:DELTA:BTC": { "id": "1789163972987-4", "index": 9123456 } },
  "sealed_through_us": { "quote-bars": 1789163880000000, "reference-bars": 1789163880000000,
                         "spot-bars": 1789163880000000, "computed-bars": 1789163940000000 },
  "pauses": [ { "from": { "md.option_quote:DELTA:BTC": "…-0" }, "to": null } ] }
```

* `streams` — one entry per stream of the store's lossless subscription. `id` is the
  **replay-from** position, exclusive. `index` is that entry's logical position in the stream
  (`entries-added` counting), which §4 turns into an exact loss count.
* `sealed_through_us` — each aggregator's `_sealed_through_us`, verbatim, in microseconds. Not
  minute-aligned and must not be rounded: it is `now − grace − 60 s` as the live seal left it.
* `pauses` — spans of recording pause, per stream, that lie after the replay-from position.
  `"to": null` means the store was still paused when this checkpoint was written.
* A missing file is the first ever start. A file that will not parse, or whose `format` or
  `version` is unknown, **refuses start-up** with the path in the message — never a guess.

`<root>/_store-flush-intent.json`

```json
{ "format": "deltapayoff.store-flush-intent", "version": 1, "generation": 18,
  "files": ["quote-bars/underlying=BTC/date=2026-09-12/20260912T100000Z-g00000018.parquet"] }
```

Relative POSIX paths, exactly the files generation 18 is about to publish.

## 2. Flush, as a commit

A store-mode flush runs from the writer's loop at a **drained point** — the queue emptied by
`get_nowait`, so `last_ids` is what has been ingested and nothing more.

1. Seal with the log clock (§3). Take `R` = the replay-from position (§3), the four
   `sealed_through_us`, the pause spans, and the bars each store has buffered. `g` =
   committed generation + 1.
2. Compute the file paths those bars will occupy, `{earliest-minute}-g{g:08d}.parquet` per
   `(dataset, underlying, date)`. Write the intent.
3. For each path: write `<path>.flushing`, `fsync`, `os.replace` onto the final name. The
   suffix is **not** `.tmp`: compaction's `_recover` deletes `*.tmp` in a partition it is
   recovering, and the store may be flushing yesterday's partition minutes after midnight.
4. Write the checkpoint at generation `g`. **This is the commit point.**
5. Delete the intent. Drop the written bars from the buffers and prune origins (§3).

A failure anywhere in 2–4 is all-or-nothing: delete this generation's files and the intent,
leave every bar in its buffer for the next interval, `flush_errors += 1`, log at error and
publish an `alert` (`code="store.flush_failed"`). Nothing is lost to a failed flush, which is
a change from the monolith, where the buffer was already empty by then.

**The failure points, walked**

| Crash at | On disk | Recovery | Result |
|---|---|---|---|
| before step 2 | checkpoint `g−1` | nothing to undo | replay from `R_{g−1}`; minutes ≤ `sealed_through_{g−1}` refused; the rest rebuilt |
| during the intent write | `…intent.json.tmp` | deleted at start-up | as above |
| during step 3 | intent `g`, some files, maybe one `.flushing` | intent `g` > checkpoint `g−1` → delete every listed file and its `.flushing` | as above; nothing written twice |
| after step 3, before the checkpoint | intent `g`, all files | same as above | as above |
| after the checkpoint, before step 5 | intent `g`, files, checkpoint `g` | intent `g` ≤ checkpoint `g` → delete the intent only | replay from `R_g`; the files stand |
| after step 5 | clean | none | replay from `R_g` |

**Why over-replay is safe.** An event re-delivered for a minute at or below a restored
watermark is refused and counted in a new `already_flushed` counter — not in `late`, which
stays a statement about live arrivals. An event that was refused as `late` live is refused
again, because the watermark only ever moved forward.

## 3. The three mechanisms the protocol needs

**The replay-from position `R`.** The writer keeps `prev_positions`, a copy of the
subscription's positions taken at the end of each drained pass, and `origins[minute_us]`, set
the first time an event for that minute is *folded* — set to `prev_positions`, so every event
of that minute has an id after it on its own stream. `R` = element-wise minimum, per stream,
over the origins of all minutes still open in any of the four aggregators; with nothing open,
`R` = the current drained positions. An origin is dropped once its minute is at or below every
aggregator's watermark.

**The log clock.** `seal_now = min(wall_clock, min(id_seconds(position[s]) for s behind))`,
where a stream is *behind* when it is still replaying, or when its last `XREADGROUP` returned a
full `read_count` of entries. No stream behind → the wall clock, which is the monolith's
behaviour unchanged. A stream that is behind constrains sealing for every table, which is
correct: the laggard is the one whose minutes are still arriving. `_maybe_flush`'s five-minute
interval stays on the wall clock.

**Pause spans.** A `control.command` is applied at the next drained point, so the boundary is a
position and not a moment. Pause: seal, flush, checkpoint with `recording: false` and a span
`{from: positions, to: null}`. Resume: checkpoint with the span closed at the drained
positions. During replay the **reader** drops entries inside a span on their own stream —
counted, never folded — and `to: null` means "through this stream's replay target", which is
exactly what the dead process had drained. Spans are pruned once `to` is at or below `R` on
every stream.

## 4. Start-up

1. Read the checkpoint. Absent → first ever start: groups are created at the head (`$` taken
   once as a concrete id), a generation-0 checkpoint is written as soon as every reader is in
   position, and an info record says so. Unparseable → refuse to start.
2. Recover the intent (§2's table). Delete stray root `*.tmp`.
3. Restore each aggregator's `_sealed_through_us` and `_restored_through_us`.
4. Subscribe: lossless `store` over `md.option_quote`, `md.option_reference`,
   `md.index_quote` and `computed.chain`, with `start_ids` and the pause spans; drop-oldest
   `store-control` over `control.command`.
5. Group creation id: the checkpoint's id for that stream; otherwise the head.
6. **The gap check**, per stream, from one `XINFO STREAM`: `trimmed = entries-added − length`;
   `lost = max(0, trimmed − index)`. `lost > 0` → replay what remains and signal: an `alert`
   (`severity="error"`, `code="store.replay_gap"`), an error record naming the stream, the
   saved id, the first retained id, both as times, and `lost`; set the replay base index to
   `trimmed` so counting stays true. A stream whose **group is missing while a checkpoint names
   it** is a Redis that lost its state: the same signal with `lost: null` and the bounds
   given as times.
7. Publish `store.state` once, then run.

`max-deleted-entry-id` is not usable for this: `measured`, it stays `0-0` after `XTRIM`.

## 5. What each decision asks a worker to build

| # | Build | Edge cases that must be tested |
|---|---|---|
| 1 | `subscribe(..., start_ids: Mapping[str, Position] \| None, group_start: "0" \| "$" = "0", skip: Sequence[Span] = ())`; positions carry id **and** index; `behind` per stream; `_replay` cursors per stream; `_deliver` drops span entries and counts them | a stream in the checkpoint but not in configuration (ignored, logged); a configured stream absent from the checkpoint (replays all retained); `start_id` (singular) is gone |
| 2 | `store.write_json_atomic`, `CHECKPOINT_NAME`, `INTENT_NAME`, `read_checkpoint`, `write_checkpoint` | unparseable file refuses start-up; stray `.tmp`; `scan()`, `partitions()` and `compact()` unaffected by both files |
| 3 | `BarStore.flush(tag=…)` + planned paths + `.flushing` publish; `_Watermarked.restore()` and `already_flushed`; origins and `prev_positions` in `BarWriter`; `seal_clock`; store-mode flush protocol; `compact_all` refuses to run while an intent exists | every row of §2's table, driven at a seam like `COMPACTION_STAGES`; a flush failure loses no bars; the monolith's file names and stop behaviour are byte-identical |
| 4 | the §4 gap check, the alert, the record, the counters | `lost` exactly right against a stream trimmed by a known amount; missing group with a checkpoint; nothing trimmed → no alert |
| 5 | `ChainLeg`, `ChainStrike{strike, call, put}`, `ComputedChain` v2 with `fetched_at`; `computed_chain_event()` (api) and `computed_ticks_from_event()` (store); the api's publisher on the writer's sampling schedule; table C grace 2.0 s in the store | `computed_ticks_from_event(computed_chain_event(c)) == computed_ticks_from_chain(c)` for any enriched chain, and again after a Redis round trip; a leg with no `computed` block; an unparseable `fetched_at`; a writer with `chains` set counts a `computed.chain` in `skipped` |
| 6 | `StoreState` event, the store's publisher, the api's `StoreStateCache`, `/recording` in split mode | never seen → 503; past the stale bound → 503 naming the age; `POST` with no acknowledgement → 504; already in the requested state → publish once, answer at once; the monolith's two routes unchanged apart from one nullable field |
| 7 | `ControlCommand.target`; the store's control consumer; **the filter in `FeedSupervisor.dispatch_command`** | a store `pause` moves no controller and produces no `feed.connection`; a feed command is unchanged in both modes; `reconnect` with `target="store"` is refused by the model |
