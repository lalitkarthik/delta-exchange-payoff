# Discord alerts — the consumer and the webhook boundary

This is the low-level design for issue #66. The event contract is [events.md](events.md),
the broker contract is [redis-bus.md](redis-bus.md), and the owning high-level boundary is
[hld.md](../hld.md). The row for this design is [index.md](index.md).

## 1. Types and invariants

`AlertConsumer` reads only `Alert` events. On Redis it calls `RedisBus.subscribe` with
`event_types=("alert",)`, `lossless=True`, `group_start="$"`, and
`GROUP_NAME = "discord-alerts"`; `SUBSCRIBE_MAXSIZE = 100` is the
`assumed` (ticket #66) lossless watermark, with generous headroom for the catalogue's
rare alert codes. The group name is one word, with no venue or environment, as required by
the bus naming rule.

`group_start="$"` makes a newly created group begin at the stream tail, so an alert already
present before this process starts is not replayed. The tail position is supplied through the
landed bus API rather than a second Redis client: the earlier proposed
`reposition_group_at_tail` helper and `XGROUP SETID` are deliberately not used. An existing
group reads with `>` and therefore receives new entries without adding replay logic here.
This is intentionally different from the store's lossless replay-from-last-flush policy in
[redis-bus.md](redis-bus.md) §2: a stale Discord notification is not a durable record.

**#66's "no replay" is enforced by the consumer, not by the bus.** `group_start="$"` only
keeps a *newly created* group off the backlog. Rejoining an existing group — this process
restarting — reads with `>` from wherever that group's `last-delivered-id` already sits,
which is everything published while nothing was reading, up to the retention window. So
`AlertConsumer.subscribe()` records its own start time and `_run_once` drops, before the gate
sees it, any alert whose `ts_received` predates that time. A restart therefore still
*receives* everything Redis held, and *posts* none of it; only an alert published after this
instance started can reach Discord. Audited as #86, which reproduced three alerts published
during a consumer's downtime being delivered to its replacement.

RedisBus's existing group reader acknowledges each batch before it calls this consumer's
queue offer. Acknowledgement therefore happens inside the bus read path, before alert code
sees an entry; this service adds no acknowledgement logic of its own. The FanOut path uses
the same lossless queue but has no broker-side event filter, so the consumer's `isinstance`
check is also the pause guard.

The drop happens **ahead of** the gate deliberately: a stale alert that reached the gate
would spend a real alert's collapse slot or its rate-limit slot, so the first genuine alert
after an outage could be silently swallowed by the noise of the outage itself.

`AlertGate` returns the frozen `GateDecision(post, collapsed_count, rate_limited)`. A
signature is `(code, adapter, severity)`. `assumed` (ticket #66, pending orchestrator
confirmation) `ALERT_MIN_POST_INTERVAL_SECONDS = 2.0` is a global floor between any posts;
`assumed` (ticket #66, pending orchestrator confirmation)
`ALERT_COLLAPSE_WINDOW_SECONDS = 300.0` folds repeats of one signature into a running count.
The collapse counter and `rate_limited_count` are separate observations: collapse is checked
first, while the local floor is global and never resets a signature's post timestamp.

`DiscordPoster` receives an async `post_fn(url, payload)` seam. It renders
`[{severity}] {code} -- {adapter}: {detail}`, using `engine` for a null adapter and adding
`(collapsed N times since last post)` only when a later post releases a folded count. An
`assumed` (ticket #66) `1.0`-second fallback is used when a `429` body has no usable
`retry_after`.

## 2. States

| State | Change and next action |
|---|---|
| Unconfigured | The webhook is empty; alerts are consumed and acknowledged but no HTTP call is made. |
| Ready | A configured consumer may ask `AlertGate` and call the poster. |
| Locally blocked | The poster skips calls until its fake-clock `now` reaches `_blocked_until`. |
| Discord blocked | A `429` sets the local block for the returned delay; the message is dropped, never retried. |
| Failed post | A transport exception or `5xx` is logged and dropped; a `5xx` does not extend a `429` block. |

The webhook URL is never included in a log message. The one-time unconfigured startup line
names `DISCORD_WEBHOOK_URL` and says that alerts will be consumed and acknowledged but not
posted.

## 3. Failure modes

| Failure | Behaviour |
|---|---|
| Repeat inside the collapse window | Drop and increment that signature's suppressed count; do not queue a later flush. |
| Different signature inside the local floor | Drop and increment `rate_limited_count`; do not mark it as a post. |
| `derived` (ticket R5) Discord status `429` | Read `retry_after` or use the `assumed` `1.0`-second fallback, then block and drop. |
| `derived` (ticket R5) Discord status `5xx` | Log at error and drop without changing `_blocked_until`. |
| HTTP exception | Log only its safe exception type at error, never the URL, then continue. |
| An alert timestamped before this consumer instance started | Dropped in `_run_once` before the gate sees it, so it cannot spend a real alert's collapse or rate-limit slot; logged at warning under `ALERT`. |
| Feed pause or any non-`Alert` queue item | Consume it and do not call Discord; a pause is not an alert. |

## 4. Test seam and numbers

`engine/tests/test_discord_alerts.py` drives the gate and poster with explicit float
timestamps and a fake `post_fn`; no socket is opened. `engine/tests/test_alert_consumer.py`
drives the bus seam with FanOut and fakeredis, also with an injected fake clock. The entrypoint
test starts `uvicorn` as a subprocess on loopback and checks `/health` in the unconfigured
case; this is the only process-launch test. `engine/tests/test_alert_main.py` also checks
that the committed `stack.env` value is empty and the real secret belongs in the optional,
git-ignored `stack.local.env` overlay.

Two tests pin the restart seam specifically, both parametrised over fakeredis **and**
Docker-Redis. `test_alerts_published_while_no_consumer_ran_are_dropped_as_stale` publishes
with nothing reading, rejoins the group, and asserts one post — the fresh alert, carrying no
collapse suffix, which is what pins the rate-limit and collapse rules across the restart.
`test_an_existing_alert_group_can_be_joined_again_without_a_busygroup_failure` asserts the
rejoined reader **task is still alive** and delivers, not merely that no exception reached the
test body: the bus sets `positioned` in a `finally`, so a reader that died while positioning
still looks ready, and the old form of that test passed with `_ensure_group` raising
`BUSYGROUP` unconditionally (#91).

All timing figures above are `assumed` from ticket #66 pending orchestrator confirmation;
the stream, status, and queue behaviours are `derived` from the landed code and the tests.
