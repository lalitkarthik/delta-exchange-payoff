# Low-level design: the connection controller

**What is built inside `engine/src/deltapayoff/controller.py`.** The parts are
[../hld.md](../hld.md); what crosses out of one is [../events.md](../events.md), the
authority on the event names and fields. Numbers carry the run that produced them.

**Landed by #38, and half-finished on purpose.** This is the state machine, staleness and
the two events they emit. Backoff, the lifetime reconnect budget and subscription replay
are still inside `feed.DeltaFeed`, where they are correct and tested; #39 lifts them up
here and puts a supervisor over the result, and #41 adds the commands.

## 1. What it is for

A socket that is open and silent is indistinguishable over TCP from a socket that is open
and busy in a quiet market. The operating system keeps a dead connection alive for
minutes, nothing raises, `/health` says `ok`, and a browser looking at a ladder that
stopped moving cannot tell a dead feed from a quiet one. That is the plausible-and-wrong
failure, and before this ticket it was the default.

The controller turns *nothing has arrived* into a **state**, and every change of state
into an **event** on the bus. It is the only thing that may change a connection's state.

## 2. The states

| State | Meaning |
|---|---|
| `connecting` | An attempt is open; no messages yet. |
| `connected` | The socket is up and messages are arriving. |
| `degraded` | The socket is up and nothing has arrived for the staleness interval. |
| `reconnecting` | The socket is gone or unusable; backoff is running. |
| `stopped` | Not running: paused by command, or the reconnect budget is spent. |

`state` is `None` before `start()` and one of the five at every moment after.

## 3. The transition table

`hld.md` §3, with its two `any` rows expanded and each trigger named as a method.
**Thirteen allowed moves**; the rows below are triggers, so one move can appear on two
rows and one row can cover four moves. **Every pair not in the thirteen raises
`IllegalTransition`**, which is the whole value of the machine — a connection cannot be
both connected and reconnecting, and cannot reach `connected` without passing through
`connecting`.

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
| `reconnecting` | `connecting` | `backoff` | `connection_opened()` — an attempt succeeded |
| any of the four running states | `stopped` | `stopped` | `stop()` — a pause, or the adapter's stream returning |
| `reconnecting` | `stopped` | (#39) | the lifetime budget exhausted |

**Two readings of the table this design fixed, and both are choices a later reader may
disagree with.**

- **`any` excludes `stopped`.** A stopped connection is not trying, and the table's own
  resume row already takes it to `connecting`; a `stopped -> reconnecting` move would
  contradict it.
- **`any` excludes a state moving to itself.** A `feed.connection` whose `from_state`
  equals its `to_state` describes no change, and #40 would put it on the wire as a badge
  that did not move.

**One trigger was added, not one row.** `connecting -> connected` is in `hld.md`, reached
by an open; the controller also reaches it on a message, because a frame is stronger
evidence that the socket is up and subscribed than the open is, and an adapter that
reported its opens late would otherwise sit in `connecting` while data flowed. The set of
*allowed moves* is unchanged from the design.

## 4. Numbers

| Number | Value | Tag | Where from |
|---|---|---|---|
| Longest quiet gap, live BTC chain, both channels, one hour | **not yet run** | — | `tools/measure_quiet_gap.py` exists and is the way to take it. The one-hour run was started and had not reported when this landed, so `degraded_after` stays `assumed` below. Whoever takes the measurement should replace this row and revisit that default. |
| `degraded_after` | 15 s | `assumed` | Three ticker refreshes at `measured` 5001 ms (`feed.py`) |
| `reconnect_after` | 45 s | `assumed` | Three degraded intervals, under Delta's documented 60 s idle disconnect so we notice before the venue drops us |
| `heartbeat_every` | 10 s | `assumed` | 8,640 heartbeats per adapter per day — fast enough for a badge, slow enough not to be a flood |
| `poll_seconds` | 1 s | `assumed` | The staleness timer's resolution; a 15 s bound observed to the nearest second |

`tools/measure_quiet_gap.py` subscribes every listed BTC option on both channels —
exactly what the engine subscribes — and records the wall-clock gap between consecutive
frames off the socket, which is precisely what the staleness timer measures.

<!-- MEASUREMENT -->

## 5. The connection signal

The adapter protocol carried no way for an adapter to say its socket had come or gone, and
a machine cannot describe a connection it cannot observe. `adapters/base.py` gained one
member — a **seventh** — and one enum:

    class ConnectionSignal(str, Enum):
        OPENED = "opened"   # the socket is up AND every subscription has been replayed
        CLOSED = "closed"   # the socket is gone, or the dial never opened one

    def on_connection(self, listener: ConnectionListener) -> None: ...

`ConnectionListener` is `(ConnectionSignal, str) -> None` — the signal and a detail
string. **Synchronous and never blocking**, for the reason `Publish` is: the socket reader
calls it between reads. A **register**, not a slot, so the controller and a future
recorder can both listen without either knowing about the other.

**Two facts, and no states.** An adapter reports what happened to its socket; what that
means — `connected`, `degraded`, `reconnecting` — is the controller's to decide. An
adapter that reported states would be a second state machine disagreeing with the first.

**`OPENED` is not "the socket connected".** It promises a socket that has been
**resubscribed**. `DeltaFeed` fires it after the subscribe payload is on the wire, never
before: a fresh, empty socket wearing a green badge is precisely the healthy-connection,
zero-messages failure the resubscribe-everything rule exists to prevent, and announcing it
early would hide that failure rather than surface it. A test pins the ordering.

**A dial that never opened still reports `CLOSED`.** A controller told only about sockets
that had opened would sit in `connecting` for the length of an endpoint outage, which
reads on a badge as "starting up". **A stop is not a drop** and is not reported as one.

`feed.DeltaFeed` carries two bare registers — `on_open(cb)` and `on_close(cb)`, each
`(detail) -> None` — rather than the protocol's enum, because importing `adapters` into
the socket owner is a cycle: `adapters/delta.py` imports `feed`. `DeltaAdapter` translates
the two facts into the protocol's vocabulary, which is the same job it does turning `sy`
into an `Instrument`.

## 6. What drives it

The machine is **synchronous**, one method per cause, which is what makes it testable
without a clock or an event loop:

| Method | Cause |
|---|---|
| `sink(event)` | An event off the adapter. Notes the arrival, then publishes onward. |
| `connection_signal(signal, detail)` | The adapter's listener, registered in `__init__`. |
| `poll(now)` | The staleness timer fired; also the heartbeat's cadence. |
| `start()` / `stop(reason)` | Started or stopped by request. |
| `transition(to, reason)` | The one place state changes. **Public**, because #39 drives `reconnecting -> stopped` on a spent budget and #41 drives the commands. |

`run()` is the only async thing: it starts, launches the timer as a **separate task**, and
awaits `adapter.stream(self.sink)`. The timer is a task rather than a read timeout because
a controller that only woke when a message arrived could never notice that none had.

**Two clocks, injected.** `clock` is monotonic and measures elapsed time — `time.time()`
steps backwards under an NTP correction, which would make an age negative and a staleness
bound meaningless. `wall_clock` stamps `ts_received`, which the envelope defines as a wall
clock. `sleep` is injected too, so a twenty-second silence costs the suite nothing.

## 7. The events

Both from `events/catalogue.py`; [../events.md](../events.md) is the authority on fields.
`source` is `"controller"` on both — the component, not the venue, so a consumer can tell
a transition we decided from a quote the venue sent. The adapter's name travels in the
payload's `adapter` field. `instrument` is `null` on both.

- **`feed.connection`** — one per transition and nowhere else: `adapter`, `from_state`
  (`null` on the first), `to_state`, `reason`.
- **`heartbeat`** — one per cadence whatever the state: `adapter`, `state`,
  `last_message_age_seconds`. **`null` before the first message ever arrives** — an
  unknown age, not an age of zero.

**`reason` is a short stable name**, not a sentence: `start`, `resume`, `open`, `message`,
`stale`, `silent`, `closed`, `backoff`, `stopped`. #40 badges on them and #42 greps them,
and a thousand distinct venue sentences would match neither. The venue's own words go in
the log line, where a person reads them.

## 8. Failure modes

| Failure | What happens |
|---|---|
| A move outside the table | `IllegalTransition` raises. Never logged and continued: a machine that ignored a forbidden move would have more states than it admits to. |
| A socket open that delivers nothing | Staleness is measured from the state's entry when no message has ever arrived, so `connecting` reaches `reconnecting` at `reconnect_after` rather than never. |
| A connection listener raises | Swallowed and logged by `DeltaFeed`, the rule `publish` already follows: a broken consumer must not take the feed down. |
| The adapter's `stream` returns | `run()` leaves the connection `stopped`. An adapter that is no longer streaming is not connecting. |
| A message while `reconnecting` | Routed through `connecting` in two transitions, so the table is honoured and the machine cannot stick. |
| A resume after a long pause | `start()` forgets the last-message age. Time spent stopped is our silence, not the venue's, and a resumed connection would otherwise arrive already past the reconnect bound. A reconnect does not pass through `start()` and keeps its age. |

## 9. Logging

**One line per transition, at INFO, through `logging.getLogger("deltapayoff.controller")`
— the standard library and no new dependency.** The line carries the adapter, both states,
the reason and the detail. #42 replaces the format with JSON lines and adds the levels;
this ticket only guarantees that a transition is never silent. A test pins the three lines
a start-open-close sequence produces.

## 10. The seam the tests drive

`tests/test_controller.py`, on the **scripted fake adapter** (#36) — seam 2 of #33's
testing decisions. Every connection test is written in the fake's four verbs and its clock
is injected, so a suite exercising a twenty-second silence and a sixty-second one runs in
under a second. `poll` is driven from the script's own clock, and **one** test uses the
real timer with millisecond intervals, so the wiring is proved as well as the machine.
`caplog` covers the log line (seam 6); `tests/test_feed.py` covers the socket's two
signals against the existing scripted-socket harness.
