# Logging

The engine writes **one structured JSON object per line**, per operational record. A day's file can
be filtered by `event`, `venue`, `instrument` or `conn_state`, which is what makes a hole in the
Parquet store visible instead of silently lost.

## The record

Five fields are always present.

| Field | Meaning |
|---|---|
| `ts` | UTC, ISO 8601, millisecond precision |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `logger` | The Python logger name |
| `event` | A short stable name from the catalogue below |
| `msg` | The human sentence. **Not machine-matched** |

Optional fields are `venue`, `instrument`, `conn_state` and `event_id`. Anything else passed as
`extra` lands beside them under its own name -- `table`, `rows`, `file` and `duration_seconds` on a
`store.flush`. **A `None` value is dropped rather than sent as `null`**, for the same reason the
wire omits an absent field rather than spelling it.

```json
{"ts":"2026-09-14T09:08:35.798Z","level":"INFO","logger":"deltapayoff.store",
 "event":"store.flush","msg":"wrote quote bars","table":"quote-bars","rows":4821,
 "file":"20260914T090800Z-000287.parquet","duration_seconds":0.214}
```

## One function, and it refuses an unknown name

```python
log_event(logger, level, event, msg, **extra)
```

Every call site uses it, and **it raises if `event` is not in `log_events.ALL`**. A name invented at
a call site would be a name no filter and no runbook knows, discovered during the incident it was
written for. A raw logger call still produces a parseable line, with `event: "log"`.

`event_id` is the join key. A record carrying the `event_id` of the bus event that caused it can be
lined up against that event without a timestamp guess.

## The sinks

**A daily file.** One `DailyFileHandler` per process writes `<repo root>/logs/<YYYY-MM-DD>.log`,
one JSON line per record, **checking the date on every `emit()` rather than with a timer** -- a
timer is another thing that can stop, and the check costs a comparison.

**Standard error, always.** A second handler always writes to `sys.stderr`; `is_terminal` decides
only its **formatter** -- colour at a terminal, JSON otherwise -- so redirected stderr contains no
ANSI escapes and a container is never silent.

**The gate is on the formatter and never on the handler.** In a container `/proc/1/fd/2` is a pipe,
so gating the handler on `isatty()` detaches it entirely and the only diagnostics the system has go
to a file nobody mounted. Both halves are held: stderr always carries records, and Compose mounts
`./.stack-logs/<service>` over `/app/logs` -- one directory per service, so writers cannot collide
on one day's file.

Both handlers attach to `logging.getLogger("deltapayoff")`, **not root**, and `propagate` stays true
so pytest's `caplog` captures through the root handler. Configuration is idempotent, so repeated
imports do not multiply the handlers or the volume.

## Levels

| Record | Level | Why |
|---|---|---|
| An ordinary transition | Info | The baseline |
| `stopped` by a spent budget | Error | No other symptom exists |
| Entering `degraded` | Warning | The staleness question this project exists to answer |
| Touching `reconnecting` | Warning | Worth attention whether or not it recovers |
| Contracts newly subscribed | Info | Rare; the record of what is being recorded |
| A flush | Info | Routine, and useful to see the cadence |
| A replay gap | Error | A saved position needs entries the bus has trimmed |
| A committed checkpoint | Info | The generation's files are durable |
| A lossless drop | Error | Should be impossible |
| The recompute set changing | Debug | Rare; for someone reading closely |
| A websocket client attaching or detaching | Debug | Volume matters more than one connection |
| An alert | Warning | Always, regardless of the alert's own severity |
| A defensive catch-all | Error | Never an expected path |

**An alert is logged at warning before the publish is attempted**, so an alert that could not reach
the bus is still in the file.

## The catalogue

Every registered name. `tests/test_logging.py` parses these names out of the design documents and
asserts the set equals `log_events.ALL`, so prose and code cannot drift apart.

| `event` | What it records |
|---|---|
| `feed.transition` | Any ordinary connection move, including the first connect and a resume |
| `feed.stale` | The connection crossed the staleness bound and entered `degraded` |
| `feed.reconnect` | Any move touching `reconnecting` -- a close, prolonged silence, or a redial |
| `feed.instruments` | The venue listing caused new contracts to be subscribed |
| `store.flush` | One `BarStore.flush()` writing one file. An empty flush writes nothing and logs nothing |
| `store.replay_gap` | A saved stream position had entries trimmed before it, with both bounds and an exact `lost` count |
| `store.checkpoint` | A generation became durable, a start-up adopted a checkpoint, or a generation committed zero rows |
| `queue.drop` | A record dropped off a lossless subscription |
| `bus.selected` | Which bus the process selected at start-up; also the drop-oldest resync record |
| `bus.reader` | A bus reader's own lifetime: a retry, a recovery, or a reader that gave up |
| `compute.recompute_set` | The set of pairs being recomputed changed |
| `ws.client_attach` / `ws.client_detach` | A browser `/ws/chain` socket was accepted, or ended |
| `alert` | An `events.Alert` was raised |
| `engine.error` | A defensive branch failed. Always error; never an expected path |

### Three records worth knowing before an incident

**`store.checkpoint` carries three sentences under one name.** A committed generation (info, once
per generation, with the generation, stream count and four `sealed_through_us` values). A start-up
adoption (info, exactly once per process start **whether or not a gap was found**, naming every
stream and the position it reads forward from). And a generation that committed **zero rows across
all four tables** -- warning while recording, info while paused, beside a
`store.empty_generation` alert on the recording case only.

That last one exists because **sealed-empty is correct behaviour and is indistinguishable from a
dead system on disk**, so the store says which it was. For the same reason the adoption record is
emitted on every start, gap or no gap: a silent clean replay leaves a restart that dropped a minute
with nothing to show it happened.

**`bus.reader` is warning while a reader is still running and error once it is not** -- a retry, a
recovery after consecutive failures, and a reader that reached its retry bound are three different
records, because a bus reader can die while its process stays healthy.

**`feed.instruments` is the record of what is being recorded.** A contract listed after start-up and
never subscribed damages only the history, and nothing else would say so.

## Volume

**Nothing scales with message rate**, and that is a design constraint rather than an observation.
The recurring part is `store.flush` -- 8 records per flush, four tables across two underlyings -- so
at `FLUSH_SECONDS=300` the whole steady-state volume is `derived` **96 records an hour** plus a
dozen checkpoints. Bounding the cost by the flush interval is what makes info-level flush logging
affordable on a feed carrying over a thousand messages a second.

## Failure modes

| Failure | What happens |
|---|---|
| A record's `event` is not registered | `log_event` raises before the record is built |
| The logs directory cannot be created | `DailyFileHandler.__init__` raises and start-up fails loudly |
| A write to the day's file fails | `handleError` prints to stderr once; nothing else stops |
| Midnight UTC passes while up | The next `emit()` opens the new file |
| `main` imported twice | `configure_logging()` is a no-op the second time |
| The venue's listing cannot be read | `feed.instruments` at warning, and the cycle retries |

## Related guides

[Events](events.md) -- the `alert` event and its codes | [Architecture](architecture.md)
