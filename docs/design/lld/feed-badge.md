# Low-level design: the connection badge

**How the feed's own state reaches a browser that connected at an arbitrary moment.**
`docs/live-chain-contract.md` is the wire contract; this is how the API produces it.
The projection is also the source of split-mode `/health`. **Landed by #40, extended by
#64.**

## 1. What it is for

`ConnectionController` publishes a `FeedConnection` on every transition and a `Heartbeat`
on its cadence. A transition is a momentary announcement, while a heartbeat carries the
controller's current state as well as the age of the last venue message. The API needs both:
the transition gives a reason and a precise state change, while the heartbeat lets an API
that started late hydrate state without requiring a new transition.

There is one `FeedConnectionCache` per API process. It keeps the latest `feed.connection`
and latest `heartbeat` per adapter, together with when the API observed each one. The
websocket badge and split-mode `/health` read the same per-adapter projection.

## 2. The cache in the two modes

In split mode the cache owns one filtered `feed-state` subscription. The subscription
admits `feed.connection` and `heartbeat`, drops older observations when its bounded queue
is full, and ignores market-data and unrelated events. Its consumer records each event and
its API-observation time. No local `FeedSupervisor` or Delta socket exists in this process.

In the monolith, `build_feed_stack` keeps the synchronous path from #40:

    def publish(event):
        feed_cache.apply(event)
        events.publish(event)

The wrapper updates the cache before FanOut publishes the event. It remains the low-lag
path for a browser connected to the in-process controller; the filtered subscription is the
process-boundary path, not a replacement for it.

## 3. Building the effective state

For an adapter with a recent `feed.connection`, the projection starts with that event's
`to_state` and `reason`. If the API started after that transition, the first heartbeat it
observes supplies `state` and establishes the adapter's effective state. This is the
late-start mechanism: `Heartbeat` already carries `state`; the feed makes no special replay
or startup change for the API.

The heartbeat silence rule is **`FEED_HEARTBEAT_STALE_SECONDS = 25.0` (`derived`)**.
The controller cadence is `controller.HEARTBEAT_SECONDS = 10.0` (a setting in the
code, not an assumption), so the bound tolerates two missed heartbeat intervals plus `derived` five
seconds of slack. Once the API-observation age of the latest heartbeat crosses the bound,
the projection synthesizes `state: "stopped"`, `reason: "silent"`. Its `since` is the
instant the stale bound was crossed, not the timestamp on the old heartbeat.

If no heartbeat has arrived yet, the first observed connection is the silence origin for
the same bound; the cache does not treat a missing heartbeat as healthy.

A fresh heartbeat replaces the synthesized stale result with its carried state. If that
state is `connected`, the next websocket `feed` message is the recovery and the web badge
clears. An unchanged effective state is coalesced; heartbeats do not flicker the badge.

The cache takes a clock dependency for observation age and stale-bound crossing. Tests use
a fake clock to advance API-observation time, cross the bound deterministically, and then
deliver a fresh heartbeat to prove recovery without waiting or reading wall clock time.

## 4. What the websocket sends

On accept, `/ws/chain` asks the projection for the adapter's effective state and sends the
`feed` envelope before `chain` or `waiting` when an observation exists. In split mode the
adapter name comes from the cache; in the monolith it is the supervisor's configured
adapter. It never reads a controller directly.

Each connection remembers the last effective state it sent. A repeated state is skipped,
including ordinary heartbeats; a new state, stale/silent synthesis, or fresh-heartbeat
recovery is sent once. This keeps a transition burst from flickering the badge while
preserving the state a new browser needs.

## 5. Failure modes

| Failure | What happens |
|---|---|
| No cache or no observation for an adapter | No `feed` message is guessed; `chain`, `waiting` and `error` are unaffected. |
| API starts after the feed is already connected | The first observed heartbeat hydrates the adapter state. |
| Heartbeat observation crosses the stale bound | Effective state becomes `stopped` with reason `silent`; `since` is the crossing time. |
| A fresh heartbeat arrives after synthesis | Its carried state becomes effective again; a changed state reaches the websocket and `/health`. |
| Two observations leave the effective state unchanged | The websocket coalesces them; `/health` still reports current ages. |

## 6. The seam the tests drive

The websocket tests inject `FeedConnectionCache` and a fake clock, then drive a
`FeedConnection` and `Heartbeat` through the filtered feed-state seam. They cover current
state on accept, late-start hydration, stale/silent synthesis, and fresh-heartbeat
recovery. The monolith test keeps the synchronous `publish` wrapper. `web/tests/feed.test.ts`
continues to pin `feedBadge`, the pure map from a `FeedStatus` to what the header shows.
