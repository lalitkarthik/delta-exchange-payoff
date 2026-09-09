# Epic: split the engine into services on a Redis Streams bus

Decided 2026-09-09 in a grilling session. State lives in the GitHub issue that mirrors this
file; where they disagree the issue wins. Terms are the events catalogue's and `hld.md`'s.

## Problem Statement

One process holds everything: the Delta adapter, the connection controller and supervisor,
the chain cache with our IV and Greeks, the bar writer, the REST routes and the websocket.
A disk stall can hold up the socket reader. A restart to change the screen restarts the
recorder, and every restart throws away up to five minutes of unflushed bars. Nothing can
be added beside it — a second dashboard, an order path, an alert channel — without being
compiled into it. The senior's architecture puts a message bus between the data feed, the
database and the dashboard so each is deployable alone, and asks for two documents: the
cloud architecture of the message bus and of the data feed engine. Neither exists, and
nothing here runs anywhere but one laptop. The dashboard also prints prices with no
currency, which stops being harmless the day NIFTY arrives in INR beside BTC in USD.

## Solution

Four services and a Redis. `feed` owns the adapter, the controller and the supervisor and
publishes every canonical event onto Redis Streams in pipelined batches. `store` consumes
every event losslessly and writes the four Parquet tables. `api` consumes the same events
into the chain cache, solves our IV and Greeks, and serves the REST routes and the
websocket unchanged. `web` is unchanged. Redis is a pipe, not an archive: thirty minutes
of retention, trimmed by age, no disk persistence. Because the pipe outlives a consumer,
a restarted `store` replays from its last flushed minute and the five-minute loss window
closes for the first time. Every instrument carries a quote and a settlement currency, and
its canonical symbol gains a currency suffix. Everything runs locally under Docker Compose
first. Which AWS services host it — compute, Redis, the durable store, the region — is
decided by research tickets against fixed criteria, not assumed, and only after the local
split works. The senior's two documents are assembled from those research findings.

## User Stories

1. As the senior, I want the data feed, the store and the dashboard to be separate processes on a bus, so that one can be redeployed without touching the others.
2. As the senior, I want a document describing the message bus's cloud architecture, so that I can review the Redis hosting, naming, acknowledgement, trimming and persistence rules.
3. As the senior, I want a document describing the data feed engine's cloud architecture, so that I can review the AWS services chosen, the comparison behind each, and the connection controller's rules.
4. As the operator, I want `feed` to publish every canonical event at full rate, so that no consumer needs a second aggregation and the store's highs and lows are never shaved.
5. As the operator, I want `feed` to batch its writes to Redis, so that the publisher costs a fraction of a core at 1,700 messages a second rather than most of one.
6. As the operator, I want the batch interval measured and recorded, so that the latency it adds is a known number rather than a guess.
7. As the operator, I want Redis to keep thirty minutes and trim by age, so that memory is bounded and a crashed consumer can still catch up.
8. As the operator, I want Redis to run with no disk persistence, so that the disk growth the senior fights never starts here.
9. As the operator, I want `store` to replay from its last flushed minute after a restart, so that a crash loses no bars.
10. As the operator, I want `store` to keep its five-minute flush, so that the file count per day stays at 288 per table.
11. As the operator, I want `api` to ack on receipt and never replay, so that its cache refills from live frames in 508 ms and never serves a backlog.
12. As the operator, I want each consumer in its own consumer group, so that the store and the screen never compete for the same message.
13. As the operator, I want every stream named by environment, event type, venue and underlying, so that a reader takes only what it wants and two stacks never share a Redis by accident.
14. As the operator, I want the message format on the wire decided by a research ticket against standard practice, so that JSON is a choice and not a habit.
15. As a trader, I want every price on the dashboard to state its currency, so that a BTC option in USD and a NIFTY option in INR cannot be confused.
16. As a developer, I want `quote_currency` and `settlement_currency` on every instrument, so that the day they differ nothing is redesigned.
17. As a developer, I want the canonical symbol to end in its currency, so that a symbol read in a log or a key is unambiguous.
18. As a developer, I want the symbol parser to accept the new suffix and the fields to default sensibly, so that the schema version stays at one.
19. As the operator, I want `api` to learn the feed's state from the bus, so that the badge and `/health` work with no HTTP call between services.
20. As the operator, I want pause, resume and reconnect to travel over the bus as `control.command`, so that the screen's controls still reach the feed.
21. As the operator, I want each service to answer its own `/health`, so that liveness and readiness are reported by the process that knows them.
22. As the operator, I want a `feed` that starts with no Redis to fail loudly rather than buffer forever, so that a misconfiguration is visible at once.
23. As a developer, I want one image per service and one Compose file that starts all five containers, so that the whole system runs on a laptop with one command.
24. As a developer, I want a reverse proxy in front of `api` and `web`, so that CORS stops being a port rule.
25. As a developer, I want a contract test suite that runs against both the in-process fan-out and Redis, so that every consumer behaviour proven today is re-proven on the bus.
26. As a developer, I want a Compose smoke test, so that the split is proven as a system and not as four passing suites.
27. As a developer, I want tests never to touch the network, with Redis tests skipping loudly when Docker is absent, so that the suite stays honest.
28. As the operator, I want our connection controller compared against Nautilus Trader's reconnect policy, so that anything better is adopted with a reason and `reconnect_after` is re-measured.
29. As the operator, I want the durable store's cloud home chosen by research, so that S3, RDS and the rest are compared against fixed criteria rather than assumed.
30. As the operator, I want the compute platform and region chosen by research, so that EC2, Fargate and EKS are compared with costs at our rate and latency to Delta measured.
31. As the operator, I want Redis hosting chosen by research, so that a co-located container, ElastiCache, MemoryDB and self-managed EC2 are compared, and the senior's disk-bloat question is diagnosed rather than fled.
32. As the operator, I want research decided against one ordered set of criteria, so that "best overall" means the same thing in every ticket.
33. As the operator, I want container logs in CloudWatch, alerts in Discord, and one alarm on the feed being `stopped`, so that the 02:00 silent failure is caught.
34. As the operator, I want the prod dashboard private, over a tunnel or Tailscale, so that nothing sits on the public internet without a login.
35. As the operator, I want a `dev` environment on a laptop and a `prod` on AWS, with `staging` added last, so that cost stays small until there is something to stage.
36. As a learner, I want every research ticket to carry a question, ordered criteria, options, primary sources, a findings file, a decision record and the spec section it fills, so that I can study the decision afterwards.
37. As a learner, I want every ticket body to carry paths explored, chosen and why, rejected and why, and numbers tagged measured, assumed or derived, so that I can read the reasoning back.
38. As a learner, I want tickets worked one at a time with findings mirrored into the issue, so that I can study the last one while the next one runs.
39. As a maintainer, I want the HLD and LLDs updated inside the ticket that changes a component, so that the design never lags the code.
40. As a maintainer, I want the shared sections of the senior's two documents written once and linked, so that they cannot drift apart.

## Implementation Decisions

**Services.** Four: `feed` (adapter, controller, supervisor, Redis publisher), `store` (bar
writer, four Parquet tables), `api` (chain cache, pricing core, REST, websocket), `web`
(unchanged). The pure core, the events package and the tests are shared by the three
Python services, so the repo stays one repo with a `services/` layout. Splitting the web
app into its own repo is deferred until it earns it.

**The bus.** Redis Streams behind the existing `publish` and `subscribe` interface, which
does not change. Every one of the nine event types crosses it at full rate, about 1,700
messages a second for BTC and ETH. `feed` pipelines its writes; the batch interval is
measured and recorded in the ticket that lands it. One stream per event type per venue per
underlying with an environment prefix, subject to the naming research. Events without an
underlying are per venue. Payload encoding is JSON unless the research says otherwise.

**Retention and persistence.** Redis is a pipe. Thirty minutes, trimmed by age on every
batch write. No AOF, no snapshots. The archive is the Parquet store.

**Acknowledgement.** One consumer group per service. Both consumers ack on receipt. `store`
records the ID of the last message it flushed and, on restart, reads forward from there
rather than from the pending list; a Redis restart is the only way to lose that replay.
`api` never replays. Flush stays at five minutes.

**Instrument and symbol.** Two new optional fields on the canonical instrument,
`quote_currency` and `settlement_currency`, ISO 4217, set by the adapter. Delta is USD for
both, per `docs/settlement.md`. The canonical string becomes
`VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY`, the last token the quote currency. The
string is derived and never stored as truth, so the change touches parsing, keys, logs and
tests, not data. Schema version stays at one. The dashboard prints the currency beside
every price.

**Health and control across processes.** `api` keeps the last `feed.connection` and
`heartbeat` it saw and reports them, with their age, beside its own liveness. `feed`
answers its own `/health` from the supervisor. `control.command` travels over the bus. No
service calls another over HTTP.

**Containers.** One image per service, the official Redis image, one Compose file, a
reverse proxy in front of `api` and `web`. Prod runs the same images.

**Cloud.** No AWS service is decided. Three research tickets decide the durable store, the
compute platform with region, and Redis hosting, each against these criteria in this
order: does not break our invariants; operations burden for a small team; monthly cost at
our rate and at ten times it; path to OMS and NSE; latency to the venue. Prod is private,
logs go to CloudWatch, alerts to Discord, one alarm on `stopped`. Environments are `dev`
locally and `prod`, then `staging` last. Cloud tickets run after the local split and after
access arrives.

**Controller.** Ours stays. A research ticket gap-analyses it against Nautilus Trader's
live reconnect policy and re-measures `reconnect_after` over a longer window.

**Documents.** This epic is the plan. The senior's two documents, the message bus and the
data feed engine cloud architectures, are deliverables assembled from the research
findings, with shared sections (AWS services, nomenclature) written once and linked.

## Testing Decisions

A good test drives a seam and observes what crosses it, never how. Five seams, four of
them already in the codebase.

- **The bus interface.** The primary seam. One contract suite, parametrised over the
  in-process fan-out and a Redis implementation: lossless delivery, drop counting,
  ordering, consumer-group isolation, replay after a reader restarts, trim by age. Prior
  art: the existing fan-out tests and `tools/measure_bus.py`.
- **The scripted fake adapter.** Drives `feed` without a venue: the right streams, the
  batching, the trim, the loud failure with no Redis. Prior art: the controller and
  supervisor suites.
- **The event parser.** Round trip of every event type through JSON and Redis, including
  the currency fields and the new symbol. Prior art: the envelope and instrument tests.
- **The chain contract and web fixtures.** Unchanged; a DOM fingerprint before and after
  proves the dashboard did not move. Prior art: the #22 refactor proof.
- **The Compose smoke test.** New and the highest. Starts the stack, waits for both
  `/health`, publishes one scripted frame, checks a bar reaches the store root.

Tests never touch the network. Redis tests use a Redis the suite starts on a test-only
port and skip loudly without Docker; unit tests use an in-memory fake. No test may depend
on the wall clock.

## Out of Scope

Order management, execution, portfolio, risk, sandbox, and the NSE adapter itself. A
message broker other than Redis. Kafka. Splitting the web app into its own repo. Public
access with a login. Grafana. Backfilling the store's existing holes. Changing the four
bar tables' schemas. Any AWS service named before its research ticket closes.

## Further Notes

Ticket order, one at a time: R1 naming and payload standard practice; R2 controller against
Nautilus; I1 currency fields and symbol suffix; I2 `feed` as a service with batching
measured; I3 `store` as a consumer with replay; I4 `api` as a consumer with health and
commands over the bus; I5 Dockerfiles, Compose, proxy, smoke test; then R3 durable store,
R4 compute and region, R5 Redis hosting; then I6 store on the chosen home, I7 deploy prod
with logs, alarm and private access, I8 Discord alert consumer, I9 staging. Model routing:
Opus for research and anything touching the controller, the bus policy or a schema; Sonnet
for mechanical work with a fixed contract. Commits are authored centrally; agents do not
run git. Every number carries `measured`, `assumed` or `derived` and the run behind it.

## Tickets

Published 2026-09-09, in dependency order. Blockers first; the frontier is any ticket whose
blockers are closed.

| # | Key | Title | Blocked by | Model |
|---|---|---|---|---|
| #58 | R1 | Stream naming and payload format | — | Opus |
| #59 | R2 | Controller against Nautilus reconnect policy | — | Opus |
| #60 | I1 | Currency on instrument, symbol, screen | — | Sonnet |
| #61 | I2 | Redis Streams bus behind the seam, one process | #58 | Opus |
| #62 | I3 | Feed as its own process | #61 | Opus |
| #63 | I4 | Store as its own process, replaying from last flush | #61 | Opus |
| #64 | I5 | Api learns feed state and commands over the bus | #62 | Opus |
| #65 | I6 | Images, Compose, proxy, smoke test | #62 #63 #64 | Sonnet |
| #66 | I7 | Discord consumer for alerts | #65 | Sonnet |
| #67 | R3 | Where the durable store lives | #65 | Opus |
| #68 | R4 | Compute platform and region | #65 | Opus |
| #69 | R5 | Where Redis lives, disk-bloat diagnosis | #65 | Opus |
| #70 | I8 | Store writes to its chosen home | #67 | Sonnet |
| #71 | I9 | Deploy prod | #68 #69 #70 | Opus |
| #72 | D1 | Assemble the two architecture documents | #58 #59 #67 #68 #69 #71 | Opus |
| #73 | I10 | Staging | #71 | Sonnet |
