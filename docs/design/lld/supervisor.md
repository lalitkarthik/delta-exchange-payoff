# Low-level design: the supervisor and the health report

**What is built inside `engine/src/deltapayoff/supervisor.py`.** The controllers it owns
are [controller.md](controller.md); what they do about a dropped socket is
[reconnect.md](reconnect.md). Landed by #39.

**Process boundary.** In split mode, only `deltapayoff.feed_main:app` constructs
`FeedSupervisor`; it owns the Delta adapters and their connection lifecycle. The feed exposes
only `GET /health`, which returns its local `FeedSupervisor.report`. The API has no local
controller: its `/health` and websocket badge use the `FeedConnectionCache` projection of
bus observations. With `DELTA_BUS` unset — the default — the supervisor, adapter and
consumers remain the unchanged in-process FanOut monolith.

## 1. What it is for

The whiteboard has many adapters behind one feed-management box, and **one feed health call
has to answer for the box.** Without something that owns them all, "is the feed up?" becomes
"is *an* adapter up?", and the answer depends on which adapter the caller happened to ask.
`FeedSupervisor.report` makes the aggregate state explicit.

## 2. What a supervisor is here

The Erlang idea, minus the language: a process whose only job is the lifecycle of the
processes under it. It does not do the work and it survives its children, which is the
point — something has to be alive to answer when a child is not.

**Ours restarts nothing, on purpose.** A controller already owns its own reconnect, with a
budget and a backoff; a supervisor that restarted one whose budget was spent would undo
the single decision that budget exists to make, and hammer the venue while doing it. What
this one adds is ownership, aggregation and a shutdown that actually detaches.

## 3. Liveness and readiness

The feed's `/health` is `FeedSupervisor.report`: `status` is process liveness and `feed` is
readiness, the worst local controller state. The API's `/health` keeps the same liveness
field, but in split mode its per-adapter state is authoritative from the last observed
`feed.connection` and `heartbeat` on the filtered `feed-state` subscription. The websocket
badge reads that same projection. A missing observation remains `null` in its additive age
fields; heartbeat silence can synthesize `stopped` / `silent` rather than reporting a
healthy default.

## 4. The order, from best to worst

`SEVERITY`, written as an explicit tuple rather than left to the enum's declaration order,
so that adding a sixth state cannot silently change what "worst" means:

| Rank | State | Why here |
|---|---|---|
| 1 | `connected` | Data is arriving. |
| 2 | `degraded` | Has delivered and has gone quiet. Stale, not absent. |
| 3 | `connecting` | Has delivered nothing yet. |
| 4 | `reconnecting` | Known to be down. |
| 5 | `stopped` | Not trying. |

Two readings a later reader may disagree with, so both are written down:

- **`degraded` above `connecting`**, because a degraded connection has delivered data and
  merely gone quiet, while a connecting one has delivered none at all.
- **A controller that has never started reports `stopped`, not `null`.** A connection
  nobody started is not running, which is what `stopped` means; `null` on the wire would
  be a sixth state every reader had to invent a rule for.

**An empty supervisor reports `stopped`,** which is the case an `any()` or a bare `max()`
gets backwards. A process with no feed at all is the strongest possible "not ready", and
reporting `connected` because there was nothing to disagree is the plausible-and-wrong
answer.

## 5. The report shape — fixed here, read by #40, #41, #44 and #64

`models.HealthReport` and `models.AdapterHealth`. `GET /health`:

```json
{
  "status": "ok",
  "feed": "degraded",
  "adapters": [
    {
      "adapter": "DELTA",
      "state": "degraded",
      "last_message_at": "2026-09-07T18:31:04.512000+00:00",
      "last_message_age_seconds": 20.0,
      "reconnects": 2,
      "budget_remaining": 8,
      "transitions": 7,
      "empty_opens": 0,
      "undecodable": 0,
      "state_learned_at": null,
      "state_age_seconds": null,
      "last_connection_at": null,
      "last_connection_age_seconds": null,
      "last_heartbeat_at": null,
      "last_heartbeat_age_seconds": null
    }
  ]
}
```

| Field | Rule |
|---|---|
| `status` | Liveness. Always `"ok"`. The shape this route used to be, kept whole. |
| `feed` | Readiness. The worst state among the adapters, by §4. |
| `adapters` | **A list, not a map keyed by name.** The order is the configured one rather than a dictionary's. |
| `last_message_at` | **Derived from the age at request time**, not stamped per message. The controller measures on a monotonic clock, the only kind a staleness bound can be measured on; taking a `datetime` on a `measured` 1,322.9 messages/second hot path to avoid one subtraction per request is the wrong trade. |
| `last_message_age_seconds` | In feed and monolith, the controller's age of the last venue message. In split API health, it starts with the latest heartbeat payload's venue-message age and advances with the API-observation age of that heartbeat. It is not the heartbeat-observation age; `null` means the controller has seen no venue message. |
| `state_learned_at` | **Additive.** When the API observed the event that currently supplies `state`; `null` before a state event is observed. For synthesized `stopped` / `silent`, it is the stale-bound crossing time. |
| `state_age_seconds` | **Additive.** Age since `state_learned_at`; `null` when `state_learned_at` is `null`. This is the age of the API's effective-state observation. |
| `last_connection_at` | **Additive.** When the API observed the newest `feed.connection`; `null` if none arrived. |
| `last_connection_age_seconds` | **Additive.** Age since `last_connection_at`; `null` before the API observes a `feed.connection`. |
| `last_heartbeat_at` | **Additive.** When the API observed the newest `heartbeat`; `null` if none arrived. |
| `last_heartbeat_age_seconds` | **Additive.** Age since `last_heartbeat_at`; `null` before the API observes a `heartbeat`. It is distinct from `last_message_age_seconds`, which comes from the heartbeat payload and describes venue-message silence. |
| `reconnects` | Times this connection has **dropped** — incremented in `connection_closed` beside the budget it spends, not on entry to `reconnecting`, which the watchdog can reach without a drop. A count, not a rate: a reader comparing two polls gets the rate, and a rate computed here needs a window nobody agreed on. In split API health this controller-only counter is `null`; it remains populated in feed and monolith reports. |
| `budget_remaining` | Reconnects left. A feed two drops from `stopped` and one that has never dropped are otherwise the same green badge. This controller-only counter is `null` in split API health and remains populated in feed and monolith reports. |
| `transitions` | Every state change since start. A connection flapping between `connected` and `degraded` appears here and in no other field. This controller-only counter is `null` in split API health and remains populated in feed and monolith reports. |
| `empty_opens` | Sockets opened with **nothing subscribed** — guaranteed to deliver nothing, the failure with no error. |
| `watched` | **Engine health only, not the supervisor's report.** What the 100 ms live solve is running for — a pair per open `/ws/chain` connection, plus any inside its grace. Built in the route from `ChainStream.watching()`; empty is ordinary and **not** a fault. `grace_remaining_seconds` is `null` while anyone is watching. See `docs/design/lld/chain-cache.md` §5. |
| `undecodable` | Frames that parsed as JSON and then made no sense. `delta.py` logs the first and counts the rest, and its own comment asked for this field: a systematic decode bug zeroes the event stream while the message counter climbs. It is `null` when the API has no local adapter to count. |

Both counters are **`null`, never `0`, when the adapter keeps none.** Each is a counter
whose entire signal is being *above* zero, so a reader watching for that has to be able to
tell "still nought" from "nobody is counting".

**No field says `healthy`.** Whether a state is acceptable is the reader's judgement — a
badge, an alert rule, an operator — and one boolean here would fix one of those readings
for all three.

## 6. The two routes and remote observations

In split mode the feed route is only `GET /health`, and it returns the feed process's local
`FeedSupervisor.report`. The API route also answers `GET /health`, but its adapter rows are
the authoritative remote projection: the latest `feed.connection` and `heartbeat` observed
on `feed-state`, with their observation ages. The API has no local supervisor to query.

The first heartbeat after an API start is the late-start mechanism. Its carried `state`
hydrates an adapter even when the API missed the feed's earlier transition; no feed-side
change is needed. If the latest heartbeat's API-observation age crosses
**`FEED_HEARTBEAT_STALE_SECONDS = 25.0` (`derived`)**, the API synthesizes
`stopped` / `silent`. Heartbeats are emitted at `controller.HEARTBEAT_SECONDS = 10.0`
(a setting in the code, not an assumption), so the bound tolerates two missed intervals plus `derived`
five seconds of slack. A fresh heartbeat restores its carried state.

The command route remains on the API. It publishes `control.command`; feed consumes it and
dispatches it through its local supervisor. Feed exposes no command HTTP route.

## 7. Lifecycle

| Phase | What happens |
|---|---|
| Split feed / Build | `feed_main` constructs `FeedSupervisor` over the configured adapters. Redis must be ready first; `BusUnavailable` stops startup before the venue opens. |
| Split feed / Start | After every contract is subscribed, one task per controller runs under the feed supervisor, named `feed-<venue>`. |
| Split engine | `main` constructs `ChainStream`, `BarWriter` and `FeedConnectionCache`; it constructs no `FeedSupervisor` and opens no Delta socket. The cache attaches one filtered, drop-oldest `feed-state` subscription for `feed.connection` and `heartbeat`, then drains it. |
| Split feed control | `feed_main` attaches one filtered `feed-control` subscription for `control.command`; its consumer calls `FeedSupervisor.dispatch_command`. |
| Default | With `DELTA_BUS` unset, `main` keeps the existing supervisor and consumers together in the in-process FanOut monolith, and the cache remains updated by the synchronous publish wrapper. |
| Stop | `stop_feed_stack` awaits `aclose()` **before** cancelling the other tasks. |

In the feed process, `aclose()` stops every controller, cancels its task, and then **detaches every one of
them**. The detach is not tidiness: a controller left registered goes on being told about
a socket it no longer owns, and answers a signal it should never have seen by raising
`IllegalTransition` inside a socket reader that does not belong to it. `run()` detaches on
its own way out, but a supervisor closed before it ever started, or one cancelled before
`run` reaches its `finally`, has controllers `run()` never spoke for. `detach()` is
idempotent precisely so this can be unconditional.

In split mode, a live controller's `feed.connection`, `heartbeat`, `alert` and market-data
events reach the Redis publisher. The API's filtered `feed-state` reader takes only the two
feed-state event types; the feed's filtered `feed-control` reader takes only
`control.command`. In default mode the same events remain on FanOut.

## 8. Failure modes

| Failure | What happens |
|---|---|
| One adapter degraded, one connected | `feed` reports `degraded`. Each adapter's own state is in its own row. |
| A controller gives up (budget spent) | Its row reads `stopped`, `feed` reads `stopped`, and the alert it published is on the bus. **No restart.** |
| API starts after the feed | The first observed heartbeat hydrates the adapter's state, even if the API missed the earlier `feed.connection`. |
| Heartbeat silence crosses the stale bound | API health and the badge synthesize `stopped` / `silent`; a fresh heartbeat recovers the carried state. |
| No supervisor at all in the API process | The API still reports liveness and only the remote observations it has; it does not invent local controller counters or a healthy feed. |
| `aclose()` twice | Nothing raises. A lifespan that fails part way tidies up on both paths. |
| An adapter with no socket owner | `empty_opens` is `null`. Controller-only counters are `null` in API health and remain populated in feed/monolith health. |
| `watched` in the supervisor report | It is absent by design. Watched pairs belong only to the API's engine health route, never to `FeedSupervisor.report`. |

## 9. The seam the tests drive

`tests/test_supervisor.py` drives the aggregate on **pairs** of scripted fakes, held in
different states by hand, because worst-rather-than-first, worst-rather-than-best and
empty-reads-as-green are all invisible until there are two. Split-mode health tests use a
fake bus and a fake clock: they publish only filtered `feed.connection` and `heartbeat`
events into `feed-state`, advance API-observation age across the stale bound, and verify
late-start hydration and fresh-heartbeat recovery. The command consumer has its own
filtered `feed-control` seam.

The route is exercised at seam 1 — the app under `TestClient` with `DELTA_LIVE_FEED=0` —
because the health shape, remote projection and command acknowledgement are contracts
read through HTTP, not private supervisor details. No seam opens a network connection.
