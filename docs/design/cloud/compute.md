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
definition holding all seven containers, `host` network mode.** `derived` **$78.07 a month**,
on-demand, 2026-09-12. The class is [0008](../decisions/0008-topology.md)'s; 0005 chose `m7g.large` ($48.94).

| | `dev` | `prod` |
|---|---|---|
| Runs on | Docker Compose on a laptop | ECS on one EC2 instance |
| Region | — | `ap-south-1` |
| Instance | — | `c7g.xlarge`, Graviton3, 4 vCPU, 8 GiB, 30 GB gp3 root; `feed` reserves 1,024 CPU units |
| Containers | the same seven | the same seven |
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
| ECS on Fargate | identical; no host to misconfigure | lightest of the four: no AMI, no agent, no disk | $89.33 → $242.41 | best per-service isolation | `awsvpc` is mandatory: seven tasks puts a VPC hop between `feed` and Redis |
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
#65 has not split the monolith, so nothing per-container has been measured yet. **Sized at the
50 ms batch interval I2 (#61) chose**, which is the only interval this system is configured to
run at; the 100 ms figures belong to a run, not to a deployment.

| Container | vCPU | Memory | Basis |
|---|---|---|---|
| `feed` | **1.0** | 1 GB | `derived` 0.52–0.71 of a core **at 50 ms**: 22.1 points of the monolith's `measured` 30.89% ([0007](../research/0007-load-profile.md) §2) plus `derived` **29.96 points** to encode and publish; upper bound `measured` 70.73%, I2's feed-shaped process. **1,024 CPU units**, as §1 and [0008](../decisions/0008-topology.md) already say |
| `store` | 0.5 | 1 GB | `derived` 201.5 MB/day **written** for BTC+ETH ([0007](../research/0007-load-profile.md) D1; the `measured` 143 MB/day is **retained**, and BTC alone), five-minute flush buffer |
| `api` | 1.0 | 2 GB | `measured` 175.227 ms full solve pass; the pre-#44 all-expiry loop was `derived` ~64% of a core |
| `web` | 0.25 | 0.5 GB | `measured` 203.1 MiB private, 0.00% of a core with no viewer |
| `discord-alerts` | 0.05 | 0.25 GB | `assumed` (#66). It subscribes `event_types=("alert",)` and nothing else ([../lld/discord-alerts.md](../lld/discord-alerts.md) §1), so it pays **no market-data decode** — the 0.22–0.45 core every raw-stream consumer costs (0007 §6) does not apply. Memory is `measured` m5's ~50 MiB import base |
| proxy | 0.25 | 0.5 GB | `assumed` |
| **`redis`** | 0.5 | **3 GB** | the `maxmemory 2gb` ceiling ([redis-hosting.md](redis-hosting.md)) plus overhead |
| **Total, seven containers** | **3.55** | **8.25 GB** | → [0008](../decisions/0008-topology.md)'s `c7g.xlarge` (4 vCPU, 8 GiB); the host overcommits vCPU, which is why Fargate — where reservations are the bill — costs more |

**`feed` is sized on 29.96 points and not on 5.88%, and the difference is five-fold.** 5.88% is
#69's publisher measured **alone, on loopback, at a 100 ms batch** — Redis's own write cost. At
the chosen 50 ms the same publisher inside the feed costs `derived` 29.96 points (`measured`
70.73% against the bus-off control's 40.77%, I2/#61). Sizing from the smaller figure is the route
[0007](../decisions/0007-load-profile.md) rejects as option B: it "under-counts by 3.6–5.7×. It
is how 0005 reached 6 vCPU at 10×." **Both figures are real**;
[compute-numbers.md](compute-numbers.md) §2 puts them side by side with the conditions that
separate them. **These reservations still under-count CPU and over-count memory** (0007: the
`derived` need at 1× is 1.21–1.95 cores and 5.05 GiB): 1× needs 4 vCPU and 10× 16–32, so the
instance is `c7g.xlarge`, and `c7g.4xlarge`–`c7g.8xlarge` at 10×, `derived` $295.72–582.39 (§9).

**Why not `t4g.medium` at $22.74.** Burstable instances have a CPU baseline — 20% per vCPU on
`t4g.medium`, so **0.4 of a core**. 0005 set that against the monolith's `measured` 0.31 plus the
isolated publisher's 5.88% at a 100 ms batch and called it a coincidence, not headroom; at the
chosen 50 ms the split's `derived` 1.21–1.95 cores is three to five times the baseline, so the
rejection is not close. Out of credits the instance is throttled to baseline **with nothing
raised**. `t4g.large` at $39.09 has 8 GiB and a 0.6-core baseline and is the honest cheap
option; $9.85 a month removes the credit model entirely.

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

**Moved to [compute-numbers.md](compute-numbers.md)** by #95, this file being at the 200-line
bound: every figure with its tag and its run, the two publisher measurements side by side, and
the unit behind the Redis working set. **Quote it from there, never from a sentence here.**

## 8. The load profile, per service

`derived` by R6 (#75): arithmetic in [../research/0007-load-profile.md](../research/0007-load-profile.md),
belief in [../decisions/0007-load-profile.md](../decisions/0007-load-profile.md). I13 (#79) measures it; its day-long run has not happened yet, and [../research/0007a-container-measurement.md](../research/0007a-container-measurement.md) holds the pending table meanwhile.

| Service | Bound by | 1× | 10× |
|---|---|---|---|
| `feed` | CPU, one core | 0.52–0.71 core | 5.2–7.1 cores: 8–11 processes, one Python process is one core |
| `store` | CPU (decode), not disk | 0.28–0.51 core; 2.33 KB/s to disk | 2.8–5.1 cores |
| `api` | CPU, set by viewers | 0.25–0.49, + 0.05–0.14 a watched expiry | 2.5–4.9, + viewers |
| `web` / Redis | memory | 240.8 MiB / 1,056.4 MiB `measured` | unchanged / 10.269 GiB |

**The split costs 1.10–1.75 cores against the monolith's 0.31**: §4 under-counts CPU and over-counts memory.

## 9. One instance or several

`derived` by R7 (#78): the table is [compute-topology.md](compute-topology.md), the decision [0008](../decisions/0008-topology.md). It answers 0005's re-cost trigger.
- **One `c7g.xlarge`, $78.07 at 1×**: on 4 vCPU three one-core Python services cannot starve `feed`, which reserves 1,024 CPU units.
- Two boxes cost $73.22–84.46 and one per service $128.07 at the 1× upper bound. One box stays cheapest until `strategy` arrives.
- **Split into `feed`+`store`+Redis ∣ `api`+`web`+proxy** when the box passes 2.8 cores or `oms` places its first order.
- The same-AZ hop is `derived` ≤ 1 ms, invisible to every real consumer. Same-AZ transfer is free on private addresses only.
