"""Every `event` name any part of the engine logs. One place, so a name cannot drift.

`docs/design/lld/logging.md` documents each one in prose; `tests/test_logging.py`
asserts the two lists are the same set. Add a name here and to that document together,
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
#: A `store.BarStore.flush()` wrote a file. Info, with the table, the rows, the file and
#: the time it took — the numbers an operator wants after the three-day hole.
STORE_FLUSH = "store.flush"
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
#: A catch-all for a defensive branch that should not run: an unexpected exception in a
#: background task, a watchdog that died, a config value that could not be honoured.
#: Always error or worse; never on a path this project expects to take.
ENGINE_ERROR = "engine.error"

#: Every name above, and the thing `docs/design/lld/logging.md` is checked against.
ALL = frozenset(
    {
        FEED_TRANSITION,
        FEED_STALE,
        FEED_RECONNECT,
        STORE_FLUSH,
        QUEUE_DROP,
        COMPUTE_RECOMPUTE_SET,
        WS_CLIENT_ATTACH,
        WS_CLIENT_DETACH,
        ALERT,
        ENGINE_ERROR,
    }
)
