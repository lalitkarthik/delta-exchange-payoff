# R1 — Stream naming and payload format: what standard practice actually is

Findings for #58, under epic #57. The decision is
[../decisions/0001-stream-naming-and-payload-format.md](../decisions/0001-stream-naming-and-payload-format.md),
the standard [../cloud/nomenclature.md](../cloud/nomenclature.md), the full run
[0001a-measurement-run.md](0001a-measurement-run.md).

## The question

How should our nine event types be named as Redis streams and their payloads encoded, so that a
reader takes only what it wants, two stacks can never share a Redis by accident, and the choice is
a standard rather than a habit?

Decided against #57's criteria, in order: **(1)** invariants — no silent data loss, never
forward-fill, `null` is not `0`; **(2)** ops burden for two or three people; **(3)** cost at our
rate and ten times it; **(4)** path to OMS, NSE and more consumers; **(5)** latency to the venue.

## 1. What three real systems do

### Redis itself

| Claim | Source |
|---|---|
| "there is a convention for using the colon ':' character to split keys into sections"; "Dots or dashes are often used for multi-word fields"; "Try to stick with a schema" | [Keys and values](https://redis.io/docs/latest/develop/using-commands/keyspace/) |
| A stream entry is "One or more field-value pairs"; "Redis stores the field-value pairs in the same order you provide them"; `MAXLEN` evicts by count, `MINID` by id, `~` is "Approximate trimming - more efficient" | [XADD](https://redis.io/docs/latest/commands/xadd/) |
| `XREAD` takes an explicit list of stream keys. **There is no wildcard and no pattern.** | [Streams](https://redis.io/docs/latest/develop/data-types/streams/) |
| "Use Redis databases to separate keys within the same application when needed. Don't use them to run multiple unrelated applications in a single Redis instance"; "Redis Cluster only supports database zero"; Redis Software "does not support shared databases" | [SELECT](https://redis.io/docs/latest/commands/select/) |
| A stream is "a radix tree of listpacks"; each listpack opens with a **master entry** holding that node's field names, and a later entry "if the fields of the entry are the same as the master entry fields … the entry fields and number of fields will be omitted" (`STREAM_ITEM_FLAG_SAMEFIELDS`) | `redis/redis`, [`src/t_stream.c`](https://github.com/redis/redis/blob/unstable/src/t_stream.c) |

The last row is the one nobody quotes and it decides two of our questions at once: **field names
in a Redis stream are stored once per macro node when the field set repeats** — which a stream
carrying one event type does on nearly every entry, and a stream carrying several does not.

### Nautilus Trader

Source read at `nautechsystems/nautilus_trader`, branch `develop`.

| Claim | Source |
|---|---|
| Topics are `data.<kind>.<VENUE>.<SYMBOL>` — `data.quotes.XCME.ESZ24`, `data.book.deltas.…`, `data.option_greeks.…` — dot-separated, **per instrument** | [`crates/common/src/msgbus/switchboard.rs`](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/common/src/msgbus/switchboard.rs) L374–L438 |
| Stream key is `trader-{trader_id}:{instance_id}:{streams_prefix}`, colon-separated, prefix always included | [`crates/infrastructure/src/redis/mod.rs`](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/infrastructure/src/redis/mod.rs) `get_stream_key`, L223–L246; `REDIS_DELIMITER: char = ':'` L32 |
| With `stream_per_topic` (default `true`) the key becomes `format!("{stream_key}:{}", msg.topic)`. Reason given: "This is particularly useful for Redis backings, which do not support wildcard topics when listening to streams." | [`crates/infrastructure/src/redis/msgbus.rs`](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/infrastructure/src/redis/msgbus.rs) L564–L568; [`docs/concepts/message_bus.md`](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/message_bus.md) |
| The wire record is four flat Redis fields — `topic`, `type`, `payload`, `encoding` (plus `payload_kind` for typed payloads) — with the whole payload opaque inside one of them | [`crates/infrastructure/src/redis/stream_fields.rs`](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/infrastructure/src/redis/stream_fields.rs) |
| Encoding is `json` by default, `msgpack` optional: "The `json` encoding is used by default for human readability and interoperability. Use `msgpack` when payload size and serialization performance are a primary concern." | `docs/concepts/message_bus.md` |
| Timestamps default to UNIX nanosecond integers; ISO 8601 is opt-in (`timestamps_as_iso8601`) | [`crates/common/src/msgbus/config.rs`](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/common/src/msgbus/config.rs) L44 |
| `types_filter` exists "To prevent flooding the stream with data like high-frequency quotes" — a list of payload types **excluded from external publication** | `docs/concepts/message_bus.md` |

That last row is the uncomfortable one. **A shipping system whose external transport is Redis
Streams offers, as a documented feature, not publishing quotes at all.** We cannot — our `store`
is the archive — but it is the honest answer to "what did they choose", and why the memory number
below has to be real.

### cryptofeed

A market-data collector for ~30 crypto venues, with a first-class Redis Streams backend.
Source at `bmoscon/cryptofeed`, branch `master`.

| Claim | Source |
|---|---|
| Stream key is `f"{self.key}-{update['exchange']}-{update['symbol']}"` where `self.key` defaults to the **data type** — `trades`, `book`, `ticker`, `open_interest`, `candles`, `funding`, `liquidations`. So: **type, venue, instrument**, dash-separated, per contract | [`cryptofeed/backends/redis.py`](https://github.com/bmoscon/cryptofeed/blob/master/cryptofeed/backends/redis.py) L106, L137–L211 |
| The payload is **spread flat across Redis fields** — `pipe.xadd(key, update)` on a flat dict — with only genuinely nested parts (`book`, `delta`) collapsed into one JSON field | same file, L100–L108 |
| `none_to='None'` by default: every `None` in the record is replaced before the write, by `convert_none_values(data, none_to)` | same file, L28; [`cryptofeed/types.pyx`](https://github.com/bmoscon/cryptofeed/blob/master/cryptofeed/types.pyx) L73–L78 |

**That last row is our first invariant failing, in a real system.** cryptofeed spreads its payload
across Redis fields; a Redis field cannot hold a null; so an absent value is written as the
*string* `"None"`. A careless consumer turns that into `0`. It is the confusion
`docs/design/events.md` exists to prevent, forced by the layout rather than chosen.

### What to notice: does anyone split by underlying?

**No, and the reason is that in their markets there is nothing to split by.** Nautilus splits at
`{VENUE}.{SYMBOL}`, cryptofeed at `{exchange}-{symbol}`. For a spot pair or a perpetual the symbol
*is* the underlying — `BINANCE.BTCUSDT` — so per-symbol and per-underlying are one split. An
option chain is where they part: one underlying carries 504 live contracts (`measured`,
2026-09-08). Both split at *the finest unit a subscriber would choose*; ours is the underlying,
because no consumer of ours wants one strike and every one of them wants one asset. Nautilus
agrees where its own data is chain-shaped — `data.option_chain.{series_id}`, not per contract
(`switchboard.rs` L438).

## 2. Stream granularity

| Option | Streams today | (1) Invariants | (2) Ops | (3) Cost | (4) Path | (5) Latency |
|---|---|---|---|---|---|---|
| Per event type | 9 | safe | one `XREAD` list | cheapest | `api` must read ETH and NSE to get BTC; every added venue taxes every reader | same |
| Per event type per venue | `derived` 9 — one venue today | safe | 9 keys | same as above until NSE arrives | NSE separable; a BTC-only screen still reads ETH | same |
| **Per event type per venue per underlying** | **14** | **safe — the key set changes only when an underlying is added, an act of configuration** | **14 keys, still one line of config** | **`derived` +5 keys over per-venue** | **a NIFTY screen and a BTC screen are different readers** | **same** |
| Per contract | `derived` 1,564+ | **breaks (1).** New contracts list a few times a day (`lld/relisting.md` §3), so keys appear while readers are running; `XREAD` has no wildcard, so a stream not in the list is silently not read — #51's bug, restaged | 1,564 keys, 1,564 `XGROUP CREATE`, an `XREAD` argument list per call | `measured` 1.15x memory, 4,797 B per extra key | worse with every venue | same |

The cost column is `measured` (2026-09-09, `tools/measure_payload_size.py` §4): 13,600 real
entries cost 4,316,152 bytes in one stream and 4,968,508 across 136. Memory is *not* the argument
against per contract — 15% is affordable — criterion 1 is, and it is fatal. **One stream per event
type is also cheaper than one for everything**, the master entry showing up as bytes: `measured`
12,000 entries cost 4,534,628 bytes as three single-type streams and 5,115,188 as one interleaved
stream — 1.13x, 48.4 B an entry.

## 3. Environment separation

| Option | Verdict |
|---|---|
| **Key prefix** | **Chosen.** Costs 5 bytes a key, works on every Redis including Cluster and every managed one, and is visible in `redis-cli`, in a log line and in a metric name |
| Numbered databases (`SELECT`) | **Rejected.** Redis's own page tells you not to: don't "run multiple unrelated applications in a single Redis instance"; Cluster "only supports database zero"; Redis Software and Redis Cloud block it. It would also be invisible — a wrong `-n` looks like an empty stream, which is what silent means |
| Separate instances | **Chosen as well, and it is the real separation.** `dev` is a laptop container and `prod` is its own; they never share. The prefix makes a mistaken share harmless rather than preventing it |

Nautilus reaches the same place from the other end: its mandatory prefix carries
`trader-{trader_id}` and an optional `{instance_id}`, and no database selector at all.

## 4. Encoding, and what it costs

Five candidates, each measured as a **whole Redis entry** — field names and values, because that
is what Redis stores. The events are real: fixtures `ws-ob-l2-04-09-2026.json` and
`ws-ticker-04-09-2026.json` decoded by `adapters.DeltaAdapter`, 136 each, the named ones
`P-BTC-75600-040926` and `P-BTC-78500-040926`. The protobuf encoder is hand-written to the wire
format and `verified byte-for-byte against google.protobuf` at the top of every run. All
`measured`, `tools/measure_payload_size.py`, 2026-09-09, ±0.5% run to run.

| Encoding | `md.option_quote` entry | `md.option_reference` entry | Redis/entry, quote | Redis/entry, reference |
|---|---|---|---|---|
| A `json:one-field` | 422 B | 675 B | 487.9 B | 735.9 B |
| **B `json:envelope-flat`** | **329 B** | **582 B** | **315.6 B** | **627.9 B** |
| C `msgpack:envelope-flat` | 310 B | 519 B | 294.6 B | 549.1 B |
| D `msgpack:one-field` | 253 B | 462 B | 294.1 B | 548.1 B |
| E `protobuf:one-field` | 182 B | 288 B | 211.5 B | 317.7 B |

Two things the ticket's working assumption did not say. **The flat envelope costs Redis less than
the bytes handed to it** — 315.6 B held against 331.0 B written, the nine field names compressing
against the master entry. And **JSON is not "roughly twice" a binary format here**: 1.07x against
MessagePack in the same layout, 1.49x against protobuf, which is the real gap.

## 5. Memory at thirty minutes

`measured` 1,693.6 venue frames a second, BTC+ETH, 782 contracts (`tools/measure_feed.py`,
2026-09-08), `derived` split by the channels' `measured` 508 ms and 5,001 ms refresh intervals
(2026-09-03) into 1,537.4 book and 156.2 ticker frames — `derived` **1,849.8 events a second**,
because one ticker frame is two events. Retention 1,800 s.

| Encoding | bytes/s | 30 minutes | at ten times the rate |
|---|---|---|---|
| A `json:one-field` | 886.8 KiB/s | **1,558.7 MiB** | 15.2 GiB |
| **B `json:envelope-flat`** | 598.2 KiB/s | **1,051.5 MiB** | 10.3 GiB |
| C `msgpack:envelope-flat` | 554.5 KiB/s | **974.8 MiB** | 9.5 GiB |
| D `msgpack:one-field` | 557.4 KiB/s | **979.7 MiB** | 9.6 GiB |
| E `protobuf:one-field` | 383.7 KiB/s | **674.4 MiB** | 6.6 GiB |

All `derived` from `measured` per-entry sizes and rates. Excluded: the other six types — five are
rare by construction, and `computed.chain` is `derived` under 1% of the above at one pass a minute
per live expiry and is **not measured**. **B against C is 76.7 MiB; B against E is 377.1 MiB** — at
ten times the rate, 0.8 GiB and 3.7 GiB, and 3.7 GiB is a different Redis node. Units corrected
2026-09-12 (#95), values unchanged: [0001-units-reconciliation.md](0001-units-reconciliation.md).

## What I learned

| Term | What it means here |
|---|---|
| **Stream** | A named, append-only list inside Redis. You add to the end and read forward. `prod:md.option_quote:DELTA:BTC` is one. |
| **Entry** | One item in a stream. It has an id and a flat list of field-and-value pairs. One of our events is one entry. |
| **Field** | A named slot in an entry. Both the name and the value are just bytes. **There is no number type and no null.** |
| **XADD** | The command that appends an entry. `XADD <stream> * type md.option_quote payload {...}` |
| **XREAD** | The command that reads forward. You must name the streams you want; there is no `*` and no pattern. |
| **Consumer group** | A named bookmark on one stream, shared by the copies of one service. Two groups on one stream each get every entry — `store` and `api` are two. |
| **Trimming** | Deleting old entries. `MAXLEN` keeps a count, `MINID` everything newer than an id — how you keep thirty minutes. |
| **Listpack / macro node** | How Redis actually stores a run of entries: a compact block holding many. The block starts with a **master entry** listing that block's field names. |
| **Envelope** | Our seven fields that every event carries: `type`, `event_id`, `schema_version`, `source`, `ts_venue`, `ts_received`, `instrument`. |
| **Payload** | The rest of one event — the fields only that event type has. `bid`, `ask` on `md.option_quote`; eighteen more on `md.option_reference`. |
| **Encoding** | How a payload is turned into bytes. JSON is text. MessagePack and protobuf are binary. |

**A stream name is a filter you cannot undo.** Whatever you leave out of the name, every reader
pays to skip. We publish 1,850 events a second; if BTC and ETH shared a stream, a BTC-only screen
would parse ETH's half and throw it away forever. So the underlying goes in the name.

**A stream name is also a promise you have to keep.** `XREAD` takes a list of names and has no
wildcard, so a stream nobody named is a stream nobody read and nothing says so. No stream per
contract, then: Delta lists new contracts a few times a day, and those streams would appear while
`store` was running and be missed in silence. That bug was #51, three days of history.

**Redis fields cannot hold "missing".** A field's value is bytes; there is no null. Spread a whole
event across fields and you must invent a spelling for absent — cryptofeed spells it `"None"`,
where a price should be. Our oldest rule is that `null` is not `0` and JSON has one, so the
always-present envelope fields go flat and everything that can be absent goes in one JSON field.

**The flat envelope is free, and that surprised me.** Nine Redis fields should cost nine field
names an entry. They do not: Redis writes a block's field names once and any entry whose fields
match points at them, and every entry in `prod:md.option_quote:DELTA:BTC` matches. `measured` 331
bytes handed over, 315.6 held. Mix three types into one stream and it stops — 48.4 B/entry more.

**JSON is not twice the size of binary. It is 1.07x here.** The factor of two shows up only
against protobuf (1.49x), which writes a `double` as eight bytes with no field name. MessagePack
barely helps: most of an entry is the envelope's text — event id, timestamps, symbol — which
MessagePack writes as text too. So thirty minutes of our feed is `derived` 1,051.5 MiB, the number
R5 will spend; ten times the rate is 10.3 GiB, where moving the payload to MessagePack — one
function, no name change — buys back 0.8 GiB.

**Redis's own advice is to separate with names, not database numbers.** Numbered databases are a
trap: Cluster does not have them, managed Redis blocks them, and picking the wrong one looks like
an empty stream. So the environment is the first section of every key — and `dev` and `prod` do
not share a server either.

**Everyone who ships this puts the type first, then the venue, then the instrument.** Nautilus
writes `data.quotes.XCME.ESZ24`, cryptofeed `ticker-BINANCE-BTC-USDT`, we
`prod:md.option_quote:DELTA:BTC` — one rung coarser, because one of our underlyings is 504
contracts and none of theirs is.
