# The store's view of its own consumption — the continuous gap check and `/health`

**Sibling of [store-replay.md](store-replay.md)**, split out by #103 rather than grown in
place, for the same reason [store-numbers.md](store-numbers.md) was. That note designs the
replay protocol; this one designs what the store watches about it while it is running.

On 2026-09-12 the store stopped consuming at 11:02:38Z, answered `GET /health` with
`{"status":"ok", ..., "replay_gap_entries":0}` for two hours, and had 97 minutes of market
data trimmed away unread in the meantime. Both facts below were reachable at any moment during
it, from two `XINFO` calls nobody was making.

## 1. The gap check is continuous, not a start-up step

Step 4 above runs once, inside `_prepare_process`. **A store that never restarts never runs
it**, which is why `replay_gap_entries` read `0` for two hours on 2026-09-12 through the exact
condition it was built to report. A replay gap is not an event that happens at start-up; it is
a condition that becomes true the moment retention passes the watermark.

`store_main.poll_bus` therefore asks every `STORE_BUS_MONITOR_INTERVAL_SECONDS` (10.0, the
cadence `store.state` already uses). One pipeline of `XINFO STREAM` and `XINFO GROUPS` per
stream gives both answers:

* **The replay gap, exactly and with no threshold.** The group's `last-delivered-id` older
  than the oldest entry the stream still holds means entries existed, were never delivered
  and are gone. `lost` is `(entries-added - length) - entries-read` -- step 4's arithmetic
  with the group's own counter in place of the saved index. **Counted every poll, alerted
  once** (`code="store.replay_gap"`): the condition stays true for as long as the store stays
  stopped -- 720 polls in the incident -- so the alert marks the transition and `/health`
  carries the standing signal. The counter still moves as the hole grows.
* **The lag, which does need a threshold.** `STORE_LAG_ALERT_ENTRIES` is `STORE_QUEUE_SIZE`,
  100,000 -- the lossless watermark this store already declares, not a new number. §6 has the
  derivation. Past it, `code="store.consumer_lag"`, once per excursion, with hysteresis.

## 2. What `GET /health` answers

**`"status": "ok"` was a literal.** It stayed `ok` through two hours in which the store read
nothing and 97 minutes of data were trimmed unread. The payload is now `ok` or `error`, and
**an `error` is served with HTTP 503**: Compose's check is `urlopen`, which fails on a status
and never reads a body, so a body saying "stalled" behind a 200 would leave `docker ps` green.

`status`, `problems` (every reason, in words), then `recording`, `generation`,
`buffered_rows`, `rows_written`, `replay_gap_entries`, `already_flushed`, `flush_errors`,
`readers`, `reader_exits`, `consumer_lag`, `lag_threshold`, `replay_gap_streams`,
`bus_checked_seconds_ago` and `bus_check_errors`.

A problem is a fact, never a judgement, with one exception: a reader not running, a task
exited, a watermark trimmed past, or the monitor itself silent for more than three intervals.
**The last one is this ticket's own lesson turned on the loop it just added** -- `/health`
does not take the monitor's word that it is alive. Only the lag bound is a judgement.

It reads no Redis. A health route that made a round trip would hang on exactly the Redis
whose sickness it exists to report, and Compose would read the timeout as the process being
down rather than the bus being behind.

