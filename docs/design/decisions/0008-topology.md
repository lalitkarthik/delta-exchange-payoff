# 0008 — One instance, or several

**Status** decided, #78, 2026-09-12. **Supersedes** [0005-compute-and-region.md](0005-compute-and-region.md)
in part: the instance class at 1× and at 10×. Its platform, network mode, subnet and region stand.
**Fills** [../cloud/compute-topology.md](../cloud/compute-topology.md) and
[../cloud/compute.md](../cloud/compute.md) §9. **Evidence**
[../research/0008-topology.md](../research/0008-topology.md). **Depends on**
[0007-load-profile.md](0007-load-profile.md), whose profile this prices, and
[0002-redis-hosting.md](0002-redis-hosting.md), whose loopback Redis a split would change.
**Measured by** I14 (#80).

## The question

One EC2 instance, or several — and if several, which services share a box — given R6's profile of
what each service costs and #57's five criteria?

## The options

| | Considered |
|---|---|
| **T1** | **one box — 0005's shape, re-costed** |
| T2 | `feed` + Redis ∣ `store` + `api` + `web` + proxy |
| T3 | one box per service |
| T4 | `feed` + `store` + Redis ∣ `api` + `web` + proxy — R6's contention finding, `feed` apart from `api` |
| Class at 1× | `m7g.large`; `m7g.xlarge`; **`c7g.xlarge`** |
| Class at 10× | `m7g.2xlarge`; **`c7g.4xlarge`**; **`c7g.8xlarge`** |

## The decision

**One instance stays. Its class changes: ECS on EC2, one `c7g.xlarge` (Graviton3, 4 vCPU, 8 GiB) in
a public subnet of `ap-south-1`, `host` network mode, `derived` $78.07 a month. At ten times the rate,
one `c7g.4xlarge` or `c7g.8xlarge`, `derived` $295.72–582.39.**

- **`feed`'s container reserves 1,024 CPU units**, a whole vCPU, as ECS CPU shares. They are
  guaranteed under contention and free when idle, and they cost $0.
- **The next topology is T4**, `feed` + `store` + Redis ∣ `api` + `web` + proxy, with `oms` on the
  first box and `strategy` on the second. It is costed now at $73.22–127.02 at 1×.
- **The threshold:** we split into T4 when **the box's load passes 2.8 cores** (70% of 4 vCPU,
  measured by I13 or CloudWatch) or **`oms` places its first order through the bus**, whichever comes
  first. At `strategy`'s assumed upper bound that is where one box costs $149.69 and T4 $127.02.
- **If we split, every box is in one subnet of one AZ**, and `DELTA_REDIS_URL` names a private address.

## Why, in the criteria's order

**1. Invariants.** A starved `feed` leaves holes. On `m7g.large` at the upper bound the six containers
want 1.95 of 2 vCPU, and `api`'s viewers can take `feed`'s core — so 0005's class fails here. On 4 vCPU
it cannot happen at 1×: each Python service is one event loop and one core at most (0007). With `api`
saturated the box peaks at 2.32 of 4, and the CPU shares hold `feed`'s vCPU regardless. The lossless
path `feed`→Redis→`store` stays on loopback. T4 ties on this criterion; T2 puts `store` behind a box
replacement that must finish inside Redis's thirty minutes; T3 puts three boxes and two hops on the
lossless path.

**2. Operations burden.** One box keeps compute.md §6's list: about fifty minutes a month plus
deploys, `assumed`. A second box adds a second AMI, placement constraints, a fixed private address for
Redis, a security-group rule and a password, per-box alarms, and a monthly same-AZ check: about 1.5
hours. One box per service is about 3.5 hours with sixteen alarms, `assumed`. **This is the
criterion T1 wins on after tying T4 on the first.**

**3. Cost.** `derived`, all-in, ap-south-1: prices from the Bulk API published 2026-09-10 and read
2026-09-12, plus gp3 and IPv4 from 0005b.

| | 1× lower | 1× upper | 10× lower | 10× upper | 1× with `oms` + `strategy` |
|---|---|---|---|---|---|
| **T1, one box** | **$48.94** | **$78.07** | **$295.72** | **$582.39** | $78.07–149.69 |
| T2 | $55.40 | $84.46 | $445.40 | $588.70 | $91.17–127.02 |
| T3 | $110.11 | $128.07 | $384.34 | $670.86 | $158.66–194.57 |
| T4 | $73.22 | $73.22 | $373.79 | $732.08 | $76.65–127.02 |

One box is cheapest at every load but the 1× upper bound, where T4 is $4.85 less. T4 gets there on a
one-vCPU `api` box that a second large-ladder viewer pushes past the fit rule. Same-AZ traffic is free
on private addresses only; across AZs it is $0.01/GB in each direction, so T4 in two AZs adds $31.44
at 1×.

**4. Path to the right half.** On one box `oms` and `strategy` are containers with reserved shares.
T4 is the growth path because it is the only split that keeps both the lossless path and a would-be
order path at loopback, and moves the viewer-driven load away from them. `oms` latency is left open
(below).

**5. Latency to the venue.** Topology does not move it. The lever is an uncontended core for `feed`,
which this decision buys. The same-AZ hop is `derived` at most 1 ms round trip: three orders below the
one-second ladder and five below the one-minute bar. I2's 50 ms batch is untouched, because `feed`
and Redis stay on one box in T1 and T4.

## Rejected, and why

| Rejected | Why |
|---|---|
| **`m7g.large`, 0005's class** | Criterion 1. It carries R6's lower bound (1.21 cores), not the upper (1.95 of 2 vCPU), where `api` can take `feed`'s core. It returns if I13 measures the 1× total at 1.4 cores or less |
| **`m7g.xlarge`, 0005's forecast** | Criterion 3. It buys 16 GiB against a 5.05 GiB need, $13.44 a month over `c7g.xlarge` for memory that never binds |
| **`m7g.2xlarge` at 10×** | Criterion 1. 8 vCPU against 11.15–17.75 cores |
| **T2, `feed` + Redis apart** | Criteria 1 and 2. `store` crosses the hop and shares a box with `api`'s spikes, and the second box adds the full list of new tasks. It ties T4 on cost with the slots in and loses to it on criterion 1 |
| **T3, one per service** | Criteria 1, 2 and 3. Three boxes on the lossless path, ~3.5 hours a month, 1.6–2.2× T1 at 1×. Redis on its own box is the self-managed option 0002 rejected, and the only topology that puts the hop inside `feed`'s flush |
| **`feed` alone ∣ the rest** | Criterion 1. The publisher's flush would cross the network to Redis. $91.17 at 1× against T1's $78.07 |
| **Two AZs for a split** | Criterion 3, and it still buys no redundancy for `feed`. $31.44–94.32 a month at 1× and ten times that at 10× |

## Open: `oms` latency

Whether an order path can spend a same-AZ hop — and so whether `oms` must sit beside Redis — is
**not decided**. It is answered by Delta's order round trip measured from ap-south-1 (0005a §3), I14's
measured hop, and how intents travel. Findings §12.

## What would change this decision

- **I13 (#79)** measuring the 1× total at 1.4 cores or less brings back `m7g.large` at $48.94. Past
  2.8 cores, the threshold above applies.
- **`oms` or `strategy` becoming real.** The first order through the bus moves us to T4, with the
  `oms` latency question answered first.
- **I14 (#80)** measuring the same-AZ hop materially above 1 ms. Re-derive the hop table in the findings
  (§7), and re-run the batch interval if Redis ever leaves `feed`'s box.
- **Ten times the rate.** `feed` becomes 8–11 processes. In `host` mode one task definition runs once
  per instance, so the shards are separate task definitions, or containers in one task, each on its
  own port.
- **Wanting AZ redundancy.** That is a second `feed` in another AZ plus cross-AZ transfer at $0.01/GB
  each way, and a different decision.
- **Graviton's per-core speed**, or a compiled codec. Either moves every CPU cell by one factor, and
  with it the class.
