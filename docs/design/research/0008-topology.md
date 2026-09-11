# R7 — One instance, or several

Findings for #78, under epic #57. Decision [../decisions/0008-topology.md](../decisions/0008-topology.md);
spec [../cloud/compute-topology.md](../cloud/compute-topology.md); load R6 [0007-load-profile.md](0007-load-profile.md)
§4–§8; baseline R4 [0005-compute-and-region.md](0005-compute-and-region.md). I14 (#80) measures the hop.

## The question

One instance, or several — and if several, which services share a box? #57's criteria, in order:
(1) invariants, (2) operations for two or three people, (3) cost at 1× and 10×, (4) path to OMS, NSE
and more consumers, (5) latency to the venue. **No engine measurement was taken here.** CPU and memory
are R6's `derived` profile (0007 §8: cores per service at one viewer, reservations, Redis 3 GiB, 13 at
10×, 0.5 GiB OS a box); `oms` and `strategy` are R6's `assumed` rows (0007 §6). All below is `derived`
from those and §1 unless tagged. **Fit rule**, R6's, `assumed`: upper CPU ≤ 70% of vCPUs, reservations
≤ 90% of RAM, burstable `t4g` only below baseline and only for `store`, `web`, proxy or Redis. **A box**
is the instance, a 30 GB gp3 root (60 GB on `store`'s box at 10×) and one public IPv4, as in 0005.

## 1. Prices

On-demand Linux, ap-south-1, `measured`, **AWS Price List Bulk API** (`.../AmazonEC2/current/ap-south-1/index.csv`,
`publicationDate` 2026-09-10T19:55:14Z, read 2026-09-12; shared tenancy). Monthly × 730 h, `derived`.
The seven classes 0005b read on 2026-09-09 are unchanged.

| Class | vCPU / GiB | $/hr | $/month | Class | vCPU / GiB | $/hr | $/month |
|---|---|---|---|---|---|---|---|
| `c7g.medium` | 1 / 2 | 0.0245 | 17.89 | `m7g.medium` | 1 / 4 | 0.0292 | 21.32 |
| `c7g.large` | 2 / 4 | 0.0491 | 35.84 | `m7g.large` | 2 / 8 | 0.0583 | 42.56 |
| `c7g.xlarge` | 4 / 8 | 0.0982 | 71.69 | `m7g.xlarge` | 4 / 16 | 0.1166 | 85.12 |
| `c7g.2xlarge` | 8 / 16 | 0.1963 | 143.30 | `m7g.2xlarge` | 8 / 32 | 0.2333 | 170.31 |
| `c7g.4xlarge` | 16 / 32 | 0.3926 | 286.60 | `m7g.4xlarge` | 16 / 64 | 0.4666 | 340.62 |
| `c7g.8xlarge` | 32 / 64 | 0.7853 | 573.27 | `r7g.large` | 2 / 16 | 0.0751 | 54.82 |
| `t4g.small` | 2 / 2 | 0.0112 | 8.18 | `t4g.medium` | 2 / 4 | 0.0224 | 16.35 |

gp3 $0.0912/GB-month and IPv4 $0.005/hour: 0005b §2, `measured` 2026-09-09 — **$6.39 a box**. Baseline
bandwidth `.large` 0.937 Gbps, `.xlarge` 1.876 (AWS [gp](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html),
[co](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html), read 2026-09-12).

## 2. Is traffic between two instances free?

**Yes — in one AZ, over private addresses, and nowhere else.** AWS's
[EC2 on-demand pricing page](https://aws.amazon.com/ec2/pricing/on-demand/), *Data Transfer within the same
AWS Region* (read 2026-09-12 from a copy cached 2026-09-11 03:17Z; page updated 2026-08-20): traffic
between instances and interfaces "in the same Availability Zone is free". Across AZs, and to or from a
public or Elastic IPv4, it is $0.01/GB **in each direction**; the [CUR guide](https://docs.aws.amazon.com/cur/latest/userguide/cur-data-transfers-charges.html)
meters it as `{Region}-DataTransfer-Regional-Bytes`, one line per side.

1. **R6's assumption holds on one condition:** `DELTA_REDIS_URL` (`redis_bus.py`, default loopback)
   names box A's *private* address. A public address works too — and bills silently.
2. **R6's cross-AZ price is half.** Both sides pay, so $0.02/GB: T2's 3,144 GB/month is **$62.88** at
   1× and $628.83 at 10×, not 0007 §4's $31.44.
3. **Every box of a split sits in one AZ**, so a split buys no AZ redundancy.

## 3. How long is the hop?

**AWS's documentation states no same-AZ latency figure.** It says an AZ is one or more discrete data
centres, within 100 km of every other AZ ([Regions and AZs](https://aws.amazon.com/about-aws/global-infrastructure/regions_az/)).
**Infrastructure Performance** publishes intra-AZ round-trip latency live — the p50 of AWS's probes per
five minutes, ap-south-1 included, free, readable before any instance exists
([what](https://docs.aws.amazon.com/network-manager/latest/infrastructure-performance/what-is-nmip.html),
[how](https://docs.aws.amazon.com/network-manager/latest/infrastructure-performance/how-nmip-works.html)).
**`derived` ceiling, 1 ms round trip:** 100 km each way at ~5 µs/km (`assumed`, light in fibre); two
instances in one AZ are nearer (`assumed`). Every hop cell below uses 1 ms, the bad end.

**The topologies.** T1 one box (0005, re-costed). T2 `feed`+Redis ∣ `store`+`api`+`web`+proxy. T3 one
per service. T4 `feed`+`store`+Redis ∣ `api`+`web`+proxy — R6's contention finding: `feed` apart from
`api`, the lossless path kept whole.

## 4. Criterion 1 — invariants

| | Lossless path `feed`→Redis→`store` | When a box dies | Can `feed` be starved? |
|---|---|---|---|
| **T1** | one box, loopback | everything stops: holes until replaced | on `c7g.xlarge`, no (below). On `m7g.large` at the upper bound, yes: 1.95 of 2 vCPU |
| **T2** | crosses the hop to `store` | box A: holes. Box B: nothing if back within Redis's 30 min; after, `store` jumps what was trimmed, counted | no; `api` can starve `store`, which Redis covers for 30 min |
| **T3** | three boxes, two hops | Redis's box: `feed` fills its outbox for 108 s, then drops oldest, counted ([../lld/redis-bus.md](../lld/redis-bus.md) §4); `store`'s replay point is gone | no |
| **T4** | one box, loopback | box A: holes, as T1. Box B: nothing; `api` refills in `measured` 508 ms (redis-hosting §2) | no |

**Why a 4-vCPU box cannot starve `feed` at 1×:** each Python service is one event loop, one core at
most (0007). With `api` saturated: `feed` 0.71 + `store` 0.51 + `api` 1.0 + Redis and proxy 0.10 = 2.32
of 4 vCPU. ECS maps a container's `cpu` to Linux CPU shares, guaranteed under contention, free when idle
([task definition](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters_ec2.html)):
1,024 units reserve `feed` a vCPU for $0. **T1 on 4 vCPU ties T4; T2 is behind; T3 last.**

## 5. Criterion 2 — operations, as monthly tasks

**T1** is [../cloud/compute.md](../cloud/compute.md) §6: replace the AMI (~30 min), read ECS events (~10),
check disk (~5) and bill (~5), confirm the private path, deploy per change. In every topology, replacing
`feed`'s box leaves a hole as long as the replacement. **A second box adds:**

1. **Patching:** a second AMI, ~30 min `assumed`. T4's box B loses nothing; T2's must beat 30 min.
2. **Deploy:** each service pinned to its box by a placement constraint; a misplaced task is a new failure.
3. **Address:** `host`-mode [service discovery](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-discovery.html)
   registers SRV records only, and our client reads one URL: box A keeps a fixed private IP.
4. **Redis leaves loopback:** a private bind, a security-group rule for 6379, a password (0002 sets none).
5. **Alarms:** status, CPU and disk per box plus #57's `stopped`: 4 for T1, 7 for T2/T4, 16 for T3 — past
   CloudWatch's 10 free alarm metrics ([pricing](https://aws.amazon.com/cloudwatch/pricing/), cached 2026-09-11; the rate beyond not read).
6. **Monthly check:** both boxes in one AZ; no `APS3-DataTransfer-Regional-Bytes` line on the bill.
7. **Incidents** read two hosts' logs; a bus fault may be a network fault.

`assumed` totals: T1 ~50 min a month plus deploys; T2 or T4 ~1.5 h; **T3 ~3.5 h** — five AMIs, three
rules, and Redis on the self-managed EC2 0002 rejected on this criterion.

## 6. Criterion 3 — cost

`derived`, all-in, cheapest class passing the fit rule; every box in [../cloud/compute-topology.md](../cloud/compute-topology.md) §3.

| Topology | 1× boxes (upper) | 1× lower | 1× upper | 10× lower | 10× upper |
|---|---|---|---|---|---|
| **T1** one box | `c7g.xlarge` | **$48.94** (`m7g.large`) | **$78.07** | **$295.72** | **$582.39** |
| **T2** | `c7g.large` ∣ `c7g.large` | $55.40 | $84.46 | $445.40 | $588.70 |
| **T3** | `c7g.large`, `c7g.medium` ×2, `t4g.small`, `t4g.medium` | $110.11 | $128.07 | $384.34 | $670.86 |
| **T4** | `m7g.large` ∣ `c7g.medium` | $73.22 | $73.22 | $373.79 | $732.08 |

**With `oms` and `strategy`** (§7), 1× lower–upper: T1 $78.07–149.69, T2 $91.17–127.02, T3
$158.66–194.57, T4 $76.65–127.02. At 10×: T1 $582.39, **no class priced** at the upper (28.25 cores);
T2 $588.70–875.38; T3 $486.68–988.12; T4 $445.40–1,162.05. **Two AZs would add** (§2) T2 $62.88, T3
$94.32, T4 $31.44 a month at 1×, ten times that at 10×.

- **0005's trigger, answered.** `m7g.large` carries only the lower bound (1.21 of 2 vCPU). The upper,
  1.95 cores (0007 §8 prints 1.94: rounding), needs 4 vCPU; memory never binds (5.05 GiB), so that is
  `c7g.xlarge` at $78.07, not 0005's forecast `m7g.xlarge` at $94.24. At 10× its `m7g.2xlarge` carries
  neither bound: 11.15–17.75 cores need 16–32 vCPU.
- **One box is cheapest at every load but one.** T4 is $4.85 under it at the 1× upper bound, with `api`,
  `web` and proxy on a one-vCPU `c7g.medium` at 68%; a second large-ladder viewer (+0.14, 0007 C4) makes it 82%.
- **The crossover is `strategy`.** At its assumed upper bound one box needs 3.05 cores, a `c7g.2xlarge`
  at $149.69; T4 carries it on two boxes for $127.02.
- **T3 is 1.6–2.2× T1 at 1×**, though one vCPU suits a one-core service: `store` and `api` fit `c7g.medium`.

## 7. Criterion 4 — slots for `oms` and `strategy`, `assumed`

| | `oms` | `strategy` |
|---|---|---|
| **T1** | a container on the box, `cpu` units reserved | same box; its upper bound forces `c7g.2xlarge` |
| **T2** | box A, beside `feed` and Redis | box B |
| **T3** | its own `c7g.medium` | its own `c7g.large` |
| **T4** | box A, Redis at loopback, away from `api` and viewers | box B, beside `api`, reading `computed.chain` |

Each raw-stream consumer pays a 0.22–0.45 core decode at 1× wherever it sits (0007 §6). **T4 is the only
split keeping the lossless path and a would-be order path at loopback**, with viewer load elsewhere.

## 8. Criterion 5 — latency, to the venue and across the hop

**To the venue, unchanged by topology:** `feed`'s box reaches the same CloudFront POP (0005 §5), and the
lever is an uncontended core for `feed`, whose decode dominates (I2). **Across the hop**, at 1 ms:

| Consumer | What observes it | T1 | T2 | T3 | T4 | Hop ÷ cadence |
|---|---|---|---|---|---|---|
| `feed` | flush every 50 ms, `measured` 96.4 ms achieved (0061); `publish` never awaits | loopback | loopback | **hop**: 1 ms + 57.2 KB a batch (177 × 323 B) at 0.937 Gbps, 0.49 ms | loopback | ≤ 3% of the interval |
| `store` | one-minute bars on the venue's stamp, never arrival ([../lld/store.md](../lld/store.md)); 8.0 s grace; flush 300 s | loopback | hop | hop | loopback | 1/60,000 of a bar, 1/8,000 of the grace |
| `api` | ladder push once a second; 100 ms solve pass | loopback | hop | hop | hop | ≤ 0.1% of a push, ≤ 1% of a pass |
| `web` | page loads; the browser reaches `api` via the proxy | same box | same box | hop | same box | ≤ 0.1% of a push |

**Throughput never binds:** a reader takes up to 500 entries a call (`DEFAULT_READ_COUNT`); 1.85 arrive
per 1 ms at 1×, 18.5 at 10×. Redis serves 1,196.4 KB/s (0007 N4): 1% of 0.937 Gbps at 1×, 10% at 10×.

## 9. What to notice — does the 50 ms batch survive a hop?

**Yes, and in three topologies of four the question never arises:** T1, T2 and T4 keep `feed` and Redis
on loopback. Only T3 puts the network in the flush: ≤ 1.5 ms a batch against a 50 ms request and a
`measured` 96.4 ms achieved period, and I2 found the decode, not the interval, sets latency (p50 230,
195, 235 ms, unordered; 0061). **The answer does not change.** I14 (#80) re-runs `tools/measure_bus_live.py`
on the deployed `feed`, as 0061 asks; if Redis leaves `feed`'s box (T3, or 0002's ElastiCache fallback),
it falls under 0002's trigger "the network hop, once measured".

## 10. Redis off loopback — what changes in 0002

In T2 and T4 Redis stays beside `feed` but serves another box: a private bind behind a security group,
so 0005's "nothing outside the box can reach it" stops being true. Persistence off, `noeviction` and
2 GiB stand; an `OOM` still lands at the publisher. In T3 Redis alone is 0002's rejected self-managed
option — $22.74 at 1× (`t4g.medium`), $61.21 at 10× (`r7g.large`) — so 0002's ElastiCache fallback is
only $24.56 dearer and removes the patching; and `feed` outlives a Redis failure, dropping after 108 s.

## 11. `oms` latency — open, not decided

**Question:** how much latency can an order path spend between intent and venue — and so must `oms` sit
with Redis, with `strategy`, or alone? **What would answer it:** (1) Delta's order round trip from
ap-south-1, unmeasured ([0005a-venue-latency-run.md](0005a-venue-latency-run.md) §3); the WARP-tunnelled
`measured` 238.71 ms ping and `derived` ~167 ms edge-to-origin suggest the venue leg dwarfs 1 ms, an
argument, not a measurement. (2) I14's measured hop. (3) How intents travel: over Redis, or in process
with `strategy`. (4) Whether an order path makes Redis durable (0002's first trigger). T4 forecloses none.

## 12. Left open

**I14 (#80):** read the ap-south-1 intra-AZ p50 in Infrastructure Performance on day one of access, then measure
`feed`→Redis→consumer on the boxes; note whether 1 ms held. **I13 (#79):** a 1× total ≤ 1.4 cores puts `m7g.large` back.

## What I learned

**A bigger box and more boxes answer different questions.** Each of our services uses one core at most, so a
4-vCPU box has room for three of them flat out. Size buys room; separate machines buy isolation. We need room.

**Where you cut decides whether a split is safer.** Keep `feed`, Redis and `store` together and the data we must
never lose never touches the network; the screen, which may drop a frame, can go elsewhere.

**"Free" on a cloud bill has conditions.** Same zone, private address: free. Another zone, or a public address:
charged at both ends. So R6's $31.44 was really $62.88. Read the sentence, not the headline.

**A latency is big or small only next to something.** One millisecond matters to an order router, not to a screen
redrawn once a second. Divide the hop by the cadence of whoever would notice; `oms` has none yet, so it stays open.
