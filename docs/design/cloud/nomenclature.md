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
{env}:{event_type}:{VENUE}[:{UNDERLYING}]
```

| Part | Drawn from | Spelling |
|---|---|---|
| `{env}` | the deployment | lower case, one of `dev`, `prod`, later `staging`. Mandatory; there is no unprefixed key |
| `{event_type}` | the event's own `type` field, verbatim | exactly as `docs/design/events.md` spells it, dots included |
| `{VENUE}` | `Instrument.venue`, or the event's `adapter` | upper case — `DELTA`, later `NSE` |
| `{UNDERLYING}` | `Instrument.underlying`, or the payload's `underlying` | upper case — `BTC`, `ETH` |

**`:` separates sections and `.` lives inside one.** That is Redis's own convention: "there is a
convention for using the colon ':' character to split keys into sections", and "Dots or dashes are
often used for multi-word fields"
([Keys and values](https://redis.io/docs/latest/develop/using-commands/keyspace/)). So the
environment is a section of its own — `prod:md.option_quote:DELTA:BTC`, not
`prod.md.option_quote:DELTA:BTC`, which is #57's working proposal with its first separator
corrected.

### The nine

| Event | Stream | Why that arity |
|---|---|---|
| `md.option_quote` | `{env}:md.option_quote:{VENUE}:{UNDERLYING}` | the hot one; a reader wanting BTC must not parse ETH |
| `md.option_reference` | `{env}:md.option_reference:{VENUE}:{UNDERLYING}` | same |
| `md.index_quote` | `{env}:md.index_quote:{VENUE}:{UNDERLYING}` | `instrument` is `null`; the underlying comes off the payload |
| `md.option_bar` | `{env}:md.option_bar:{VENUE}:{UNDERLYING}` | the `table` discriminator stays in the payload — four names for one envelope would be four registries |
| `computed.chain` | `{env}:computed.chain:{VENUE}:{UNDERLYING}` | `instrument` is `null`; the expiry stays in the payload, because expiries list and settle daily and a key that appears daily is a key a reader misses |
| `feed.connection` | `{env}:feed.connection:{VENUE}` | no underlying; one socket per venue |
| `heartbeat` | `{env}:heartbeat:{VENUE}` | same |
| `alert` | `{env}:alert` | `adapter` is nullable and no reader wants a subset: the logger and the Discord consumer take all of them |
| `control.command` | `{env}:control.command:{VENUE}` | **inbound.** Per venue because the reader is one `feed` per venue, and a DELTA feed must never read an NSE command |

Examples, in full:

```
prod:md.option_quote:DELTA:BTC          dev:md.option_quote:DELTA:BTC
prod:md.option_reference:DELTA:ETH      prod:md.index_quote:DELTA:BTC
prod:md.option_bar:DELTA:BTC            prod:computed.chain:DELTA:ETH
prod:feed.connection:DELTA              prod:heartbeat:DELTA
prod:alert                              prod:control.command:DELTA
```

Longest key today, `prod:md.option_reference:DELTA:BTC`, is 34 bytes — far inside the "very long
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
#57 and not open here.

- Joined on `-`, and no part may contain one; `venue` and `underlying` already refuse it.
- `STRIKE` carries no trailing zeros and never an exponent — `format_strike`.
- The string is **derived and never stored as truth.** It exists so a log line, a cache key, a URL
  and a stream field agree.
- `venue_symbol` is deliberately not in it: the string names a contract, and two venues listing the
  same contract should produce the same string.
- `Instrument.from_canonical` splits on `-` and expects five parts today. **I1 widens it to six**
  and defaults the currency; until I1 lands, no wire form carries the suffix.

---

## 4. Environments

**The prefix is mandatory and separate instances are the real separation.** `dev` runs a Redis
container on a laptop, `prod` runs its own; they never share one. The prefix is what makes a
mistaken share harmless — no key collides — rather than what prevents it.

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
| Start id, `store` | `0` — take everything Redis still holds | |
| Start id, `api` | `$` — never replay; the cache refills from live frames | |

**No environment and no venue in a group name.** A group lives inside one stream and the stream key
already carries both; repeating them would be two places for one fact.

**One group per service, never one per instance.** That is what makes the store and the screen
independent readers of the same stream rather than competitors for one message.
