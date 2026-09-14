# Deployment

**Nothing here is built.** There is no AWS account on this machine, and no figure on this page was
taken against one: every dollar is `derived` from `measured` AWS Price List Bulk API unit prices
read 2026-09-09 in `ap-south-1`, on-demand, 730 hours a month. Treat every latency figure as a
floor, not a reading.

The shape that *is* built is the local Compose stack -- see [Getting started](getting-started.md).

## The platform, in one line

**ECS on EC2: one `c7g.xlarge` in a public subnet of `ap-south-1` (Mumbai), one task definition
holding all seven containers, `host` network mode.** `derived` **$78.07 a month** of compute, and
**$79.99 all-in** for BTC and ETH.

| | `dev` | `prod` |
|---|---|---|
| Runs on | Docker Compose on a laptop | ECS on one EC2 instance |
| Instance | -- | `c7g.xlarge`, Graviton3, 4 vCPU, 8 GiB, 30 GB gp3 root |
| Containers | the same seven | the same seven |
| Networking | the Compose network | `host` mode -- every container on the instance's stack |
| Store root | `data/` (or `./.stack-data/`) | `s3://<env>-deltapayoff-bars/` |
| Reachable from | the laptop | a tunnel or Tailscale only; **never a public listener** |
| Stream names | no environment in the name | **the same names**; one Redis each, never shared |

**The images are identical.** One `Dockerfile` per service, built `linux/arm64`, and the same tag
runs in both places. Graviton is not a preference: the x86 twin of the same shape costs **82% more**
in ap-south-1.

## Why this platform

Four platforms against five criteria, at today's `measured` footprint and ten times it.

| | Invariants | Ops for 2-3 people | $/mo, 1x to 10x | Path to more services | Latency |
|---|---|---|---|---|---|
| **ECS on EC2** (chosen) | a dead container restarts **and the restart is an alarmable event** | one AMI to patch; restarts and deploys are the platform's | **$48.94 to $179.43** | a new consumer is a container in the task | Redis on `127.0.0.1` under `host` mode |
| EC2 with Compose | identical, but **nothing records that it restarted** | you are the only restarter; every deploy is an `ssh` | $48.94 to $179.43 -- ECS adds $0 | one YAML file on one box | same |
| ECS on Fargate | identical; no host to misconfigure | lightest: no AMI, no agent, no disk | $89.33 to $242.41 | best per-service isolation | `awsvpc` is mandatory, putting a VPC hop between `feed` and Redis |
| EKS | identical | heaviest: a Kubernetes minor upgrade at least yearly | $121.94 to $252.43 one node | the most, and the most unused | same as ECS on EC2 |
| *behind a NAT gateway* | -- | -- | *+$165.00 to +$1,282.09* | -- | -- |

**Two rows tie exactly on cost**, because ECS on the EC2 launch type charges $0 for compute. When
the cost criterion ties, the decision moves up to operations, and that is where plain Compose loses:
a container that dies at 02:00 restarts with **no record anywhere that it happened**.

## The network shape, and the number behind it

| Rule | Value | Why |
|---|---|---|
| Subnet | **public** | so the instance's own public IPv4 is the egress |
| Public IPv4 | one, in-use | `measured` $0.005/hour = `derived` **$3.65/month** |
| NAT gateway | **none** | `derived` **$165.00/month** at 1x, **$1,282.09** at 10x |
| ECS network mode | **`host`** | `feed` to Redis stays loopback |
| Inbound listeners | **none public** | the dashboard is reached over a tunnel or Tailscale |

**A NAT gateway would cost more than the compute it fronts.** The feed pulls a `measured` 843.4 KB/s
-- `derived` **2,216.5 GB a month**. Inbound to AWS is free; a NAT gateway meters it at `measured`
$0.056/GB, so the same bytes become `derived` $124.12 a month of data processing on top of $40.88 of
gateway hours. **The platform question is partly a subnet question**, and that is the number that
makes it one.

`awsvpc` mode is rejected for the same reason: on EC2, task ENIs get no public IP, so tasks must run
in a private subnet with a NAT gateway. A third path exists and is not built -- both venue endpoints
resolve `AAAA`, so IPv6-only egress through an egress-only internet gateway is free of both charges.

## Sizing

Reservations, not measurements of split containers. **Sized at the 50 ms batch interval**, which is
the only interval this system is configured to run at.

| Container | vCPU | Memory | Basis |
|---|---|---|---|
| `feed` | **1.0** | 1 GB | `measured` 0.3592 core at 1x; `derived` 0.52-0.71 with encode and publish at 50 ms. **1,024 CPU units** |
| `store` | 0.5 | 1 GB | `measured` 0.3048 core, 1.49 KB/s to disk; a five-minute flush buffer |
| `api` | 1.0 | 2 GB | `measured` 0.3045 core, +0.0466 for the first viewer; a 175.227 ms full solve pass |
| `web` | 0.25 | 0.5 GB | `measured` 110.6 MiB idle, no CPU with no viewer |
| `discord-alerts` | 0.05 | 0.25 GB | Subscribes to `alert` only, so it pays no market-data decode |
| `proxy` | 0.25 | 0.5 GB | `assumed` |
| `redis` | 0.5 | **3 GB** | the `maxmemory 2gb` ceiling plus overhead |
| **Total** | **3.55** | **8.25 GB** | -> `c7g.xlarge`; the host overcommits vCPU |

**`feed` is sized on 29.96 points of CPU and not on 5.88%, and the difference is five-fold.** 5.88%
is the publisher measured **alone, on loopback, at a 100 ms batch** -- Redis's own write cost. At the
chosen 50 ms the same publisher inside the feed costs `derived` 29.96 points (`measured` 70.73%
against a bus-off control's 40.77%). Sizing from the smaller figure under-counts by 3.6-5.7x, and is
how an earlier pass reached 6 vCPU at 10x.

**These reservations under-count CPU and over-count memory**: the `derived` need at 1x is 1.21-1.95
cores and 5.05 GiB. **The split costs `derived` 1.0888 cores against the monolith's 0.31** -- 3.52x.
At ten times the rate it is `c7g.4xlarge` to `c7g.8xlarge`, `derived` $295.72-582.39.

**Why not `t4g.medium` at $22.74.** Burstable instances have a CPU baseline -- 20% per vCPU, so 0.4
of a core. The split's `derived` 1.21-1.95 cores is three to five times that, and out of credits the
instance is throttled to baseline **with nothing raised**. `t4g.large` at $39.09 is the honest cheap
option; $9.85 a month removes the credit model entirely.

## One instance or several

- **One `c7g.xlarge`, $78.07 at 1x.** On 4 vCPU three one-core Python services cannot starve `feed`,
  which reserves 1,024 CPU units.
- Two boxes cost $73.22-84.46; one per service is $128.07 at the 1x upper bound.
- **Split into `feed`+`store`+Redis and `api`+`web`+proxy** when the box passes 2.8 cores, or when an
  order path places its first order.
- The same-AZ hop is `derived` at most 1 ms, invisible to every real consumer. Same-AZ transfer is
  free on private addresses only.

## The region

**`ap-south-1`, Mumbai.** Cheapest of the four priced, in-country for the NSE adapter named next, and
in the same city as the CloudFront edge that serves us.

| | ap-south-1 | ap-southeast-1 | ap-northeast-1 | us-east-1 |
|---|---|---|---|---|
| 1x | **$48.94** | $80.99 | $83.47 | $65.62 |
| 10x | **$179.43** | $307.25 | $317.18 | $246.72 |

**What is between us and the venue, `measured` 2026-09-09.** Both Delta endpoints are Amazon
CloudFront, and our POP is `BOM78-P11`, Mumbai. Delta's own documentation says the origin is "AWS
Tokyo", and a cache-busted REST call costs `measured` 191.18 ms more than a cacheable one --
`derived` ~167 ms of edge-to-origin round trip. **So a region buys the client-to-edge leg only**,
except in ap-northeast-1, which would sit in the venue's own region. That is the last criterion, and
execution is out of scope.

Every latency figure was taken through an active tunnel and is an upper bound on one laptop, not a
statement about an EC2 instance. **The from-AWS measurement has not been taken.**

## What it costs, all in

| Line | Underlying set | 1x | 10x |
|---|---|---|---|
| ECS on EC2, all-in | BTC+ETH | $78.07 | $295.72-582.39 |
| S3 Standard, compacted | BTC+ETH | $1.92 | $19.19 |
| Redis container | BTC+ETH | $0 -- its memory is bought inside the compute | $0 |
| **Total** | BTC+ETH | **$79.99** | **$314.91-601.58** |

**Quote the BTC+ETH row.** An earlier total mixed two underlying sets -- BTC+ETH compute against a
BTC-only S3 bill. The difference is $0.40 a month, which is why it went unnoticed for as long as it
did: the error is the mixing, not the amount.

## The services bought, and the ones not

| Bought | For |
|---|---|
| Amazon ECS, EC2 launch type | one task definition holding every container |
| Amazon EC2 | one `c7g.xlarge`, `host` network mode |
| Amazon VPC | one public subnet, one in-use public IPv4, no NAT gateway |
| Amazon EBS gp3 | the instance's 30 GB root volume, and nothing else |
| Amazon S3 Standard | the four bar tables, one bucket per environment |
| Amazon ECR | the images a deploy registers |
| Amazon CloudWatch | alarms on a stopped task and on the box |

| Not bought | Why, in one line |
|---|---|
| ElastiCache for Valkey | `derived` $47.30/month against a container's $0. **The named fallback for Redis** |
| ElastiCache Serverless | no `maxmemory`, so a trim that stops working is a bill and not an error |
| MemoryDB | durability is the product and cannot be turned off, for frames the venue can re-tell |
| RDS for PostgreSQL | $61.32/month to be awake. **The named fallback for OMS state**, beside the bars, never holding them |
| Timestream | one flavour is closed to new customers; the other is a second query language |
| Athena | it reads the same files, and one catalogue adds it later with no file changed |
| Amazon EKS | `measured` $73.00/month of control plane before a container starts |
| ECS on Fargate | `awsvpc` is mandatory. **The named fallback for the platform**, if host work becomes the constraint |
| NAT gateway | more than the compute it fronts |

## A month of operations

1. **Patch the AMI** -- replace the instance, do not patch in place. ~30 minutes, monthly.
2. **Read the ECS event stream** for task stops nobody noticed, and the stopped-reason strings. ~10
   minutes.
3. **Check disk on the root volume** -- images, logs, anything left locally. ~5 minutes.
4. **Check the bill** against the `derived` $78.07. An unexpected line is usually a NAT gateway or a
   forgotten public IP. ~5 minutes.
5. **Confirm the private path still works** -- the tunnel that fronts the dashboard.
6. **Deploy, when there is one**: build, push to ECR, register a task definition revision,
   `aws ecs update-service`. Per change, not monthly.

**What the platform does instead of us**: restarts a container that exited, reports that it did,
replaces a task that fails its health check, and holds the desired state so a deploy is a revision
rather than an `ssh`.

## What nobody has measured

1. **Venue latency from ap-south-1 and ap-northeast-1.** The commands are written out in the design
   record.
2. **The network hop to a managed Redis endpoint.**
3. **S3 read latency, and the request count per Parquet read.** The `assumed` 2 requests per object
   is a floor and is labelled as one.
4. **The same-AZ hop**, if the topology ever splits.

## Related guides

[Architecture](architecture.md) | [Data store](data-store.md) | [Message bus](message-bus.md)
