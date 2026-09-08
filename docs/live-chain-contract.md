# The `/ws/chain` envelope

The websocket [chain-contract.md](chain-contract.md) names but never spells out. That
file is the authority for the payload — `ChainResponse`, unchanged from `/chain` — and
this one is the authority for the **envelope** around it: the four things the socket can
say, and when it says each one.

**A fourth file split off `chain-contract.md` for the reason `recording-contract.md`
gives for itself:** two documents are only two authorities when they describe the same
thing, and the envelope is not the payload.

## `GET /ws/chain?underlying=BTC&expiry=04-09-2026&interval=1.0`

`interval` is optional, in seconds, floored server-side so a hand-edited URL cannot peg
the event loop the feed runs on — see `main.py`'s `MIN_PUSH_INTERVAL_SECONDS`.

Four message shapes, each a JSON object with a `type` field:

```json
{"type": "chain",   "data": {...ChainResponse}}
{"type": "waiting", "detail": "..."}
{"type": "error",   "detail": "..."}
{"type": "feed",    "data": {"adapter": "DELTA", "state": "degraded",
                              "since": "2026-09-08T09:21:04Z", "reason": "stale"}}
```

`chain` is the ladder, on the timer named by `interval`. `waiting` says the socket has
not spoken yet for this underlying and expiry — never an empty `chain`, which would read
as "Delta lists nothing" instead of "wait". `error` reports a bad `underlying` or
`expiry` and precedes a close; a websocket cannot answer 400, so it says why before it
goes.

## `feed` — the venue connection, not the browser's socket

**A different fact from the chip that already exists.** `web/lib/live.ts`'s
`LiveStatus` — `connecting` / `live` / `waiting` / `closed` / `error` — describes the
browser's own socket to *this engine*. `feed` describes the engine's socket to *Delta*,
and the two fail independently: the browser's connection to a healthy engine can read
`live` while the engine's connection to Delta reads `reconnecting`, and that gap is
exactly why this message exists. Nothing on screen may collapse the two into one
indicator.

`data`:

| field | meaning |
|---|---|
| `adapter` | the venue name, e.g. `"DELTA"` — matches `/health`'s `adapters[].adapter` |
| `state` | one of the five states below |
| `since` | ISO 8601 UTC, second precision, `Z`-suffixed — when the engine last **told a browser** the feed entered this state (see "Coalescing", below; not necessarily the instant the controller itself transitioned) |
| `reason` | one of the ten stable reasons below, or `""` |

Five states, `docs/design/events.md`'s `ConnectionState`: `connecting`, `connected`,
`degraded`, `reconnecting`, `stopped`. Ten reasons, all stable and greppable: `start`,
`resume`, `open`, `message`, `stale`, `silent`, `closed`, `backoff`, `stopped`, and
`paused` since #41 — a `stopped` an operator asked for rather than one the engine chose,
which the badge shows on hover and `/health` now carries too.

**Sent once on accept, with the feed's state as of that moment, before any `chain` or
`waiting`.** A browser that connects mid-outage must not render a ladder for one push
interval before learning the feed behind it is not well. Sent again on every state the
engine has told a *previous* browser about — see "Coalescing" — so a tab left open
during a real reconnect sees the badge arrive and clear on its own.

**No badge for `connected`.** The web app renders nothing for it; showing a "connected"
badge would put a second amber-adjacent object on a header that has to say "this is
fine" by saying nothing at all, which is the same restraint `docs/chain-contract.md`
already applies to a fully-quoted chain.

## Coalescing, and why

**The engine sends a `feed` message only when the reported state differs from the last
one it sent.** Two real transitions landing inside one push `interval` — plausible during
exactly the kind of flap #39 fixed one instance of — collapse into the newer of the two
rather than both reaching the browser. This is a deliberate trade: the ticket that added
this message was itself written against a bug where an announced redial fired a spurious
`connecting → reconnecting` pair roughly every attempt, 5–6 times in a ten-minute outage,
which would have flickered this exact badge through the whole incident an operator is
watching it for. Coalescing at the transport is what a periodic push already does to the
ladder itself — `/chain` does not send every tick either — and it costs nothing an
operator reads off this badge, because the two states it can hide are `connected` and a
non-`connected` state that immediately preceded another non-`connected` state, neither of
which changes what the badge shows.

**One adapter today.** The engine holds a single connection to Delta serving every
underlying it lists (`BTC` today; `docs/ingestion.md` and #43 cover `ETH`), so `/ws/chain`
reports on that one adapter regardless of the `underlying` it was asked for. A second
venue would need this envelope to say which adapter a given connection cares about —
not designed here, because there is only one to choose from.

## What an old client sees

`type` is a field like any other: a client written before this ticket landed reads
`chain`, `waiting` and `error` exactly as before and never receives a `type` it does not
recognise unless it asks for `feed` explicitly, which nothing but this file's readers
know to do. Nothing about the existing three messages changed shape.
