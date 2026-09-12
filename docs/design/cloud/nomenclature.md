# Nomenclature: streams, the envelope on the wire, symbols, consumer groups

**One name, written once, so that two people and three services spell the same thing the same
way.** This is the standard #58 fixes. Why each rule is what it is — the three systems read, the
options rejected, the bytes measured — is
[../research/0001-stream-naming-and-payload-format.md](../research/0001-stream-naming-and-payload-format.md);
the decision itself is
[../decisions/0001-stream-naming-and-payload-format.md](../decisions/0001-stream-naming-and-payload-format.md).
What the nine events *are* stays [../events.md](../events.md), which wins on names and directions.

Landed by #58 as a standard, not as code. Nothing here is built yet.

---

## 1. Stream names

```
{event_type}:{VENUE}[:{UNDERLYING}]
```

| Part | Drawn from | Spelling |
|---|---|---|
| `{event_type}` | the event's own `type` field, verbatim | exactly as `docs/design/events.md` spells it, dots included |
| `{VENUE}` | `Instrument.venue`, or the event's `adapter` | upper case — `DELTA`, later `NSE` |
| `{UNDERLYING}` | `Instrument.underlying`, or the payload's `underlying` | upper case — `BTC`, `ETH` |

**`:` separates sections and `.` lives inside one.** That is Redis's own convention: "there is a
convention for using the colon ':' character to split keys into sections", and "Dots or dashes are
often used for multi-word fields"
([Keys and values](https://redis.io/docs/latest/develop/using-commands/keyspace/)). The event type
is the first section — `md.option_quote:DELTA:BTC`, with the dots in `md.option_quote` kept
inside that section.

### The ten

| Event | Stream | Why that arity |
|---|---|---|
| `md.option_quote` | `md.option_quote:{VENUE}:{UNDERLYING}` | the hot one; a reader wanting BTC must not parse ETH |
| `md.option_reference` | `md.option_reference:{VENUE}:{UNDERLYING}` | same |
| `md.index_quote` | `md.index_quote:{VENUE}:{UNDERLYING}` | `instrument` is `null`; the underlying comes off the payload |
| `md.option_bar` | `md.option_bar:{VENUE}:{UNDERLYING}` | the `table` discriminator stays in the payload — four names for one envelope would be four registries |
| `computed.chain` | `computed.chain:{VENUE}:{UNDERLYING}` | `instrument` is `null`; the expiry stays in the payload, because expiries list and settle daily and a key that appears daily is a key a reader misses |
| `store.state` | `store.state:{VENUE}` | no underlying; one `store` process per venue reports its own state |
| `feed.connection` | `feed.connection:{VENUE}` | no underlying; one socket per venue |
| `heartbeat` | `heartbeat:{VENUE}` | same |
| `alert` | `alert` | `adapter` is nullable and no reader wants a subset: the logger and the Discord consumer take all of them |
| `control.command` | `control.command:{VENUE}` | **inbound.** Per venue because the reader is one `feed` per venue, and a DELTA feed must never read an NSE command |

Examples, in full:

```
md.option_quote:DELTA:BTC          md.option_quote:DELTA:ETH
md.option_reference:DELTA:ETH      md.index_quote:DELTA:BTC
md.option_bar:DELTA:BTC            computed.chain:DELTA:ETH
store.state:DELTA                  feed.connection:DELTA
heartbeat:DELTA                    alert
control.command:DELTA
```

Longest key today, `md.option_reference:DELTA:BTC`, is 29 bytes — far inside the "very long
keys are not a good idea" advice on the same Redis page, and far above "very short keys are often
not a good idea".

### Discovery is forbidden

**A reader builds its key list from configuration and never from the keyspace.** No `KEYS`, no
`SCAN`, no pattern. `XREAD` and `XREADGROUP` take an explicit list of streams and have no wildcard,
so a stream discovered late is a stream that was silently not read — the #51 failure exactly, where
contracts listed after start-up were never subscribed and only the history was damaged. The
underlyings a service reads are the same configured list `--underlyings` already gives the feed.

---

## 2. The envelope on the wire

One Redis stream entry per event: `XADD <stream> * field value [field value ...]`. The envelope is
flat, one Redis field per key; the type's own keys are one nested JSON object.

| Redis field | Value | Present |
|---|---|---|
| `type` | the event type, e.g. `md.option_quote` | always |
| `event_id` | 32 hex characters | always |
| `schema_version` | the integer as text, e.g. `1` | always |
| `source` | who built it, e.g. `DELTA` | always |
| `ts_received` | RFC 3339 with an offset, e.g. `2026-09-04T09:08:35.798142+00:00` | always |
| `ts_venue` | RFC 3339 with an offset | **omitted** when the venue gave none |
| `instrument` | the canonical string, §3 | **omitted** when the event is not about one contract |
| `venue_symbol` | the venue's own string, verbatim | **omitted** when the instrument carries none |
| `payload` | a JSON object holding every key that is not one of the seven envelope keys | always |

Rules, in the order they matter:

1. **Absent is omitted, never spelled.** No field carries `""`, `"None"`, `"null"` or `0` to mean
   absent. A Redis stream field is a binary string with no null in it, which is why the absent
   values live inside `payload` where JSON has one.
2. **`null` is not `0`, and inside `payload` it is a JSON `null`.** Every decimal is a JSON number
   or `null`, converted once at the adapter boundary, exactly as `docs/design/events.md` fixes it.
3. **`payload` plus the envelope fields is the whole event.** Reassembling them into one mapping
   and calling `events.parse_event` is the only decode there is: `UnknownEventType`,
   `UnknownSchemaVersion`, `extra="forbid"` and the non-finite refusal all keep working over the
   wire with nothing new written to hold the line.
4. **`venue_symbol` travels because the canonical string does not carry it.**
   `Instrument.from_canonical(text, venue_symbol=...)` takes it back, which is what that keyword
   argument is for. Without it a consumer could not call the venue back without a reverse lookup.
5. **Field order is as tabled.** Redis "stores the field-value pairs in the same order you provide
   them" ([XADD](https://redis.io/docs/latest/commands/xadd/)), and a stream whose entries repeat
   one field set in one order is the case its listpack compresses against the node's master entry.
6. **A `type` on the wire that disagrees with the stream it arrived on is an error**, not a
   preference. The stream name is a claim about its contents and a consumer that trusts it must be
   able to.

---

## 3. The canonical symbol

```
VENUE-UNDERLYING-YYYYMMDD-STRIKE-C|P-CCY
```

`DELTA-BTC-20260627-60000-C-USD`. The last token is the **quote** currency, ISO 4217. Decided in
#57 and landed by #60 (I1) — this is the shape every wire form and cache key carries now.

- Joined on `-`, and no part may contain one; `venue` and `underlying` already refuse it.
- `STRIKE` carries no trailing zeros and never an exponent — `format_strike`.
- The string is **derived and never stored as truth.** It exists so a log line, a cache key, a URL
  and a stream field agree.
- `venue_symbol` is deliberately not in it: the string names a contract, and two venues listing the
  same contract should produce the same string.
- `Instrument.from_canonical` splits on `-` and expects **six** parts. #60 (I1) widened it from
  five and made the five-part pre-I1 form raise `InstrumentParseError` rather than defaulting a
  currency onto it — a stale caller fails loudly instead of silently addressing a contract with
  no currency. `settlement_currency`, the instrument's second currency field, does not travel in
  this string at all; only the quote currency does, because that is the one a reader of a price
  needs.

---

## 4. Environments

Stream names carry no environment: there is one Redis on a laptop and one in prod, and they never share one (I11, #74; decision record 0006).

**Numbered databases are not used, and `SELECT` is never called.** Redis's own page says it:
"Use Redis databases to separate keys within the same application when needed. Don't use them to
run multiple unrelated applications in a single Redis instance", and "When using Redis Cluster,
the `SELECT` command cannot be used, since Redis Cluster only supports database zero"
([SELECT](https://redis.io/docs/latest/commands/select/)). Redis Software and Redis Cloud do not
support shared databases at all. A layout that depends on database numbers cannot move to a
managed Redis, and #57 has not chosen one yet.

---

## 5. Consumer groups

| Thing | Rule | Example |
|---|---|---|
| Group name | the service name, lower case, one word | `store`, `api` |
| Consumer name | `{service}-{instance}`, the container's short id or `1` | `store-1` |
| Creation | `XGROUP CREATE <stream> <group> <id> MKSTREAM` | |
| Start id, `store` | checkpoint id in `<root>/_store-checkpoint.json` for that stream; on first start the head (`$`, taken once as a concrete id); never `0` — [0010](../decisions/0010-store-replay.md), which supersedes [0002](../decisions/0002-redis-hosting.md) on this point | |
| Start id, `api` | `$` — never replay; the cache refills from live frames | |

**No environment and no venue in a group name.** A group lives inside one stream and the stream key
already carries the venue; repeating it would be two places for one fact.

**One group per service, never one per instance.** That is what makes the store and the screen
independent readers of the same stream rather than competitors for one message.
