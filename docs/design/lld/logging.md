# Low-level design: structured logging

**What is built inside `engine/src/deltapayoff/logging_setup.py` and `log_events.py`.**
The controller's own use of it is [controller.md](controller.md). Landed by #42.

## 1. What it is for

The engine wrote almost nothing before this. One logger existed and it said one line per
connection transition, in prose. A **three-day hole in the Parquet store —
2026-09-04 09:38Z to 2026-09-07 09:45Z — went unnoticed because there was nothing to
notice it with.** This ticket's whole test is whether that hole would now be obvious the
same day: filter one day's file by `event`, `venue`, `instrument` or `conn_state` and get
an answer, with standard tools, without reading prose.

## 2. The record — fixed fields, then extras

Every record is one JSON object on one line. Five fields on every one:

| Field | Meaning |
|---|---|
| `ts` | UTC, ISO 8601, millisecond precision. |
| `level` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `logger` | The Python logger name, e.g. `deltapayoff.controller`. |
| `event` | A short stable name — §4 is the whole catalogue. Never free text. |
| `msg` | The human sentence. Free text; not machine-matched against. |

Four more, present only when the caller passed them:

| Field | Meaning |
|---|---|
| `venue` | The adapter's name, e.g. `DELTA`. |
| `instrument` | The canonical instrument string. |
| `conn_state` | The connection state a record concerns. |
| `event_id` | The bus event's own id, when raised alongside one. |

Anything else passed as `extra` lands beside them under its own name — `table`, `rows`,
`file` and `duration_seconds` on a `store.flush` record, for one example. A `None` value
is **dropped rather than sent as `null`**: a field that was never asked for and a field
that came back empty read the same on the wire otherwise, and the project's own rule —
absent is `null`, not fabricated — reads better as the field being absent altogether.

**Enforced, not just documented.** `logging_setup.log_event(logger, level, event, msg,
**extra)` is the one function every call site in this engine uses; it raises if `event`
is not in `log_events.ALL`. A call that reaches a raw `logger.info(...)` instead — a
library's own logger, or code this ticket did not touch — still produces a parseable
line, because `JsonFormatter` falls back to `event: "log"` rather than refusing to format
a record it did not build.

## 3. The sinks

One `DailyFileHandler` per process, always on: `<repo root>/logs/<YYYY-MM-DD>.log`, one
JSON line per record, checked on every `emit()` rather than a timer — a background thread
that wakes at midnight is one more thing that can silently stop running, which is the
exact failure this ticket exists to refuse elsewhere. `logs/` is gitignored, added by this
ticket, the same convention `store.default_root()` uses for `data/`.

A second handler, coloured and human-readable, is attached **only when
`sys.stderr.isatty()`** at the moment `configure_logging()` runs. Redirecting stderr to a
file means this handler is never attached, so no ANSI escape reaches the redirected file —
by construction, not by a check on every line.

Both are attached to `logging.getLogger("deltapayoff")`, not root. Every module's own
`logging.getLogger(__name__)` resolves to a child of it and propagates upward by default,
which is untouched — `propagate` stays `True` everywhere, because `pytest`'s `caplog`
fixture captures through the root logger's handler and needs that propagation. Idempotent,
guarded by a module-level set of configured logger names: importing `main` more than
once — every test file in this suite does — must not attach the handlers twice, or the
volume figures in §6 would be wrong by whatever factor `main` was imported.

## 4. Every event name this engine emits

The whole catalogue. `tests/test_logging.py` parses these headings and asserts the set
equals `log_events.ALL` — add a name to one without the other and the suite fails.

### `feed.transition`

Any connection move that is not specifically `feed.stale` or `feed.reconnect` below —
`connecting`, the first `connected`, a resume, an ordinary stop. Info, **except the one
move `_spend_reconnect` marks error**: the lifetime reconnect budget is spent and the
connection reaches `stopped` without a resume coming. The loudest thing this engine says,
because a feed that has given up produces no other symptom — the screens simply stop
moving.

### `feed.stale`

`connected -> degraded`: the staleness bound was crossed and nothing has arrived. Warning,
with `conn_state: degraded`. This is the record a three-day hole would have produced the
day it started.

### `feed.reconnect`

Any move that touches `reconnecting`, on either side: a socket closing, silence past the
longer bound, or a redial attempt being dialled. Warning — a reconnect is always worth an
operator's attention, whether or not it succeeds.

### `store.flush`

One `BarStore.flush()` writing one file. Info, carrying `table`, `rows`, `file` and
`duration_seconds`. Never on the per-message path: this fires once per `flush_seconds`
interval per table that had something buffered, not once per tick — an empty flush writes
no file and logs nothing, the same restraint the file layout already has.

### `queue.drop`

A record dropped off a **lossless** subscription. Error. `fanout.subscribe(...,
lossless=True)` gives the queue `maxsize=0` — unbounded — so this is unreachable under
the module's own construction; guarded and logged anyway, because "impossible" describes
the code as written today and not a promise about every later change to it.

### `compute.recompute_set`

A new `(underlying, expiry)` pair joins the set `ChainStream` has ever seen a contract
for. Debug — rare, once per expiry the venue ever lists, and useful only to someone
reading closely. Explicitly **not** on `dirty` changing, which happens on nearly every
one of the feed's ~1,323 messages a second and would be exactly the per-message record
this ticket rules out.

### `ws.client_attach`

A browser's `/ws/chain` socket accepted. Debug: routine, and volume matters more than any
one connection.

### `ws.client_detach`

The other half, in a `finally` so it fires however the connection ended.

### `alert`

An `events.Alert` was raised. Warning, **always** — regardless of the alert's own
`severity` field, which describes the alert for whatever reads it off the bus, not how
loudly the log should say so. Logged before the publish is attempted, so the record
exists even on the one path most likely to need it: a poll that just failed because
publishing raised.

### `engine.error`

A catch-all for a defensive branch that should not run: a background task that ended
without being cancelled, a watchdog that died, a final flush that failed, a configured
underlying Delta does not list. Always error; never a path this project expects to take.

## 5. Levels, in one table

| Record | Level | Why |
|---|---|---|
| An ordinary transition | Info | The baseline. |
| A transition to `stopped` by spent budget | Error | No other symptom exists. |
| Entering `degraded` | Warning | The staleness question this project exists to answer. |
| Touching `reconnecting` | Warning | Worth attention whether or not it recovers. |
| A flush | Info | Routine, and useful to see the cadence. |
| A lossless drop | Error | Should be impossible. |
| The recompute set changing | Debug | Rare; for someone reading closely. |
| A websocket client attaching or detaching | Debug | Routine; volume matters more than one connection. |
| An alert | Warning | Always, regardless of the alert's own severity. |
| A defensive catch-all | Error | Never an expected path. |

## 6. Volume — the numbers this ticket asked to notice

**A reconnect, scripted and live.** Scripted, deterministic
(`test_controller.py::test_a_reconnect_only_logging_scenario_produces_a_bounded_number_of_records`):
`measured` **6 records** for one whole drop-and-recover session — start, open,
closed→reconnecting, backoff→connecting, open, stopped. **The reconnect alone is 3
records** — two `feed.reconnect` warnings plus the recovery `feed.transition` — confirmed
live: the run below hit a real `ConnectionClosedError: ... keepalive ping timeout` at
05:17:45Z and produced exactly those 3 lines.

**A live 300.4 s window**, `measured` against `api.india.delta.exchange`, BTC+ETH,
`tools/measure_log_volume.py --since 2026-09-08T05:14:41.129Z --minutes 5.02`, right after
start-up: **29 records, 8,813 bytes** — `feed.transition: 3, compute.recompute_set: 16,
feed.reconnect: 2, store.flush: 8`.

**Not a quiet hour, and `derived` rather than `measured` past this window.** The 16
`compute.recompute_set` lines are every expiry joining the set **for the first time since
start** — an already-warm hour adds one only when a new expiry lists — and the reconnect
is one real event this window happened to catch, not a steady rate. The recurring part is
`store.flush`: **8 per flush**, `measured` (4 tables × BTC and ETH), so at
`flush_seconds`=300s, `derived` **96 records/hour** from flushes in steady state.
Heartbeats are **not** logged, only published to the bus. **Filtering works**: of the 29
records, exactly 1 named `DELTA-BTC-20261127-78000-C`, `measured` from the same file.

Nothing scales with message rate — the recurring cost is bounded by the flush interval,
not the ~1,693.6 msg/s (`measured`, `tools/measure_feed.py`, 2026-09-08) the feed runs at.

## 7. Failure modes

| Failure | What happens |
|---|---|
| A record's `event` is not registered | `log_event` raises before the record is built. Caught at the call site, not silently downgraded to a generic line. |
| The logs directory cannot be created | `DailyFileHandler.__init__` raises — startup fails loudly rather than running with no file sink. |
| A write to the day's file fails mid-process | `Handler.handleError` — Python's own default: printed to stderr once, the record is lost, nothing else stops. |
| Midnight UTC passes while the process is up | The next `emit()` notices the date changed and opens the new file. No timer, nothing to miss. |
| `main` imported twice | `configure_logging()` is a no-op the second time — one set of handlers, not two. |

## 8. The seam the tests drive

`caplog`, seam 6 of #33's testing decisions — every record here is asserted through it in
`tests/test_controller.py`, `tests/test_store.py`, `tests/test_fanout.py`,
`tests/test_stream.py`, `tests/test_ws_endpoint.py` and `tests/test_logging.py`. The
formatter and the daily handler are exercised directly in `tests/test_logging.py`, on a
clock a test moves by hand, the discipline `ScriptClock` uses for the staleness timer.
The colour guard is a unit test on `configure_logging`'s injected `is_terminal` callable.
