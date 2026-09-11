# R4 — prices, arithmetic and sources

The working behind [0005-compute-and-region.md](0005-compute-and-region.md). Nothing is
decided here; this file exists so every number in that one can be checked without re-reading
it. Same method as R5's [0002b-prices-and-sources.md](0002b-prices-and-sources.md).

## 1. How the prices were read

All from the **AWS Price List Bulk API**, the machine-readable form of the public pricing
pages, fetched **2026-09-09**:

```
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEKS/current/<region>/index.json
```

`publicationDate`: EC2 **2026-09-09T00:46:05Z**; ECS **2026-08-31T09:21:55Z**; EKS
**2026-08-31T09:21:57Z**. Regions `ap-south-1` (Mumbai) **primary**, `ap-southeast-1`
(Singapore), `ap-northeast-1` (Tokyo) and `us-east-1` (N. Virginia). **On-demand terms only** —
`offerTermCode` `JRTCKXETXF`; Linux, shared tenancy, `preInstalledSw` `NA`, `capacitystatus`
`Used`, no license. One month is **730 hours**.

**Why no Reserved Instances or Savings Plans.** Both buy a one- or three-year commitment to a
size. The footprint here is `measured` on a monolith #65 has not yet split into five
containers, and #68's own acceptance criteria require a re-cost after it lands. A discount
bought against a size we intend to re-derive is a discount on the wrong thing. Note the
lever; pull it when the shape stops moving.

Spot check, so the extraction can be trusted: `m7g.large` in ap-south-1 carries the
description "$0.0583 per On Demand Linux m7g.large Instance Hour" — the same value the EC2
on-demand pricing page renders.

## 2. Unit prices, `measured` from the price list

**EC2 on-demand Linux, USD/hr**, and gp3 storage USD/GB-month.

| | vCPU | mem | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|---|---|
| `t4g.medium` | 2 | 4 GiB | 0.0224 | 0.0424 | 0.0432 | 0.0336 |
| `t4g.large` | 2 | 8 GiB | 0.0448 | 0.0848 | 0.0864 | 0.0672 |
| `c7g.large` | 2 | 4 GiB | 0.0491 | 0.0833 | 0.0910 | 0.0725 |
| **`m7g.large`** | 2 | 8 GiB | **0.0583** | 0.1020 | 0.1054 | 0.0816 |
| `m7g.xlarge` | 4 | 16 GiB | 0.1166 | 0.2040 | 0.2108 | 0.1632 |
| `r7g.xlarge` | 4 | 32 GiB | 0.1502 | 0.2584 | 0.2584 | 0.2142 |
| **`m7g.2xlarge`** | 8 | 32 GiB | **0.2333** | 0.4080 | 0.4216 | 0.3264 |
| `m7i.large` (x86) | 2 | 8 GiB | 0.10605 | 0.1260 | — | 0.1008 |
| gp3 provisioned storage | | | 0.0912 | 0.096 | 0.096 | 0.08 |

`m7i.large` is here for one reason: it is the x86 twin of `m7g.large` and costs **82% more**
in ap-south-1 for the same two vCPUs and 8 GiB. Graviton is not a preference, it is the price.

**AWS Fargate, USD/hr.**

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| ARM vCPU-hour | **0.02383** | 0.04045 | 0.04045 | 0.03238 |
| ARM GB-hour | **0.00261** | 0.00442 | 0.00442 | 0.00356 |
| x86 vCPU-hour | 0.04256 | 0.05056 | 0.05056 | 0.04048 |
| x86 GB-hour | 0.004655 | 0.00553 | 0.00553 | 0.004445 |
| Ephemeral storage, GB-hour over the free 20 GB/task | 0.000127 | 0.000133 | 0.000133 | 0.000111 |

**EKS.** `AmazonEKS-Hours:perCluster` **$0.10/hour in all four regions**, `= $73.00/month`.
`AmazonEKS-Hours:extendedSupport` **+$0.50/hour** in all four.

**ECS.** No SKU exists for the EC2 launch type — the price list carries only Fargate,
Managed Instances and Windows SKUs, which is the price list's way of saying $0. ECS Managed
Instances, if ever used, is a per-instance surcharge on top of EC2: `m7g.large` **$0.006996/hr
= $5.11/month** in ap-south-1 (12% of the instance), `t4g.medium` $0.002688/hr = $1.96.

**Network.**

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| NAT gateway, per hour | 0.056 | 0.059 | 0.062 | 0.045 |
| NAT gateway, **per GB processed** | **0.056** | 0.059 | 0.062 | 0.045 |
| In-use public IPv4, per hour | 0.005 | 0.005 | 0.005 | 0.005 |
| Regional data transfer, in/out/between AZs, **charged in each direction** | 0.01 /GB | 0.01 | 0.01 | 0.01 |

## 3. The arithmetic, cell by cell

**What is being sized.** `measured` 1,693.6 msg/s and 843.4 KB/s (BTC+ETH, `hld.md` §5);
`derived` 1,849.8 events/s and a 1,051.5 MiB Redis pipe at thirty minutes (#58), so a **2 GiB
`maxmemory`** (#69) and 12 GiB at ten times. `measured` engine CPU 30.89% of one core and
117.1 MiB resident, 2026-09-09.

**Inbound volume.** 843.4 KB/s × 2,628,000 s = 2.2165 × 10¹² bytes = **2,216.5 GB/month**;
22,164.6 GB at ten times.

**NAT gateway, ap-south-1.** Fixed 0.056 × 730 = $40.88. Data 2,216.5 × 0.056 = **$124.12**;
total **$165.00**. Ten times: $40.88 + 22,164.6 × 0.056 = $1,241.22 → **$1,282.09**.
us-east-1, the cheapest of the four: $132.59 and $1,030.25. **In every region the data charge
alone exceeds the compute it fronts.**

**EC2 with Compose, and ECS on EC2 — the same bill.** One instance + a 30 GB gp3 root volume
+ one in-use public IPv4 ($0.005 × 730 = $3.65):

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| 1×, `m7g.large` + 30 GB | 42.56 + 2.74 + 3.65 = **48.94** | 80.99 | 83.47 | 65.62 |
| 1×, `t4g.large` + 30 GB | **39.09** | 68.43 | 69.60 | 55.11 |
| 1×, `t4g.medium` + 30 GB | **22.74** | 37.48 | 38.07 | 30.58 |
| 10×, `m7g.2xlarge` + 60 GB | 170.31 + 5.47 + 3.65 = **179.43** | 307.25 | 317.18 | 246.72 |
| 10×, `m7g.xlarge` + 60 GB | **94.24** | 158.33 | 163.29 | 127.59 |

**Fargate.** Six tasks at the §1 reservations of the findings file — 1×: **3.0 vCPU, 8.0 GB**;
10×: 9.5 vCPU, 29 GB. Each task in a public subnet takes one public IPv4 at $3.65.

| | vCPU cost | memory cost | 6 × IPv4 | total |
|---|---|---|---|---|
| 1× ARM, ap-south-1 | 3.0 × 0.02383 × 730 = 52.19 | 8.0 × 0.00261 × 730 = 15.24 | 21.90 | **89.33** |
| 1× x86, ap-south-1 | 93.21 | 27.18 | 21.90 | 142.29 |
| 1× ARM, us-east-1 | 70.91 | 20.79 | 21.90 | 113.60 |
| 1× ARM, ap-northeast-1 | 88.59 | 25.81 | 21.90 | 136.30 |
| 10× ARM, ap-south-1 | 9.5 × 0.02383 × 730 = 165.26 | 29 × 0.00261 × 730 = 55.25 | 21.90 | **242.41** |

Behind a NAT gateway instead of public IPs, ap-south-1: 67.43 + 165.00 = **$232.43** at 1×
and 220.51 + 1,282.09 = **$1,502.60** at ten times. That is the whole Fargate argument, and
it is a subnet choice, not a platform one.

**EKS.** $73.00 control plane + nodes priced as the EC2 rows above:

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| 1×, one `m7g.large` node | **121.94** | 153.99 | 156.47 | 138.62 |
| 1×, two `m7g.large` nodes | 170.89 | 234.98 | 239.94 | 204.24 |
| 10×, one `m7g.2xlarge` node | **252.43** | 380.25 | 390.18 | 319.72 |
| 10×, two `m7g.2xlarge` nodes | 431.86 | 687.50 | 707.36 | 566.44 |

One node is the honest comparison against one EC2 host, and it is also the configuration
that makes EKS pointless: a control plane whose job is to reschedule a pod has nowhere to
reschedule it to.

**Sizing rule, `derived`.** 1× needs ≥ 2 vCPU and ≥ 8 GiB: the 2 GiB Redis ceiling, five
Python/Node containers at a `measured` 117–203 MiB each, the OS, and page cache for a store
writing `derived` 143 MB/day. `t4g.medium`'s 4 GiB leaves ~1.3 GiB for everything but Redis,
and its baseline is 20% per vCPU × 2 = **0.4 of a core** against a `measured` 0.31 plus the
publisher's `measured` 5.88% — no margin. 10× needs ≥ 6 vCPU (0.37 × 10, `derived`) and
≥ 16 GiB (12 GiB Redis + services + OS), so `m7g.2xlarge`; `m7g.xlarge`'s 4 vCPU is under the
`derived` requirement and is listed only to show what the tight buy costs.

## 4. Sources

Primary only. Every claim in [0005-compute-and-region.md](0005-compute-and-region.md) traces
here. All read **2026-09-09**.

| Claim | Source |
|---|---|
| "There is no additional charge for Amazon EC2 compute. You pay for AWS resources… you create"; Fargate billed per second with a one-minute minimum "from the time your container images are pulled until the Amazon ECS Task terminates"; "20 GB of ephemeral storage is available for all Fargate Tasks and Pods by default"; ECS Managed Instances adds "a per-instance management fee" | [Amazon ECS pricing](https://aws.amazon.com/ecs/pricing/) |
| "$0.10 per cluster per hour" standard support; "$0.60 per cluster per hour" extended; nodes billed separately through EC2 or Fargate | [Amazon EKS pricing](https://aws.amazon.com/eks/pricing/) |
| Fargate valid task CPU/memory combinations (0.25–32 vCPU and their memory ranges); Linux containers on Fargate "can use the X86\_64 CPU architecture, or the ARM64 architecture"; a Fargate task in a public subnet needs a public IP "with a route to the internet or a NAT gateway"; "Fargate tasks always use the `awsvpc` network mode" | [ECS task definition differences for Fargate](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html) |
| "containers that belong to the same task can communicate over the `localhost` interface"; "Each task can only have one ENI"; on EC2 with `awsvpc`, task ENIs "aren't given public IP addresses… tasks must be launched in a private subnet that's configured to use a NAT gateway"; dual-stack IPv6 makes NAT gateways "optional" | [Allocate a network interface for an Amazon ECS task](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-networking-awsvpc.html) |
| `t4g.medium` has 2 vCPUs and a baseline utilization of **20% per vCPU**; `t4g.large` 30%; in Standard mode an instance out of credits "gradually comes down to baseline CPU utilization"; T4g default to Unlimited, which bills "a flat additional rate per vCPU-hour" above baseline over 24 hours | [Key concepts for burstable performance instances](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html) |
| "Hourly charge for In-use Public IPv4 Address $0.005", applying to EC2 instances and ENIs; NAT gateway hourly and per-GB data processing charges | [Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/) |
| `13.224.0.0/14` is service `CLOUDFRONT`, region `GLOBAL`; `syncToken` 1788945425, `createDate` 2026-09-09-09-17-05 | [AWS IP address ranges](https://ip-ranges.amazonaws.com/ip-ranges.json) |
| "Delta Exchange data centers are in **AWS Tokyo**", under *Data Centers*; REST base `https://api.india.delta.exchange`; API-key IP whitelisting; 20,000 requests per fixed 5-minute window | [Delta Exchange API documentation](https://docs.delta.exchange/) |
| `wss://public-socket.india.delta.exchange`, the URL the adapter connects to | `engine/src/deltapayoff/adapters/delta_socket.py` L98 |
| CNAME chains, addresses, POP `BOM78-P11`, TCP/TLS/websocket/REST timings, the WARP vantage point | `tools/measure_venue_latency.py`, `measured` 2026-09-09 — [0005a-venue-latency-run.md](0005a-venue-latency-run.md) |
| Engine 30.89% of a core, 117.1 MiB resident; `web` idle | `Get-Process` 60.69 s sample, `measured` 2026-09-09 13:18 UTC |
| 1,693.6 msg/s, 843.4 KB/s, 782 contracts | `tools/measure_feed.py`, `measured` 2026-09-08 — `docs/design/hld.md` §5 |
| Full solve pass 175.227 ms; the pre-#44 loop at ~64% of a core | `t44-solve` — `docs/design/lld/chain-cache.md` §9 |
| Redis publisher 5.88% of a core at 100 ms batches; 1,051.5 MiB at thirty minutes; the 2 GiB ceiling | #58, #69 — [0002-redis-hosting.md](0002-redis-hosting.md), [../decisions/0002-redis-hosting.md](../decisions/0002-redis-hosting.md) |
| Store 143 MB/day | `docs/storage.md` |
| All EC2, Fargate, EKS, NAT and gp3 unit prices | AWS Price List Bulk API, §1 above |
