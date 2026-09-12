# The AWS services this system uses

**Shared section. [message-bus.md](message-bus.md) and [data-feed-engine.md](data-feed-engine.md)
both link here, and neither repeats it.** Every term below resolves to
[../../../CONTEXT.md](../../../CONTEXT.md). Nothing here is built: there is no AWS account on this
machine, and no figure in this file was taken against one.

---

## 1. The services we buy

**Check this table before you name a service in any design note.** A service that is not in it is
not bought.

| Service | What it does here | Decided by |
|---|---|---|
| Amazon ECS, EC2 launch type | runs one task definition that holds every container | [0005](../decisions/0005-compute-and-region.md), [0008](../decisions/0008-topology.md) |
| Amazon EC2 | one `c7g.xlarge`, Graviton3, 4 vCPU, 8 GiB, `host` network mode | [0008](../decisions/0008-topology.md), which supersedes 0005's class |
| Amazon VPC | one public subnet, one in-use public IPv4, no NAT gateway | [0005](../decisions/0005-compute-and-region.md) |
| Amazon EBS gp3 | the instance's 30 GB root volume, and nothing else | [0005](../decisions/0005-compute-and-region.md) |
| Amazon S3 Standard | the four bar tables, one bucket per environment | [0004](../decisions/0004-durable-store.md) |
| Amazon ECR | holds the images a deploy registers | [compute.md](compute.md) §6 |
| Amazon CloudWatch | alarms on a stopped task and on the box | [compute.md](compute.md) §1 |

**The region is `ap-south-1`, Mumbai.** It is the cheapest of the four priced, it is in-country for
the NSE adapter #57 names next, and it holds the CloudFront edge that serves us. The venue's own
origin is in ap-northeast-1, and [0005](../decisions/0005-compute-and-region.md) says plainly that
criterion 5 loses to criteria 3 and 4 here.

## 2. The services we do not buy

**Read the fallback column before you propose one of these.** Four of them are already chosen as
the answer to a named change.

| Not bought | Why, in one line | Fallback for | Decided by |
|---|---|---|---|
| ElastiCache for Valkey | one `cache.t4g.medium` costs `derived` $47.30 a month against a container's `derived` $0–16.35 | **Redis**, if any of 0002's five criteria moves | [0002](../decisions/0002-redis-hosting.md) |
| ElastiCache Serverless | it has no `maxmemory`, so a trim that stops working is a bill and not an error | — | [0002](../decisions/0002-redis-hosting.md) |
| MemoryDB | durability is the product and cannot be turned off, for frames the venue can re-tell | — | [0002](../decisions/0002-redis-hosting.md) |
| RDS for PostgreSQL | `db.t4g.medium` costs `derived` $61.32 a month to be awake | **OMS state**, beside the bars, never holding them | [0004](../decisions/0004-durable-store.md) |
| Timestream, both flavours | LiveAnalytics has been closed to new customers since 2025-06-20; InfluxDB is a second query language | — | [0004](../decisions/0004-durable-store.md) |
| Athena | it reads the same files, and one Glue catalogue adds it later with no file changed | ad hoc SQL | [0004](../decisions/0004-durable-store.md) |
| Amazon EKS | `measured` $73.00 a month of control plane before a container starts | — | [0005](../decisions/0005-compute-and-region.md) |
| ECS on Fargate | `awsvpc` is mandatory there, which puts a VPC hop between `feed` and Redis | **the platform**, if host work becomes the constraint | [0005](../decisions/0005-compute-and-region.md) |
| NAT gateway | `derived` $165.00 a month at 1x, more than the compute it fronts | — | [0005](../decisions/0005-compute-and-region.md) |

## 3. The one component that is not an AWS service

**Look for Redis in the task definition, not in the console.** Redis runs as a container beside the
services, one per environment, started with persistence off and a memory ceiling. The rules it runs
under are [redis-hosting.md](redis-hosting.md). The named fallback is the ElastiCache row above, and
moving there is one endpoint string.

## 4. What it costs

**Check a monthly bill against this table first.** An unexpected line is usually a NAT gateway or a
forgotten public IPv4 ([compute.md](compute.md) §6).

| Line | 1x | 10x | Tag | Run behind it |
|---|---|---|---|---|
| ECS on EC2, all-in | $78.07 | $295.72–582.39 | `derived` | [0008](../decisions/0008-topology.md) §3, Price List Bulk API published 2026-09-10, read 2026-09-12 |
| S3 Standard, compacted | $1.52 | $15.18 | `derived` | [0004](../decisions/0004-durable-store.md) §3, month 12, at `measured` 143 MB/day |
| Redis container | $0 | $0 | `derived` | [0002](../decisions/0002-redis-hosting.md) §3; its memory is bought inside the compute |
| **Total** | **$79.59** | **$310.90–597.57** | `derived` | the three rows above, added |

**The S3 row is low, and the reason is a unit rather than a price.** Its 143 MB/day is `measured`
for BTC alone; [compute.md](compute.md) §4 puts BTC and ETH at `derived` 201.5 MB/day. Re-run
[0004](../decisions/0004-durable-store.md) §3 against the larger figure before quoting $1.52.

## 5. What nobody has measured

**Treat every latency figure in the two documents as a floor, not a reading.** There is no AWS
account on this machine, so four things are open:

1. **The venue latency from ap-south-1 and ap-northeast-1.** The commands are written out in
   [../research/0005a-venue-latency-run.md](../research/0005a-venue-latency-run.md) §3.
2. **The network hop to a managed Redis endpoint.** `tools/measure_redis_hosting.py` re-runs from an
   EC2 instance against an ElastiCache endpoint.
3. **S3 read latency, and the request count per Parquet read.** The plan is one hour and under a
   dollar, in [0004](../decisions/0004-durable-store.md) *Still open*.
4. **The same-AZ hop, if we ever split.** I14 (#80) measures it; until then it is `derived` at most
   1 ms.
