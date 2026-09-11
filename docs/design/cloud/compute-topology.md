# One instance or several — the topology comparison

The data feed engine document's topology section, split out of [compute.md](compute.md) (§9 there
is the summary). Why each cell is what it is:
[../research/0008-topology.md](../research/0008-topology.md). The decision:
[../decisions/0008-topology.md](../decisions/0008-topology.md). The load it prices: R6,
[../decisions/0007-load-profile.md](../decisions/0007-load-profile.md). Landed by #78 as a standard;
nothing here is built.

---

## 1. The choice, in one line

**One `c7g.xlarge` (4 vCPU, 8 GiB), ECS on EC2, `host` mode, ap-south-1: `derived` $78.07 a month.**
`feed` reserves 1,024 CPU units. At ten times the rate, one `c7g.4xlarge` or `c7g.8xlarge`, $295.72–582.39.
**The next topology is T4**, `feed` + `store` + Redis ∣ `api` + `web` + proxy. We move to it when the
box passes 2.8 cores, or `oms` places its first order.

## 2. The comparison the senior asked for

#57's five criteria in order. Dollars are `derived`, all-in (instance, 30 GB gp3, one IPv4), at R6's
upper bound, 1× → 10×. Prices: AWS Price List Bulk API, ap-south-1, published 2026-09-10, read 2026-09-12.

| | (1) Invariants | (2) Ops, a month | (3) $/mo, 1× → 10× | (4) Path to the right half | (5) Latency |
|---|---|---|---|---|---|
| **T1 one box** ← chosen | lossless path on loopback; on 4 vCPU `api` cannot take `feed`'s core; a box failure stops everything | ~50 min + deploys; 4 alarms | **$78.07 → $582.39** | `oms` and `strategy` as containers; `strategy`'s upper bound forces `c7g.2xlarge` ($149.69) | no hop anywhere |
| T2 `feed`+Redis ∣ rest | `store` crosses the hop; box B's replacement must beat Redis's 30 minutes | ~1.5 h; 7 alarms; Redis leaves loopback | $84.46 → $588.70 | `oms` beside Redis, `strategy` on box B | `store`, `api` read across ≤ 1 ms |
| T3 one per service | three boxes and two hops on the lossless path; Redis's box dying drops `feed`'s outbox after 108 s | ~3.5 h; 16 alarms; five AMIs | $128.07 → $670.86 | a box per new service | the hop enters `feed`'s flush: ≤ 1.5 ms per 50 ms batch |
| **T4** `feed`+`store`+Redis ∣ `api`+`web`+proxy ← next | lossless path on loopback; box B dying loses nothing | ~1.5 h; 7 alarms; Redis leaves loopback | $73.22 → $732.08 | `oms` beside Redis at loopback, `strategy` beside `api` | only `api` reads across ≤ 1 ms |

**At the lower bound**, 1× → 10×: T1 $48.94 → $295.72, T2 $55.40 → $445.40, T3 $110.11 → $384.34,
T4 $73.22 → $373.79. **With `oms` and `strategy` added** (`assumed`), 1×: T1 $78.07–149.69, T2
$91.17–127.02, T3 $158.66–194.57, T4 $76.65–127.02.

## 3. The boxes

The cheapest class passing R6's fit rule (`assumed`: upper CPU ≤ 70% of vCPU, reservations + 0.5 GiB
OS ≤ 90% of RAM, burstable only for `store`, `web`, proxy or Redis below baseline).

| | 1× lower | 1× upper | 10× lower | 10× upper |
|---|---|---|---|---|
| T1 | `m7g.large` | `c7g.xlarge` | `c7g.4xlarge` | `c7g.8xlarge` |
| T2 | `m7g.medium` ∣ `m7g.medium` | `c7g.large` ∣ `c7g.large` | `c7g.4xlarge` ∣ `c7g.2xlarge` | `c7g.4xlarge` ∣ `c7g.4xlarge` |
| T3 `feed` / `store` / `api` / `web`+proxy / Redis | `c7g.medium` / `c7g.medium` / `c7g.medium` / `t4g.small` / `t4g.medium` | `c7g.large` / `c7g.medium` / `c7g.medium` / `t4g.small` / `t4g.medium` | `c7g.2xlarge` / `c7g.xlarge` / `c7g.xlarge` / `t4g.small` / `r7g.large` | `c7g.4xlarge` / `c7g.2xlarge` / `c7g.2xlarge` / `t4g.small` / `r7g.large` |
| T4 | `m7g.large` ∣ `c7g.medium` | `m7g.large` ∣ `c7g.medium` | `c7g.4xlarge` ∣ `c7g.xlarge` | `c7g.8xlarge` ∣ `c7g.2xlarge` |

T4's `c7g.medium` holds `api` at 68% with one viewer; a second viewer on the largest ladder takes it
to 82%, past the rule, and the next size is `c7g.large`.

## 4. The hop, per consumer

`derived` at a 1 ms same-AZ round-trip ceiling. AWS's documentation gives no figure; the ceiling comes
from AZs lying within 100 km of each other. Infrastructure Performance publishes the live intra-AZ p50
for ap-south-1.

| Consumer | Cadence | Hop ÷ cadence |
|---|---|---|
| `feed` (T3 only) | flush every 50 ms | ≤ 3%: 50 ms holds |
| `store` (T2, T3) | one-minute bars on the venue's stamp; flush every 300 s | 1/60,000 of a bar |
| `api` (T2, T3, T4) | ladder push once a second | ≤ 0.1% |
| `web` (T3 only) | page loads; proxy→`api` per push | ≤ 0.1% |
| `oms` | unknown | **open**: findings §12 |

## 5. What a second box adds

A second AMI to replace; each service pinned to its box by an ECS placement constraint; box A on a
fixed private IP in `DELTA_REDIS_URL`, since `host`-mode service discovery offers SRV records only;
Redis bound to that address behind a security-group rule and a password; per-box alarms (past 10,
CloudWatch's free alarm metrics are exceeded); and a monthly check that both boxes share one AZ and the
bill has no `APS3-DataTransfer-Regional-Bytes` line.

**Same-AZ transfer is free between instances on private addresses.** Across AZs, or through a public
or Elastic IPv4 even in one AZ, it is $0.01/GB **in each direction** (EC2 on-demand pricing, read
2026-09-12). A split's bytes across two AZs add `derived` T2 $62.88, T3 $94.32, T4 $31.44 a month at
1×, and ten times that at 10×.

## 6. The numbers behind this section

| Number | Tag | Source |
|---|---|---|
| Per-service CPU and memory, 1× and 10× | `derived` | R6, [../research/0007-load-profile.md](../research/0007-load-profile.md) §4, §5, §8 |
| `oms`, `strategy` | `assumed` | R6 §6 |
| Instance $/hr, 16 classes | `measured` | AWS Price List Bulk API, EC2 ap-south-1, published 2026-09-10T19:55:14Z, read 2026-09-12 |
| gp3 $0.0912/GB-month, IPv4 $0.005/hr | `measured` | [../research/0005b-prices-and-sources.md](../research/0005b-prices-and-sources.md) §2, 2026-09-09 |
| Same-AZ free; cross-AZ and public-IPv4 $0.01/GB each way | `measured` | [EC2 on-demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/), read 2026-09-12 |
| Same-AZ round trip ≤ 1 ms | `derived` | AZs within 100 km ([AWS](https://aws.amazon.com/about-aws/global-infrastructure/regions_az/)) × ~5 µs/km `assumed` |
| Every monthly total | `derived` | × 730 h; arithmetic in the findings §4 |
| Ops minutes | `assumed` | [compute.md](compute.md) §6, and the findings §6 |
| The hop, measured | **unmeasured** | I14 (#80), after access |
