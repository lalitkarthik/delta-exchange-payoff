# 0005 — The compute platform, and the region

**Status** decided, #68, 2026-09-09. **Supersedes** nothing. **Fills**
[../cloud/compute.md](../cloud/compute.md). **Evidence**
[../research/0005-compute-and-region.md](../research/0005-compute-and-region.md), the venue
latency run [../research/0005a-venue-latency-run.md](../research/0005a-venue-latency-run.md),
prices and sources
[../research/0005b-prices-and-sources.md](../research/0005b-prices-and-sources.md). **Depends
on** [0002-redis-hosting.md](0002-redis-hosting.md), whose Redis container this compute
carries.

## The question

Where do the containers run — one EC2 host under Compose, ECS on Fargate, ECS on EC2, or
EKS — and in which region, given the footprint and #57's five criteria?

## The options

| | Considered |
|---|---|
| Platform | EC2 with Compose; **ECS on EC2**; ECS on Fargate; EKS |
| Network mode on EC2 | **`host`**; `bridge`; `awsvpc` |
| Egress | **public subnet + one in-use public IPv4**; private subnet + NAT gateway; IPv6-only + egress-only gateway |
| Instance at 1× | `t4g.medium`; `t4g.large`; `c7g.large`; **`m7g.large`**; `m7i.large` (x86) |
| Instance at 10× | `m7g.xlarge`; **`m7g.2xlarge`**; `r7g.xlarge` |
| Region | **ap-south-1** (Mumbai); ap-southeast-1 (Singapore); ap-northeast-1 (Tokyo); us-east-1 |

## The decision

**ECS on EC2 — one `m7g.large` (Graviton3, 2 vCPU, 8 GiB) in a public subnet of
`ap-south-1`, `host` network mode, all six containers in one task definition, `derived`
$48.94 a month.** At ten times the rate the same shape on an `m7g.2xlarge`, `derived`
$179.43.

- **`host` network mode, not `awsvpc`.** Every container shares the instance's network stack,
  so `feed` reaches Redis on `127.0.0.1` exactly as it does under Compose, and Redis binds to
  loopback where nothing outside the box can reach it. `awsvpc` on EC2 would force a private
  subnet and a NAT gateway.
- **A public subnet and one in-use public IPv4, no NAT gateway.** `derived` $3.65/month
  against `derived` $165.00.
- **`dev` stays Compose on a laptop.** The same images, the same six containers, one
  `docker compose up`. Only `prod` is ECS.
- **The named fallback is EC2 with Compose on the same instance** — identical bill, one
  `ssh`, no control plane. It is what we run if ECS turns out to be more ceremony than the
  restarts it does for us.

Everything this implies about sizing, the task definition and the monthly operations list is
[../cloud/compute.md](../cloud/compute.md).

## Why, in the criteria's order

**1. Invariants.** All four platforms run the same images and none of them can forward-fill
or turn a `null` into a `0`. The invariant that bites is #57's "a `feed` that starts with no
Redis fails loudly". Under Compose that loud exit becomes a restart loop that **nothing
records** — the box is the only witness. ECS turns the same exit into a task state change and
a stopped-reason string, which is a thing an alarm can be hung on, and #57 already wants an
alarm on the feed being `stopped`. R5's `maxmemory 2gb` and `noeviction` need the Redis
container to sit where the publisher can reach it without a hop, which `host` mode gives and
`awsvpc` does not.

**2. Operations burden.** Fargate is the lightest of the four and EKS the heaviest by a wide
margin — a Kubernetes minor upgrade at least yearly, sequenced add-ons, and manifests, for
six containers on one host. Between Compose and ECS on EC2 the host work is identical (the
same AMI to patch, the same disk to watch), and ECS removes two jobs: **restarting a dead
container** and **deploying by `ssh`**. Both leave the human list for $0. That is the
criterion this decision turns on.

**3. Cost.** `derived`, ap-south-1, 2026-09-09, at today's `measured` footprint and ten times
the rate:

| | now | ten times |
|---|---|---|
| **EC2 + Compose** and **ECS on EC2** | **$48.94** | **$179.43** |
| ECS on Fargate, ARM, 6 tasks, public subnet | $89.33 | $242.41 |
| EKS, one node | $121.94 | $252.43 |
| EKS, two nodes | $170.89 | $431.86 |
| *Any of them behind a NAT gateway, add* | *+$165.00* | *+$1,282.09* |

**ECS on the EC2 launch type charges $0** — "There is no additional charge for Amazon EC2
compute" — so rows one and two are the same bill and criterion 3 cannot separate them, which
is why the decision was made on criterion 2. Fargate costs 82% more at 1× because it prices
reserved vCPU and GB rather than a host we can overcommit; EKS adds $73.00 of control plane
before a container starts.

**4. Path to the right half.** A new consumer — the Discord alert consumer (#66), an NSE
feed, later an OMS — is a container in the task definition, or its own service on the same
cluster, and neither requires touching the host. That is ECS's whole advantage over Compose
here, and it is the same advantage EKS sells at four times the operations. When one host
stops being enough, the cluster grows a second instance and the services that must scale move
to `awsvpc` **with the NAT question answered first**.

**5. Latency to the venue.** This is the criterion that argues against the chosen region, and
it is last. `measured`: **both Delta endpoints are Amazon CloudFront** — CNAMEs to
`d2gb279nx3imhv.cloudfront.net` and `d15zy4kc8a63om.cloudfront.net`, every address inside
`13.224.0.0/14`, which AWS's own `ip-ranges.json` labels `CLOUDFRONT`/`GLOBAL` — and our
requests are served by POP `BOM78-P11`, Mumbai. **Delta's documentation says the origin is
"AWS Tokyo"**, and a cache-busted REST call costs `measured` 191.18 ms more than a cacheable
one, leaving `derived` ~167 ms of edge-to-origin round trip after the origin's own 23.9 ms.

So ap-northeast-1 would sit in the venue's own region. It also costs `derived` $137.75/month
more at ten times the rate, is offshore for the NSE adapter #57 names next, and buys
freshness rather than correctness on a feed with no order path. **Criterion 5 loses to 3 and
4 here, deliberately, and the entry above says what would reverse that.**

**The from-AWS measurement has not been taken.** There is no AWS account on this machine, and
every figure in the run was taken through an active Cloudflare WARP tunnel. The commands for
the real measurement are written out, ready to run, in
[../research/0005a-venue-latency-run.md](../research/0005a-venue-latency-run.md) §3.

## Rejected, and why

| Rejected | Why |
|---|---|
| **EKS** | Criterion 2, then 3. $73.00/month of control plane before anything runs, a Kubernetes minor upgrade at least yearly (standard support ends at fourteen months; extended is +$0.50/hour), add-ons to sequence, and manifests to keep current — for six containers a two-person team can name individually. A single-node cluster also makes the control plane pointless: there is nowhere to reschedule to |
| **ECS on Fargate** | Criterion 3, then 1. 82% dearer at 1× and 35% at 10×, and `awsvpc` is not optional there: six tasks puts a VPC hop between `feed` and Redis, breaking the loopback assumption I2's batch interval was chosen on; one task keeps loopback and throws away the independent deployability that is the point of #57. **Kept as the fallback if host operations become the constraint** — it is the only option with no host at all |
| **EC2 with Compose** | Criterion 2 only, and by a small margin. Identical bill, identical host work, but nothing records a container restart and a deploy is an `ssh`. **Kept as the named fallback**, because it is the same instance and the same images |
| **`awsvpc` network mode on EC2** | Criteria 1 and 3. Task ENIs "aren't given public IP addresses… tasks must be launched in a private subnet that's configured to use a NAT gateway", which costs `derived` $165.00/month at our inbound volume and $1,282.09 at ten times — more than the compute — and puts a hop between `feed` and Redis for no gain |
| **`t4g.medium`** | Criterion 1. $26.20/month cheaper and a baseline of 20% per vCPU — 0.4 of a core against a `measured` 0.31 plus the publisher's 5.88%. Running out of credits raises nothing; it makes the feed slower precisely when the market got loud enough to exhaust them, which is the silent failure the first criterion exists to refuse. `t4g.large` at $39.09 has the memory and a 0.6-core baseline and is the honest cheap option; $9.85 buys the credit model away |
| **x86 (`m7i.large`)** | Criterion 3 only. 82% more than `m7g.large` in ap-south-1 for the same 2 vCPU and 8 GiB. The images are ours to build `arm64` |
| **ap-northeast-1 (Tokyo)** | Criteria 3 and 4 over 5. The venue's own region, and `derived` $137.75/month more at ten times the rate, offshore for NSE, buying freshness on a feed with no order path |
| **ap-southeast-1 (Singapore)** | Every criterion. 65% dearer than Mumbai at 1×, 71% at 10×, no closer to the origin than Mumbai, and offshore for NSE. It is on the list because #68 named it; nothing recommends it |
| **Reserved Instances and Savings Plans** | A one- or three-year commitment to a size derived from a monolith #65 has not split, which this ticket's own criteria require re-costing afterwards. The right lever, the wrong month |

## What would change this decision

- **#65 landing, which is already an acceptance criterion.** Re-cost against the `measured`
  image sizes and per-container CPU and memory once the five services exist. If the split
  costs materially more than the monolith's `measured` 30.89% of a core, `m7g.large` is the
  first thing that stops fitting and the 1× row moves to `m7g.xlarge` at $94.24.
- **The venue latency measurement, from ap-south-1 and ap-northeast-1.** If Tokyo turns out
  materially faster on the websocket ping — that is, if CloudFront passes the connection
  through to the origin rather than terminating it usefully at the edge — criterion 5 gets a
  number instead of an argument, and the region is re-argued against it.
- **An order path on the bus.** The moment execution is in scope, latency stops buying
  freshness and starts buying fills. Re-open the region first, then the platform.
- **A second host.** One instance is one failure. The day the answer to "who restarts it" has
  to be "another instance", ECS is already the right shape and the cost table's two-node rows
  apply — but so does the `awsvpc`-and-NAT question, which must be answered before, not after.
- **The NAT gateway becoming unavoidable.** If a private subnet is forced on us — a
  compliance requirement, an interface endpoint we cannot avoid — the `derived` $124.12/month
  of data processing at 1× makes an IPv6-only egress path worth building rather than worth a
  spike.
- **NSE.** An Indian equities adapter makes ap-south-1 an in-country requirement rather than
  a preference, and closes the region question rather than re-opening it.
