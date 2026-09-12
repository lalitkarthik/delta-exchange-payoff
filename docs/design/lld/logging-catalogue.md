# Structured log record catalogue

This is the record catalogue referenced by [logging.md](logging.md). Every heading is a
registered name in `deltapayoff.log_events.ALL`; the tests parse these headings so code and
prose cannot drift apart.

### `feed.transition`

Any ordinary connection move, including the first connection and a resume. Info except a
move that spends the reconnect budget and reaches `stopped`, which is error.

### `feed.stale`

The connection crossed the staleness bound and entered `degraded`. Warning, with
`conn_state: degraded`.

### `feed.reconnect`

Any move touching `reconnecting`, including a socket close, prolonged silence or a redial.
Warning because every reconnect needs attention.

### `feed.instruments`

The venue listing caused new contracts to be subscribed. Info with `underlying`, `listed`
and `subscribed`; a listing or subscribe failure is warning.

### `store.flush`

One `BarStore.flush()` writing one file. Info with `table`, `rows`, `file` and
`duration_seconds`; an empty flush writes no file and logs nothing.

### `store.replay_gap`

A saved stream position had entries trimmed before it. Error, emitted once per stream at
startup, carrying the stream, saved id, first retained id (both positions as times), and the
exact `lost` count.

### `store.checkpoint`

One committed generation became durable. Info, emitted once per generation, carrying the
generation, stream count and four `sealed_through_us` values. It is bounded by the flush
interval, exactly as `store.flush` is, not by message rate.

### `queue.drop`

A record dropped off a lossless subscription. Error; this is impossible under the current
construction but remains guarded.

### `bus.selected`

Which bus the process selected at startup. Info with Redis configuration when applicable.
The same name carries a warning resync record when a drop-oldest reader jumps over entries.

### `compute.recompute_set`

The set of pairs being recomputed changed. Debug, once per expiry or websocket interest
change, not on per-message dirty updates.

### `ws.client_attach`

A browser `/ws/chain` socket was accepted. Debug.

### `ws.client_detach`

A browser `/ws/chain` socket ended, from the handler's `finally`. Debug.

### `alert`

An `events.Alert` was raised. Warning regardless of its own severity, and logged before a
publish is attempted.

### `engine.error`

A defensive branch failed: an unexpected task, watchdog, flush or configuration error.
Always error; never an expected path.
