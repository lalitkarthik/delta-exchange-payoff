# The bus reader — surviving a transient, and being seen when it does not

**Sibling of [redis-bus.md](redis-bus.md).** That note designs the bus; this one designs the
one thing inside it that can stop without anyone noticing. Split out by #103 rather than
grown in place, for the same reason [hld-evidence.md](../hld-evidence.md),
[logging-catalogue.md](logging-catalogue.md) and [store-numbers.md](store-numbers.md) were.

## 1. What happened, in one paragraph

At **11:02:41.028Z on 2026-09-12** a Redis socket read timed out inside `xreadgroup` in
`dxp-store`. `RedisBus._read` logged the traceback and **re-raised into an `asyncio` task
nobody was watching**. Every bus reader in all three services died the same way inside 34
seconds (`measured`, from the three preserved container logs). The processes stayed up,
`/health` kept answering `200 ok`, and 97 minutes of market data were trimmed away before
anything read them. `feed` kept publishing throughout for one reason only: its publisher loop
catches the same class of exception, logs *"the bus flusher raised; it keeps running"*, and
loops. **The two loops sat eleven lines apart in the same file and disagreed about what an
exception means.**

`self._readers` held a strong reference to each task, so a dead one was never garbage
collected and Python never printed even its own *"Task exception was never retrieved"*. The
one free safety net the language offers was held shut by the dictionary that owned the task.

## 2. The retry policy

A read pass is retried on any exception, **bounded by consecutive failures**, with the count
restored in full by one successful pass — the sense `controller-policies.md` C6 already gives
the reconnect budget.

| Knob | Default | Tag | Why |
|---|---|---|---|
| `read_retries` | 5 | `assumed` | `derived` 15.5 s of disturbance ridden out; covers a Redis restart (`measured` #61: answering again inside 2 s) and the 2.0 s socket timeout several times over |
| `read_retry_seconds` | 0.5 | `assumed` | doubling, no jitter — one reader per process per stream, so there is no herd to disperse (`controller-policies.md` C5) |
| `read_retry_ceiling_seconds` | 8.0 | `assumed` | 0.5+1+2+4+8 = `derived` 15.5 s over five attempts |

**Why bounded rather than symmetric with the publisher.** The publisher retries forever
because its alternative is worse: the batch it holds exists nowhere else, its outbox is
bounded and counted, and a publisher that stopped would lose the venue's own data. A reader
holds nothing — everything it has not read is still in the stream — so retrying forever buys
it nothing, and on a subscription that is genuinely broken rather than briefly unreachable it
spins silently while retention eats the backlog. That is this same incident reached by the
other road.

**Bounded retry is only safe because giving up is loud.** At the bound the reader raises an
`alert` with `code="bus.reader_stopped"`, logs at error under `bus.reader`, marks the
subscription not alive, and every stream it holds a position on begins reporting behind.
Without §3 and §4 below, bounding the retry would be strictly worse than retrying forever;
with them it is strictly better, because a reader that cannot recover is a fact an operator
should be told rather than one a counter should hide.

**Not retried: positioning and replay.** `_ensure_group`, `_position_at_head` and `_replay`
run once, before the retry driver, and a failure in any of them ends the reader. `_replay` is
not idempotent — re-running it from the top would re-deliver what it had already delivered,
which is #84 — so retrying it would trade a dead reader for a double fold. The exit is
escalated by §3. Making replay resumable is its own change.

## 3. Supervision

Every reader task is created through `RedisBus._start_reader`, which attaches an
`add_done_callback` in the same expression, so a task cannot exist unwatched. The publisher's
flusher gets the same treatment. The callback records the exit in `reader_exits()`, logs at
error, and raises `bus.reader_stopped` unless the reader had already announced that it gave
up. **A cancellation is not a death** — `aclose` and `unsubscribe` both cancel, and a
supervisor that cried wolf at every shutdown is one somebody turns off.

**It does not restart the reader, and that is a decision rather than an omission.** Re-entering
`_read` on a lossless subscription re-runs `_replay`, which reads forward from `start_ids` —
the position saved in the *checkpoint*, not the position this reader has since reached.
Everything between the two has already been delivered and folded, so an automatic restart
would re-fold it: #84 at the scale of the whole run. Rebasing `start_ids` onto the live
positions first would make a restart safe, and that is its own ticket with its own test.

## 4. `behind` is derived, never a cached flag

`behind` used to be a dict the read loop wrote and nothing else touched, initialised `False`.
When the loop died it froze at its last value — caught up — and the store's log clock
(`store-replay.md` §3) went on advancing on the wall clock over two hours of entries nobody
had read. Those minutes sealed empty. **A bar sealed empty is unrecoverable**: a seal closes a
minute so nothing more can enter it.

`RedisSubscription.behind_streams()` now answers from three things, in this order:

1. **Is the reader running?** `alive` is false before the task starts and after it ends.
2. **Has it come round recently?** Every pass stamps a monotonic reading *before* the read, so
   a pass that never returns ages. The bound is `max(5.0, socket_timeout × 2)` seconds —
   `derived` 5.0 s at the shipped 2.0 s socket timeout. A healthy pass cannot outlast the
   timeout on the socket it reads through.
3. **Was the last read a full batch?** The original signal, and still the right one for a
   reader that is running and merely trailing.

Either of the first two failing means **maximally behind**: every configured stream, at the
position where the reader stopped.

**The cost of the two mistakes is not symmetric, which is why the bound is tight.** A reader
wrongly called behind seals a minute late and the next pass corrects it. A reader wrongly
called caught up seals a minute empty, and nothing corrects that.

**A stream whose position is still `0-0` is left out**, and that is not a loophole. `0-0` is a
stream nobody has written to; its time is the Unix epoch, and handing the log clock a `min` of
zero would stop the store sealing anything ever again — a worse failure than the one being
repaired.

## 5. What it counts

`readers()` per subscription: `alive`, `supervised`, `lossless`, `retries` (consecutive, now),
`retries_total` (lifetime), `gave_up`, `failure` (the last one, as text), and `behind` (the
derived list). `reader_exits()` maps a task name to why it ended, including `bus-flush`.

`consumer_lag(subscription)` is the other half and keeps no state at all: one pipeline of
`XINFO STREAM` and `XINFO GROUPS` per stream, returning a `StreamLag` carrying Redis's own
`lag`, `entries-read`, `entries-added`, `length`, `last-delivered-id` and the oldest retained
id. `trimmed_past` is the exact replay-gap test — the group's position older than the oldest
surviving entry — and `lost` is `(entries-added − length) − entries-read`, the same arithmetic
`replay_gaps` uses at start-up with the group's own counter in place of the saved index.

## 6. Log records

All under the `bus.reader` event ([logging-catalogue.md](logging-catalogue.md)): **warning**
for a read that raised and is being retried, with the attempt number and the backoff, and for
a reader that recovered; **error** for a reader that gave up or exited, naming the
subscription and the failure that ended it.

## 7. What is not here

**No restart, no `XAUTOCLAIM`, no dead-letter.** The first is §3; the other two are
[redis-bus.md](redis-bus.md) §8 and unchanged by this.

**No reader-side alert throttling.** `bus.reader_stopped` is raised once per reader per exit,
and a reader exits once. The lag alert is throttled, and that one lives in the store —
[store-replay.md](store-replay.md) §5.
