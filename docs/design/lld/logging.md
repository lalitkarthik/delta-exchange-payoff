# Low-level design: structured logging

**What is built inside `engine/src/deltapayoff/logging_setup.py` and `log_events.py`.** The
controller's use of it is [controller.md](controller.md). Landed by #42, extended by #51.

## 1. What it is for

The engine writes one structured JSON line for each operational record. A day's file can be
filtered by `event`, `venue`, `instrument` or `conn_state`, making a hole in the Parquet store
visible instead of silently losing it.

## 2. The record - fixed fields, then extras

Every record is one JSON object on one line. Five fields are always present:

| Field | Meaning |
|---|---|
| `ts` | UTC, ISO 8601, millisecond precision. |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `logger` | The Python logger name. |
| `event` | A short stable name; section 4 links to the whole catalogue. |
| `msg` | The human sentence; not machine-matched. |

Optional fields are `venue`, `instrument`, `conn_state` and `event_id`. Anything else passed
as `extra` lands beside them under its own name: `table`, `rows`, `file` and
`duration_seconds` on a `store.flush`. A `None` value is dropped rather than sent as `null`.

`logging_setup.log_event(logger, level, event, msg, **extra)` is the one function every call
site uses, and it raises if `event` is not in `log_events.ALL`. A raw logger call still
produces a parseable line with `event: "log"`.

## 3. The sinks

One `DailyFileHandler` per process writes `<repo root>/logs/<YYYY-MM-DD>.log`, one JSON line
per record, checking the date on every `emit()` rather than with a timer.

**A second handler always writes to `sys.stderr`, and `is_terminal` decides only its
formatter** (#103): `ColorFormatter` at a terminal, `JsonFormatter` otherwise, so redirected
stderr still contains no ANSI escape codes and a container is no longer silent. The handler
itself used to be gated on `sys.stderr.isatty()`. In `dxp-store` `/proc/1/fd/2` is a pipe, so
it was never attached: seven hours of records -- 252 `store.flush` among them -- went only to
`/app/logs`, which no volume was mounted on, while `docker logs` showed four lines of uvicorn
and the store had stopped consuming. `compose.yml` now also mounts `./.stack-logs/<service>`
over `/app/logs`, so the durable copy survives the container as well.

Both handlers are attached to `logging.getLogger("deltapayoff")`, not root, and `propagate`
stays true so `pytest`'s `caplog` captures through the root handler. Configuration is
idempotent, so repeated imports do not multiply the handlers or the volume.

## 4. Every event name this engine emits

The complete record catalogue is in [logging-catalogue.md](logging-catalogue.md).
`tests/test_logging.py` parses the headings in both files and asserts the set equals
`log_events.ALL`.

## 5. Levels, in one table

| Record | Level | Why |
|---|---|---|
| An ordinary transition | Info | The baseline. |
| `stopped` by a spent budget | Error | No other symptom exists. |
| Entering `degraded` | Warning | The staleness question this project exists to answer. |
| Touching `reconnecting` | Warning | Worth attention whether or not it recovers. |
| Contracts newly subscribed | Info | Rare; the record of what is being recorded. |
| A flush | Info | Routine, and useful to see the cadence. |
| A replay gap | Error | A saved position needs entries Redis has trimmed. |
| A committed checkpoint | Info | The generation's files are durable. |
| A lossless drop | Error | Should be impossible. |
| The recompute set changing | Debug | Rare; for someone reading closely. |
| A websocket client attaching or detaching | Debug | Routine; volume matters more than one connection. |
| An alert | Warning | Always, regardless of the alert's own severity. |
| A defensive catch-all | Error | Never an expected path. |

## 6. Volume - the numbers this ticket asked to notice

`measured`: a live 300.4 s window produced 29 records and 8,813 bytes. It included
`feed.transition: 3`, `compute.recompute_set: 16`, `feed.reconnect: 2` and
`store.flush: 8`; it predates #51's two extra start-up records.

The recurring part is `store.flush`: **8 per flush**, `measured` (4 tables x BTC and ETH),
so at `flush_seconds`=300s, `derived` **96 records/hour**. A `store.checkpoint` is
`derived`, one per flush interval, **12 per hour**, against `store.flush`'s 8 per flush.
Nothing scales with message rate; the cost is bounded by the flush interval.

## 7. Failure modes

| Failure | What happens |
|---|---|
| A record's `event` is not registered | `log_event` raises before the record is built. |
| The logs directory cannot be created | `DailyFileHandler.__init__` raises and startup fails loudly. |
| A write to the day's file fails | `Handler.handleError` prints to stderr once; nothing else stops. |
| Midnight UTC passes while up | The next `emit()` opens the new file. |
| `main` imported twice | `configure_logging()` is a no-op the second time. |
| The venue's listing cannot be read | `feed.instruments` is warning and the cycle retries. |

## 8. The seam the tests drive

`caplog` is the seam for records. The formatter, daily handler and colour guard are exercised
directly in `tests/test_logging.py`, with the clock and terminal predicate injected.
