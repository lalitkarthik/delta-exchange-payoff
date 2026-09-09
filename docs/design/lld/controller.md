# Low-level design: the connection controller

**What is built inside `engine/src/deltapayoff/controller.py`.** The parts are
[../hld.md](../hld.md); what crosses out of one is [../events.md](../events.md), the
authority on the event names and fields. Numbers carry the run that produced them.

**Landed by #38, completed by #39, commanded by #41, and its policy checked against
Nautilus Trader by #59.** This is the state machine, staleness and the events they emit.
Three designs were split out of this one at its 200-line bound and are part of it:

- **[connection-signal.md](connection-signal.md)** — what an adapter reports about its
  socket, and the register it reports through (#38).
- **[reconnect.md](reconnect.md)** — backoff, the lifetime budget, the dial loop and what
  giving up sounds like (#39).
- **[commands.md](commands.md)** — `pause`, `resume` and `reconnect`: the route, the
  budget rule, and cutting a socket without a new protocol member (#41).

Above it, **[supervisor.md](supervisor.md)** owns one controller per adapter, answers
`/health` for all of them, and since #41 routes commands to them.

## 1. What it is for

A socket that is open and silent is indistinguishable over TCP from one that is open and
busy in a quiet market: the OS keeps a dead connection alive for minutes, nothing raises,
`/health` says `ok`, and a browser cannot tell a dead feed from a quiet one. That is the
plausible-and-wrong failure, and before this ticket it was the default.

The controller turns *nothing has arrived* into a **state**, and every change of state into
an **event** on the bus. It is the only thing that may change a connection's state.

## 2. The states

| State | Meaning |
|---|---|
| `connecting` | An attempt is open; no messages yet. |
| `connected` | The socket is up and messages are arriving. |
| `degraded` | The socket is up and nothing has arrived for the staleness interval. |
| `reconnecting` | The socket is gone or unusable; backoff is running. |
| `stopped` | Not running: paused by command (reason `paused`), or the reconnect budget is spent (reason `stopped`). |

`state` is `None` before `start()` and one of the five at every moment after.

## 3. The transition table

`hld.md` §3, with its two `any` rows expanded and each trigger named as a method.
**Thirteen allowed moves**; the rows below are triggers, so one move can appear on two rows
and one row can cover four. **Every pair not in the thirteen raises `IllegalTransition`**,
which is the whole value of the machine — a connection cannot be both connected and
reconnecting, and cannot reach `connected` without passing through `connecting`.

| From | To | `reason` | Driven by |
|---|---|---|---|
| — | `connecting` | `start` | `start()` — adapter start |
| `stopped` | `connecting` | `resume` | `start()` — a resume command (#41) |
| `connecting` | `connected` | `open` | `connection_opened()` — socket up, subscriptions replayed |
| `connecting` | `connected` | `message` | `message_arrived()` — a frame is stronger proof than an open |
| `connected` | `degraded` | `stale` | `poll()` — no message for `degraded_after` |
| `degraded` | `connected` | `message` | `message_arrived()` — the next message |
| `degraded` | `reconnecting` | `silent` | `poll()` — silence past `reconnect_after` |
| `connecting` | `reconnecting` | `closed` / `silent` | `connection_closed()`, or a `connecting` that delivers nothing |
| `connected` | `reconnecting` | `closed` / `silent` | `connection_closed()`, or silence past `reconnect_after` before `degraded` was reached |
| `reconnecting` | `connecting` | `backoff` | **The backoff elapsed and a redial begins** (#39); or `connection_opened()`; or `message_arrived()`, a frame off a socket believed gone |
| any of the four running states | `stopped` | `stopped` | `stop()` — a pause, or the adapter's stream returning |
| `reconnecting` | `stopped` | `stopped` | **The lifetime budget is spent** — [reconnect.md](reconnect.md) §3 |

**Two readings of the table this design fixed, and both are choices a later reader may
disagree with.** **`any` excludes `stopped`** — a stopped connection is not trying, and the
table's own resume row already takes it to `connecting`, so a `stopped -> reconnecting`
move would contradict it. **`any` excludes a state moving to itself** — a `feed.connection`
whose `from_state` equals its `to_state` describes no change, and #40 would put it on the
wire as a badge that did not move.

**One trigger was added, not one row.** `connecting -> connected` is in `hld.md`, reached
by an open; the controller also reaches it on a message, because a frame is stronger
evidence that the socket is up and subscribed than the open is, and an adapter that
reported its opens late would otherwise sit in `connecting` while data flowed. The set of
*allowed moves* is unchanged from the design.

## 4. Numbers

| Number | Value | Tag | Where from |
|---|---|---|---|
| Longest quiet gap, live BTC chain, both channels, **one hour**, untagged | **44.785 s** | `measured` | `tools/measure_quiet_gap.py`, run `20260907T135951Z`, 3610 s, 2026-09-07. Two shorter runs the same day: 3.355 s over 550 s, 0.321 s over 35 s. [../quiet-gap.md](../quiet-gap.md) |
| Longest quiet gap, **tagged**, unbroken connection, 610 s | **0.724 s** | `measured` | Run `20260909T125124Z`, #59. One connection, no gap spanned a connection event — the first run of any length that can say so |
| The same over **six hours** | **6.792 s** on an unbroken connection; 22.83 s spanning a drop; 13 connections | `measured` | run `20260909T130306Z`, 21,610 s, 2026-09-09. `reconnect_after` = 45 s is 6.6x above the worst live-socket gap and is **supported**, no longer `assumed`. [../quiet-gap.md](../quiet-gap.md) |
| `degraded_after` | 15 s | `assumed`, and now supported | Three ticker refreshes at `measured` 5001 ms (`feed.py`). The hour crossed it twice, both times on an interruption rather than a quiet market — which is what the badge is for |
| `reconnect_after` | 45 s | `assumed` | Three degraded intervals, under Delta's documented 60 s idle disconnect so we notice before the venue drops us, and above Delta's own 35 s heartbeat window. **The untagged hour's worst gap missed it by 0.215 s.** Confirmed unchanged by #59, on documents rather than on a measurement |
| `heartbeat_every` | 10 s | `assumed` | 8,640 heartbeats per adapter per day — fast enough for a badge, slow enough not to be a flood |
| `poll_seconds` | 1 s | `assumed` | The staleness timer's resolution; a 15 s bound observed to the nearest second |

### What the hour showed

**The hour below is untagged and stays that way.** #39 added connection tagging so a gap
spanning a drop could be told from a quiet market, and #59 was the first to run it — 610 s,
one connection, no gap spanning anything. #39 also found the tools stopped recording at the
first drop while reporting a full window, so any run before that fix is truncated and must
not be quoted. Full record: [../quiet-gap.md](../quiet-gap.md). Two findings the bounds
rest on:

**Only the tail moves.** p99 (0.011 s), p95 (0.002 s) and the median (0.0 s) are identical
across 35 s, 550 s, an hour, and #59's 610 s; the maximum goes 0.321 → 3.355 → **44.785 s**.
The quiet gap is heavy-tailed, so a short window measures only the part of the distribution
never in question — 35 seconds would have said 15 s is 47x the worst gap, confidently wrong,
and 610 seconds would have said the same thing again. **The flap this design's review fixed
came 0.215 s from production**: the old code demoted a reopened socket once the pre-drop age
passed `reconnect_after`, and that age reached 44.785 s in the first hour.

## 5. The connection signal

**Moved to [connection-signal.md](connection-signal.md).** The number is kept so
references land.

## 6. What drives it

The machine is **synchronous**, one method per cause, which is what makes it testable
without a clock or an event loop:

| Method | Cause |
|---|---|
| `sink(event)` | An event off the adapter. Notes the arrival, then publishes onward. |
| `connection_signal(signal, detail)` | The adapter's listener, registered in `__init__`. |
| `poll(now)` | The staleness timer; also the heartbeat's cadence. |
| `start()` / `stop(reason)` | Started or stopped by request. |
| `pause()` / `resume()` / `reconnect()` | An operator's three verbs, off `control.command` through `command(event)` — [commands.md](commands.md). |
| `transition(to, reason)` | The one place state changes. **Public**, because #39 drives `reconnecting -> stopped` on a spent budget. |

`run()` is the only async thing: it starts, launches the timer as a **separate task**, and
dials `adapter.stream(self.sink)` once per connection, backing off between attempts —
[reconnect.md](reconnect.md) §4. The timer is a task rather than a read timeout because a
controller that only woke when a message arrived could never notice that none had.

**Two clocks, injected.** `clock` is monotonic and measures elapsed time — `time.time()`
steps backwards under an NTP correction, making an age negative and a staleness bound
meaningless. `wall_clock` stamps `ts_received`, which the envelope defines as a wall clock.
`sleep` is injected too, so a twenty-second silence costs the suite nothing.

## 7. The events

All three from `events/catalogue.py`; [../events.md](../events.md) is the authority on
fields. `source` is `"controller"` on all — the component, not the venue, so a consumer
can tell a transition we decided from a quote the venue sent. The adapter's name travels
in the payload's `adapter` field. `instrument` is `null` throughout.

- **`feed.connection`** — one per transition and nowhere else: `adapter`, `from_state` (`null` on the first), `to_state`, `reason`.
- **`heartbeat`** — one per cadence whatever the state: `adapter`, `state`,
  `last_message_age_seconds`, which is **`null` before the first message ever arrives**:
  an unknown age, not an age of zero.
- **`alert`** — `severity`, `code`, `detail`, `adapter`. Three codes and no more from this
  module: **`connection_silent`** when silence past `reconnect_after` forces
  `-> reconnecting`; **`poll_failing`** when the watchdog's own polls keep raising; and
  **`reconnect_budget_spent`** (#39) when the feed gives up, the loudest thing this engine
  says. `events.md` promised an alert on a stale connection and none was emitted anywhere.

**`degraded` deliberately does not alert**, and **nor does a `pause`**: fifteen quiet
seconds is a badge and a heartbeat, a pause is something a person just did, and an alert on
either is the flood an alert exists to stand out from.

**`reason` is a short stable name**, not a sentence: `start`, `resume`, `open`, `message`,
`stale`, `silent`, `closed`, `backoff`, `stopped`, and `paused` since #41. #40 badges on
them, `/health` carries the latest one since #41, and #42 greps them;
and a thousand distinct venue sentences would match neither. The venue's own words go in
the log line, where a person reads them.

## 8. Failure modes

| Failure | What happens |
|---|---|
| A move outside the table | `IllegalTransition` raises. Never logged and continued: a machine that ignored a forbidden move would have more states than it admits to. |
| A socket open that delivers nothing | Staleness is measured from the state's entry when no message has ever arrived, so `connecting` reaches `reconnecting` at `reconnect_after` rather than never. |
| A connection listener raises | Swallowed and logged by `DeltaFeed`, the rule `publish` already follows: a broken consumer must not take the feed down. |
| The adapter's `stream` returns | One connection has ended. `run()` redials it **only if the adapter reported its socket gone** — [reconnect.md](reconnect.md) §4 — and otherwise leaves the connection `stopped`. A `pause` is the exception: the loop parks instead of returning, so a `resume` still has an adapter to dial — [commands.md](commands.md) §4. |
| The reconnect budget is spent | `-> stopped`, one `alert` at error, one error log record, and the adapter is stopped with it. [reconnect.md](reconnect.md) §3. |
| A message while `reconnecting` | Routed through `connecting` in two transitions, so the table is honoured and the machine cannot stick. |
| **A socket the venue never closed and stopped speaking on** | Since #59 the watchdog **ends it itself**: crossing `reconnect_after` cuts the stream and reports the drop, which is `reconnect()`'s own body, and the ordinary loop backs off and dials. Before that it marked the state, alerted and waited for the venue or a person. It costs one of the budget, as any drop does. [reconnect.md](reconnect.md) §4, [../decisions/0003-controller-policy.md](../decisions/0003-controller-policy.md). |
| A resume after a long pause | `start()` forgets the last-message age. Time spent stopped is our silence, not the venue's, and a resumed connection would otherwise arrive already past the reconnect bound. |
| A reconnect after an outage | **Where the line falls, corrected by review.** "A reconnect keeps its age" is right *while* it is reconnecting — that gap was the venue's and it is what the bound exists to catch — and wrong the instant the replay completes. `connection_opened()` records `_opened_at`, and staleness is measured from the **later** of that and the last message, so the replayed socket gets the same grace a fresh start gets. Without it every outage longer than `reconnect_after` ended in a flap: the reopened socket was demoted on the very next poll for a gap belonging to the socket before it, at three spurious `feed.connection` events a second, on the badge #40 draws, during the incident an operator is watching. The heartbeat's `last_message_age_seconds` is untouched — that one is the venue's own silence and stays true across the reconnect. |
| A staleness poll raises | Logged with its traceback and the watchdog keeps ticking; after `POLL_FAILURES_BEFORE_ALERT` consecutive failures, an `alert`. `poll()` reaches `_publish`, which is the bus and, from #39, a consumer's code; letting that out killed the timer task, and `run()`'s `gather(..., return_exceptions=True)` retrieved the exception so not even asyncio's unretrieved-exception warning fired. The connection then sat in `connected` through any silence at all — this module's own thesis, reintroduced one layer up. `run()` now reads the gather result rather than discarding it. |
| An open reported while already connected | Not dropped on the floor. No move is allowed out of `connected`, so the state is unchanged, but the grace is rebased and the fact is logged: a socket that says it reopened is a socket that stopped and started. |
| A discarded controller | `detach()` takes it off the adapter's register — see [connection-signal.md](connection-signal.md) §4. |

## 9. Logging

**One line per transition, at INFO, through `logging.getLogger("deltapayoff.controller")`
— the standard library and no new dependency.** The line carries the adapter, both states,
the reason and the detail. #42 replaces the format with JSON lines and adds the levels; this
ticket only guarantees a transition is never silent. A test pins the three lines a
start-open-close sequence produces. Since #41 a refused `resume` and a cut socket log here
too: neither changes a state, and both are things a person asked for.

## 10. The seam the tests drive

`tests/test_controller.py`, on the **scripted fake adapter** (#36) — seam 2 of #33's
testing decisions. Every connection test is written in the fake's four verbs and its clock
is injected, so a suite exercising a twenty- and a sixty-second silence runs in under a
second. `poll` is driven from the script's clock, and **one** test uses the real timer at
millisecond intervals, so the wiring is proved as well as the machine. `caplog` covers the
log line (seam 6); `tests/test_feed.py` covers the socket's two signals; since #39 the same
file drives the **real `DeltaFeed` over a scripted socket** for the dial
([reconnect.md](reconnect.md) §7), and #41's verbs are `tests/test_commands.py`.
