# Low-level design: structured logging

**Start here if you have never read this engine's log.** What a record looks like, where it
goes, how to watch it, and — section 5 — what each component says and when. The per-record
field lists are the sibling, [logging-catalogue.md](logging-catalogue.md), organised by the
same components in the same order. Built in `logging_setup.py`, `log_events.py` and
`throughput.py`; landed by #42, extended by #51, #103 and the component/throughput work.

## 1. What it is for

The engine writes **one JSON object per line** for each operational record. A three-day hole
in the Parquet store — 2026-09-04 09:38Z to 2026-09-07 09:45Z — went unnoticed because there
was nothing to notice it with. So a day's file can be filtered by `component`, `event`,
`venue`, `instrument` or `conn_state`, and each of "what happened to this contract", "what
was the store doing" and "what happened while degraded" is one filter over one file.

## 2. The anatomy of one record

Five fields are always present:

| Field | Meaning |
|---|---|
| `ts` | UTC, ISO 8601, millisecond precision. |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `logger` | The Python logger — always the **module** that wrote it. |
| `event` | A short stable name from `log_events.ALL`; section 5 lists them by component. |
| `msg` | The human sentence. Never machine-matched — filter on `event`. |

Then the optional fixed fields — `component`, `venue`, `instrument`, `conn_state`,
`event_id` — and after them anything the call site passed as `extra`, under its own name:

```json
{"ts": "2026-09-15T08:00:18.804+00:00", "level": "INFO", "logger": "deltapayoff.store",
 "event": "store.flush", "msg": "wrote 1,284 rows to quote-bars", "component": "store",
 "table": "quote-bars", "rows": 1284, "file": "...", "duration_seconds": 0.031}
```

**A `None` extra is dropped, not sent as `null`.** The store's own rule, one layer up: an
absent field and a field that was asked for and came back empty must not look the same.

### The component field: subsystem, not process

`logger` already tells you the module. `component` tells you which **part of the engine** it
belongs to — `feed`, `bus`, `store`, `chain`, `api`, `alerts`, and `web` for what the viewer
folds in. The map is `logging_setup.COMPONENT_BY_MODULE`, matched on the longest module
suffix; `tests/test_logging.py` asserts every module it names still exists.

Naming the *process* was the first design, and understanding why it is not is worth a
paragraph. It reads well in the split stack — but an ordinary `uvicorn main:app` run is one
process, so every line said `api`, and `tools/logs.py feed` printed nothing while the venue
socket was busily reading Delta **inside that same process**. The subsystem is right in both
deployments: in the split stack the feed process only runs feed modules anyway, so the two
answers coincide there and only the monolith gains.

A module the map does not name falls back to the **process** name, so a new module is
unlabelled rather than mislabelled. Each entrypoint sets that — `main.py` `api`,
`feed_main.py` `feed`, `store_main.py` `store`, `alert_main.py` `alerts` — with
`DELTA_COMPONENT` overriding it for a deployment. It is a module global read when a record
is *emitted*, not an argument to `configure_logging`, because `main.py` configures at import
and `feed_main.py` imports `main`: a parameter would stamp `api` on the feed process
whatever the feed asked for.

A call site may also pass `component=` outright, which wins. Two records do — see section 5's
note on `throughput.py`, which is about a subsystem it does not live in.

## 3. Where records go

Two handlers, attached to `logging.getLogger("deltapayoff")` and not to root, so `propagate`
stays true and `pytest`'s `caplog` still captures. Configuration is idempotent: repeated
imports do not multiply the handlers or double the volume.

| Sink | What it gets | Why |
|---|---|---|
| `DailyFileHandler` | `<repo>/logs/<YYYY-MM-DD>.log`, JSON | The durable record. Date checked on every `emit()`, never a timer. |
| `StreamHandler(stderr)` | colour at a tty, JSON otherwise | What `docker logs` and your terminal show. |

**The stream handler is unconditional; a tty picks only its formatter** (#103). It used to be
gated on `sys.stderr.isatty()`, and in `dxp-store` `/proc/1/fd/2` is a pipe — so it was never
attached, seven hours of records (252 `store.flush` among them) went only to `/app/logs`,
which no volume was mounted on, and `docker logs` showed four lines of uvicorn while the
store had stopped consuming. `compose.yml` now also mounts `./.stack-logs/<service>` over
`/app/logs`, one directory per service so four containers cannot collide on one day's file.

**Uvicorn's own loggers get the same two handlers**, with `propagate` false so root does not
print an unformatted second copy. They are not children of `deltapayoff`, so until recently a
server's start-up and error lines never reached the file at all — the other half of that same
#103. They arrive with no `event`, which the formatter renders as `"log"` by design.
`uvicorn.access` keeps its own handler and stays on stderr only: a request line per poll is
volume the file does not need, and `ws.client_attach` already records the connection that
matters.

## 4. Reading it live — `tools/logs.py`

Every process appends to the same daily file, so **the master log is a file, not something to
be assembled**:

```sh
python tools/logs.py                      # master: every component, live
python tools/logs.py feed                 # one component; `feed store` for two
python tools/logs.py --level WARNING --event store.flush -n 50
python tools/logs.py --dir .stack-logs    # the stack's per-service dirs, merged
cd web && bun run dev 2>&1 | python ../tools/logs.py --ingest web
```

Per-component viewing is a **filter over the one file**, not a second set of files: splitting
the file would turn the master view back into a merge problem and put ordering at risk. The
file set is recomputed on every pass rather than resolved once, which is what makes midnight
work — the same check-the-date-when-you-touch-it rule `DailyFileHandler` writes by. A line
that does not parse as one of our records passes every filter untouched, because a stray
traceback is the thing a filtered view can least afford to drop.

`--dir` is for the stack, where the per-service mounts mean the master view really is a merge.
`--ingest` wraps each line Next.js prints as a record with `component: web` and `event: "log"`
— an ingested line has no registered event name, and inventing one per source would be a
catalogue nothing enforces — appends it to the same day's file, and echoes it rendered so the
dev-server terminal stays readable. ANSI codes are stripped before storing: colour is for a
terminal, not for a file that gets grepped a week later. `tools/run_logged.py` shares the same
`render()` for one child process's stdout.

## 5. Part by part — what each component says

Six components, **71 call sites**. Each row is "when it fires"; the fields are in the
[catalogue](logging-catalogue.md).

### Component feed — the venue connection

`adapters/delta_socket.py`, `adapters/delta.py`, `controller.py`, `supervisor.py`,
`feed_runtime.py`, `delta_client.py`, `feed_main.py`. Owns the one socket to Delta.

| Event | Level | When |
|---|---|---|
| `feed.transition` | Info, **error** on a spent budget | Any ordinary connection move, including the first connect and a resume. |
| `feed.stale` | Warning | The staleness bound was crossed and the connection entered `degraded`. |
| `feed.reconnect` | Warning | Anything touching `reconnecting`: a close, silence past the bound, a redial. |
| `feed.instruments` | Info, **warning** on failure | The venue listing was read and new contracts subscribed — or could not be read. |
| `feed.throughput` | Info, every 10 s | Frames, bytes and malformed since the last report. **The record that says the feed is alive.** |
| `feed.message` | Debug, **trace only** | One frame off the socket. See section 7. |
| `alert` | Warning | The controller raised an `events.Alert`. |
| `engine.error` | Error | A defensive branch in the controller. |

The first three come from **one call site**, `controller._transition`, which picks the name
and the level from the reason for the move.

### Component bus — the event bus, either implementation

`fanout.py` (in-process) and `redis_bus.py` (Redis Streams, `DELTA_BUS=redis`). The two behave
identically through the seam, which is why `bus.selected` exists at all.

| Event | Level | When |
|---|---|---|
| `bus.selected` | Info, **warning** on a resync | Which bus this process chose, said once at start-up. A drop-oldest reader that jumped counts what it skipped here. |
| `bus.reader` | Warning running, **error** once not | A read raised and was retried, a reader recovered, or a reader gave up. |
| `bus.throughput` | Info, every 10 s | Published, and per consumer offered / consumed / dropped / queued. **A `queued` climbing report over report is a consumer falling behind.** |
| `queue.drop` | Error | A record was dropped off a **lossless** subscription, or the Redis outbox hit its ceiling. |
| `bus.publish` | Debug, **trace only** | One event entered the bus. |
| `bus.consume` | Debug, **trace only** | One event was read off it by a consumer. |
| `bus.release` | Debug, **trace only** | It left for good — the last `take()`, or Redis's `XACK` (per batch, not per entry). |
| `engine.error` | Error | An event that could not be keyed or encoded; a batch that could not be written. |

The last three are possible **only because every consumer reads through
`fanout.Subscription.take()`**. The bus can count what it hands to a queue; it cannot see a
`queue.get()`, and a lifecycle nobody can observe the end of is not a lifecycle. No bare
`queue.get()` calls remain in `engine/src`, and `Subscription.consumed` is what
`bus.throughput` reports against `offered`.

### Component store — the Parquet record

`store.py`, `store_main.py`, `bar_buffer.py`, `bars.py`, `store_home.py`, `contract_bars.py`,
`historical.py`. The only modules that touch a file.

| Event | Level | When |
|---|---|---|
| `store.flush` | Info | One `BarStore.flush()` wrote one file. Eight per flush — four tables × two underlyings. |
| `store.checkpoint` | Info, **warning** if recording with zero rows | A generation's files and checkpoint became durable; the checkpoint adopted at start-up; an empty generation. |
| `store.replay_gap` | Error | A saved stream position needs entries Redis has already trimmed. Names both positions and the exact count lost. |
| `alert` | Error | Consumer lag crossed its threshold. |
| `engine.error` | Error, one **warning** | A flush failed, a file could not be named, a buffer could not be folded. |

### Component chain — the live ladder and everything computed from it

`stream.py`, `chain.py`, `compute.py`, `smile.py`, `volatility.py`, `iv_index.py`.

| Event | Level | When |
|---|---|---|
| `compute.recompute_set` | Debug | An `(underlying, expiry)` pair joined or left the recomputed set — a browser started or stopped watching it. |

Two call sites, and that is the whole component. The pure core solves and returns; it does not
log, which is what lets the test suite be large and fast.

### Component api — the HTTP and websocket surface

`main.py`, plus uvicorn's own loggers folded in beside it.

| Event | Level | When |
|---|---|---|
| `ws.client_attach` | Debug | A browser's `/ws/chain` socket connected. |
| `ws.client_detach` | Debug | It closed, however it closed. |
| `bus.selected` | Info | Which bus the api process chose. |
| `engine.error` | Error | Six defensive branches: a failed final flush, a background task that died. |
| `log` | Info / Error | Uvicorn: start-up, shutdown, and any unhandled ASGI exception with its traceback. |

**`throughput.py` lives here but its two records do not.** A record belongs to the subsystem it
is *about*, and `COMPONENT_BY_MODULE` can only see the logger — so `feed_report` and
`bus_report` pass `component=` explicitly. Without that, `tools/logs.py feed` would omit the
one record that says whether the feed is alive.

### Component alerts — the Discord consumer

`alert_consumer.py`, `discord_alerts.py`, `alert_main.py`.

| Event | Level | When |
|---|---|---|
| `alert` | Warning, info on a post | An `events.Alert` was consumed; a post was made. Warning **regardless of the alert's own `severity`**, which is a property of the alert and not of how loudly the log should say so. |
| `engine.error` | Error, one warning, **one info** | A post failed, a gate refused, or the webhook is unconfigured. |

That one `info` is `alert_main`'s "webhook not configured", and it is the single call site in
the engine where `engine.error` is not error or worse — the catalogue says "always error or
worse", and this is the exception. Worth resolving rather than quoting.

### Component web — the Next.js dev server

No logger of its own; every line arrives through `--ingest`, as `event: "log"` at info.

## 6. Reading a level

| Level | What it means here |
|---|---|
| `DEBUG` | Routine and high-volume, or trace-gated. Nobody watches these. |
| `INFO` | The cadence — flushes, checkpoints, throughput, transitions. What "working" looks like. |
| `WARNING` | Worth attention whether or not it recovered: a reconnect, degradation, a failed listing. |
| `ERROR` | Either a path this project says is impossible, or one where no other symptom exists. |

The rule behind the table: **a level answers "should I look at this", never "how bad is the
thing it describes"**. An alert logs at warning whatever severity it carries, and a `stopped`
reached by a spent reconnect budget logs at error because no other symptom of it exists.

## 7. The two dials, and the volume behind them

`DELTA_LOG_TRACE=1` turns on `feed.message`, `bus.publish`, `bus.consume` and `bus.release` —
a record per frame and per bus event. **Off by default, and never left on.** `measured`
2026-09-02 over 40 minutes and 585 BTC contracts (`tools/capture_ws.py`): **307,301
frames a minute, 5,122 a second**, `ob_l1` alone 4,874. At roughly 200 bytes a record that is
about 1 MB/s of log for one underlying, before the bus multiplies it by the consumer count —
and the write would land on the socket read loop that `adapters/delta_socket.py` documents as
never allowed to block. Turn it on to follow one contract for a minute. The gate is a module
constant read once at import, not an `os.environ` lookup five thousand times a second.

`DELTA_THROUGHPUT_SECONDS` (10 s) sets the `feed.throughput` and `bus.throughput` cadence —
six records a minute per reporter, against that three hundred thousand. Every counter they
read was already being kept and never said aloud, so the log could report a connection
`connected` and not whether one byte had crossed it since: the "healthy badge, zero messages"
failure, one layer up. Reporters are started from each entrypoint's task list, so they are
cancelled with everything else.

Otherwise nothing scales with message rate. `measured`: a live 300.4 s window produced 29
records and 8,813 bytes. The recurring part is `store.flush` at 8 per flush, `derived` **96
records/hour** at `flush_seconds`=300, and `store.checkpoint` at **12 per hour**.

## 8. How a record is written

`logging_setup.log_event(logger, level, event, msg, *args, **extra)` is the one function every
call site uses — **nothing in this engine calls `logger.info` directly.** It raises if `event`
is not in `log_events.ALL`, because a typo in an event name is a document that has quietly
stopped being true. `tests/test_logging.py` parses the `###` headings out of this file's
sibling catalogue and asserts the set equals `log_events.ALL`, so a name added in code without
a paragraph, or a paragraph without a name, fails the suite. That pairing is the whole
mechanism that keeps the catalogue honest.

Adding one: a constant in `log_events.py`, a `###` section in the catalogue, and the call.

## 9. Failure modes

| Failure | What happens |
|---|---|
| A record's `event` is not registered | `log_event` raises before the record is built. |
| The logs directory cannot be created | `DailyFileHandler.__init__` raises and startup fails loudly. |
| A write to the day's file fails | `Handler.handleError` prints to stderr once; nothing else stops. |
| Midnight UTC passes while up | The next `emit()` opens the new file. |
| `main` imported twice | `configure_logging()` is a no-op the second time. |
| A throughput report raises | Logged as `engine.error`; the loop goes round again. |
| A line is read mid-write | The viewer holds the partial back and completes it next pass. |
| An `--ingest` line is not JSON | It is wrapped as a record with `event: "log"`. |
| The venue's listing cannot be read | `feed.instruments` is warning and the cycle retries. |

## 10. The seams the tests drive

`caplog` for records. The formatter, daily handler, colour guard and component map are
exercised directly in `tests/test_logging.py` with the clock and terminal predicate injected;
the reporters and the trace gate in `tests/test_throughput.py`, against duck-typed subjects so
no test needs the network.
