# The connection controller: policies and rules

**What the feed does when the venue goes quiet, drops the socket, or refuses to take it.**
The *data feed engine* document's connection-controller section, written to be read on its
own: nothing below assumes you have seen a low-level design. Why each rule is what it is —
Nautilus Trader read in source, Delta's own websocket documentation, the runs behind each
number — is
[../research/0003-controller-against-nautilus.md](../research/0003-controller-against-nautilus.md);
the decision is
[../decisions/0003-controller-policy.md](../decisions/0003-controller-policy.md); the
mechanism is [../lld/controller.md](../lld/controller.md) and its three companions, which
win where they disagree with the prose here.

**Landed as code, not as a standard.** Every rule below runs today in
`engine/src/deltapayoff/controller.py`. Rule **C7** is #59's one change; everything else is
#38, #39 and #41 restated in one place. Every number carries `measured`, `assumed` or
`derived`.

**The failure all of it refuses.** A websocket that is open and silent is
indistinguishable, at the TCP layer, from one that is open and busy in a quiet market. The
operating system keeps a dead connection alive for minutes, nothing raises, a health check
answers "ok", and a dashboard that has stopped moving looks exactly like a market that has
stopped trading. Every rule below turns *nothing has arrived* into something a person or a
machine can see and act on.

## 1. The five states

One connection per adapter — today one, `DELTA` — in exactly one of these at every moment.

| State | What it means |
|---|---|
| `connecting` | An attempt is open. Nothing has arrived yet. |
| `connected` | Data is arriving. |
| `degraded` | The socket is up and nothing has arrived for `degraded_after`. |
| `reconnecting` | The socket is gone or unusable, and a backoff is running. |
| `stopped` | Not running: paused by an operator, or the reconnect budget is spent. |

Thirteen moves between them are allowed and every other pair raises rather than happening
quietly. Each move publishes one `feed.connection` event and writes one log line;
`/health` reports the state, why it is in it, and the counters.

## 2. The rules

### C1 — Silence is measured from data, and from nothing else

The staleness clock is reset by a market-data event and by no other frame. Delta's control
traffic — subscription acknowledgements, keepalive replies — is dropped before it reaches
the clock. A connection that is provably alive and carrying no data is stale, which is the
only definition that matches what the feed is for: rows in the store. The clock is
monotonic, so a system time correction cannot make an age negative.

### C2 — A quiet connection is `degraded` at 15 s, and that is a badge, not an alarm

`degraded_after` = **15 s**, `assumed`. It is three refreshes of Delta's slower channel,
which is `measured` at 5,001 ms per contract. Entering `degraded` publishes an event and
turns the dashboard badge amber. **It raises no alert.** Fifteen quiet seconds happens in
an ordinary session; an alert on each one is the flood an alert exists to stand out from.

### C3 — A silent connection is `reconnecting` at 45 s, and that is an alarm

`reconnect_after` = **45 s**, `assumed`. It is three degraded intervals, and it sits under
Delta's own documented bound: *"You will be disconnected, if there is no activity within 60
seconds after making connection."* So we notice before the venue would drop us. It sits
above Delta's own recommended heartbeat window of 35 s. Crossing it publishes an `alert` at
severity `error`, code `connection_silent`.

**Bounded above by measurement, and the measurement has landed.** Run `20260909T130306Z` finished
**2026-09-09T19:03Z**: six hours, 21,610 s, 502 BTC symbols, both channels, 23,713,768 messages, 13 connections. The longest gap on an **unbroken** connection is `measured` **6.792 s**, 6.6× inside the bound; the longest gap *spanning* a connection event is `measured` 22.83 s and is a drop, not a quiet market. The untagged hour's 44.785 s was, as suspected, the same thing, and the `measured` 0.724 s over 610 s (run `20260909T125124Z`) says nothing either way.

**That changes the bound, not the setting.** 45 s is still `assumed` in that nothing measured picked it — three degraded intervals under Delta's 60 s — and the run gives no reason to move it; what it is no longer is unevidenced. [../quiet-gap.md](../quiet-gap.md) puts that as "supported, not assumed"; [../decisions/0003-controller-policy.md](../decisions/0003-controller-policy.md) says "stays `assumed`" in a decision body written while the run was in flight and records the landing in its triggers, a decision record here being appended to and never rewritten. **Same value, better evidence.** Every run so far is an active BTC session, so a thin ETH chain or a weekend could still make 45 s an ordinary gap — 0003's own remaining trigger.

### C4 — Grace is given to a new socket, and to an attempt

Staleness is measured from the **latest** of: the last message, the moment a socket was
reported open and resubscribed, and the moment a dial attempt began. Without the second, a
socket reopened after a long outage is demoted on its next poll for a gap belonging to the
socket before it; without the third, every announced retry during an outage is called
silent a second later and raises its own alert.

### C5 — Backoff doubles from 1 s to a 60 s ceiling, with no jitter

`retry_delay` = **1 s**, ceiling **60 s**, factor **2**, all `assumed`. The wait doubles
after each consecutive failed attempt and is restored to 1 s in full the moment a
connection delivers anything.

**No jitter, deliberately.** Jitter exists to stop many clients redialling in the same
instant. We are one process with one connection per venue, so it would be a setting that
cannot change an outcome — and our 1 s first wait already meets the 1-second floor
Nautilus Trader enforces for the same venues' sake.

### C6 — The budget is ten consecutive failures, and only data restores it

`reconnect_budget` = **10**, `assumed`. One is spent per drop and it is **restored in
full** the moment a message arrives, so it counts *consecutive* failures, not lifetime ones
— a cumulative counter kills a feed that reconnects once a day after a month, with no
failure anywhere to point at.

**Restored by a delivered message, never by a socket opening.** Delta can accept a
connection and close it immediately — a throttled address, a draining endpoint — and a
budget restored on connecting never runs out at all. That was `measured` before it was
fixed: **21 attempts in 0.3 s** with a budget of 3, still going. Delivering data is the
only thing that proves the endpoint, the subscription and the decode together. The budget
is checked **before** it is spent, so a budget of two allows two reconnects and the third
drop is the one that stops.

### C7 — Silence ends the socket itself — #59

**New in this ticket.** When silence crosses `reconnect_after`, the feed no longer waits
for the venue or for a person. It cuts the socket, reports the drop, backs off and redials
— the same sequence an operator's `reconnect` command produces, and indistinguishable from
a real drop in the events. Before this, crossing 45 s turned the badge red, raised the
alert, and then the connection sat there until the venue finally closed the socket or
someone sent `POST /feed/{adapter}/reconnect`. At 02:00 there is nobody to send it, and
every minute of that is bars the store does not write. Nautilus Trader's reader breaks its
own connection on the same condition, and Delta's own documentation instructs clients to
*"exit the existing connection and try to reconnect"*.

It costs one of the budget, as any drop does, and the first frame off the replacement
restores it in full. A socket that reopens and stays silent burns the budget over ten
silences and stops out loud, rather than retrying forever.

### C8 — Everything is resubscribed on every open, and "open" means resubscribed

The subscription registry is **never cleared** and is replayed in full on every connection.
A reconnected socket is a fresh, empty socket and the venue has forgotten everything; skip
the replay and you get a healthy connection, zero messages, and a screen that quietly stops
updating.

A socket is not reported open until its subscribe is on the wire, and one opened with
**nothing** subscribed is not reported at all — counted, logged at warning, and left to
reach `reconnecting` at its own bound, because a green badge over a socket with no
subscriptions on it is the exact failure this rule prevents.

**Known gap.** Delta answers a subscribe with a `subscriptions` message and the feed drops
it as control traffic, so "open" means *sent*, not *acknowledged*. A subscription the venue
silently refused delivers nothing and is caught by **C3** within 45 s — bounded, not
silent. Tracking acknowledgements is an improvement, not a fix, and is not built.

### C9 — Giving up is the loudest thing the engine says

When the budget is spent the connection produces, in this order and exactly once each: the
drop's own transition; a move to `stopped`; one `alert` at severity `error` with code
`reconnect_budget_spent`; one error-level log record; and a stop of the adapter, so nothing
goes on dialling for a connection that has given up.

**It does not come back on its own.** A supervisor sits above the controllers and
deliberately restarts nothing: one that restarted a controller whose budget was spent would
undo the single decision the budget exists to make. Recovery is a process restart.
`derived`: exhausting the budget takes **303 s** of backoff across 11 dials —
1+2+4+8+16+32+60+60+60+60 seconds — which is **7.3%** of Delta's documented allowance of
150 connections per 5 minutes per address.

### C10 — An operator has three verbs, and a pause is not a failure

`POST /feed/{adapter}/pause`, `/resume`, `/reconnect`. The route answers **after** the
command, with the state the command put the connection in — never the state it will settle
into.

| Verb | Result | Budget |
|---|---|---|
| `pause` | `stopped`, reason `paused`; socket cut, subscriptions kept | **untouched** |
| `resume` | `stopped` → `connecting`; the ordinary loop redials | **restored in full** |
| `reconnect` | socket cut, then reported as a drop | **one spent**, as any drop |

A pause spends nothing because a deliberate stop is not a failure: an operator announcing
a maintenance window must not find the feed one drop nearer giving up. `resume` refuses a
connection whose budget is spent, and logs the refusal rather than reporting a feed as
coming up that nothing will dial.

### C11 — The heartbeat we send, and the one we do not receive

The feed sends a websocket **ping control frame every 30 s** and does not wait for the
pong. Separately, the controller publishes a `heartbeat` **event** on the bus every 10 s
(`assumed`) carrying the state and the age of the last message — `null`, never `0`, when
nothing has ever arrived: an unknown age is not an age of zero.

**Delta's own application heartbeat is not enabled.** The venue offers
`{"type": "enable_heartbeat"}`, answers it every 30 s, and recommends a 35-second client
timer — a liveness signal independent of market data. It carries a trap that must be
written down before anyone turns it on: a `heartbeat` frame routed to the market-data sink
would reset the staleness clock in **C1** and retire this whole design without a test
failing.

## 3. What a reader of `/health` sees

`status` is liveness and is always `ok` — the process answered you. `feed` beside it is
readiness: the **worst** state among the adapters, ordered `connected`, `degraded`,
`connecting`, `reconnecting`, `stopped`. An engine with no feed reports `stopped`, the
strongest possible "not ready". Each adapter's row carries its state, its reason, the age
of its last message, its drops, its remaining budget, its transition count, and the two
counters whose whole signal is being above zero — sockets opened with nothing subscribed,
and frames that parsed and made no sense; those two are `null`, never `0`, when nobody is
counting. **No field says `healthy`**: whether a state is acceptable is a badge's, an alert
rule's or an operator's judgement, and one boolean here would fix one of those readings for
all three.
