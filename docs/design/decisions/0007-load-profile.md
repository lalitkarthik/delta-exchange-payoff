# 0007 — The shape of the load

**Status** decided, #75, 2026-09-12. **Supersedes** nothing. **Fills**
[../cloud/compute.md](../cloud/compute.md) §8. **Evidence**
[../research/0007-load-profile.md](../research/0007-load-profile.md). **Depends on**
[0005-compute-and-region.md](0005-compute-and-region.md), whose re-cost trigger this meets, and
[0002-redis-hosting.md](0002-redis-hosting.md), whose memory ceiling it carries. **Replaced by
`measured`** in I13 (#79).

## The question

Which of our processes is compute-bound, which memory-bound, which disk- or network-bound, and
by how much? The answer lets R7 (#78) size instances to the work rather than to a guess. The
profile is the decision: what we believe the load looks like.

## The options

| | Considered |
|---|---|
| A. By what each service produces | `store` disk-bound, Redis memory-bound, `feed` network-bound, `api` CPU-bound |
| B. The monolith, divided | the split costs the monolith's `measured` 0.31 core, shared out. 0005 worked on this and flagged it as an under-count |
| C. Memory-led | size by reservations: 8 GB at 1×, which set 0005's instance |
| **D. Per event, per process** | **every Python service CPU-bound on its one core; Redis and `web` memory-bound; nothing disk- or network-bound** |

## The decision

**Every Python service is compute-bound, capped at one core by its single event loop. Redis and
`web` are memory-bound. Nothing is disk- or network-bound, at 1× or at 10×.** `derived` at 1×
(1,849.8 events/s, 782 contracts):

| | Bound by | 1× | 10× |
|---|---|---|---|
| `feed` | CPU | 0.52–0.71 core | 5.2–7.1 cores, 8–11 processes |
| `store` | CPU (decode and fold), not disk | 0.28–0.51 core; 2.33 KB/s to disk | 2.8–5.1 cores |
| `api` | CPU, set by viewers as much as by rate | 0.25–0.49 core, + 0.05–0.14 per watched expiry | 2.5–4.9, + viewers |
| `web` | memory | ~0 core, 240.8 MiB | unchanged |
| Redis | memory | 1,056.4 MiB `measured`, 2 GiB ceiling | 10.3 GiB, 12 GiB ceiling |
| `oms` | latency and durability, `assumed` | 0.1 core `assumed` | 0.1–0.5 `assumed` |
| `strategy` | CPU, `assumed` | 0.3–1.0 core `assumed` | 2.5–10 `assumed` |

- **The split costs 1.10–1.75 cores against the monolith's 0.31.** The publisher encodes every
  event and each consumer decodes the whole stream. One process never paid for either.
- **`feed` and `api` contend first if co-located**, on CPU.
- **The share we least believe is `api`'s**, so I13 measures it first.

## Why, in the criteria's order

**1. Invariants.** The belief decides where the risk of loss sits, so it has to be right about
CPU first. A starved `feed` falls behind the venue socket. The receive buffer fills and Delta
closes the connection, which leaves holes in the store: the loss criterion 1 refuses. A starved
`store` loses nothing, because Redis holds thirty minutes and it catches up. A starved `api`
serves an older ladder, because its reader drops oldest. So `feed` never runs on a burstable
class, and never beside an unbounded `api` without headroom. `store` is the one service allowed
a credit model.

**2. Operations burden.** One box still fits at 1×: `m7g.large` at the lower bound, `m7g.xlarge`
at the upper. The profile does not force a second host on a two-person team. Splitting buys
isolation between `feed` and `api`, not capacity.

**3. Cost.** `derived`, ap-south-1, instance only, at 0005b's prices: one box
**$42.56–85.12**; `feed` and Redis apart from the rest **$71.68**; one box per service
**$137.08**. At ten times the rate **no class priced in 0005b carries one box at the upper
bound**: 11.1–17.7 cores against 8 vCPU. The cheaper lever is the per-event cost, paid three
times over: the encoding R1 left open, or a compiled decoder. The instance is not the lever.

**4. Path to the right half.** Every new raw-stream consumer pays one decode, 0.22–0.45 core at
1×: `strategy`, an alert channel, an NSE screen. `oms` is assumed small in every resource and
bound by latency instead. That argues for its own core, never one shared with `api`.

**5. Latency to the venue.** I2 found that the feed's own decode, not the batch interval,
dominates publish-to-consume latency. Giving `feed` a core of its own is the latency lever this
profile controls.

## Rejected, and why

| Rejected | Why |
|---|---|
| **A. By what each service produces** | The store writes 2.33 KB/s against a gp3 baseline of 125 MiB/s: 0.002%. The feed reads 6.9 Mbit/s against a 0.937 Gbps baseline: 0.73%. The adjectives are wrong for `store` and `feed`, and would buy disk and network nobody uses |
| **B. The monolith, divided** | It omits the two costs the bus adds: 29.96 points to encode and publish, and 22–45 points per consumer to decode. So it under-counts by 3.6–5.7×. It is how 0005 reached 6 vCPU at 10× |
| **C. Memory-led** | The derived need at 1× is 5.05 GiB, with Redis's 3 GiB inside it; the Python services are 50–115 MiB each. 0005's 8 GB is fine, and it is not what binds. Its 3.0 vCPU is |

## What would change this decision

- **I13 (#79) moving any cell by more than 25%**, measured per container on the host. `api`
  comes first, at 0, 1 and 3 viewers. Then `feed`, whose derived 0.52 disagrees with I2's
  `measured` 0.71.
- **The decode measured with `BLOCK` reads on Linux.** Near the lower bound, 1× fits
  `m7g.large` with room. Near the upper, it does not.
- **A compiled codec.** MessagePack, or a compiled JSON decoder, cuts the per-consumer cost
  everywhere at once and moves every service toward the lower bound.
- **Graviton's per-core speed.** Every CPU figure is one laptop core, `assumed` equal to one
  Graviton3 vCPU. If Graviton is slower per core, every cell grows by the same factor.
- **`oms` or `strategy` becoming real**, which turns their `assumed` rows into measurements. An
  order path moves `oms` from "small" to "latency-critical" before anything else.
- **Ten times the rate arriving.** The first question becomes processes per service, not
  instance size.

## I13 (#79) status

I13 has not run (#65/I6 has not landed as of this writing); R6's re-cost threshold is: "I13 (#79) moving any cell by more than 25%, measured per container on the host. api comes first, at 0, 1 and 3 viewers. Then feed, whose derived 0.52 disagrees with I2's measured 0.71." crossed: pending; see docs/design/research/0007a-container-measurement.md. This note is appended, not rewritten, per this repository's rule that a decision record is superseded rather than edited in place; the substantive determination (crossed or not) is written here once docs/design/research/0007a-container-measurement.md carries measured values.

## #95 correction — the seventh service, and where 29.96 is now quoted

**Appended, not rewritten.** Every figure in this record stands; #95 verified 29.96 against the
run and changed nothing here.

**`discord-alerts` (#66) is a seventh service and is in none of the tables above.** It reads
`event_types=("alert",)` only (`../lld/discord-alerts.md` §1), so the rule this record states —
"every raw-stream consumer we add pays a 0.22–0.45 core decode at 1×" — **does not apply to it**;
alerts are rare by construction. It is `assumed` 0.05 core and 0.25 GiB. Added to §4's 1× upper
bound of 1.95 cores that gives `derived` **2.00**, inside [0008](0008-topology.md)'s 2.8-core
split threshold, so no row of §4, §5 or §8 moves and no decision reopens. I13 (#79) measures it
with the rest.

**Option B's 29.96 points is now what `../cloud/compute.md` §4 sizes `feed` on.** That file
carried #69's 100 ms 5.88% until #95 — the under-count this record rejects — so §4's reservation
table and the profile above now agree. `feed` reserves 1,024 CPU units, as
[0008](0008-topology.md) already required.

**Redis's 10× figure, reconciled.** The table above reads **10.3 GiB**; `../research/0002-redis-hosting.md`
reads **10.269 GiB** and `../research/0061-batch-interval.md` read **10.5**. There is one number:
ten times the `derived` 1,051.5 MiB working set is 11,025,776,640 B = **10.269 GiB**, and 10.3 is
that rounded — both correct, and this record's rounding stands. 10.5 was a decimal divide of a
binary quantity and is corrected in 0061 by #95. The 12 GiB ceiling is unaffected.

**One tagging note.** 29.96 points is `derived`, not `measured`: it is `measured` 70.73% minus
`measured` 40.77%, from I2's run of 2026-09-09. Both it and the 5.88% it replaces trace to real
runs, and the tag was no help in telling them apart — what separated them was the batch interval.
`../cloud/compute-numbers.md` §2 sets out the two conditions side by side.
