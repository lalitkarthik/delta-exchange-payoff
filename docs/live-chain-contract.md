# The `/ws/chain` envelope

The websocket [chain-contract.md](chain-contract.md) names but never spells out. That
file is the authority for the payload - `ChainResponse`, unchanged from `/chain` - and
this one is the authority for the **envelope** around it: the four things the socket can
say, and when it says each one.

**A fourth file split off `chain-contract.md` for the reason `recording-contract.md`
gives for itself:** two documents are only two authorities when they describe the same
thing, and the envelope is not the payload.

## `GET /ws/chain?underlying=BTC&expiry=04-09-2026`

`interval` is optional, in seconds, and is floored server-side so a hand-edited URL cannot
peg the event loop the feed runs on - see `main.py`'s `MIN_PUSH_INTERVAL_SECONDS`.

Four message shapes, each a JSON object with a `type` field:

```json
{"type": "chain",   "data": {...ChainResponse}}
{"type": "waiting", "detail": "..."}
{"type": "error",   "detail": "..."}
{"type": "feed",    "data": {"adapter": "DELTA", "state": "degraded",
                              "since": "2026-09-08T09:21:04Z", "reason": "stale"}}
```

`chain` is the ladder, on the timer named by `interval`. `waiting` says the socket has
not spoken yet for this underlying and expiry - never an empty `chain`, which would read
as "Delta lists nothing" instead of "wait". `error` reports a bad `underlying` or
`expiry` and precedes a close; a websocket cannot answer 400, so it says why before it
goes.

## `feed` - the venue connection, not the browser's socket

`web/lib/live.ts`'s `LiveStatus` - `connecting` / `live` / `waiting` / `closed` / `error` -
describes the browser's socket to *this engine*. `feed` describes the engine's socket to
Delta. The two fail independently: a browser can read `live` from a healthy engine while
the engine reports `reconnecting` for Delta. Nothing on screen may collapse them into one
indicator.

`data`:

| field | meaning |
|---|---|
| `adapter` | The venue name, e.g. `"DELTA"`; it matches `/health`'s `adapters[].adapter`. |
| `state` | The effective per-adapter state: the latest observed `feed.connection`, or the `state` carried by a heartbeat when that hydrates a late API. Heartbeat silence can synthesize `stopped`. |
| `since` | ISO 8601 UTC, second precision, `Z`-suffixed: when the API learned the effective state, or when the heartbeat stale bound was crossed for synthesized `stopped`. It is not when the browser happened to ask. |
| `reason` | The latest connection reason, or `"silent"` for a synthesized stale result; `""` when no reason is available. |

Five states, `docs/design/events.md`'s `ConnectionState`: `connecting`, `connected`,
`degraded`, `reconnecting`, `stopped`. Reasons remain stable and greppable: `start`,
`resume`, `open`, `message`, `stale`, `silent`, `closed`, `backoff`, `stopped`, and
`paused` since #41. A `stopped` requested by an operator is therefore distinct from
the `stopped` synthesized by heartbeat silence.

The API's projection uses **`FEED_HEARTBEAT_STALE_SECONDS = 25.0` (`derived`)**. The
controller emits heartbeats every `controller.HEARTBEAT_SECONDS = 10.0` (`assumed`, code
constant), so the bound allows two missed heartbeat intervals plus `derived` five seconds
of slack. A stale heartbeat can therefore turn the badge into `stopped` / `silent` without
a new feed transition. Before the first heartbeat, the first observed connection is the
silence origin; a missing heartbeat is never treated as healthy.

## When `feed` is sent

When the cache already has an observation, the websocket sends `feed` on accept, before
any `chain` or `waiting`. If the API starts after the feed is connected, the first
heartbeat it observes establishes the adapter's state; no feed-side startup change is
needed. The same cache projection supplies `/health` and the websocket badge.

The websocket sends again when that effective state changes. A heartbeat that leaves the
state unchanged only refreshes its age in `/health`; it does not flicker the badge. If
heartbeat silence crosses the stale bound, `stopped` / `silent` is sent. A fresh heartbeat
then restores its carried state, and a recovery to `connected` clears the web badge.

One adapter is configured today. `/ws/chain` reports that adapter regardless of the
requested underlying; a second venue would need an explicit adapter selector.

## What an old client sees

The three original message shapes are unchanged. A client written before `feed` can keep
handling `chain`, `waiting` and `error`; it ignores an unrecognised `type` according to its
normal envelope handling. Clients that render the badge use the `feed` projection above,
not the browser socket's `LiveStatus`.
