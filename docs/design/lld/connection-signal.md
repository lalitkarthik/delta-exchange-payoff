# Low-level design: the connection signal

**How an adapter says its socket came or went, and what that promise is worth.** Split out
of [controller.md](controller.md) when that design reached its 200-line bound: the machine
and the signal that drives it are two components, and the signal is the one the venue side
implements. The states built on top of these two facts are in
[controller.md](controller.md); the parts are [../hld.md](../hld.md).

**Landed by #38.** #39 moves backoff and replay up into the controller, and this signal is
what it will be reporting from.

## 1. What it is for

A machine cannot run a state machine over a connection it cannot observe. Before #38 the
adapter protocol carried no way for an adapter to say its socket had come or gone: the
feed reconnected inside its own loop and nothing outside knew. `adapters/base.py` gained
two members — the **eighth** and the seventh — and one enum:

    class ConnectionSignal(str, Enum):
        OPENED = "opened"   # the socket is up AND every subscription has been replayed
        CLOSED = "closed"   # the socket is gone, or the dial never opened one

    def on_connection(self, listener: ConnectionListener) -> None: ...
    def off_connection(self, listener: ConnectionListener) -> None: ...

`ConnectionListener` is `(ConnectionSignal, str) -> None` — the signal and a detail string.
**Synchronous and never blocking**, for the reason `Publish` is: the socket reader calls it
between reads.

## 2. Two facts, and no states

An adapter reports what happened to its socket; what that means — `connected`, `degraded`,
`reconnecting` — is the controller's to decide. An adapter that reported states would be a
second state machine disagreeing with the first.

## 3. `OPENED` is not "the socket connected"

It promises a socket that has been **resubscribed**. `DeltaFeed` fires it after the
subscribe payload is on the wire, never before: a fresh, empty socket wearing a green badge
is precisely the healthy-connection, zero-messages failure the resubscribe-everything rule
exists to prevent, and announcing it early would hide that failure rather than surface it.
A test pins the ordering.

**An empty registry is not announced at all.** `_subscribe_payload()` returns `None` when
nothing is subscribed, so no subscribe goes out and the socket is guaranteed to deliver
nothing; firing `OPENED` there would put the green badge on the one case §3 says must never
wear it. The open is counted in `DeltaFeed.empty_opens` and logged at warning instead, and
the controller — told nothing — stays in `connecting` and reaches `reconnecting` at its own
bound. **Found by review, not by a failure:** `main.py` subscribes before it streams, so
the case is unreachable today, but nothing enforces that ordering and #39's supervisor
takes over the start sequence.

**A dial that never opened still reports `CLOSED`.** A controller told only about sockets
that had opened would sit in `connecting` for the length of an endpoint outage, which reads
on a badge as "starting up". **A stop is not a drop** and is not reported as one — the
guard in `DeltaFeed.run` that skips the report when `_stopping` is already set.

## 4. A register, with a way out

`on_connection` is a **register**, not a slot, so the controller and a future recorder can
both listen without either knowing about the other. `off_connection` removes one
registration, matching the listener by equality, and is **quiet about a listener that was
never registered** — a supervisor that tidies up on both the normal and the failed path
must not be handed a second failure by the tidy-up.

**The removal was missing until review found it.** The controller registers itself as a
construction side effect, `DeltaAdapter.on_connection` appends two closures per call, and
nothing removed either. A controller that was replaced or discarded stayed strongly
referenced by the adapter and went on being told about a socket it no longer owned: one
live socket driving two machines, the dead one answering a signal by raising
`IllegalTransition` inside the socket reader path. #39 holds controllers across a lifespan
and is the first caller that would have hit it.

`ConnectionController.detach()` is the way out and is idempotent; `run()` calls it on the
way out and `start()` re-attaches, so the ordinary path needs no bookkeeping and a resumed
controller is not left deaf.

## 5. Why `DeltaFeed` speaks a different vocabulary

`feed.DeltaFeed` carries four bare registers — `on_open(cb)` / `off_open(cb)` and
`on_close(cb)` / `off_close(cb)`, each `(detail) -> None` — rather than the protocol's enum,
because importing `adapters` into the socket owner is a cycle: `adapters/delta.py` imports
`feed`. `DeltaAdapter` translates the two facts into the protocol's vocabulary, which is
the same job it does turning `sy` into an `Instrument`.

That translation builds **two closures per `on_connection` call**, and they are the only
handles on what was put on the feed. `off_connection` is handed nothing but the original
listener, so `DeltaAdapter` keeps `(listener, on_open, on_close)` triples in `_translated`:
a translation layer that forgot what it built could register but never remove.

## 6. Failure modes

| Failure | What happens |
|---|---|
| A connection listener raises | Swallowed and logged by `DeltaFeed._tell`, the rule `publish` already follows: a broken consumer must not take the feed down. |
| A socket opens with nothing subscribed | Not announced. Counted in `empty_opens`, logged at warning; the controller stays in `connecting`. |
| `off_connection` for a listener never registered | Nothing happens, and nothing raises. |
| The same listener registered twice | Two registrations; one `off_connection` removes one of them. The register is a list, not a set. |
| A stop | Not reported as a close. A badge that flashed `reconnecting` on every clean shutdown would teach a person to ignore it. |

## 7. The seam the tests drive

`tests/test_feed.py` against the scripted-socket harness covers the socket's own two
signals, the empty-registry case and the stop-is-not-a-drop guard;
`tests/test_delta_adapter.py` covers the translation and the removal, including that
removing one listener leaves the others registered. `tests/test_controller.py` covers
`detach()` and the re-attach on `start()`, against the scripted fake adapter.
