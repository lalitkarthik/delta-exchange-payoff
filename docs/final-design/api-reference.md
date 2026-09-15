# API reference

Every route the engine serves. `docs/chain-contract.md` is the authority on the ladder's shape and
`web/lib/contract.ts` mirrors it field for field; this page is the surface, not the schemas.

**Two mutating routes and no more.** Everything else is a read.

## Conventions

- **Every decimal is a JSON number or `null`, never a string.** The web app never calls
  `parseFloat`, and raises `ContractViolationError` if the engine breaches this.
- **IV is a decimal fraction** on the wire and a percentage only on screen.
- **`underlying`** is `BTC` or `ETH`. **`expiry`** is `DD-MM-YYYY`, as the venue spells it.
  **`date`** is `YYYY-MM-DD`, the store's own spelling. **`minute`** is ISO 8601 UTC at second
  precision.
- **Absence from the store is `200` and empty, never `404`.** A day nobody has lived through yet is
  "nothing yet", not an error. Only the venue-backed routes can answer `502`.
- Interactive docs are at `/docs`, `/redoc` and `/openapi.json` -- **not proxied** by the stack.

## Live chain

| Route | Query | Returns |
|---|---|---|
| `GET /expiries` | `underlying` | Every listed expiry, ascending. The dropdown's source |
| `GET /chain` | `underlying`, `expiry` | The pivoted ladder for one expiry |

In split mode both answer from `ChainStream` rather than the venue, so the api never opens a socket
to Delta to serve a screen.

### `WS /ws/chain`

Query `underlying`, `expiry`, and an optional `interval` (default one second).

**The payload is the identical `ChainResponse` `/chain` returns**, wrapped in an envelope so the
four things the socket can say stay distinguishable -- a ladder, a `waiting`, a `feed` state change
and an error. `ChainLadder.tsx` renders either unchanged.

The socket **sends the feed state once on connect**, so a browser that joined mid-stream is not
blind, and forwards every `feed.connection` transition after that. It is a handshake, not a
cross-origin request, so CORS does not apply to it.

## History

All four read Parquet and never the venue.

| Route | Query | Returns |
|---|---|---|
| `GET /chain/minutes` | `underlying`, `expiry`, `date` | Every minute the store holds quotes for. The slider's domain, and by omission its gaps |
| `GET /chain/at` | `underlying`, `expiry`, `minute` | The ladder as it stood at one stored minute, **in the `/ws/chain` envelope** so a client needs no third vocabulary |
| `GET /bars` | `instrument` (canonical string), `date` | One contract's minute bars, from `quote-bars` and `reference-bars` |
| `GET /smile` | `underlying`, `expiry` | Every stored minute of implied volatility for one expiry |

`GET /bars?instrument=DELTA-BTC-20260627-60000-C-USD&date=2026-09-08`.

**These are `def`, not `async def`, and that is deliberate.** They open Parquet files, which blocks;
FastAPI runs a plain `def` route on a thread pool, so a slow read cannot stall the event loop that
is reading the venue socket.

## Volatility

| Route | Query | Returns |
|---|---|---|
| `GET /volatility/bounds` | `underlying`, `interval` (default `1m`) | What `N` may be, before anyone has chosen one |
| `GET /volatility` | `underlying`, `lookback_days`, `interval`, `estimators`, `alignment`, `max_points` | Implied against realised, in the units the chart draws |

**A store holding nothing usable answers `200` with `usable: false`**, not an error. "No lookback
works yet" is a real answer a screen can print, and printing it is the difference between an
instrument that is honest about its range and one that looks broken for a month.

`lookback_days` is deliberately one parameter rather than two: it sets both series at once, so the
two cannot be compared over different windows by accident.

## Operations

| Route | Body / path | Does |
|---|---|---|
| `GET /health` | -- | Liveness, watched pairs, and the **remote** feed state |
| `GET /recording` | -- | Whether the store is writing |
| `POST /recording` | `{"recording": bool}` | Stop or start the store |
| `POST /feed/{adapter}/{command}` | `pause`, `resume`, `reconnect` | Drive one adapter's connection |

### `/health`

Authoritative about remote feed state, projected from the `feed.connection` and `heartbeat`
observations in `FeedConnectionCache`, and carrying process liveness and the watched pairs beside
it. The websocket badge is derived from that same projection, heartbeat-silence rule included.

The other two processes serve their own `/health`: **`feed`** answers 503 when stopped, paused, out
of reconnect budget, silent past 135 s, or when its bus reader, flusher or control consumer has
died. **`store`** answers readiness rather than liveness -- 503 once its reader stops, its group is
trimmed past, or its lag passes the threshold.

### `POST /recording`

**The engine's only mutating data route, and the state lives here and nowhere else** -- not in the
browser and not in `localStorage`. Two tabs must not be able to disagree about whether the store is
writing, and a reader arriving on a fresh page is told the truth rather than a default.

It answers with the state **after** the change, so a client needs no second request and cannot
render a state that was never true. Idempotent. **Switching off flushes what is buffered before it
stops**: the buffer holds up to a five-minute interval of sealed bars, and discarding them would
throw away data the store already has.

### `POST /feed/{adapter}/{command}`

The route is thin on purpose: it checks the two names, builds one `control.command` and hands it to
the supervisor, which puts it on the bus and offers it to the controller that owns the adapter.
**Everything a command does is the controller's.** No service-to-service HTTP is involved.

| Command | Effect |
|---|---|
| `pause` | To `stopped`, reason `paused`, **spending no reconnect budget** |
| `resume` | To `connecting`, with the budget restored in full |
| `reconnect` | Cut the socket and let ordinary close handling reach `reconnecting`. Feed-only |

**It answers with the adapter's health line after the command has been applied**, not before, so the
response is never a claim about an intention.

## Behind a proxy

**The browser always sees one origin**, in the Docker stack and in production alike. `/api/` is
stripped and forwarded to the api; everything else is the front end. So `/api/chain` reaches the
engine as `/chain`, and the websocket uses the same prefix.

| Environment | What does the stripping |
|---|---|
| The Docker stack | the `nginx` proxy container, on one host port |
| Production | Amplify's 200 rewrite of `/api/<*>` to the instance's HTTPS endpoint |
| `bun run dev` | nothing -- the browser calls port 8000 directly, under the CORS allow-list |

**`/volatility` is a deliberate collision**: the api declares the route and the web app has a page
at the same path, so no per-route table could serve both. The api's series wins, and the web page at
that path is not reachable through the proxy today.

## Related guides

[Architecture](architecture.md) | [Data store](data-store.md) | [Events](events.md)
