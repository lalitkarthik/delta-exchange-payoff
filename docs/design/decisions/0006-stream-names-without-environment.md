# 0006 — Stream names carry no environment

**Status** decided, #74, 2026-09-12. **Supersedes** [0001](0001-stream-naming-and-payload-format.md)
in one part: the environment section of the grammar. Everything else in 0001 stands.
**Fills** [../cloud/nomenclature.md](../cloud/nomenclature.md). **Evidence** the senior's
review of the closed research, recorded in #57 under "Amendments, 2026-09-12".
**Changes** [../lld/redis-bus.md](../lld/redis-bus.md).

## The question

0001 made `{env}` the mandatory first section of every stream name, so that two stacks
sharing a Redis by mistake would not read each other's data. The senior has ruled that
there will be exactly one local stack and one prod, never a staging, never a shared
container. Does the slot still earn its place?

## The options

| Option | What a key looks like |
|---|---|
| **Remove the slot** | `md.option_quote:DELTA:BTC`, `heartbeat:DELTA`, `alert` |
| Keep it, fixed to one value | `prod:md.option_quote:DELTA:BTC` everywhere, always |
| Make it optional | present if `DELTA_BUS_ENV` is set, absent if not |

## The decision

**The slot is removed.** The grammar is `{event_type}:{VENUE}[:{UNDERLYING}]`. `alert`,
the one type with neither a venue nor an underlying, is keyed as `alert`. The
`DELTA_BUS_ENV` setting is gone; if it is set, it is ignored. Streams left under the old
`dev:` or `prod:` names in a local Redis are not migrated — the pipe holds thirty minutes,
and they age out.

## Why, in the criteria's order

**1. Invariants.** The share the slot guarded against cannot happen any more. A laptop's
Redis and prod's Redis are different processes on different hosts, and prod's is not
reachable from outside its instance (0002). And the slot was itself a way to lose data
silently: a producer configured `prod` and a consumer configured `dev` read nothing,
raise nothing, and look healthy. Removing the slot removes that failure.

**2. Operations burden.** One fewer setting to get right on every service, in every
Compose file, in every deploy. A value that must match across five containers and is
never checked is a value that will one day not match.

**3. Cost.** None. A stream's key is stored once per stream, not once per entry, so the
bytes saved are nothing worth counting. `derived` from Redis's stream structure.

**4. Path to OMS, NSE, more consumers.** Unchanged. The venue section stays, so NSE is
`:NSE` beside `:DELTA`. A new consumer still builds its key list from configuration, never
from the keyspace.

**5. Latency.** Unchanged.

## Rejected, and why

| Option | Why not |
|---|---|
| **Fixed to one value** | A slot that always holds the same value is a false statement about the design, carried by every key, every test and every document. It still has to match across every service. |
| **Optional** | It makes the grammar two grammars. Any code that reads a key by position then has to know whether the first section is an environment or an event type. `stream_type` already read the type from position one, because the environment was position zero. |

## What would change this decision

- **A second environment sharing one Redis** — a staging reintroduced on prod's instance,
  for example. The answer then is a new record that brings a prefix back. This one is not
  edited.
- **Redis Cluster.** 0001 already says it would need a hashtag in the grammar. That change
  would be made in its own record.
