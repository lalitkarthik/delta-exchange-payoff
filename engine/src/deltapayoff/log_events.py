"""Every `event` name any part of the engine logs. One place, so a name cannot drift.

`docs/design/lld/logging-catalogue.md` documents each one in prose;
`tests/test_logging.py` asserts the two lists are the same set. Add a name here and to
that document together,
or the test fails — that is the whole mechanism that keeps the document true.

`logging_setup.log_event` refuses an event not in `ALL`, so a call site cannot reach a
log line with a name that was never registered — the enforcement is at the call, not
only in a test that happens to exercise it.
"""

from __future__ import annotations

#: A connection's state changed. The ordinary case: info, except a `stopped` reached by
#: a spent reconnect budget, which is the loudest thing the engine says and is error.
FEED_TRANSITION = "feed.transition"
#: A connection entered `degraded` — the staleness bound was crossed. Warning: this is
#: the "quiet market or dead feed" question this project exists to answer.
FEED_STALE = "feed.stale"
#: Anything that touches `reconnecting`: a socket closing, silence past the longer
#: bound, or an attempt being dialled. Warning, because a reconnect is always worth an
#: operator's attention even when it succeeds.
FEED_RECONNECT = "feed.reconnect"
#: The venue was asked what it lists and something was subscribed as a result. Info,
#: with the underlying, how many contracts were new and how many are now subscribed —
#: and **warning when the listing could not be read at all**, which is a gap in the
#: record rather than a failure of the feed. Issue #51 existed for six and a half hours
#: of one night's data because nothing anywhere said either sentence.
FEED_INSTRUMENTS = "feed.instruments"
#: A `store.BarStore.flush()` wrote a file. Info, with the table, the rows, the file and
#: the time it took — the numbers an operator wants after the three-day hole.
STORE_FLUSH = "store.flush"
#: A saved stream position had entries trimmed before it. Error, with both positions
#: and the exact number of entries that cannot be replayed.
STORE_REPLAY_GAP = "store.replay_gap"
#: Everything a `store` says about a checkpoint: a generation's files and checkpoint
#: became durable, info once per committed flush; the checkpoint one start-up adopted,
#: info exactly once per process start (#110); and a generation committed with zero rows
#: in all four tables, warning while recording and info while paused. One event name,
#: because all three are statements about one checkpoint and a second name would split
#: every "what did this generation do" query in two.
STORE_CHECKPOINT = "store.checkpoint"
#: A record was dropped off a **lossless** subscription. Error: `fanout.py` documents
#: this as impossible by construction, and a guard that never fires is cheap insurance
#: against the day the construction changes.
QUEUE_DROP = "queue.drop"
#: A new `(underlying, expiry)` pair joined the set this engine recomputes. Debug: rare
#: — once per expiry the venue ever lists — and useful only to someone reading closely.
COMPUTE_RECOMPUTE_SET = "compute.recompute_set"
#: A browser's `/ws/chain` socket connected. Debug: routine, and volume matters more
#: than any one connection.
WS_CLIENT_ATTACH = "ws.client_attach"
#: The other half of `WS_CLIENT_ATTACH` — the socket closed, however it closed.
WS_CLIENT_DETACH = "ws.client_detach"
#: An `events.Alert` was raised, on the bus and here beside it. Warning, regardless of
#: the alert's own `severity` field, which is a property of the alert and not of how
#: loudly the log should say so.
ALERT = "alert"
#: Which bus this process is running on, said once at start-up. Info: the fan-out and
#: Redis Streams behave identically through the seam, so the one moment anybody can tell
#: them apart is the line that says which was chosen — and a laptop pointed at the wrong
#: Redis is otherwise a silent misconfiguration. Also carries the queue's own resync,
#: which is a **warning**: a drop-oldest reader that jumped counts what it skipped here.
BUS_SELECTED = "bus.selected"
#: A bus reader's own lifetime: a read that raised and was retried, a reader that
#: recovered, and a reader that gave up or exited and is no longer consuming. Warning
#: while it is still running, error once it is not -- #103, where every reader in three
#: services died inside 34 seconds and the only record of it was one `engine.error`
#: apiece in a file no volume was mounted on.
BUS_READER = "bus.reader"
#: A catch-all for a defensive branch that should not run: an unexpected exception in a
#: background task, a watchdog that died, a config value that could not be honoured.
#: Always error or worse; never on a path this project expects to take.
ENGINE_ERROR = "engine.error"

#: Every name above, and the thing `docs/design/lld/logging-catalogue.md` is checked
#: against.
ALL = frozenset(
    {
        FEED_TRANSITION,
        FEED_STALE,
        FEED_RECONNECT,
        FEED_INSTRUMENTS,
        STORE_FLUSH,
        STORE_REPLAY_GAP,
        STORE_CHECKPOINT,
        QUEUE_DROP,
        COMPUTE_RECOMPUTE_SET,
        WS_CLIENT_ATTACH,
        WS_CLIENT_DETACH,
        ALERT,
        BUS_SELECTED,
        BUS_READER,
        ENGINE_ERROR,
    }
)
