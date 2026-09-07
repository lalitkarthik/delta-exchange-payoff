# Engine Feed Management: broker adapters, connection control, events, logging, and the screens that read the store

> Published as [issue #33](https://github.com/lalitkarthik/delta-exchange-payoff/issues/33). The issue is the record; this file is the copy that ships with the code.

**One microservice, drawn on the whiteboard: brokers on the left, adapters, then "Engine Feed
Management" with a connection controller inside it, then a queue.** This spec is that box and
the two screens that read what it stores. The order-execution side of the drawing and the
message broker itself are later work and are named in *Out of Scope*.

Every number in this spec is tagged `measured`, `assumed` or `derived`, with the run that
produced it. A number without a tag is a bug in the spec.

---

## Problem Statement

The engine knows one venue and says so everywhere. Delta's symbol spelling (`C-BTC-60000-270624`)
is parsed in the wire decoder, keyed in the chain cache, stored in the bar tables and read back
by the screens. Delta's two websocket channels are named as such on the bus. When a second
broker arrives — NSE spells the same instrument `NIFTY-20260908-25500CE`, in rupees, in lots,
with a different calendar — there is no place to put its adapter, because there is no adapter:
the feed *is* the venue client.

The connection to that one venue is managed inside the feed loop. It reconnects and it
resubscribes, and it does both silently: nothing outside the loop knows whether the socket is
up, degraded, or gone, `/health` says `ok` regardless, and a browser looking at a ladder that
stopped moving cannot tell a quiet market from a dead feed. That is the plausible-and-wrong
failure this project keeps refusing, and today it is the default.

The engine writes almost no logs. One logger exists; nothing structured, nothing a person could
grep by instrument or connection state after the fact. A three-day hole in the store
(2026-09-04 09:38Z to 2026-09-07 09:45Z, `measured` from file timestamps) went unnoticed
because there was nothing to notice it with.

The hot loop solves implied volatility for every listed expiry on every tick — eight expiries
today (`measured` 2026-09-07, `/expiries`) — whether or not anyone is looking at them. One
expiry costs `measured` 7.25 ms on the 69-row fixture (previous session, `tools/measure_bus.py`);
eight is `derived` 58 ms of a 100 ms tick. That figure is flagged `assumed` until it is re-run
against the live engine, but the shape of the problem does not depend on the exact number:
work scales with what is listed, not with what is watched. Adding ETH doubles it; adding a
second venue doubles it again.

The store holds every minute of every contract since recording began, and the screens show
almost none of it. There is no way to see what a contract's price did over the day, and no way
to see the ladder as it stood an hour ago. The volatility screen has a scrubber; the option
chain does not.

And none of this is written down as a design. What the system is, how its parts talk, what
events cross between them — that lives in module docstrings and in the heads of two people.

## Solution

Split the feed into three named things and put an event between each of them.

A **broker adapter** owns everything venue-specific: the socket, the REST calls, the symbol
spelling, the channel names, the wire layout. It emits **canonical events** carrying a
**canonical instrument**, and nothing downstream of it ever sees venue JSON. Delta is the first
adapter. NSE is the test of the abstraction and is not built.

A **connection controller** wraps each adapter with a state machine — connecting, connected,
degraded, reconnecting, stopped — and owns backoff, the reconnect budget, subscription replay
and staleness detection. It reports state on `/health`, emits connection events on the bus, and
pushes them to every open browser so the ladder wears a badge when its feed is not. A
**supervisor** holds one controller per adapter and answers for all of them.

**Events** are typed, versioned, registered, and carry two timestamps — the venue's and ours —
because the gap between them is a number this project measures. The catalogue is small and
written down.

**Logging** is structured JSON lines, one file per day, with the instrument and the connection
state on every record that has one, and pretty output on a terminal.

**Compute follows the page.** Raw quotes are stored for every contract of every recorded
underlying, always — the record has no holes that we chose. Our IV and Greeks are stored for
every expiry once a minute, stamped with the model that produced them, and rebuildable from raw.
The 100 ms live solve runs only for the `(underlying, expiry)` pairs a browser is looking at,
with a short grace after the last viewer leaves. ETH joins BTC on the feed, and the cost of
that is measured before it is called fine.

Two screens read the store back: a **contract chart** — candles of a contract's minute bars,
opened by clicking a strike on the ladder — and a **historical chain** — a slider on the
option-chain page whose right edge is live and whose left is every stored minute of the day.

The **design is written down**: a high-level design, an events catalogue, and one low-level
design per component, in the repository beside the code, superseding the architecture note
that predates all of this.

## User Stories

### Broker adapters and the canonical instrument

1. As an engine maintainer, I want every venue-specific detail behind one adapter interface, so that adding a second broker means writing one class and no other module changes.
2. As an engine maintainer, I want a canonical instrument model with venue, underlying, expiry, strike and right as typed fields, so that no downstream code parses a symbol string.
3. As an engine maintainer, I want the canonical instrument to keep the venue's own symbol beside it, so that a request back to the venue needs no reverse lookup.
4. As an engine maintainer, I want one canonical string form derived from the instrument, so that logs, cache keys and URLs agree on one spelling.
5. As an engine maintainer, I want the Delta adapter to emit canonical events from both of Delta's channels, so that nothing downstream knows there were two channels.
6. As an engine maintainer, I want the adapter to own the distinction between the venue's absent-quote spellings and a real zero, so that `null` is not `0` holds at the one boundary where the venue's data enters.
7. As an engine maintainer, I want the adapter to declare which underlyings it records, so that the recorded set is configuration and not a constant in the application module.
8. As a future NSE integrator, I want the adapter interface to demand only what a feed needs — list instruments, subscribe, stream events, describe itself — so that a venue with a different shape can still implement it.
9. As a tester, I want a scripted fake adapter that replays fixture frames, disconnects on cue and goes silent on cue, so that every component above the adapter can be tested from the HTTP seam without a network.

### Events

10. As an engine maintainer, I want every event to carry a type, an id, a schema version and a source, so that any event can be identified, deduplicated and attributed on its own.
11. As an engine maintainer, I want every event to carry both the venue's timestamp and our arrival timestamp, so that arrival lag is a column and not a separate measurement.
12. As an engine maintainer, I want events to be registered by type in one place, so that an unknown type is an error at parse time and the catalogue is enumerable.
13. As an engine maintainer, I want events to be immutable once built, so that a consumer holding one cannot be surprised by another.
14. As an engine maintainer, I want a top-of-book quote event, a venue reference event, an index quote event and a sealed-bar event, so that the four kinds of market data the store already distinguishes are distinguished on the bus.
15. As an engine maintainer, I want a computed-chain event carrying our IV and Greeks for one expiry, so that a consumer can take our numbers without reaching into the chain cache.
16. As an engine maintainer, I want connection, heartbeat and alert events, so that the health of the feed is data on the bus and not a side channel.
17. As an engine maintainer, I want an inbound command event, so that pause, resume and reconnect are the same kind of thing as everything else on the bus.
18. As an engine maintainer, I want the bus behind an interface with publish and subscribe, so that a broker can replace the in-process fan-out without touching a producer or a consumer.
19. As an engine maintainer, I want the bus to keep the existing choice of lossless subscription for the store and drop-oldest for the screen, so that the two consumers do not fight over one queue.
20. As a reader of the design, I want the events catalogue as a document beside the code, so that the names, fields and directions are readable without opening the registry.

### Connection control

21. As an operator, I want each adapter's connection to be in exactly one of connecting, connected, degraded, reconnecting or stopped, so that its state can be read at a glance.
22. As an operator, I want a connection that receives nothing for a bounded interval to be marked degraded, so that a silent socket is distinguished from a quiet market.
23. As an operator, I want reconnects to back off and to be counted against a lifetime budget, so that a feed that reconnects daily cannot exhaust a venue's tolerance overnight.
24. As an operator, I want every subscription replayed after a reconnect, so that a reconnected socket that receives nothing is impossible rather than silent.
25. As an operator, I want to pause, resume and force a reconnect of a specific adapter, so that a misbehaving feed can be dealt with without restarting the engine.
26. As an operator, I want `/health` to report every adapter's state, the time of its last message, its reconnect count and its remaining budget, so that liveness means the feed is alive and not just the process.
27. As an operator, I want a supervisor that holds every controller and reports the worst state among them, so that one call answers for the whole feed.
28. As a browser user, I want the ladder to show a badge when its feed is degraded or reconnecting, so that I do not read a stale ladder as a live one.
29. As a browser user, I want the badge to clear on its own when the feed recovers, so that I do not have to reload to find out.
30. As a tester, I want connection state transitions to be observable at the HTTP seam, so that the controller is tested by what it reports and not by how it reports it.

### Logging

31. As an operator, I want every log record as one JSON object on one line, so that a day of logs can be filtered by any field with standard tools.
32. As an operator, I want the instrument and the connection state on every record that has one, so that "what happened to this contract" and "what happened while degraded" are each one query.
33. As an operator, I want the event id on records that concern an event, so that a log line and the event it describes can be joined.
34. As an operator, I want one log file per day, rotated automatically, so that the log does not grow without bound and a day can be archived on its own.
35. As a developer at a terminal, I want readable, coloured output when a terminal is attached, so that development does not mean reading JSON.
36. As an engine maintainer, I want logging on the standard library with no new dependency, so that the format is ours and the runtime is unchanged.
37. As an operator, I want state transitions, reconnects, budget exhaustion, staleness, flush results and dropped messages logged at levels that reflect their severity, so that a warning means something.
38. As a tester, I want log records capturable in tests, so that "this path logs a warning with these fields" is a test and not a hope.

### Compute and store

39. As an analyst, I want raw quotes stored for every contract of every recorded underlying, always, so that the record has no holes we chose.
40. As an analyst, I want our IV and Greeks stored for every expiry once a minute, stamped with the model and solver that produced them, so that the volatility screen reads them in milliseconds rather than solving a day on every load.
41. As an engine maintainer, I want the stored computed bars to be rebuildable from raw quotes by a batch job, so that changing the model does not orphan the history.
42. As an engine maintainer, I want the live solve to run only for the `(underlying, expiry)` pairs a browser is watching, so that the hot loop's cost scales with viewers and not with the venue's listing.
43. As a browser user, I want the live solve to keep running for a short grace after I leave an expiry, so that switching back is instant.
44. As an engine maintainer, I want the set of watched pairs visible on `/health`, so that "what is the engine solving right now" is answerable.
45. As an analyst, I want ETH recorded beside BTC, so that the store covers both underlyings the venue lists.
46. As an engine maintainer, I want the feed's message rate and bandwidth measured for sixty seconds after ETH is enabled and written down, so that the cost is a number and not a guess.
47. As an analyst, I want a minute with no arrivals to produce no row, in every table, still, so that the no-forward-fill rule survives the refactor.

### Contract chart

48. As a trader, I want to click a strike on the ladder and see that contract's minute candles for the day, so that I can see what the price did and not only what it is.
49. As a trader, I want the candles built from the stored bid/ask midpoint, so that they describe the book rather than a trade that may not have happened.
50. As a trader, I want to toggle the candles to the venue's last-traded price, so that I can compare what the book said with what printed.
51. As a trader, I want bid and ask drawn as lines over the candles, so that the spread is visible at every minute.
52. As a trader, I want a gap in the stored minutes drawn as a gap, so that I am never shown a candle that was invented.
53. As a trader, I want the URL to carry the contract, so that a chart can be sent to someone.
54. As a trader, I want the chart to follow the live minute at its right edge while the page is open, so that the chart and the ladder agree.
55. As a browser user, I want the chart to be a proper financial chart — candles, crosshair, time axis, price axis, zoom — so that it reads like the terminal I already use.
56. As an engine maintainer, I want one REST route that serves a contract's bars for a date, so that the chart has one contract to honour.

### Historical chain

57. As a trader, I want a time slider on the option-chain page whose right edge is live, so that history is one drag away and live is where I left it.
58. As a trader, I want to drag left and see the ladder as it stood at that minute, so that I can see how the chain moved into the present.
59. As a trader, I want the historical ladder to carry the same columns as the live one — quotes, the venue's reference values, and our IV and Greeks — so that the two are compared like for like.
60. As a trader, I want a minute with no stored data shown as no data, so that I am never shown a previous minute's ladder as that minute's.
61. As a trader, I want the URL to carry the minute I am looking at, so that a moment in the chain can be shared.
62. As a browser user, I want releasing the slider at the right edge to resume the live stream, so that history and live are one control and not two modes.
63. As an engine maintainer, I want one REST route that serves the ladder at a stored minute, built from the three bar tables, so that the slider has one contract to honour.
64. As an engine maintainer, I want the historical ladder to be the same response shape the live one is, so that the ladder component renders either unchanged.

### Design documents

65. As a reader of the design, I want a high-level design that names every component in the whiteboard drawing and how each talks to the next, so that the architecture is readable without reading code.
66. As a reader of the design, I want one low-level design per component, written as the component lands, so that the design records what was built and not what was planned.
67. As a reader of the design, I want the previous architecture note superseded with a pointer rather than deleted, so that links into it still resolve.
68. As a reader of the vault, I want the vault to hold pointers into these documents and not copies, so that one copy exists and cannot drift.
69. As a future maintainer, I want the design documents to state which decisions were measured and which were taken on judgement, so that a later reader knows which to re-test.

## Implementation Decisions

### The instrument

- An `Instrument` is a frozen record: `venue`, `underlying`, `expiry` as a calendar date,
  `strike` as a decimal, `right` as call or put, and `venue_symbol` as the venue's own
  spelling, kept verbatim.
- Its canonical string is derived, never stored as a source of truth:
  `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P`, e.g. `DELTA-BTC-20260627-60000-C`. Hyphen-separated
  throughout; ISO date; strike printed without trailing zeros.
- Fields NSE will need — currency, lot size, tick size, settlement style, trading calendar —
  are **not** added now. Deferred deliberately; the second adapter is the moment to add them
  against a real need.
- The chain cache, the store's columns and the screens' contract keep `underlying`, `expiry`,
  `strike` and `option_type` as they are today; the instrument is the boundary type, and the
  existing wire contract is derived from it. The engine↔web contract document remains the
  authority for the web side and is updated where the badge and the two new routes touch it.

### The adapter

- An `Adapter` protocol with four responsibilities: describe itself (venue name, recorded
  underlyings), list instruments for an underlying, subscribe to a set of instruments, and
  stream canonical events until stopped. It also carries the venue's REST reads the screens
  need — expiries and a chain snapshot — so the venue client is inside the adapter and not
  beside it.
- The Delta adapter is the existing feed, venue client and wire decoder moved behind that
  protocol. Delta's two channels become two event types at the adapter's edge. The
  `null`-is-not-`0` conversion happens here and nowhere else.
- The adapter's `connect` factory stays injectable, exactly as it is today; that is the seam
  the existing feed tests use and the fake adapter reuses.
- The recorded set of underlyings becomes configuration read at start-up, defaulting to BTC and
  ETH. Every contract of every recorded underlying is subscribed on both channels. Narrowing
  the subscription to watched expiries was built and reverted once already and is not reopened.
- The fake adapter is a test double implementing the protocol and driven by a script: emit
  these frames, close the socket, stay silent for this long, resume. It is the one new seam.

### Events

- A base event carries `type`, `event_id`, `schema_version`, `source`, `ts_venue`,
  `ts_received` and `instrument` (nullable). Frozen, extra fields forbidden, registered by
  `type` in one registry; parsing an unregistered type raises.
- `ts_venue` is the venue's stamp as the venue gave it; `ts_received` is our wall clock at
  arrival. Neither is a latency clock; elapsed time is measured on the monotonic clock as
  today. The arrival-lag column is `ts_received − ts_venue` and is what the previous session
  measured by hand.
- Outbound catalogue: `md.option_quote` (top of book), `md.option_reference` (the venue's mark,
  last trade, open interest and its own IV and Greeks — reference columns, never inputs),
  `md.index_quote` (spot), `md.option_bar` (a sealed minute bar, one per table), `computed.chain`
  (our IV and Greeks for one expiry, stamped with model, solver and forward method),
  `feed.connection` (a state transition, with from-state, to-state, reason and adapter),
  `heartbeat` (periodic, per adapter, carrying last-message age), `alert` (something a person
  should see: budget nearly spent, staleness, a flush that failed).
- Inbound: `control.command` with a target adapter and one of pause, resume, reconnect.
- Schema version starts at 1 and is bumped when a field changes meaning, not when one is added
  with a default.
- The bus is an interface with `publish(event)` and `subscribe(name, maxsize, lossless)`,
  implemented by the existing in-process fan-out. Subscription semantics are unchanged: the
  store's subscription is lossless, the screen's drops oldest. A future broker implements the
  same interface; producers and consumers do not change.
- The existing quote record is replaced by the events; the chain cache and the bar writer
  consume events rather than channel-tagged frames.

### The connection controller

- One controller per adapter. States: `connecting`, `connected`, `degraded`, `reconnecting`,
  `stopped`. Transitions are the only way state changes, each emits a `feed.connection` event
  and a log record.
- `connected → degraded` when no message has arrived for a bounded interval; the interval is
  configuration, default `assumed` 15 s, chosen as three ticker refreshes (`measured` 5001 ms
  each) — re-measure against the live feed's longest quiet gap before the default is trusted.
- `degraded → connected` on the next message. `degraded → reconnecting` when staleness exceeds
  a second, longer bound. Any socket close → `reconnecting`.
- Reconnect backoff and the lifetime reconnect budget move out of the feed loop into the
  controller, keeping the existing values. Budget exhaustion → `stopped`, an `alert`, and a
  log record at error level.
- Subscription replay after reconnect is the controller's job; the never-cleared
  channel-to-symbols map moves with it.
- The controller accepts `control.command`: `pause` → `stopped` without spending budget,
  `resume` → `connecting`, `reconnect` → close and `reconnecting`.
- A supervisor holds every controller, starts and stops them with the application lifespan,
  and reports the worst state among them as the feed's state.
- `/health` grows from liveness to a report: overall feed state, and per adapter its state,
  last message time, last-message age, reconnect count, budget remaining, and the watched
  pairs currently being solved. The existing `{"status": "ok"}` shape is preserved as one
  field of the report.
- The chain websocket gains a fourth message type, `feed`, carrying the adapter's state, sent
  on every transition and once on connect. The web contract document and its mirror gain it.
  The page renders it as a badge on the ladder header; no badge when `connected`.

### Logging

- Standard-library logging with a JSON-lines formatter of our own. Fields on every record:
  `ts`, `level`, `logger`, `event` (a short stable name for what happened, not free text), and
  `msg`. Present when known: `venue`, `instrument` (canonical string), `conn_state`,
  `event_id`. Extra fields are allowed and land beside these.
- Sink: one file per day, named by date, under a gitignored logs directory at the repository
  root, rotated at midnight UTC. When standard error is a terminal, a second, human-readable,
  coloured handler is attached; when it is not, the file alone.
- The web side keeps the browser console.
- What is logged, and at what level: state transitions (info; error on `stopped` by budget),
  every reconnect (warning), staleness entering and leaving (warning, info), every flush with
  row counts and duration (info), a dropped message on a lossless queue (error — it should be
  impossible), the recompute set changing (debug), each websocket client attaching and
  detaching (debug), and every `alert` event (warning).

### Compute follows the page

- The chain websocket handler registers interest in its `(underlying, expiry)` on accept and
  releases it on close. A reference count per pair lives in the chain cache; the live solve
  pass solves only pairs with a count above zero or inside the grace window.
- Grace after the last release: `assumed` 30 s. Long enough that flipping between two expiries
  never waits; short enough that an abandoned tab stops costing within a minute.
- A separate minute-cadence pass solves every expiry with a frame since its last pass and hands
  the result to the computed-bars aggregator, as the writer's sampling already does; it does
  not depend on any viewer. Its cost is `derived` 8 × 7.25 ms ≈ 58 ms per minute per
  underlying against the fixture figure, re-measured live before this is quoted anywhere else.
- Computed bars gain nothing new in shape; they already carry the model stamp. A batch rebuild
  from quote bars is specified as a tool, not a route, and is out of scope to build now.
- The recorded underlyings default becomes BTC and ETH. The feed's message rate and bandwidth
  are measured for sixty seconds with both enabled and recorded in the design document, tagged
  `measured`, beside the BTC-only figure (`measured` ~600 messages/s, ~300 KB/s).
- The no-forward-fill rule is unchanged: a minute with no arrivals produces no row.

### The contract chart

- A REST route serves one contract's minute bars for one date, from quote-bars (bid, ask, mid
  OHLC) joined to reference-bars (last trade OHLC) on the minute. Missing minutes are absent
  from the response, not null rows. The instrument is addressed by its canonical string.
- On the web, clicking a strike row opens a chart panel on the chain page. The URL carries the
  contract's canonical string so the panel opens on load when present.
- The chart is TradingView's `lightweight-charts`: candlestick series on mid by default, a
  toggle to last-traded price, bid and ask as line series over the candles. The library's gap
  handling is used so an absent minute is a gap on the time axis.
- While the page is open, the chart's last candle updates from the live chain's current
  quote for that contract, so the chart and the ladder agree at the right edge.
- The panel follows the app's existing theme and rail; the chart's colours come from the theme
  tokens, not the library's defaults.

### The historical chain

- A REST route serves the ladder for one `(underlying, expiry)` at one stored minute, rebuilt
  from quote-bars, reference-bars and computed-bars, in the same response shape as the live
  chain, with a field naming the minute it describes. A minute with no stored quotes answers
  with the same "nothing here" shape the live socket uses for `waiting`, never a ladder from an
  adjacent minute.
- A second route lists the stored minutes for the day, so the slider knows its domain and its
  gaps.
- On the web, the option-chain page gains a time slider following the volatility screen's
  scrubber: right edge is live and keeps the socket open; dragging left detaches from the
  socket and fetches the minute's ladder; releasing at the right edge reattaches. The URL
  carries the minute.
- The ladder component renders live and historical responses unchanged; the header shows which
  it is showing.

### Design documents

- Three kinds, in a design folder under the repository's docs: a high-level design covering the
  whiteboard's components and the flows between them; an events catalogue; and one low-level
  design per component, written when the component lands.
- The existing architecture note is superseded with a pointer at its top, not deleted.
- The vault records pointers to these documents in its domain notes and index; no content is
  copied.
- Every number in the design documents carries `measured`, `assumed` or `derived` and names its
  run.

## Testing Decisions

**A good test drives the system from the outside and asserts on what it says**, not on how it
said it. It feeds frames the venue actually sent — the fixtures are verbatim captures — and
reads back over HTTP, the websocket, the log, or the store on disk. It never reaches into a
controller to check a private state; it reads `/health`. It never asserts that a method was
called; it asserts that a badge message arrived. No test touches the network; the existing
fixtures that raise on a real client stay in force.

**Seams, highest first:**

- **The application under the test client** with the live feed disabled, exactly as the API,
  websocket, recording and smile tests do today. This is where the controller, the badge,
  `/health`, watched-only solving, the two new routes and the command path are all tested.
- **The fake adapter**, injected where the feed is built. Its script — frames, close, silence,
  resume — is the input for every controller test. It is the one new seam.
- **The adapter's frame-to-event boundary**, as the feed tests do today with the captured
  websocket frames: frames in, canonical events out, absent-quote spellings become `null`.
- **The bus**, as the fan-out tests do today: lossless and drop-oldest semantics unchanged
  under the interface.
- **The store on a temporary directory**, as the store and bars tests do today: the minute pass
  writes computed bars whether or not anyone is watching; no row for an empty minute.
- **Log capture** with the standard fixture: transitions and reconnects produce records with the
  expected `event`, level and fields.

**Modules under test:** the instrument and its string form; the event registry and envelope;
the Delta adapter's decoding; the controller's transitions and budget; the supervisor's
aggregate state; the health report; the websocket's `feed` message; the interest refcount and
its grace; the minute-cadence computed pass; the bars route; the historical chain routes; the
JSON log formatter.

**Prior art:** the websocket endpoint tests for message-type envelopes; the feed tests for
frame-driven decoding with an injected connect factory; the fan-out tests for queue policy; the
store tests for on-disk shape and the no-forward-fill rule; the no-Delta-inputs test for the
reference-only invariant, which must keep passing with the reference event in place; the
recording tests for a state route read and written over HTTP.

**Web:** no test runner exists; `typecheck` and `build` must pass, and the contract mirror must
match the contract document field for field, including the new `feed` message and the two new
response shapes.

## Out of Scope

- **The message broker.** Undecided. The bus interface is built so one can be slotted in; no
  Redis, Kafka or ZeroMQ is deployed, and the in-process fan-out remains the implementation.
- **Order execution, position management, the sandbox, and everything on the right half of the
  whiteboard.** Later components; not designed here beyond leaving the event envelope shaped so
  they can share it.
- **An NSE adapter, and the instrument fields it will need** — currency, lot size, tick size,
  settlement style, calendar. Added when the adapter is.
- **The computed-bars rebuild tool.** Specified as a batch job from quote bars; not built.
- **Backfilling the three-day hole** in the store. Not recoverable.
- **The IV-versus-realised work** (issues #24–#31). Another branch, another agent.
- **A web test runner.** Typecheck and build remain the gate.
- **Changing the four bar tables' schemas** beyond what the two new routes read.

## Further Notes

- **The fixture solve figure is not yet a live figure.** 7.25 ms per expiry is `measured` on a
  69-row fixture; the live path is warmer and the ladders are larger. Re-measure against the
  running engine before it appears in any design document, and record the run.
- **The vault and the repository use different default branch names** — `master` and `main`.
  A push to the wrong one has failed before.
- **Another agent is active in this repository on a separate branch.** This work goes on its
  own branch off `main`; nothing here touches the volatility screen's files.
- **Ticketing comes after this spec is agreed**, not with it. The build order when it does:
  the high-level design and events catalogue first, because every later ticket is typed against
  the names they fix; then instrument and events in code; the Delta adapter behind the
  protocol; the controller and supervisor; logging; ETH measured; watched-only solving; the
  historical chain; the contract chart; and a low-level design per component as each lands.
- **Commits carry the repository owner's name only.** No assistant attribution anywhere.
- **Two of the previous session's three errors were claims that outran their evidence.** Every
  number here is tagged for that reason; keep it that way in the tickets.
