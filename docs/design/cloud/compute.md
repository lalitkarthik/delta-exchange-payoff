# Where the data feed engine runs, and what it costs

The data feed engine document's compute section: the platform, the region, the instance, the
network shape, and the operations they buy. Why each is what it is — the four platforms, the
prices, the venue measurement — is
[../research/0005-compute-and-region.md](../research/0005-compute-and-region.md); the
decision is [../decisions/0005-compute-and-region.md](../decisions/0005-compute-and-region.md);
Redis's own rules are [redis-hosting.md](redis-hosting.md) and the names on the wire
[nomenclature.md](nomenclature.md).

Landed by #68 and #78 as standards, not as code; nothing here is built, and #65 has not yet produced the images.

---

## 1. The platform, in one line

**ECS on EC2: one `c7g.xlarge` instance in a public subnet of `ap-south-1` (Mumbai), one task
definition holding all six containers, `host` network mode.** `derived` **$78.07 a month**,
on-demand, 2026-09-12. The class is [0008](../decisions/0008-topology.md)'s; 0005 chose `m7g.large` ($48.94).

| | `dev` | `prod` |
|---|---|---|
| Runs on | Docker Compose on a laptop | ECS on one EC2 instance |
| Region | — | `ap-south-1` |
| Instance | — | `c7g.xlarge`, Graviton3, 4 vCPU, 8 GiB, 30 GB gp3 root; `feed` reserves 1,024 CPU units |
| Containers | the same six | the same six |
| Networking | the Compose network | `host` mode — every container on the instance's stack |
| Reachable from | the laptop | a tunnel or Tailscale only; **never a public listener** (#57) |
| Stream names | no environment in the name: `md.option_quote:DELTA:BTC` ([0006](../decisions/0006-stream-names-without-environment.md)) | the same names; one Redis each, never shared |

**The images are identical.** One `Dockerfile` per service, built `linux/arm64`, and the same
tag runs in both places. Graviton is not a preference — `m7i.large` is the x86 twin of
`m7g.large` and costs **82% more** in ap-south-1 for the same two vCPUs and 8 GiB.

## 2. The comparison the senior asked for

Four platforms, #57's five criteria in order, at today's `measured` footprint and ten times
it. Every dollar figure is `derived` from `measured` AWS Price List Bulk API unit prices read
2026-09-09, ap-south-1, on-demand, 730 hours a month; the arithmetic is in
[../research/0005b-prices-and-sources.md](../research/0005b-prices-and-sources.md) §3.

| | (1) Invariants | (2) Ops for 2–3 people | (3) $/mo, 1× → 10× | (4) Path to the right half | (5) Latency |
|---|---|---|---|---|---|
| **ECS on EC2** ← chosen | a dead container restarts **and the restart is an alarmable ECS event** | one AMI to patch; restarts and deploys are the platform's | **$48.94 → $179.43** | a new consumer is a container in the task, or its own service | Redis on `127.0.0.1` under `host` mode; venue path unchanged |
| EC2 with Compose | identical, but **nothing records that it restarted** | the same AMI, plus you are the only restarter and every deploy is an `ssh` | **$48.94 → $179.43** — ECS adds $0 | one YAML file on one box | same |
| ECS on Fargate | identical; no host to misconfigure | lightest of the four: no AMI, no agent, no disk | $89.33 → $242.41 | best per-service isolation | `awsvpc` is mandatory: six tasks puts a VPC hop between `feed` and Redis |
| EKS | identical | heaviest: a Kubernetes minor upgrade at least yearly, add-ons in sequence, manifests | $121.94 → $252.43 one node; $170.89 → $431.86 two | the most, and the most unused | same as ECS on EC2 |
| *any of them behind a NAT gateway* | — | — | *+$165.00 → +$1,282.09* | — | — |

**Two rows tie exactly on cost**, because ECS on the EC2 launch type charges $0 — "There is
no additional charge for Amazon EC2 compute". When criterion 3 ties, the decision moves up to
criterion 2, and that is where Compose loses.

## 3. The network shape, and the number behind it

| Rule | Value | Why |
|---|---|---|
| Subnet | **public** | so the instance's own public IPv4 is the egress |
| Public IPv4 | **one, in-use** | `measured` $0.005/hour = `derived` **$3.65/month** |
| NAT gateway | **none** | `derived` **$165.00/month** at 1×, **$1,282.09** at 10× |
| ECS network mode | **`host`** | every container on the instance's stack; `feed` → Redis stays loopback |
| Inbound listeners | **none public** | the dashboard is reached over a tunnel or Tailscale (#57) |

**A NAT gateway would cost more than the compute it fronts.** The feed pulls a `measured`
843.4 KB/s, which is `derived` **2,216.5 GB a month** — 22,164.6 GB at ten times. Inbound to
AWS is free; a NAT gateway meters it at `measured` $0.056/GB in ap-south-1, so the same bytes
become `derived` $124.12 a month of data processing on top of $40.88 of gateway hours. **The
platform question is partly a subnet question, and this is the number that makes it one.**

`awsvpc` network mode is rejected for the same reason: on EC2, task ENIs "aren't given public
IP addresses… tasks must be launched in a private subnet that's configured to use a NAT
gateway". A third path exists and is not built: both Delta endpoints resolve `AAAA`, so an
IPv6-only egress through an egress-only internet gateway is free of both charges.

## 4. Sizing, and what each container reserves

`derived` from `measured` footprint, and stated as reservations rather than measurements —
#65 has not split the monolith, so nothing per-container has been measured yet.

| Container | vCPU | Memory | Basis |
|---|---|---|---|
| `feed` | 0.5 | 1 GB | `measured` 30.89% of a core for the whole monolith, plus the publisher's `measured` 5.88% |
| `store` | 0.5 | 1 GB | `derived` 201.5 MB/day for BTC+ETH ([0007](../research/0007-load-profile.md) D1; 143 was BTC alone), five-minute flush buffer |
| `api` | 1.0 | 2 GB | `measured` 175.227 ms full solve pass; the pre-#44 all-expiry loop was `derived` ~64% of a core |
| `web` | 0.25 | 0.5 GB | `measured` 203.1 MiB private, 0.00% of a core with no viewer |
| proxy | 0.25 | 0.5 GB | `assumed` |
| **`redis`** | 0.5 | **3 GB** | the `maxmemory 2gb` ceiling ([redis-hosting.md](redis-hosting.md)) plus overhead |
| **Total** | **3.0** | **8.0 GB** | → 0005's `m7g.large` (2 vCPU, 8 GiB); the host overcommits vCPU, which is why Fargate — where reservations are the bill — costs more |

**These reservations under-count CPU** (0007): 1× needs 4 vCPU and 10× 16–32, so the instance is
`c7g.xlarge`, and `c7g.4xlarge`–`c7g.8xlarge` at 10×, `derived` $295.72–582.39 (§9).

**Why not `t4g.medium` at $22.74.** Burstable instances have a CPU baseline — 20% per vCPU on
`t4g.medium`, so **0.4 of a core** against a `measured` 0.31 today plus the publisher's 5.88%.
That is a coincidence, not headroom, and out of credits the instance is throttled back to
baseline **with nothing raised**. `t4g.large` at $39.09 has 8 GiB and a 0.6-core baseline and
is the honest cheap option; $9.85 a month removes the credit model entirely.

## 5. The region

**`ap-south-1`, Mumbai.** Cheapest of the four priced, in-country for the NSE adapter #57
names next, and in the same city as the CloudFront edge that serves us.

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| 1×, `m7g.large` + 30 GB + IPv4 | **$48.94** | $80.99 | $83.47 | $65.62 |
| 10×, `m7g.2xlarge` + 60 GB + IPv4 | **$179.43** | $307.25 | $317.18 | $246.72 |

**What is between us and Delta, `measured` 2026-09-09** (`tools/measure_venue_latency.py`,
full run in [../research/0005a-venue-latency-run.md](../research/0005a-venue-latency-run.md)):

- **Both endpoints are Amazon CloudFront.** `public-socket.india.delta.exchange` →
  `d2gb279nx3imhv.cloudfront.net`, `api.india.delta.exchange` → `d15zy4kc8a63om.cloudfront.net`;
  every address inside `13.224.0.0/14`, which AWS's `ip-ranges.json` labels
  `CLOUDFRONT`/`GLOBAL`. Our POP is **`BOM78-P11`**, Mumbai.
- **Delta's own documentation says the origin is "AWS Tokyo"**, and a cache-busted REST call
  costs `measured` 191.18 ms more than a cacheable one — `derived` ~167 ms of edge-to-origin
  round trip once the origin's own 23.9 ms is removed.
- **So a region buys the client-to-edge leg only**, except in ap-northeast-1, which would sit
  in the venue's own region. That is criterion 5, it is last, and #57 puts execution out of
  scope; the decision record says what would reverse it.

**Every latency figure was taken through an active Cloudflare WARP tunnel** and is an upper
bound on this laptop, not a statement about an EC2 instance. **The from-AWS measurement is
not taken**; the exact commands are in
[../research/0005a-venue-latency-run.md](../research/0005a-venue-latency-run.md) §3.

## 6. Operations: what a month actually contains

The criterion this decision turned on, as a list rather than an adjective. **ECS on EC2, per
month:**

1. **Patch the AMI.** Roll the instance onto the current ECS-optimized AMI — replace, do not
   patch in place. ~30 minutes, once a month.
2. **Read the ECS event stream** for task stops nobody noticed, and check the stopped-reason
   strings. ~10 minutes.
3. **Check disk on the root volume** — images, logs and anything the store leaves locally
   before R3's answer lands. ~5 minutes.
4. **Check the bill** against the `derived` $78.07, because an unexpected line is usually a
   NAT gateway or a forgotten public IP. ~5 minutes.
5. **Confirm the private path still works** — the tunnel or Tailscale that fronts `web`.
6. **Deploy, when there is one**: build, push to ECR, register a task definition revision,
   `aws ecs update-service`. Not monthly; per change.

**What the platform does instead of us**: restarts a container that exited, reports that it
did, replaces a task that fails its health check, and holds the desired state so a deploy is
a revision rather than an `ssh`.

**What Compose would add back**: item 6 becomes `ssh` and `docker compose up -d`, and a
container that dies at 02:00 restarts with no record anywhere that it happened.

**What EKS would add**: a Kubernetes minor version upgrade at least once a year — standard
support ends fourteen months after a version's release and extended support costs $0.50/hour
on top of the $0.10 — plus CNI, CoreDNS and `kube-proxy` upgrades to sequence, and manifests
to keep current. Half a day each, for a team that does not already run Kubernetes.

**What Fargate would remove**: items 1 and 3 entirely. It is the fallback if host work, not
cost, becomes the constraint.

## 7. The numbers behind this section

| Number | Tag | Source |
|---|---|---|
| Engine 30.89% of one core; 117.1 MiB resident, 178.6 MiB private | `measured` | 60.69 s `Get-Process` sample, 2026-09-09 13:18 UTC, **no viewer attached** |
| `web` 0.00% of a core, 203.1 MiB private | `measured` | same sample |
| 1,693.6 msg/s, 843.4 KB/s, BTC+ETH | `measured` | `tools/measure_feed.py`, 2026-09-08 (`../hld.md` §5) |
| Full solve pass 175.227 ms; pre-#44 loop ~64% of a core | `measured` / `derived` | `../lld/chain-cache.md` §9 |
| Redis publisher 5.88% of a core at 100 ms batches; 1,051.5 MiB at thirty minutes | `measured` / `derived` | #58, #69 |
| Inbound 2,216.5 GB/month; NAT gateway $165.00/month | `derived` | 843.4 KB/s × 730 h × $0.056/GB |
| `m7g.large` $48.94/mo; `m7g.2xlarge` $179.43/mo, ap-south-1 (0005's classes; 0008's are in §9) | `derived` | AWS Price List Bulk API, read 2026-09-09 |
| EKS control plane $73.00/month; ECS on EC2 $0 | `measured` | AWS Price List Bulk API; [ECS pricing](https://aws.amazon.com/ecs/pricing/) |
| Delta endpoints are CloudFront; POP `BOM78-P11`; edge↔origin `derived` ~167 ms | `measured` / `derived` | `tools/measure_venue_latency.py`, 2026-09-09 (**through a Cloudflare WARP tunnel**) |
| Latency from ap-south-1 and ap-northeast-1 | **unmeasured** | no AWS access; commands in `../research/0005a-venue-latency-run.md` §3 |
| Image sizes and per-container footprint | **unmeasured** | #65 has not landed; **re-cost this section when it does** |

**Nothing here is fixed against #65.** When it produces the images, re-run §2, §4 and §9 against the
`measured` image sizes and per-container CPU and memory, and note any change in the decision records.

## 8. The load profile, per service

`derived` by R6 (#75): arithmetic in [../research/0007-load-profile.md](../research/0007-load-profile.md),
belief in [../decisions/0007-load-profile.md](../decisions/0007-load-profile.md). I13 (#79) measures it; its day-long run has not happened yet, and [../research/0007a-container-measurement.md](../research/0007a-container-measurement.md) holds the pending table meanwhile.

| Service | Bound by | 1× | 10× |
|---|---|---|---|
| `feed` | CPU, one core | 0.52–0.71 core | 5.2–7.1 cores: 8–11 processes, one Python process is one core |
| `store` | CPU (decode), not disk | 0.28–0.51 core; 2.33 KB/s to disk | 2.8–5.1 cores |
| `api` | CPU, set by viewers | 0.25–0.49, + 0.05–0.14 a watched expiry | 2.5–4.9, + viewers |
| `web` / Redis | memory | 240.8 MiB / 1,056.4 MiB `measured` | unchanged / 10.3 GiB |

**The split costs 1.10–1.75 cores against the monolith's 0.31**: §4 under-counts CPU and over-counts memory.

## 9. One instance or several

`derived` by R7 (#78): the table is [compute-topology.md](compute-topology.md), the decision [0008](../decisions/0008-topology.md). It answers 0005's re-cost trigger.
- **One `c7g.xlarge`, $78.07 at 1×**: on 4 vCPU three one-core Python services cannot starve `feed`, which reserves 1,024 CPU units.
- Two boxes cost $73.22–84.46 and one per service $128.07 at the 1× upper bound. One box stays cheapest until `strategy` arrives.
- **Split into `feed`+`store`+Redis ∣ `api`+`web`+proxy** when the box passes 2.8 cores or `oms` places its first order.
- The same-AZ hop is `derived` ≤ 1 ms, invisible to every real consumer. Same-AZ transfer is free on private addresses only.
