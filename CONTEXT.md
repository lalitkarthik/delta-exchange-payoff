# CONTEXT — the words this repository uses

**Look a word up here before you write it in a design note.** Every term in
[docs/design/cloud/message-bus.md](docs/design/cloud/message-bus.md) and
[docs/design/cloud/data-feed-engine.md](docs/design/cloud/data-feed-engine.md) resolves to this file.

Three rules govern it:

1. **One word for one thing.** If the code calls it a flush, every sentence calls it a flush.
2. **The code wins.** A term here names something in `engine/src/deltapayoff/` or in a decision
   record, and the *Where it lives* column says which.
3. **A word with two meanings is written down as one**, with the qualifier that separates them.
   `flush` is the worked example.

---

## 1. The processes

**Name a process by its service name, lower case, one word.** Those names are the container names,
the consumer-group names and the log fields.

| Term | What it is | Where it lives |
|---|---|---|
| `feed` | The only process that contacts the venue. It owns the websocket, the adapter, the controller and the publisher. | `feed_main.py` |
| `store` | The only writer of the four bar tables. It reads losslessly, folds bars, and flushes Parquet. | `store_main.py` |
| `api` | The process that serves the ladder, the REST routes and `/ws/chain`. It opens no venue socket. | `main.py` |
| `web` | The Next.js page. It renders and computes nothing. | `web/` |
| proxy | One `nginx:alpine` container. It makes the stack same-origin on one host port. | `proxy/nginx.conf` |
| `discord-alerts` | The sixth service. It reads the `alert` stream only and posts to a webhook. | `alert_main.py` |
| `oms`, `strategy` | Named, costed and not built. Every figure for them is `assumed`. | [0007](docs/design/decisions/0007-load-profile.md) |
| monolith | The default composition, with `DELTA_BUS` unset: one process, one in-process bus. | `fanout.py` |
| split mode | The composition `DELTA_BUS=redis` selects: the processes above, over Redis. | [hld.md](docs/design/hld.md) |

## 2. The bus

**A stream is a Redis key; a consumer group is a reader's place in it.** The two are not the same
thing and are never used for each other.

| Term | What it is | Where it lives |
|---|---|---|
| bus | The seam `publish(event)` and `subscribe(...)` name. Two implementations sit behind it. | `events/bus.py` |
| stream | One Redis Streams key, named `{event_type}:{VENUE}[:{UNDERLYING}]`. | [nomenclature.md](docs/design/cloud/nomenclature.md) |
| entry | One item in a stream. One entry carries one event. | `redis_bus.py` |
| event | One record with an envelope and a payload. Ten types are catalogued. | [events.md](docs/design/events.md) |
| envelope | The seven keys every event carries, flat across Redis fields. | [nomenclature.md](docs/design/cloud/nomenclature.md) §2 |
| payload | The type's own keys, as one JSON object in one Redis field. | the same |
| outbox | The publisher's in-memory list of encoded entries, bounded at `max_outbox`. | `redis_bus.py` |
| publisher | The part of `feed` that encodes an event, appends it to the outbox, and never blocks the socket reader. | `redis_bus.py` |
| flusher | The publisher's own loop. It performs one bus flush every batch interval and keeps running when one raises. | `redis_bus.py` |
| batch | One pipeline of `XADD`s plus one `XTRIM` per stream it touched. | `redis_bus.py` |
| **bus flush** | Writing one batch from the outbox to Redis. Always qualified. | `RedisBus.flush` |
| market-data event | One of the three `md.*` events. Control traffic is not one, and only this resets the staleness clock. | [events.md](docs/design/events.md) |
| pending list | Redis's per-group list of entries delivered and not yet acked. Nothing here reads it. | [message-bus.md](docs/design/cloud/message-bus.md) §4.1 |
| trim | `XTRIM <stream> MINID ~`, removing entries older than the retention window. | [message-bus.md](docs/design/cloud/message-bus.md) §4.2 |
| retention | Thirty minutes, by age and never by count. The promise made to a restarting `store`. | the same |
| consumer group | A named reader of one stream. One per service, never one per instance. | [nomenclature.md](docs/design/cloud/nomenclature.md) §5 |
| lossless | A subscription that reads through a consumer group and drops nothing. | `redis_bus.py` |
| drop-oldest | A subscription that reads outside every group, evicts the oldest, and counts what it evicted. | the same |
| skip | What a drop-oldest reader jumped over, or Redis trimmed away before it arrived. Counted, never silent. | the same |
| ack | `XACK`. It says a group is done with an entry. It frees no memory. | [message-bus.md](docs/design/cloud/message-bus.md) §4.1 |
| backlog | Entries a consumer has not yet read. | `tools/measure_bus_live.py` |

## 3. The store

**A bar is sealed, then flushed.** Sealing makes it final; flushing puts it on disk. They are two
steps and two words.

| Term | What it is | Where it lives |
|---|---|---|
| **venue frame** | The venue's own JSON object, off the socket, undecoded. Only an adapter sees one. Always qualified: §5's **ping frame** is a websocket control frame and is not one. | `adapters/delta_socket.py` |
| tick | One decoded observation the writer folds into a bar. | `bars.py` |
| bar | A lossy one-minute summary that keeps the extremes and destroys the path. | `bars.py` |
| seal | Closing a minute's bucket so nothing more can enter it. | `BarAggregator.seal` |
| **store flush** | Writing sealed bars to Parquet, every `FLUSH_SECONDS`. Always qualified. | `BarStore.flush` |
| durability boundary | The store flush, and nothing before it. An ack is not one. | [message-bus.md](docs/design/cloud/message-bus.md) §4.1 |
| table | One of four: `quote-bars`, `reference-bars`, `computed-bars`, `spot-bars`. | `store.py` |
| partition | One `date=`/`underlying=` directory inside a table. Nothing else is a partition key. | `store.py` |
| compaction | Folding a closed partition's flush files into one object per table. Nightly. | `compact_partition` |
| manifest | The sidecar compaction writes before it deletes, so a partition is recoverable alone. | `store.py` |
| watermark | A per-stream `Position`: the entry id `store` last flushed, plus its logical index. | [0010](docs/design/decisions/0010-store-replay.md) R1 |
| checkpoint | `<root>/_store-checkpoint.json`. It holds the watermarks and each aggregator's seal point. | [0010](docs/design/decisions/0010-store-replay.md) R2 |
| flush intent | `<root>/_store-flush-intent.json`, written before the Parquet files and deleted after the checkpoint. | [0010](docs/design/decisions/0010-store-replay.md) R3 |
| generation | The number in a flush file's name, so two processes cannot collide on one. | the same |
| replay gap | A counted, alerted hole, when the watermark was trimmed before `store` came back. | [0010](docs/design/decisions/0010-store-replay.md) R5 |
| seal clock | `min(wall clock, the time inside the last stream id of any stream still behind)`. | [0010](docs/design/decisions/0010-store-replay.md) R4 |
| recording | Whether `store` is folding and flushing. Toggled by a `control.command`. | `store.py` |

## 4. The market

**A contract is named once, by the canonical symbol, and that name is derived and never stored as
truth.**

| Term | What it is | Where it lives |
|---|---|---|
| venue | The exchange. `DELTA` today, `NSE` named and out of scope. | `models.py` |
| underlying | What the option is on: `BTC`, `ETH`. Upper case everywhere. | `models.py` |
| expiry | One settlement date. Expiries list and settle daily. | `models.py` |
| strike | One price level. It carries no trailing zeros and never an exponent. | `format_strike` |
| leg | One side of a strike: the call, or the put. Greeks differ between them. | `chain.py` |
| instrument | The canonical contract record. Nothing downstream sees venue JSON. | `models.py` |
| canonical symbol | `VENUE-UNDERLYING-YYYYMMDD-STRIKE-C\|P-CCY`, six parts, joined on `-`. | `Instrument.from_canonical` |
| venue symbol | The venue's own string for the same contract, carried verbatim beside the canonical one. | the same |
| chain | One expiry's calls and puts, folded onto shared strikes. | `chain.py` |
| **ladder** | The chain as the screen shows it: rows ascending by strike. | `chain.py`, `stream.py` |
| spot | What the underlying did, from `md.index_quote`. | `store.py` |
| quote | Top of book, from the venue's book channel. | [events.md](docs/design/events.md) |
| reference | The venue's own view of a contract, from its ticker channel. Its IV and Greeks are columns, never inputs. | the same |
| computed chain | What **we** made of all of it: our IV, our Greeks, our forward. | `compute.py` |

## 5. The connection

**A connection is in exactly one of five states at every moment**, and every move between them
publishes one event.

| Term | What it is | Where it lives |
|---|---|---|
| adapter | The one object that owns everything venue-specific: socket, REST, symbols, channels. | `adapters/base.py` |
| controller | The state machine over one adapter's connection. | `controller.py` |
| supervisor | The object above the controllers. It deliberately restarts nothing. | `supervisor.py` |
| staleness clock | The age of the last market-data event. Control traffic never resets it. | `controller.py` |
| `degraded_after` | 15 s, `assumed`. Crossing it is a badge. | [controller-policies.md](docs/design/cloud/controller-policies.md) C2 |
| `reconnect_after` | 45 s, `assumed`. Crossing it is an alarm, and now cuts the socket. | the same, C3 and C7 |
| budget | Ten **consecutive** failed reconnects. A delivered message restores it in full. | the same, C6 |
| backoff | The wait between dials: 1 s, doubling, ceiling 60 s, no jitter. | the same, C5 |
| drop | One connection loss, however caused. It spends one of the budget. | `controller.py` |
| heartbeat | Two different things, always qualified: the **ping frame** we send every 30 s, and the **`heartbeat` event** we publish every 10 s. | the same, C11 |
| alert | An `alert` event. The Discord consumer and the logger take all of them. | [events.md](docs/design/events.md) |
| command | A `control.command` event. Its `target` names the service that must act on it. | [0010](docs/design/decisions/0010-store-replay.md) R8 |

## 6. The cloud

**Read a dollar figure with its tag and its run, or do not read it.**

| Term | What it is |
|---|---|
| `dev` | Compose on a laptop. The same images, the same containers, one Redis of its own. |
| `prod` | ECS on one EC2 instance. The same images, one Redis of its own. Stream names do not differ. |
| instance | One EC2 machine. **Not "box" and not "node"** — one word for one thing, and this is it. |
| reservation | What a container asks ECS for: vCPU and memory, guaranteed under contention and free when idle. ECS spells a whole vCPU as 1,024 CPU units; **this repository writes vCPU**. |
| task definition | The ECS object holding every container. A deploy registers a revision of it. |
| `host` network mode | Every container on the instance's own network stack, so `feed` reaches Redis on `127.0.0.1`. |
| 1x, 10x | Today's `measured` rate, and ten times it. Every cost table carries both. |
| named fallback | The option a decision record already chose for a named change. It is not a guess made later. |
| `measured` | Observed in a run this repository can name. The run travels with the number. |
| `assumed` | Chosen by a person. It is a belief, and a record says what would change it. |
| `derived` | Computed from `measured` or `assumed` inputs. The arithmetic travels with it. |

## 7. Words this repository refuses

**Do not write these.** Each one hides a distinction the system depends on.

1. **"Save", "write" or "persist"** for a store flush. A flush is a flush, and it is qualified.
2. **"Healthy"** for a connection. `/health` reports a state; whether it is acceptable is a judgement.
3. **"Lifetime budget"**. The budget counts consecutive failures and always has.
4. **"Forward-fill"** anywhere near a bar. A bar summarises events that happened.
5. **`0` for absent.** `null` is not `0`, and an unknown age is not an age of zero.
