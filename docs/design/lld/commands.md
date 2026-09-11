# Low-level design: the three commands

**What is built for `pause`, `resume` and `reconnect`.** Split out of
[controller.md](controller.md) at its 200-line bound and part of it: the state machine is
there, and this is the three things an operator can tell one. The event is
[../events.md](../events.md)'s `control.command`; the route lives in
`engine/src/deltapayoff/main.py`; the verbs live on
`engine/src/deltapayoff/controller.py`.

**Landed by #41; split delivery extended by #64.** It closes the gap [reconnect.md](reconnect.md) §4 recorded — a
connection the staleness watchdog has called `reconnecting` could not be forced down —
and it gave #40's badge the lever its last acceptance line needed.

## 1. The three verbs

| Verb | From | To | Budget | The socket |
|---|---|---|---|---|
| `pause` | anything running | `stopped`, reason `paused` | **untouched** | cut; subscriptions kept for replay |
| `resume` | `stopped` after a `pause` | `connecting`, reason `resume` | **restored in full** | redialled by the ordinary loop |
| `reconnect` | anything running | `reconnecting`, reason `closed` | one spent, as any drop | cut, then reported as a drop |

**A pause spends nothing because a deliberate stop is not a failure.** The budget counts
times a connection *failed and came back*; an operator pausing a venue for a maintenance
window who found the feed one drop nearer giving up would be punished for announcing what
they were about to do. `reconnects` is not incremented either, for the same reason.

**A reconnect spends one because it is a drop.** The ticket's own words are that the
controller's normal close handling takes it from there, and that handling spends the
budget where a drop is known. The first frame off the new socket restores it in full, so a
healthy commanded reconnect is back at the full budget within a second — `measured` below.

## 2. `paused` is a tenth reason, not a tenth state

`stopped` already existed and already meant "not running". What it could not say is *why*,
and the two ways to reach it are the opposite ends of the range: an operator's own pause,
and a spent reconnect budget — the loudest failure this engine has. So `reason` gains
`paused` beside the nine in [controller.md](controller.md) §7, and
`models.AdapterHealth` gains a `reason` field so `/health` carries it too. Adding an
optional field with a default is compatible, so no `schema_version` moves.

## 3. Delivery depends on the bus mode

The route answers only after the command has an acknowledged state. The path that gets to
that acknowledgement differs between the unchanged monolith and split mode.

### Monolith: synchronous `FeedSupervisor.command`

With `DELTA_BUS` unset, the route calls `FeedSupervisor.command` in the same process. It
publishes one `control.command` to FanOut first, then offers it to the local controllers.
The addressed controller performs the transition and publishes the resulting
`feed.connection`; the route returns `200` with the resulting `AdapterHealth`. This is the
existing synchronous delivery path, and the bus-first order keeps the cause before the
transition in the event stream.

### Split mode: publish, consume, transition, return

With `DELTA_BUS=redis`, the API has no local controller. The exact path is:

1. The API matches the adapter and command, builds one `control.command`, and publishes it
   to Redis.
2. The feed's control consumer receives it and calls
   `FeedSupervisor.dispatch_command`.
3. `dispatch_command` finds the addressed local controller and asks it to perform the
   controller transition. The controller publishes `feed.connection`.
4. Redis carries that event back to the API's filtered `feed-state` subscription. The API
   cache observes the matching transition and completes the pending request with `200` and
   the resulting `AdapterHealth`.

The API waits up to **`COMMAND_ACK_TIMEOUT_SECONDS = 2.0` (`assumed`, code constant)** for
that acknowledgement. If the command was published but no matching acknowledgement
returns, it answers HTTP `504` with exactly:

    "feed command was published but DELTA did not acknowledge it"

An already acknowledged command has an idempotent cached outcome. A repeated delivery that
matches that completed outcome returns the cached `200` state and does not apply a second
transition. The route reports the state the command put the connection in, never the state
the later dial or backoff will eventually reach.

## 4. Cutting the socket, and the protocol member that was not needed

[reconnect.md](reconnect.md) §4 predicted that forcing a socket down would need a new
member on `adapters.base.Adapter`, because `stop()` is permanent and nothing else reaches
the socket. **It did not.** `ConnectionController` now runs `adapter.stream` as a task it
holds, and `_cut` cancels it; the cancellation unwinds `stream`'s own `async with`, which
closes the socket on the way out. The protocol stays at eight members and every adapter
that implements it at all gets the behaviour, rather than only the ones that remembered a
new method.

Two consequences worth stating, because both are load-bearing:

- **A cut stream reports no close**, on every adapter, because the ending is ours and not
  the venue's. That is what makes `pause` cost no budget with no special case in the close
  path, and it is why `reconnect` reports the drop itself, immediately after the cut.
- **The dial loop reads which command did the cutting, not `self._paused`.** A `resume`
  can land in the window between the cancel and its delivery; a loop that read the flag
  would find it already cleared, treat a paused stream as an adapter that had finished,
  and return out of `run()` — leaving `/health` reporting `connecting` with nothing
  dialling it. `tests/test_commands.py` pins that window, and it fails without the
  distinction.

**A pause parks the dial loop rather than returning from it.** Returning runs `run()`'s
`finally`, which stops the adapter for good and detaches the listener, so the resume the
verb exists for would have nothing left to dial with. Parked, the controller keeps its
subscriptions, its adapter and its place on the connection register.

## 5. What resume refuses, and the gap that leaves

**Only a paused connection resumes.** The other way to reach `stopped` is a spent budget,
and that one has already left `run()`: the loop returned, the adapter was stopped
permanently and the listener detached. Moving it to `connecting` would put a state on
`/health` and on the badge that nothing was working to make true — a feed reported as
coming up that will never dial. It is refused and logged instead.

**So `reconnect_budget_spent`'s own sentence — "this connection will not come back
without a resume" — is necessary but not yet sufficient.** Reviving that one needs the
supervisor to start a fresh task for the controller, and `supervisor.md` records that this
supervisor **restarts nothing** on purpose, because a supervisor that restarted a
controller whose budget was spent would undo the decision the budget exists to make. An
operator's explicit resume is not that automatic restart and arguably should be allowed to
do it; deciding which belongs with whoever owns the supervisor's restart policy, and it is
flagged here rather than settled from inside this ticket. Today the recovery for a spent
budget is a process restart, as it was before.

## 6. The route

`POST /feed/{adapter}/{command}`. Path segments rather than a body, so an operator drives
it with `curl -X POST` and nothing else — which is exactly how the walk-through in §7 was
done.

| Case | Answer |
|---|---|
| `pause` acknowledged | `200` with `stopped` / `paused` in that adapter's `AdapterHealth` line |
| `resume` acknowledged | `200` with `connecting` / `resume` in that adapter's `AdapterHealth` line |
| `reconnect` acknowledged | `200` with `reconnecting` / `closed` in that adapter's `AdapterHealth` line |
| Unknown adapter | `404` naming it, and naming the adapters that do exist |
| Unknown verb | `422` naming it, and naming the three |
| Published without an acknowledgement | `504` with the exact detail in §3 |

**Both names are checked before anything is published.** A command nobody can carry out
must not reach the bus, where a later reader would find it and assume it happened. The
adapter name is matched case-insensitively and the event carries the adapter's **own**
spelling, so `/feed/delta/pause` publishes `DELTA`; the feed-side
`dispatch_command` applies the same case-insensitive match.

`ALLOWED_METHODS` already carried `POST` for `/recording`; this route needed no CORS
change. The feed process exposes **no command HTTP route**: command HTTP ends at the API,
and only the `control.command` event crosses to feed. Who may call it is
`docs/recording-contract.md`'s answer for the other mutating route and is the same here:
anything that can reach the port, and the port is loopback.

## 7. What was observed live

**`measured`, run `2026-09-08T05:09Z`**, engine on port 8010 against
`api.india.delta.exchange` with `DELTA_LIVE_FEED=1`, the web app on port 3000 pointed at
it with `NEXT_PUBLIC_ENGINE_URL`, one browser on the BTC ladder. `feed` messages off
`/ws/chain`, and the two commands sent with `curl`:

| t | `feed` message | |
|---|---|---|
| +0.03 s | `connected` / `open` | no badge |
| +3.55 s | `stopped` / `paused` | **badge appears**, `feed stopped`, hover `paused` |
| +33.40 s | `connecting` / `resume` | badge follows |
| +34.43 s | `connected` / `open` | **badge clears**, ladder moves again |

Across the thirty seconds paused, `/health` held `stopped` / `paused` with
`budget_remaining` **10 of 10**, `reconnects` unmoved and `transitions` frozen — no
flapping, no alert, nothing spent. The resume dialled and was `connected` in **1.03 s**.

**This closes #40's one unticked acceptance line.** That ticket could not disable the
engine's outbound leg without touching firewall settings and said so; `pause` is the lever
it was waiting for. The browser's own connection chip read `live` throughout, beside a
badge saying the venue feed was not — the two-independent-indicators case the badge exists
for, observed a second time.

## 8. The seam the tests drive

`tests/test_commands.py`, both of #33's top two seams. The route is driven through the app
under `TestClient` with `DELTA_LIVE_FEED=0` over a real `FeedSupervisor` and a real
`ConnectionController` — a fake supervisor there would leave the one seam this ticket adds
untested. What a verb *does* is driven through the scripted fake (#36).

**These are the only controller tests in the suite that need an event loop**, and
deliberately so: every other one drives the machine synchronously through `poll()` with a
scripted clock. A pause is a *cancellation of a stream in flight*, and a cancellation only
exists on a loop. So the fake's `Silence` step is given the real `asyncio.sleep`, which
parks it inside `stream()` the way `recv()` parks a real socket, and every wait is on a
**condition with a deadline** rather than a duration.
