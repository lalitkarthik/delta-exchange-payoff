# Low-level design: the connection badge

**How the feed's own state reaches a browser that connected at an arbitrary moment**, and
why that needed a cache rather than a second bus subscription. `docs/live-chain-contract.md`
is the wire contract; this is how `main.py` produces it. **Landed by #40.**

## 1. What it is for

`ConnectionController` (#38) knows its own state and publishes a `FeedConnection` event on
every transition, but it is a **momentary announcement**, not a durable fact: nothing
observes it unless something was already listening at the instant it fired. A browser opens
`/ws/chain` at an arbitrary point in that timeline and has to learn the *current* state, not
only the *next* transition — and it needs an honest `since` for whatever state that already
is, not the moment the browser happened to ask.

## 2. `FeedConnectionCache`, not a subscription

The obvious seam — subscribe to the events `FanOut` the way `ChainStream` and `BarWriter` do
— was rejected. `FanOut` has no per-type filtering: a subscriber receives every
`md.option_quote` and `md.option_reference` alongside the rare `feed.connection`, at roughly
600 messages a second (`docs/ingestion.md`) to catch an event that fires a handful of times
an hour. One `FeedConnectionCache` per process, updated **synchronously** by wrapping the
`publish` callable `build_feed_stack` already hands `FeedSupervisor`:

    def publish(event):
        feed_cache.apply(event)
        events.publish(event)

No queue, no task, no lag — the cache holds the latest `FeedConnection` per adapter name the
instant the controller transitions, because it runs inside the same call. A websocket
connecting between two transitions reads a cache entry that is exactly as current as
`ConnectionController.state` itself, without ever touching the market-data bus.

**Why not read `ConnectionController.state` directly.** It answers *which* state but not
*since when*, in wall-clock terms: `_entered_at` is on the controller's monotonic clock and
is private. The only wall-clock record of "since" that exists anywhere is on the
`FeedConnection` event itself (`ts_received`), which is why the cache remembers the event and
not a derived state.

## 3. One adapter, chosen by the supervisor, reported by the cache

The websocket handler asks `FeedSupervisor.controllers[0].adapter_name` for *which* adapter
to read, then asks the cache for *what it last said*. Two dependencies rather than one:
`get_supervisor` (already `/health`'s) names the adapter, `get_feed_cache` reports on it. If
either is `None` — every existing test that does not override them, and any process whose
lifespan never ran — the handler sends no `feed` message at all, which is exactly how the
socket already behaved before this ticket for a process with nothing to report.

**Multiple adapters are not addressed.** There is one `DeltaAdapter` today serving every
underlying it lists, so `[0]` is not a shortcut around a real choice — there is nothing else
in the list. A second venue would need `/ws/chain` to say which adapter a connection cares
about, which does not exist yet because there is nothing to disambiguate.

## 4. Coalescing, deliberately

Each websocket connection tracks the last state it actually sent and skips a `feed` message
whose state matches it. This was written directly against #39's fixed bug — an announced
redial that fired `connecting → reconnecting` roughly once per attempt, 5–6 times in a
ten-minute outage — which is exactly the failure mode a per-transition forward would still
be vulnerable to for *new* bugs of the same shape. Two real transitions landing inside one
push `interval` collapse to the newer one; the only two states this can ever hide from a
connection are `connected` and a non-`connected` state that was immediately followed by
another non-`connected` state, and neither changes what `feedBadge` (web side) renders.

**Cost of this choice, stated plainly:** a transition that both begins and ends inside one
push interval is invisible to a connection watching at exactly the wrong moment. Given
`PUSH_INTERVAL_SECONDS = 1.0` and every named threshold in `hld.md` §5 measured in tens of
seconds, that window is far shorter than any state this badge exists to show.

## 5. `lifespan()` now resets `app.state`

Found while testing this ticket, not designed into it: `app.state` is a plain namespace on
the module-level `app` singleton, and Starlette does not clear it when a lifespan's `finally`
runs. A test that runs the real lifespan (`test_recording.py`'s `client` fixture) and then
exits leaves `app.state.supervisor` and friends pointing at a *closed* stack, and a later
test on a bare `TestClient(app)` — every test in `test_ws_endpoint.py` — read that stale
supervisor back and got a `feed` message built from the previous test's shutdown, breaking
timing assumptions that had nothing to do with this ticket. `lifespan()`'s `finally` now sets
every name it assigned back to `None`, which every reader already treats as "nothing to
report" rather than as an error.

## 6. Failure modes

| Failure | What happens |
|---|---|
| No supervisor, or a supervisor with no controllers | No `feed` message is ever sent; `chain`/`waiting`/`error` are unaffected. |
| The cache has never seen a transition for the chosen adapter | Same as above — the handler skips silently rather than guessing a state. |
| Two transitions inside one push interval | The older is coalesced away; see §4. |
| A stale `app.state` from a previous test's closed lifespan | Fixed by §5; would otherwise leak a closed supervisor's last state into an unrelated test. |

## 7. The seam the tests drive

`engine/tests/test_ws_feed_badge.py`: a `ConnectionController` wrapping a `ScriptedAdapter`,
driven synchronously for the "current state on accept" case and concurrently — via
`TestClient`'s own portal and an injected clock that yields real control between poll ticks —
for the "degraded while a browser watches" case. `web/tests/feed.test.ts` pins `feedBadge`,
the pure map from a `FeedStatus` to what the header shows.
