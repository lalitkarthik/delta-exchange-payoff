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
signature is `(code, adapter, severity)`. `assumed` (ticket #66, set by the
orchestrator before dispatch) `ALERT_MIN_POST_INTERVAL_SECONDS = 2.0` is a global floor
between any posts; `assumed` (ticket #66, the same)
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
| Discord blocked | A `429` sets the local block for the returned delay; the message is dropped, never retried, and the signature's collapse window stays spent. |
| Failed post | A transport exception, a `5xx` or any other non-`2xx` is logged and dropped; a `5xx` does not extend a `429` block, and the signature's collapse window is rolled back. |

`post_alert` returns a `PostOutcome`, not a boolean. Until #115 it returned `True` for "the
HTTP seam was called" and `AlertConsumer` read it as "Discord has this" — the same answer for
every outcome but a failed post, the one that matters. `DELIVERED` (`2xx`) and `RATE_LIMITED`
(`429`, which Discord answered) have `spends_collapse_window`; `FAILED` (a transport exception,
a `5xx`, any other status) and `NOT_ATTEMPTED` (no webhook, or a locally blocked call) do not,
and only the last has `reached_discord` false.

The webhook URL is never included in a log message. The one-time unconfigured startup line
names `DISCORD_WEBHOOK_URL` and says that alerts will be consumed and acknowledged but not
posted.

## 3. Failure modes

| Failure | Behaviour |
|---|---|
| Repeat inside the collapse window | Drop and increment that signature's suppressed count; do not queue a later flush. |
| Different signature inside the local floor | Drop and increment `rate_limited_count`; do not mark it as a post. |
| `derived` (ticket R5) Discord status `429` | Read `retry_after` or use the `assumed` `1.0`-second fallback, then block and drop. `commit_post()`: Discord saw this request and named the backoff, so the window stays spent. |
| `derived` (ticket R5) Discord status `5xx` | Log at error and drop without changing `_blocked_until`. `rollback_collapse_window()`. |
| HTTP exception | Log its safe exception type plus the alert it dropped (severity, code, adapter, folded count) at error, never the URL, then continue. `rollback_collapse_window()`, so the *next* occurrence of that signature is posted rather than folded into a post that never happened (#115). See §3a and §3b. |
| An alert timestamped before this consumer instance started | Dropped in `_run_once` before the gate sees it, so it cannot spend a real alert's collapse or rate-limit slot; logged at warning under `ALERT`. |
| Feed pause or any non-`Alert` queue item | Consume it **silently** and do not call Discord; a pause is not an alert ([events.md](../events.md), "`degraded` does not alert, and nor does a `pause`"). The silence is the observable: with `_run_once`'s `isinstance` guard removed the pause still produces no post, because the dispatch body raises `AttributeError` and the handler swallows it. Both pause tests passed that way until 2026-09-12, so they now assert that nothing was logged. |

## 3a. An acked alert whose post fails is gone, and that is the trade

This consumer acknowledges on receipt and never replays. Every row of §3 therefore ends in
`drop`: no pending entry to come back to, no retry behind the HTTP call, no second chance.
**An alert that is acked and then fails to deliver is lost.**

That is deliberate and it is the right trade here. The alternative -- holding the entry unacked
until Discord confirms -- makes a Discord outage into a stalled consumer with a growing pending
list, precisely the failure #103 spent two hours on in a different service. An alert is a
notification, not a durable record; the durable record is the log and the Parquet store, and
neither depends on this consumer. A notification that arrives late enough is worth nothing
anyway, so trading delivery for liveness is the correct direction.

**The trade is only defensible if the loss leaves a record, and for one live failure it did
not.** `measured` on the live `dxp` stack, 2026-09-12: seven alerts were published, and
`XINFO GROUPS alert` reported `entries-read 7`, `pending 0`, `lag 0` -- all seven consumed
and acked. Three reached the poster; four were folded by the collapse rule. Of the three, two
returned `2xx` and were delivered, and one raised `ConnectTimeout`. The only trace it left
was:

```
16:13:45 ERROR deltapayoff.discord_alerts engine.error: Discord webhook post failed:
ConnectTimeout
```

The alert it lost was `reconnect_budget_spent` -- the one saying the venue connection would
not come back without a resume. The feed then sat dead for **10.9 minutes** (`derived`: no
`md.option_quote:DELTA:BTC` entry between 16:06:35.432Z and 16:17:27.333Z) until a person
restarted it at 16:17:24Z, having found out some other way. No reader of that log line could
have known which alert died, because it named the exception type and nothing else.

So every drop path now names what it dropped: severity, code, adapter, and the folded
`collapsed_count` that dies with it, through `DiscordPoster._lost`. Only engine-generated
fields go in -- never `detail`, never the URL, which is the rule the exception branch already
existed to honour. Pinned by five tests in `test_discord_alerts.py`
(`test_a_failed_post_names_the_alert_it_lost` and siblings) and end to end by
`test_the_live_connect_timeout_loses_its_alert_and_says_which_one`.

**Two consequences were accepted rather than fixed** by #66. One has since been fixed and
one still stands:

1. **A failed post spent the occurrence too — fixed by #115.** `post_alert` reported the
   exception path as *attempted*, so the consumer called `commit_post()`, the collapse window
   restarted from a post nobody saw, and the message eventually carrying it rendered
   `(collapsed N times since last post)` about that post. Losing the message is the trade;
   losing the next five minutes of that signature was not. `rollback_collapse_window()`
   un-spends the signature's timestamp and **nothing else**: the global
   `ALERT_MIN_POST_INTERVAL_SECONDS` floor stays spent, because the request was made and
   refunding it would leave this consumer with no rate limit during exactly the outage that
   produces alert storms; the folded count `decide()` popped stays gone, because it was
   rendered into the failed message and `_lost` logged it as lost. A retry is still refused.
2. **Folded repeats are revealed only on the next occurrence of their signature.** After the
   live run above the gate still held three folded `store.replay_gap` repeats and one
   `bus.reader_stopped` repeat; if neither signature recurs, nothing ever says they happened.
   Pinned by `test_the_four_folded_live_alerts_are_still_unreported_after_the_run`, and
   unchanged by #115, which `test_rolling_back_a_failed_post_does_not_resurrect_the_folded_count` pins.

## 3b. The alert channel failed for the same reason as the thing it reported

**Not fixed here, and recorded so it is not mistaken for an oversight.** That `ConnectTimeout`
landed five seconds after the budget was spent at 16:13:40Z, inside the name-resolution outage
#108 records from 16:07:52Z: the network event that produced the alert is the one that stopped
it being delivered, and a channel failing in the conditions that generate its traffic is not a
channel. No bookkeeping inside this process changes that — a bounded retry would have dialled
the same dead resolver, and a second channel over the same egress would have failed with it.
The answers that could work, independent egress or a watchdog on this consumer's *silence*,
are outside this service; #108 criterion 7 owns them, and §3c is what #115 owns.

## 3c. `/health` answers a question about delivery

`configured` and `alive` stay, true and useful -- and both were true at 16:44Z on 2026-09-12,
thirty-one minutes after the only log record in the container was a failed post: `CONTEXT.md`
§7 item 2's shape, a configuration fact where a judgement belongs. The route now carries
`delivered`, `failed` and `blocked` counters and a `last_post` outcome, and answers **503**
when `problems` is non-empty -- the consumer task is not running, or the latest attempt was
`FAILED`. The judgement is that latest attempt, not `failed`, which only goes up and would read
as broken forever after one timeout; a `429` is not a problem, because Discord received it and
named its own backoff. The status code is the contract (#103): Compose's check is `urlopen`,
which fails on a status and never reads a body. Nothing restarts an unhealthy
`discord-alerts`, so the 503 is a signal, not a restart loop.

## 4. Test seam and numbers

`engine/tests/test_discord_alerts.py` drives the gate and poster with explicit float
timestamps and a fake `post_fn`; no socket is opened. #115's outcome and rollback tests live
there, and its end-to-end pair (`test_a_failed_post_leaves_the_next_occurrence_free_to_post`,
`test_a_discord_429_still_spends_the_collapse_window`) in `engine/tests/test_alert_consumer.py`,
which drives the bus seam with FanOut and, for the Redis group cases, with fakeredis and a real
Redis container in parallel parametrisations, all with an injected fake clock. The entrypoint
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

All timing figures above are `assumed`, chosen by the orchestrator before dispatch on ticket
#66 and never since measured against a real alert rate; the stream, status, and queue
behaviours are `derived` from the landed code and the tests. The first live alert rate this
repository has observed is in [local-stack-numbers.md](../cloud/local-stack-numbers.md): seven
alerts in twenty-one minutes, 2026-09-12.
