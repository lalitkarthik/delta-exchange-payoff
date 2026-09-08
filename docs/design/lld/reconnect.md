# Low-level design: reconnect ownership

**Who dials, who waits, and who gives up.** Split out of
[controller.md](controller.md) in #39, when moving backoff and the lifetime budget up out
of the socket owner would have pushed that design past its 200-line bound. The state
machine those moves feed is still controller.md; the socket they dial is
[adapter.md](adapter.md).

## 1. What moved, and why it had to

Until #39 the reconnect loop was a `while` inside `DeltaFeed.run`:

```
while not stopping and consecutive_failures <= max_retries:
    connect, replay, pump
    ...
    await sleep(delay)
```

It was correct and it was tested. It moved because **the most important thing that loop
decided — *we have given up* — was a loop condition one layer below the state machine
that exists to describe the connection.** Nothing could observe it, nothing could publish
it, and `/health` said `ok` on either side of it. A controller cannot own a state machine
whose central transition is made by code it cannot see.

**Every value moved unchanged.** The rules did not change; the code that runs them did.

| Number | Value | Tag | Where from |
|---|---|---|---|
| `retry_delay` | 1.0 s | `assumed` | The first wait after a drop. Was `delta_socket.RETRY_DELAY_SECONDS` |
| ceiling on the wait | 60.0 s | `assumed` | Doubling stops here. One dial a minute through a venue outage, and back within a minute of its return. Was `delta_socket.MAX_RETRY_DELAY_SECONDS` |
| `reconnect_budget` | 10 | `assumed` | The lifetime budget. Was `delta_socket.MAX_RETRIES` |
| Attempts before the budget existed | **21 in 0.3 s** with a budget of 3 | `measured`, 2026-09 | The bug this budget exists against, recorded in `test_feed.py` |
| Delta's connection allowance | 150 per 5 minutes | `measured`, `tools/probe_api.py` | Why an unbounded retry is not merely untidy |

## 2. The two rules the budget keeps

**Spent on a drop, restored in full by a message.** Not by a connection opening.

- A cumulative counter that never resets looks correct and dies after a month: OpenAlgo's
  comment records a long-lived feed that reconnects once a day silently exhausting a
  lifetime budget and never coming back, with no failure anywhere to point at.
- Restoring on the socket merely **opening** is the same bug inverted, and worse. Delta
  can accept a handshake and close immediately — a rejected subscribe, a throttled IP, an
  endpoint draining — and a budget restored every pass never exhausts at all. Measured
  before that was fixed: 21 attempts in 0.3 s with a budget of 3, still going.

So **delivering data is the only thing that proves the endpoint works**, and it is the
only thing that restores the budget. The controller restores it in `message_arrived` —
frames arriving through the sink, nothing else. `DeltaFeed.last_error` records whether an
attempt delivered, but that string is only ever the `CLOSED` signal's detail: **the
controller never reads it.** An earlier draft of this note said the budget was decided
from it; the code has never worked that way.

**Checked before it is spent, not after.** A budget of two allows two reconnects and the
third drop is the one that stops. Any other reading makes the number a person configures
mean something other than what they typed.

**Spent on the drop, not on the transition,** and the two are not the same event. The
staleness watchdog reaches `reconnecting` on its own, over a socket the venue has not
closed yet; when that socket then really dies, the close arrives at a machine already in
`reconnecting` and no transition happens. A budget spent on the transition would not be
spent at all — an unbounded reconnect loop in exactly the case the budget exists for,
with a full budget on the books throughout. Written the wrong way first in #39, found by
re-reading the loop, and pinned by
`test_a_socket_that_dies_after_going_silent_still_spends_the_budget` — red-green verified.

## 3. Giving up is loud

Budget exhausted produces, in this order and exactly once each:

1. `connected -> reconnecting` with `reason` `closed` — the drop itself, unchanged.
2. `reconnecting -> stopped` with `reason` `stopped`, and the detail naming the budget.
3. One **`alert`** — `severity` `error`, `code` `reconnect_budget_spent`.
4. One **error-level log record** through `deltapayoff.controller`.
5. `adapter.stop()`, so the socket owner does not go on dialling for a controller that
   has given up.

**`reason` stays inside the stable set** (`start/resume/open/message/stale/silent/closed/
backoff/stopped`) rather than gaining a tenth member, because #40 badges on those strings
and #42 greps them. Why it stopped is in the alert's `code`, the detail and the log line —
three places a reader can find it and no new vocabulary for a badge to learn.

## 4. The dial loop, and the one rule that is easy to get wrong

`ConnectionController.run()` calls `adapter.stream()` once per connection. When it
returns:

| The adapter's last word about its socket | What the controller does |
|---|---|
| `CLOSED` | A drop. Back off, announce the attempt, dial again. |
| `OPENED`, or nothing at all | The adapter is finished. Leave the connection `stopped`. |

**It turns on the adapter's signal and not on the controller's own state**, and that is
the subtle part. `reconnecting` is reached two ways: by a `CLOSED` signal, and by the
**staleness watchdog** over a socket the venue never closed and merely stopped speaking
on. Redialling the second would open a second socket over one that is still open — and
because a stop also returns without reporting a close, a controller that redialled on
state alone would reconnect straight through a shutdown.

### The gap this leaves, and whose it is

**A silence-driven `reconnecting` marks the state and alerts, but does not force the
socket down.** Doing that needs a way to say "drop the connection you have and open
another", and `adapters.base.Adapter` has no such member — `stop()` is permanent. So a
connection that goes silent past `reconnect_after` without the venue closing it will sit
in `reconnecting`, saying so on `/health` and on the badge, until the socket really does
end or the process is restarted.

That is honest but incomplete, and **it belongs to #41**, which adds the commands: a
`reconnect` command needs exactly this member, and adding it here would have been
designing #41's protocol change from outside it. Until then the alert and the state are
the report; the recovery is a person's.

## 5. Announcing the attempt, not its success

#38 could only emit `reconnecting -> connecting` at the instant a socket opened, because
it did not own the dial — its own design says so and predicted this change. #39 emits it
when the **attempt begins**, straight after the backoff wait. The difference is a badge
that reads `reconnecting` for the whole of a thirty-second outage against one that shows a
try in progress. **The transition table is unchanged**; only the moment moved.

## 6. What is left in the socket owner

`DeltaFeed.run()` is one connection: dial, send the whole registry, pump until it ends,
return. It keeps the registry — **never cleared, replayed on every open**, which is the
half of the resubscribe rule that needs a socket — the empty-registry guard, the
heartbeat, and the counters. It keeps no loop, no delay and no budget.

## 7. The seams the tests drive

Two, deliberately, because they prove different things:

- **The scripted fake** (`tests/test_controller.py`, seam 2) for the budget, the
  restoring rule and the transitions. The fake reconnects inside its own `Close` verb, so
  it exercises what the controller does with a drop.
- **The real `DeltaFeed` over a scripted socket** (also `tests/test_controller.py`, seam
  3's machinery) for the dial itself: that a second socket is opened at all, that it is
  sent the complete registry, and that a stop is not redialled. A reconnect asserted only
  against a double that reconnects itself proves nothing about the code that dials.

Every guard in this design was red-green verified in #39 by breaking it and watching the
test fail: the budget check, the redial, the socket-signal rule and the announcement.
