# Logging

**What this page contains.** What the system writes down about its own behaviour, where those
records go, how they are structured, what every kind of record means, and how much of it there is.

**How to read it.** The first three sections explain the format and where to find it, which is what
you need when something has gone wrong at two in the morning. The catalogue after them is a
reference to come back to. Nothing here requires having read any other page.

## Why the logs are structured

A log written as ordinary English sentences is pleasant to read one line at a time and almost
useless in bulk. You cannot reliably ask it "show me everything about this contract" or "how many
times did the connection drop yesterday" without inventing fragile text searching.

So every record this system writes is a single line of JSON: a small block of labelled fields rather
than a sentence. That makes a day's log something you can filter, count and group with ordinary
tools. In particular it makes a gap in the permanent record *visible* -- you can ask when the writer
last succeeded -- rather than something nobody notices for a week.

## What a record looks like

Five fields are present on every record, whatever it is about. The table below lists them.

| Field | What it holds |
|---|---|
| `ts` | When it happened, in UTC, to the millisecond |
| `level` | How serious it is: `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `logger` | Which part of the code wrote it |
| `event` | A short, stable name for *what kind of thing* this is. This is the field you filter on |
| `msg` | A sentence for a human. **Never** match on this; it is free to change at any time |

Four more fields appear when they apply: `venue`, `instrument`, `conn_state` and `event_id`.
Anything else a particular record wants to say appears alongside under its own name -- for a file
write, that is which collection, how many rows, which file, and how long it took.

A field with no value is **left out entirely** rather than written as empty. This is the same rule
the rest of the system follows for absent values, and it keeps "we did not record this" clearly
different from "this was zero".

Here is a complete record, wrapped across lines for readability:

```json
{"ts":"2026-09-14T09:08:35.798Z","level":"INFO","logger":"deltapayoff.store",
 "event":"store.flush","msg":"wrote quote bars","table":"quote-bars","rows":4821,
 "file":"20260914T090800Z-000287.parquet","duration_seconds":0.214}
```

The `event_id` field is the one that repays knowing about. Every message on the bus carries a unique
identifier, and a log record caused by one carries the same identifier, so a record and the message
behind it can be lined up exactly rather than guessed at from their timestamps.

## One way in, and it refuses unknown names

Every log record in the codebase is written through a single function, and **that function refuses
any `event` name that is not on a central registered list.**

This looks like bureaucracy and is not. A name invented on the spot at a call site is a name that no
filter, no dashboard and no written procedure knows about -- and it will be discovered during the
incident it was written for, which is the worst possible moment. Requiring registration means the
list of things the system can say is finite, knowable and documented below.

## Where records go

Records are written to two places at once.

**A file, one per day**, at `logs/<date>.log`, one JSON record per line. The date is checked every
time a record is written rather than by a timer, since a timer is one more thing that can silently
stop.

**Standard error, always.** This is what `docker logs` and any container platform will show you.
Whether that output is coloured depends on whether it is going to a terminal, but **whether it is
written at all does not depend on anything.** That distinction matters: inside a container, standard
error is a pipe rather than a terminal, so a system that only writes when it detects a terminal
writes nothing at all in production -- and the only diagnostics it has go to a file inside a
container that nobody has mounted. Both halves are held here: records always go to standard error,
and the container setup also mounts the log folder out to the host, one folder per program so that
two programs cannot write over each other's day.

## How serious is each thing

Choosing the level well is what makes a log searchable by severity rather than by guesswork. The
table below gives the rule for each kind of record.

| What happened | Level | Why that level |
|---|---|---|
| An ordinary connection change | Info | The normal baseline |
| Giving up after exhausting reconnection attempts | Error | Nothing else would reveal it |
| The connection going quiet | Warning | This is the precise question the project exists to study |
| Anything involving a reconnect | Warning | Worth a look whether or not it recovered |
| New contracts subscribed | Info | Rare, and it is the record of what is being recorded |
| A file being written | Info | Routine, and seeing the rhythm is useful |
| Needing data the bus had already discarded | Error | A real gap in the permanent record |
| A recording period being safely finished | Info | The files are now durable |
| A message dropped from a lossless subscription | Error | This should not be possible |
| The set of expiries being calculated changing | Debug | Rare, and only of interest to someone reading closely |
| A browser connecting or disconnecting | Debug | Routine; the total matters more than any one |
| An alert being raised | Warning | Always, whatever the alert's own severity says |
| An unexpected failure | Error | Never an expected path |

An alert is written to the log **before** any attempt is made to send it onward, so an alert that
could not be delivered is still recorded somewhere.

## The catalogue of record names

Every name the system is allowed to use. There is a test that compares this list against the code
and fails if they ever disagree.

| `event` | What it records |
|---|---|
| `feed.transition` | Any ordinary connection change, including the first connection and a resume |
| `feed.stale` | The connection went quiet for longer than the limit |
| `feed.reconnect` | Anything involving a reconnect: a closure, prolonged silence, or a redial |
| `feed.instruments` | The venue's contract listing caused new contracts to be subscribed |
| `store.flush` | One file was written. An empty write produces no file and no record |
| `store.replay_gap` | Data was needed that the bus had already discarded, with both boundaries and an exact count |
| `store.checkpoint` | Three related things -- see below |
| `queue.drop` | A message was dropped from a lossless subscription |
| `bus.selected` | Which message bus this program chose at startup |
| `bus.reader` | A bus reader retried, recovered, or gave up |
| `compute.recompute_set` | The set of expiries being calculated changed |
| `ws.client_attach`, `ws.client_detach` | A browser connected or disconnected |
| `alert` | An alert was raised |
| `engine.error` | Something failed that was never expected to |

### Three records worth understanding before you need them

**`store.checkpoint` covers three different situations under one name.** A recording period was
safely finished and its files are durable. A program started up and adopted a saved position --
written on *every* start, whether or not anything was wrong, because a silent clean start leaves a
restart that dropped a minute with nothing at all to show it happened. And a recording period that
finished having written no rows at all.

That third case exists because finishing with nothing recorded is, on disk, completely
indistinguishable from the system being dead -- in both cases there is no file. So the store says
which it was: a warning if it believed it was recording, a quiet note if it had been paused.

**`bus.reader` is a warning while a reader is still running and an error once it is not.** A retry
after a failure, a recovery after several, and a reader that gave up entirely are three different
records, because a bus reader can die while the program containing it stays perfectly healthy and
continues answering its health check.

**`feed.instruments` is the record of what is being recorded.** A contract that the venue listed
after we started, and that we therefore never subscribed to, damages only the historical record --
quietly, invisibly, and permanently. Nothing else would ever say so.

## How much of it there is

**Nothing in the log volume scales with the number of market messages.** That is a design
constraint, not an observation, and it is what makes it affordable to log every file write at
info level on a feed carrying over a thousand messages a second.

The recurring part is the file writes: eight records each time, being four collections across two
underlyings. At a five-minute interval that is `derived` **96 records an hour**, plus about a dozen
checkpoint records. Everything else is occasional.

## When logging itself fails

The table below lists what happens in each case, since a logging system that fails silently would
defeat its own purpose.

| Failure | What happens |
|---|---|
| A record uses an unregistered name | The function raises before the record is built |
| The log folder cannot be created | Startup fails immediately and loudly |
| Writing one record fails | A note is printed to standard error once; nothing else stops |
| Midnight passes while running | The next record opens the new day's file |
| The setup code runs twice | The second time does nothing, so records are not duplicated |
| The venue's contract listing cannot be read | A warning, and the attempt is repeated later |

## Where to go next

[Events](events.md) describes the alerts that appear in this log.
[Data flow](data-flow.md) explains the connection states the `feed.*` records refer to.
