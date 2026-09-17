# Structured log record catalogue

The record catalogue referenced by [logging.md](logging.md) — **read that first** for what a
record looks like, where it goes and how to watch it. This file answers the next question:
given a component, what can it actually say, with which fields, and when.

Every `###` heading here is a registered name in `deltapayoff.log_events.ALL`, and
`tests/test_logging.py` parses the headings out of both documents and asserts the set is equal
to it. A name added in code without a paragraph, or a paragraph without a name, fails the
suite — that pairing is the whole mechanism keeping this file true.

**Grouped by the component that emits it**, in logging.md section 5's order. `component` is
the subsystem, not the process, so the same record reads identically whether it came from the
monolith or from its own container. `engine.error` belongs to every component and is
catalogued once, at the end.

Fields listed are **in addition to** the five every record carries (`ts`, `level`, `logger`,
`event`, `msg`) and to `component`.

## Component feed — the venue connection

`adapters/delta_socket.py`, `adapters/delta.py`, `controller.py`, `supervisor.py`,
`feed_runtime.py`, `delta_client.py`, `feed_main.py`.

The first three records below come from **one call site**, `controller._transition`, which
picks the name and the level from the reason for the move. They are the same sentence under
three names, deliberately: an operator grepping "the budget is spent" must not also have to
read "an attempt is being made".

### `feed.transition`

Any ordinary connection move, including the first connection and a resume. Info, except a move
that spends the reconnect budget and reaches `stopped`, which is **error** — the loudest thing
this engine says, because no other symptom of it exists.

Fields: `venue`, `conn_state` (the state moved *to*), `event_id`. The message carries both
states and the reason: `feed connection DELTA: connected -> degraded (stale)`.

### `feed.stale`

The connection crossed the staleness bound and entered `degraded`. Warning, with
`conn_state: degraded`. This is the "quiet market or dead feed" question the project exists to
answer; `feed.throughput` is what settles it.

### `feed.reconnect`

Any move touching `reconnecting` — a socket close, silence past the longer bound, or an attempt
being dialled. Warning, because a reconnect is worth attention even when it succeeds.

### `feed.instruments`

The venue was asked what it lists and something was subscribed as a result. Info with `venue`,
`underlying`, `listed` and `subscribed`.

**Warning when the listing could not be read at all**, which is a gap in the record rather than
a failure of the feed. Issue #51 existed for six and a half hours of one night's data because
nothing anywhere said either sentence.

### `feed.throughput`

What the socket read since the previous report. Info, every `throughput.REPORT_SECONDS` (10 s,
`DELTA_THROUGHPUT_SECONDS`).

Fields: `venue`, `frames`, `bytes`, `malformed`, `frames_per_second`, `since_seconds`.

**The affordable answer to "is the feed alive"** — six records a minute against three hundred
thousand — and `frames: 0` is the distinction between a quiet market and a dead socket that
nothing else in the log draws. Emitted from `throughput.py`, which maps to `api`, so it passes
`component="feed"` explicitly: a record belongs to the subsystem it is *about*.

### `feed.message`

One frame read off the venue socket. Debug, with `channel`, `instrument` and `bytes`.

**Off unless `DELTA_LOG_TRACE=1`**, and the gate is not a preference. BTC alone delivered
307,301 frames a minute — 5,122 a second — `measured` 2026-09-02 over a 40-minute capture of
585 contracts (`tools/capture_ws.py`), of which `ob_l1` was 4,874/s. At roughly 200
bytes a record that is about 1 MB/s of log for one underlying, and the write would land on the
read loop `adapters/delta_socket.py` documents as never allowed to block. Turn it on to watch
one contract for a minute, not to leave running.

## Component bus — the event bus, either implementation

`fanout.py` (in-process) and `redis_bus.py` (Redis Streams, `DELTA_BUS=redis`). The two behave
identically through the seam, which is why `bus.selected` has to exist.

### `bus.selected`

Which bus this process is running on, said once at start-up. Info, with the Redis configuration
when that is the choice. A laptop pointed at the wrong Redis is otherwise a silent
misconfiguration.

The same name also carries a **warning** resync record: a drop-oldest reader that jumped counts
what it skipped here.

### `bus.reader`

A bus reader's own lifetime. **Warning** while it is still running — a read raised and is being
retried, with the attempt number and the backoff, or a reader recovered after one or more
consecutive failures. **Error** once it is not — a reader that reached the retry bound and gave
up, or a task that exited for any other reason, each naming the subscription and the failure
that ended it. The publisher's loop exiting is reported here too.

Added by #103, where one 2-second Redis read timeout killed every bus reader in three services
inside 34 seconds and the only trace was a single `engine.error` apiece.

### `bus.throughput`

What the bus moved since the previous report. Info, every `throughput.REPORT_SECONDS`.

Fields: `published`, `events_per_second`, `since_seconds`, and `consumers` — a map of
subscriber name to `offered`, `consumed`, `dropped` and `queued`. The message names anyone
behind: `bus moved 4 events in 3.0s to 2 consumers; behind: bar-writer`.

**A `queued` climbing report over report is a consumer falling behind.** On a drop-oldest
subscription that becomes silent staleness; on a lossless one it becomes memory. `offered`
against `consumed` is the same fact as a rate rather than a depth.

### `queue.drop`

A record was dropped off a **lossless** subscription, or the Redis outbox hit its ceiling and
shed its oldest entries. Error: `fanout.py` documents the first as impossible by construction,
and a guard that never fires is cheap insurance against the day the construction changes.

### `bus.publish`

One event entered the bus, with `event_type` and the destination — `subscribers` on the
fan-out, the stream `key` and `outbox` depth on Redis. Debug, **`DELTA_LOG_TRACE=1` only**:
this is `feed.message` one layer down, at the same rate, multiplied by the consumer count.

### `bus.consume`

One event was read off the bus by a consumer, with `subscriber`, `event_type` and the `queued`
depth left behind. Debug, **`DELTA_LOG_TRACE=1` only**.

Countable at all only because every consumer reads through `fanout.Subscription.take()`. The
bus can count what it hands to a queue; it cannot see a `queue.get()`.

### `bus.release`

One event left the bus for good. Debug, **`DELTA_LOG_TRACE=1` only**.

On the in-process fan-out each subscriber holds its own copy, so the release **is** the read —
one `take()` apart from `bus.consume`, not separated by an acknowledgement. On Redis it is the
`XACK`, logged **once per batch** with `subscriber`, `key` and `entries`, because an ack covers
a whole read; the `XTRIM` riding with the next flush is a count in the store's own accounting
rather than a record here.

## Component store — the Parquet record

`store.py`, `store_main.py`, `bar_buffer.py`, `bars.py`, `store_home.py`, `contract_bars.py`,
`historical.py`. The only modules that touch a file.

### `store.flush`

One `BarStore.flush()` writing one file. Info with `table`, `rows`, `file` and
`duration_seconds`. An empty flush writes no file and logs nothing.

Eight per flush, `measured` — four tables × BTC and ETH — so `derived` 96 records/hour at
`flush_seconds`=300. Bounded by the flush interval, never by message rate.

### `store.checkpoint`

Everything a store says about a checkpoint: three sentences under one name, because all three
are statements about one checkpoint and a second name would split every "what did this
generation do" query in two.

**One committed generation became durable.** Info, once per generation, carrying `generation`,
`streams` and four `*_sealed_through_us` values.

**One start-up adopted a checkpoint.** Info, exactly once per process start whether or not a
replay gap was found (#110), adding `replayed_from` — every stream and the position it reads
forward from — plus `recording` and `replay_gap_entries`. `initialized` is a root with no
checkpoint in it; `restored` is every other start. Before #110 a clean replay was **silent**,
and a restart that dropped a minute from one of four tables left no record of the restart.

**One generation committed zero rows across all four tables.** Warning while recording and info
while paused, with `rows` 0 and `tables` 4, beside an `alert` `store.empty_generation` on the
recording case only. Sealed empty is correct behaviour and is indistinguishable from a dead
system on disk, so the store says which it was. All three of 2026-09-12's losses wore this face
(#103, #108, #110).

### `store.replay_gap`

A saved stream position had entries trimmed before it. Error, once per affected stream at
start-up, with `stream`, `saved_id`, `first_retained_id` and the exact `lost` count — the
number of entries that cannot be replayed, which is a permanent hole in the record.

## Component chain — the live ladder

`stream.py`, `chain.py`, `compute.py`, `smile.py`, `volatility.py`, `iv_index.py`. Two call
sites in the whole component: the pure core solves and returns, and does not log, which is what
lets the test suite be large and fast.

### `compute.recompute_set`

A new `(underlying, expiry)` pair joined the set this engine recomputes, or left it — a browser
started or stopped watching. Debug, with `instrument` on the join. Once per interest change,
never on a per-message dirty update: rare, and useful only to someone reading closely.

## Component api — the HTTP and websocket surface

`main.py`, with uvicorn's own loggers folded in beside it as `event: "log"` — start-up,
shutdown, and any unhandled ASGI exception with its traceback.

### `ws.client_attach`

A browser's `/ws/chain` socket connected. Debug, with `underlying` and `expiry`. Routine, and
volume matters more than any one connection.

### `ws.client_detach`

The other half: the socket closed, however it closed, from the handler's `finally`. Debug, with
the same two fields.

## Component alerts — the Discord consumer

`alert_consumer.py`, `discord_alerts.py`, `alert_main.py`.

### `alert`

An `events.Alert` was raised — on the bus and here beside it, logged before a publish is
attempted. **Warning regardless of the alert's own `severity` field**, which is a property of
the alert and not of how loudly the log should say so.

Fields vary by raiser: the controller adds `venue`, `code`, `severity` and `event_id`; the
store's consumer-lag alert adds `stream`, `lag` and `threshold` at error. A successful Discord
post logs at info.

## Every component — the catch-all

### `engine.error`

A defensive branch that should not run: an unexpected exception in a background task, a
watchdog that died, a flush that failed, a configuration value that could not be honoured, a
throughput report that raised. Always error or worse; never on a path this project expects to
take. Usually carries `exc_info`, and whatever narrows it — `venue` from the controller,
`table` from the bar buffer.

**One call site breaks that rule and is worth fixing rather than quoting**: `alert_main` logs
"the webhook is not configured" as `engine.error` at **info**. It is the only one of the
engine's 31 `engine.error` call sites that is not error or worse.
