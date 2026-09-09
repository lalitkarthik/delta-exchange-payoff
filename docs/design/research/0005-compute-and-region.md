# R4 — Where the containers run, and in which region

Findings for #68, under epic #57. The decision is
[../decisions/0005-compute-and-region.md](../decisions/0005-compute-and-region.md), the spec
section [../cloud/compute.md](../cloud/compute.md), the verbatim latency run and the commands
for the run we cannot take yet [0005a-venue-latency-run.md](0005a-venue-latency-run.md), and
every unit price and source URL [0005b-prices-and-sources.md](0005b-prices-and-sources.md).

## The question

Where do the containers run — one EC2 host under Compose, ECS on Fargate, ECS on EC2, or
EKS — and in which region, given our footprint and the five criteria?

Decided against #57's criteria, in order: **(1)** invariants; **(2)** ops burden for two or
three people; **(3)** cost at our rate and ten times it; **(4)** path to OMS, NSE and more
consumers; **(5)** latency to the venue. Findings, not a decision, live here.

**What the compute must carry.** Six containers: `feed`, `store`, `api`, `web`, a reverse
proxy, and **Redis with `maxmemory 2gb`** — R5's decided fact
([../decisions/0002-redis-hosting.md](../decisions/0002-redis-hosting.md)). Redis's memory is
bought here, which is why that decision priced it as "one instance-size step on R4's host".

**On-demand prices only.** Reserved Instances and Savings Plans buy a one- or three-year
commitment to a size we have not chosen, measured on a monolith #65 has not split and which
this ticket re-costs afterwards. The discount is real; the order is wrong.

## 1. The footprint we are sizing, `measured` today

The engine is one process on this laptop, not yet the five #65 will build, so this is an
under-count of the split.

| Number | Tag | Run |
|---|---|---|
| Engine (`uvicorn deltapayoff.main:app`, PID 31040) **30.89% of one core** | `measured` | 60.69 s sample, `Get-Process` CPU-time delta, 2026-09-09 13:18 UTC, 16 logical cores |
| Engine resident **117.1 MiB** mean / 118.3 max working set; **178.6 MiB** mean private | `measured` | same sample, 30 points at 2 s |
| `web` (Next.js dev, PID 14956) **0.00% of a core**, 43.1 MiB working set, 203.1 MiB private | `measured` | same sample — **no viewer was attached**; `/health` reported `watched: []` |
| Feed **1,693.6 msg/s**, **843.4 KB/s**, BTC+ETH, 782 contracts | `measured` | `tools/measure_feed.py`, 2026-09-08, `hld.md` §5 |
| 1,849.8 canonical events/s; Redis pipe 1,051.5 MiB at 30 min | `derived` | #58 |
| Redis publisher, 100 ms batches: **5.88% of a core** | `measured` | `tools/measure_redis_hosting.py`, 2026-09-09 (upper bound: WSL2 port forward) |
| Full solve pass **175.227 ms** median; the pre-#44 loop ran at **~64% of a core** | `measured` / `derived` | `lld/chain-cache.md` §9, `t44-solve` |
| Store **143 MB/day** | `derived` | `docs/storage.md` |

**The honest caveat.** 30.89% is the whole monolith with **nothing watched**: a solve pass
costs `measured` 0.005 ms with no screen, 9.761 ms with one, and the pre-#44 all-expiry loop
was `derived` 64% of a core. **The CPU we buy is set by the screen, not the feed.**

**Reservations — `derived`, and reservations, not measurements:** `feed` 0.5 vCPU / 1 GB,
`store` 0.5 / 1, `api` 1.0 / 2, `web` 0.25 / 0.5, `proxy` 0.25 / 0.5, **`redis` 0.5 / 3**
(the 2 GiB ceiling plus overhead) — **3.0 vCPU, 8.0 GB**. At ten times: 9.5 vCPU, 29 GB, with
Redis at 10.269 GiB needing a 12 GiB ceiling (#58, #69).

**Image sizes are not measured and cannot be:** #65 has not landed and there is no
`Dockerfile` here (`find . -iname 'Dockerfile*'`, 2026-09-09, empty). **Re-cost after #65
against the `measured` image sizes and per-container footprint** — the criterion the unblock
note added.

## 2. The four platforms against the five criteria

| | (1) Invariants | (2) Ops for 2–3 people | (3) $/mo ap-south-1, 1× / 10× | (4) Path to the right half | (5) Latency |
|---|---|---|---|---|---|
| **EC2 + Compose** | same images; a `feed` that exits loudly with no Redis restarts under `restart: unless-stopped` and **nothing records that it did** | OS patching, disk, image pruning, and **you are the only restarter** | **$48.94 / $179.43** | a new consumer is an edit to one YAML file on one box you `ssh` into | Redis on loopback, **no hop**; venue path unchanged |
| **ECS on EC2** | identical, plus every restart is an ECS event and an alarmable state change | the same host to patch, minus container restarts and minus deploys-by-`ssh` | **$48.94 / $179.43** — ECS adds **$0** on the EC2 launch type | a new consumer is a task definition and a service; the Discord consumer (#66) and an NSE feed attach without touching the host | loopback **if `host` or `bridge` network mode**; `awsvpc` breaks it and forces a NAT gateway |
| **ECS on Fargate** | identical; there is no host to misconfigure | least of the four: no AMI, no agent, no disk, no patching | **$89.33 / $242.41** (ARM, 6 tasks, public subnet) — **$232.43 / $1,502.60** behind a NAT gateway | best per-service isolation of the four | **each task gets its own ENI**; separate tasks means Redis is a VPC hop — see §4 |
| **EKS** | identical | the same host work **plus** a Kubernetes minor upgrade at least yearly, add-ons in sequence, and manifests | **$121.94 / $252.43** one node; **$170.89 / $431.86** two | the most, and the most we would not use | same as ECS on EC2 |

`derived` 2026-09-09 from `measured` unit prices; arithmetic in [0005b](0005b-prices-and-sources.md) §3.

## 3. The number that decides the shape: inbound bytes through a NAT gateway

`measured` 843.4 KB/s inbound is `derived` **2,216.5 GB a month**, 22,164.6 GB at ten times. A
NAT gateway in ap-south-1 charges `measured` **$0.056/GB processed** plus $0.056/hour.

| | fixed | data | total 1× | total 10× |
|---|---|---|---|---|
| NAT gateway, ap-south-1 | $40.88 | **$124.12** | **$165.00** | **$1,282.09** |
| One in-use public IPv4 | $3.65 | $0 | **$3.65** | $3.65 |

**A NAT gateway costs more than the compute it fronts, and seven times it at ten times the
rate.** The venue's frames are inbound, and inbound to AWS is free — until something meters
them. So: **the containers sit in a public subnet with a public IPv4 and no NAT gateway.**
That rules out ECS `awsvpc` on EC2, where AWS is explicit — task ENIs "aren't given public IP
addresses… tasks must be launched in a private subnet that's configured to use a NAT
gateway". Fargate is exempt: a task in a public subnet can carry `assignPublicIp: ENABLED`,
at $3.65/month per task.

Both endpoints also resolve `AAAA` (§5), so an IPv6-only egress through an egress-only
internet gateway avoids both charges. Not costed here; worth a spike.

## 4. What #68 told us to notice: does I2's batch interval survive?

**On EC2, yes; on Fargate, only if you give something up.** ECS `host` and `bridge` network
modes leave every container on the instance's own network stack, so `feed`→Redis stays
loopback and I2's interval, chosen on loopback, still applies. Under `awsvpc` — which Fargate
*always* uses — "Each task can only have one ENI", and only "containers that belong to the
same task can communicate over the `localhost` interface".

So Fargate forces a choice: **one task of six containers**, keeping Redis on loopback and
throwing away the independent deployability that is the point of #57; or **six tasks**,
keeping independent deployment and putting a VPC hop between `feed` and Redis.
The hop is `unmeasured` — no AWS account on this machine — and R5 already requires the batch
interval to be re-measured across any hop. **This goes back to R5 (#69): same re-measurement,
same reason, a different platform triggering it.**

## 5. Where Delta answers from, `measured`

`tools/measure_venue_latency.py`, 2026-09-09, 20 samples per figure, from this laptop; full
output in [0005a-venue-latency-run.md](0005a-venue-latency-run.md).

**Both endpoints are behind Amazon CloudFront.** `public-socket.india.delta.exchange` is a
CNAME to `d2gb279nx3imhv.cloudfront.net`, `api.india.delta.exchange` to
`d15zy4kc8a63om.cloudfront.net`; every address behind both falls inside `13.224.0.0/14`,
which AWS's own `ip-ranges.json` (`createDate` 2026-09-09-09-17-05) labels service
`CLOUDFRONT`, region `GLOBAL`. Our requests are served by POP **`BOM78-P11`** — Mumbai.

**And Delta says where the origin is.** Their API documentation, under *Data Centers*: "Delta
Exchange data centers are in **AWS Tokyo**" — consistent with what we measured from outside. A
cache-busted REST call costs `measured` **191.18 ms** more than a cacheable one; the origin's
own `Request-In-Time`/`Request-Out-Time` pair accounts for 23.9 ms of that, leaving `derived`
**~167 ms** of round trip between the Mumbai edge and the origin.

| From this laptop | `measured` p50 |
|---|---|
| TCP connect, `public-socket…` (peer 13.225.5.64) | 107.70 ms |
| TLS handshake, same | 173.23 ms |
| Websocket handshake, connect → upgrade complete | 1,178.21 ms |
| **Websocket protocol ping → pong** | **238.71 ms** (a repeat run 14 min earlier: 234.74 ms) |
| REST GET, cacheable (edge hit) | 398.43 ms |
| REST GET, cache-busted (edge miss, reaches origin) | 589.61 ms |

**Every one of those is unusable as an absolute, and the tool says so.** This machine runs
**Cloudflare WARP** (`warp=on`, egress colo `MAA`, `measured` from `cloudflare.com/cdn-cgi/trace`;
`tracert` hop 1 is Cloudflare's `2a09:bac5::`), so every packet is tunnelled through Chennai.
*Differences* between rows survive that; absolutes do not.

**A measurement from two AWS regions needs access we do not have.** The exact commands are in
[0005a](0005a-venue-latency-run.md) §3 — one `t4g.micro` in ap-south-1, one in ap-northeast-1,
the same tool with `--label`, half an hour, `assumed` under $0.02. Until then, no extrapolation.

## 6. The region, and the honest tension in it

**Criterion 5 alone points at Tokyo** — compute in ap-northeast-1 sits in the venue's own
region and the CloudFront edge→origin leg collapses. Criteria 2, 3 and 4 point at Mumbai, and
criterion 5 is last for a reason.

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| 1×, one `m7g.large` + 30 GB + IPv4 | **$48.94** | $80.99 | $83.47 | $65.62 |
| 10×, one `m7g.2xlarge` + 60 GB + IPv4 | **$179.43** | $307.25 | $317.18 | $246.72 |
| Fargate ARM 1×, 6 tasks | **$89.33** | $136.30 | $136.30 | $113.60 |
| Distance to the venue origin | one CDN backbone leg | one leg | **none** | far |
| NSE, when it arrives | in-country | offshore | offshore | offshore |

**Mumbai is the cheapest of the four, 43% under Tokyo at ten times the rate, and the only one
in the same country as the exchange the roadmap adds next.** What Tokyo buys is freshness on a
read-only feed: #57 puts order management and execution out of scope, so latency changes when
a bar is sealed, not whether it is correct. **The day an order path exists, re-open this.**

## What I learned

| Term | What it means here |
|---|---|
| **Launch type / control plane** | Who starts the container and holds *desired* state against *actual*. Compose = us on a box; ECS = AWS's scheduler on our box; Fargate = AWS's scheduler on AWS's box; EKS = Kubernetes on our box. EKS charges $0.10/hour for the control plane; ECS gives one away. |
| **Task definition** | The immutable JSON that says "these containers, these images, this much CPU and memory". A deploy is a new revision, not an `ssh`. |
| **`awsvpc` / ENI** | A network mode giving the task its own virtual network card and private IP. Containers *in one task* share it and see each other on `localhost`; separate tasks do not. |
| **NAT gateway** | A managed box letting a private subnet reach the internet. Charged by the hour **and by the gigabyte through it**. |
| **Burstable (`t4g`)** | A cheap instance with a CPU *baseline* — 20% per vCPU on `t4g.medium`. Above it you spend credits; out of credits you are throttled back to the baseline. |
| **Graviton** | AWS's ARM chips. Same image if built `arm64`; 20–40% cheaper per hour than the Intel equivalent here. |

**The bill is not where you look for it.** The largest single line in the 1× table is not
compute — it is `derived` $124.12/month of NAT gateway data processing on market data we
receive for free. Inbound bytes cost nothing until you put a metered box in front of them.
The platform question turned partly into a subnet question.

**"Same cost" is a real answer, and it settles a criterion.** ECS on the EC2 launch type
charges `measured` $0 — "There is no additional charge for Amazon EC2 compute" — so ECS on
EC2 and EC2 with Compose are the *identical* bill and criterion 3 cannot separate them. The
decision moves up to criterion 2, and that is where Compose loses: a control plane that
restarts a dead container and records that it did is free.

**A CDN in front of the venue changes what "region" means.** Our region does not set our
distance to Delta; both endpoints are CloudFront, so it sets the distance to an *edge*, and
the edge-to-origin leg is CloudFront's path from anywhere. That collapsed most of the region
question — until Delta's own docs named AWS Tokyo and put one latency argument back.

**A number from a tunnelled machine is not a number about the network.** WARP put a Chennai
hop under every figure, so 238.71 ms is true about *this laptop today* and says almost nothing
about an EC2 instance. What survives is the *difference* between two rows measured through the
same tunnel — which is how the cache-hit-versus-miss probe extracts the edge-to-origin leg
while the tunnel cancels on both sides of the subtraction.

**Burstable instances fail quietly, which is the failure mode we refuse everywhere else.**
`t4g.medium` is $26.20/month cheaper than `m7g.large` and has a baseline of 20% per vCPU —
0.4 of a core against a `measured` 0.31 today plus 5.88% for the publisher. That is not
margin, it is a coincidence, and running out of credits raises nothing; it just makes the
feed slower at the moment the market got loud enough to exhaust them.

**Kubernetes charges before anything runs.** $73.00 a month of control plane in every region
we priced, before the first container starts — then a version upgrade at least yearly, because
standard support ends after fourteen months and extended support costs $0.50/hour on top.
