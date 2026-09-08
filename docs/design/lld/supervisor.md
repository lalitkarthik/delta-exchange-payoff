# Low-level design: the supervisor and the health report

**What is built inside `engine/src/deltapayoff/supervisor.py`.** The controllers it owns
are [controller.md](controller.md); what they do about a dropped socket is
[reconnect.md](reconnect.md). Landed by #39.

## 1. What it is for

The whiteboard has many adapters behind one feed-management box, and **one health call
has to answer for the box.** Without something that owns them all, "is the feed up?"
becomes "is *an* adapter up?", and the answer depends on which one the caller happened to
ask — so a process with Delta connected and a second venue stopped for an hour reports
green, truthfully, about the half of itself that works.

## 2. What a supervisor is here

The Erlang idea, minus the language: a process whose only job is the lifecycle of the
processes under it. It does not do the work and it survives its children, which is the
point — something has to be alive to answer when a child is not.

**Ours restarts nothing, on purpose.** A controller already owns its own reconnect, with a
budget and a backoff; a supervisor that restarted one whose budget was spent would undo
the single decision that budget exists to make, and hammer the venue while doing it. What
this one adds is ownership, aggregation and a shutdown that actually detaches.

## 3. Liveness and readiness

`/health` answered `{"status": "ok"}` and meant **liveness**: the process is up and served
you. It was read as **readiness**: market data is flowing. Those come apart for hours — a
process whose socket died at 02:00 answers `ok` all night — and the gap is exactly where a
silent failure lives.

So the report carries both. `status` is unchanged, still liveness, still always `"ok"`;
`feed` beside it is readiness. **Nothing that reads the old field breaks, and nothing that
reads the new one is lied to.**

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

## 5. The report shape — fixed here, read by #40, #41 and #44

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
      "undecodable": 0
    }
  ],
  "watched": [
    {
      "underlying": "BTC",
      "expiry": "11-09-2026",
      "viewers": 2,
      "grace_remaining_seconds": null
    },
    {
      "underlying": "ETH",
      "expiry": "11-09-2026",
      "viewers": 0,
      "grace_remaining_seconds": 21.418
    }
  ]
}
```

| Field | Rule |
|---|---|
| `status` | Liveness. Always `"ok"`. The shape this route used to be, kept whole. |
| `feed` | Readiness. The worst state among the adapters, by §4. |
| `adapters` | **A list, not a map keyed by name.** #44 adds the watched set: extending a record is not changing what a key means, and the order is the configured one rather than a dictionary's. |
| `last_message_at` | **Derived from the age at request time**, not stamped per message. The controller measures on a monotonic clock, the only kind a staleness bound can be measured on; taking a `datetime` on a `measured` 1,322.9 messages/second hot path to avoid one subtraction per request is the wrong trade. |
| `last_message_age_seconds` | `null` when nothing has ever arrived — an unknown age, not an age of zero. `last_message_at` is `null` with it. |
| `reconnects` | Times this connection has **dropped** — incremented in `connection_closed` beside the budget it spends, not on entry to `reconnecting`, which the watchdog can reach without a drop. A count, not a rate: a reader comparing two polls gets the rate, and a rate computed here needs a window nobody agreed on. |
| `budget_remaining` | Reconnects left. A feed two drops from `stopped` and one that has never dropped are otherwise the same green badge. |
| `transitions` | Every state change since start. A connection flapping between `connected` and `degraded` appears here and in no other field. |
| `empty_opens` | Sockets opened with **nothing subscribed** — guaranteed to deliver nothing, the failure with no error. |
| `watched` | **#44's addition, and not the supervisor's.** What the 100 ms live solve is running for — a pair per open `/ws/chain` connection, plus any inside its grace. Built in the route from `ChainStream.watching()`, because the supervisor owns connections and what is being solved is not a property of any adapter. Empty is the ordinary state of an engine recording with no browser open and is **not** a fault: every listed expiry is still solved once a minute by the pass that fills the store. `grace_remaining_seconds` is `null` while anyone is watching — no countdown is running, and `0` would read as one that had just finished. `docs/design/lld/chain-cache.md` §5. |
| `undecodable` | Frames that parsed as JSON and then made no sense. `delta.py` logs the first and counts the rest, and its own comment asked for this field: a systematic decode bug zeroes the event stream while the message counter climbs. |

Both counters are **`null`, never `0`, when the adapter keeps none.** Each is a counter
whose entire signal is being *above* zero, so a reader watching for that has to be able to
tell "still nought" from "nobody is counting".

**No field says `healthy`.** Whether a state is acceptable is the reader's judgement — a
badge, an alert rule, an operator — and one boolean here would fix one of those readings
for all three.

## 6. A process with no feed still answers

`get_supervisor()` returns `None` rather than raising when the lifespan never ran, and the
route answers `{"status": "ok", "feed": "stopped", "adapters": []}`. **This is the
opposite call to `/recording`'s 503, and deliberately.** `/health` is what a monitor hits
to find out whether anything is wrong; a health check that fails because there is no feed
to describe tells the monitor the engine is down when it is up and merely not recording.

## 7. Lifecycle

| Phase | What happens |
|---|---|
| Build | `main.build_feed_stack` constructs the supervisor over the configured adapters. **Nothing connects.** Constructing a controller registers it on its adapter, so they are listening from here. |
| Start | `main.start_feed_stack`, after every contract is subscribed. One task per controller, named `feed-<venue>`, each with a done-callback that logs at error if it ends on its own. |
| Stop | `stop_feed_stack` awaits `aclose()` **before** cancelling the other tasks. |

`aclose()` stops every controller, cancels its task, and then **detaches every one of
them**. The detach is not tidiness: a controller left registered goes on being told about
a socket it no longer owns, and answers a signal it should never have seen by raising
`IllegalTransition` inside a socket reader that does not belong to it. `run()` detaches on
its own way out, but a supervisor closed before it ever started, or one cancelled before
`run` reaches its `finally`, has controllers `run()` never spoke for. `detach()` is
idempotent precisely so this can be unconditional.

**The feed is no longer a task of `main`'s own.** It runs through the supervisor, which
means a live controller's `feed.connection`, `heartbeat` and `alert` events reach the same
bus the market data does — the wiring #38 built and did not connect.

## 8. Failure modes

| Failure | What happens |
|---|---|
| One adapter degraded, one connected | `feed` reports `degraded`. Each adapter's own state is in its own row. |
| A controller gives up (budget spent) | Its row reads `stopped`, `feed` reads `stopped`, and the alert it published is on the bus. **No restart.** |
| No supervisor at all | The report is still given: `feed` `stopped`, no adapters. §6. |
| `aclose()` twice | Nothing raises. A lifespan that fails part way tidies up on both paths. |
| An adapter with no socket owner | `empty_opens` is `null`. Every other field is answered by the controller and is always present. |

## 9. The seam the tests drive

`tests/test_supervisor.py`. The aggregation is driven on **pairs** of scripted fakes, held
in different states by hand, because a supervisor over one controller is indistinguishable
from no supervisor at all: worst-rather-than-first, worst-rather-than-best and
empty-reads-as-green are all invisible until there are two. The route is exercised at
seam 1 — the app under `TestClient` with `DELTA_LIVE_FEED=0` — because the shape here is
the contract #40, #41 and #44 read through, and a shape asserted anywhere else is a shape
they do not get.
