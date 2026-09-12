# 0001 — Stream naming and payload format

> **Superseded in part** by [0006](0006-stream-names-without-environment.md), 2026-09-12:
> the `{env}` section is removed from the grammar. Everything else here stands.

**Status** decided, #58, 2026-09-09. **Supersedes** nothing. **Fills**
[../cloud/nomenclature.md](../cloud/nomenclature.md). **Evidence**
[../research/0001-stream-naming-and-payload-format.md](../research/0001-stream-naming-and-payload-format.md).

## The question

How should our nine event types be named as Redis streams, and how should their payloads be
encoded, so that a reader takes only what it wants, two stacks can never share a Redis by
accident, and the choice is a standard rather than a habit?

## The options

| | Considered |
|---|---|
| Granularity | per event type; per event type per venue; **per event type per venue per underlying**; per contract |
| Environments | **key prefix**; numbered databases (`SELECT`); **separate instances** |
| Encoding | JSON in one field; **JSON with the envelope flat across Redis fields**; MessagePack; Protobuf |

## The decision

```
{env}:{event_type}:{VENUE}[:{UNDERLYING}]        prod:md.option_quote:DELTA:BTC
```

**One stream per event type per venue per underlying**, with events that have no underlying per
venue, `alert` per environment, and `control.command` per venue inbound. **A mandatory environment
prefix as the first section, and `dev` and `prod` on separate Redis instances.** Numbered databases
are never used. **The envelope flat, one Redis field per key, and the type's own keys as one JSON
object in a `payload` field**; absent is omitted, never spelled. The nine names, the field layout
and the consumer-group rule are in [../cloud/nomenclature.md](../cloud/nomenclature.md).

This confirms #57's working proposal in substance and corrects it in one place: the environment is
a `:`-separated section, `prod:md.option_quote:DELTA:BTC`, not `prod.md.option_quote:DELTA:BTC`,
because Redis's own keyspace page makes `:` the section separator and reserves `.` for inside one.

## Why, in the criteria's order

**1. Invariants.** Decisive twice.

- *Against per contract.* `XREAD` takes an explicit key list and has no wildcard. Delta lists new
  contracts a few times a day (`lld/relisting.md` §3), so per-contract streams would appear while
  `store` was already running and be read by nobody, with nothing saying so. That is #51 restaged,
  and #51 cost three days of history.
- *Against spreading the payload across Redis fields.* A Redis field holds bytes and has no null,
  so a spread payload must invent a spelling for absent. cryptofeed, a real system, writes the
  string `"None"` (`backends/redis.py` L28, `types.pyx` L73–L78). `null` is not `0` is this
  catalogue's oldest rule; JSON has a real `null` and Redis fields do not.
- *For JSON over protobuf.* proto3 needs explicit-presence `optional` on all 23 nullable fields
  before an unset `double` stops reading back as `0.0`, and a consumer without the generated
  accessor cannot tell the difference at all.
- *For one `payload` field.* Envelope keys plus the payload object reassemble into exactly the
  mapping `events.parse_event` already takes, so `UnknownEventType`, `UnknownSchemaVersion`,
  `extra="forbid"` and the non-finite refusal keep working over the wire with **no second decode
  path** to hold the line in.

**2. Operations burden.** `redis-cli XRANGE prod:md.option_quote:DELTA:BTC - + COUNT 1` prints a
readable event at 02:00 on a laptop. MessagePack and protobuf print bytes; protobuf also adds a
`.proto`, a codegen step and a version-skew failure mode for a team of two or three. Fourteen
stream keys are a line of configuration; 1,564 are an inventory.

**3. Cost.** `derived` 1,051.5 MiB at thirty minutes, against 974.8 MiB for MessagePack and 674.4 MiB
for protobuf, from `measured` per-entry sizes and the `measured` 1,693.6 frames/s. The premium for
readability is 76.7 MB against MessagePack — 7%, and less than one node size anywhere.

**4. Path to the right half.** JSON is what an OMS, an alert consumer or an NSE feed can be written
against in any language with no shared schema artefact. Nautilus Trader ships exactly this default
and says why: "The `json` encoding is used by default for human readability and interoperability."
Venue and underlying in the key are what make an NSE consumer and a BTC screen different readers
rather than the same reader with a filter.

**5. Latency to the venue.** Not moved by this decision. The publisher batches; the batch interval
is I2's measurement and dominates any encoding difference, against a `measured` p50 arrival lag of
212.6 ms.

## Rejected, and why

| Rejected | Why |
|---|---|
| Per contract | Criterion 1: streams appear daily and `XREAD` has no wildcard. `measured` 1.15x memory and 4,797 B per key is the smaller half of the objection |
| One stream per event type only | Criterion 4: a BTC screen would read ETH, and every venue added would tax every reader |
| Numbered databases | Redis's own page: don't "run multiple unrelated applications in a single Redis instance"; Cluster "only supports database zero"; Redis Software blocks it. A wrong `-n` also looks exactly like an empty stream |
| JSON in one field | 1,558.7 MiB against 1,051.5 MiB, and the stream name is the only thing a reader could dispatch on without parsing |
| Payload spread across Redis fields | Criterion 1: there is no null in a Redis field |
| MessagePack | Criterion 2 beats criterion 3 at this size: 76.7 MB, 7%, against a wire nobody can read |
| Protobuf | Criteria 1 and 2: proto3 presence is a trap for 23 nullable prices, and a `.proto` plus codegen in three services is a build step this team does not have |

## What would change this decision

- **Ten times the rate.** 10.3 GiB at thirty minutes buys a bigger Redis; MessagePack's 9.5 GiB may
  not, but 3.7 GB of protobuf's saving might. The change is one function and no name change —
  `payload` stops being JSON — because nothing else on the wire moves.
- **A second consumer that is not ours.** If a third party reads the bus, protobuf's schema becomes
  an asset rather than a build step.
- **A consumer that wants one contract.** None exists. If one did, it would still be answered by a
  filter on the underlying's stream before it were answered by 1,564 keys.
- **Redis Cluster.** Not planned. It would need a hashtag in the key grammar so one venue's streams
  landed in one slot, which is an addition to §1 of the nomenclature and not a rewrite.
- **A measurement we did not take.** `computed.chain` is bounded at `derived` under 1% and never
  measured. If it turns out to be large — many expiries, many strikes, a live pass per watched pair
  — the thirty-minute number is wrong and criterion 3 gets a second look.
