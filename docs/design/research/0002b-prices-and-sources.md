# R5 — prices, arithmetic and sources

The working behind [0002-redis-hosting.md](0002-redis-hosting.md). Nothing is decided here;
this file exists so every number in that one can be checked without re-reading it.

## 1. How the prices were read

All from the **AWS Price List Bulk API**, the machine-readable form of the public pricing
pages, fetched **2026-09-09**:

```
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonElastiCache/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonMemoryDB/current/<region>/index.json
https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/<region>/index.json
```

`publicationDate`: ElastiCache and EC2 **2026-09-08T22:10:28Z**, MemoryDB
**2026-08-31T09:22:16Z**. Regions `ap-south-1` (Asia Pacific, Mumbai) and `us-east-1` (US
East, N. Virginia). On-demand terms only — `offerTermCode` `JRTCKXETXF`. One month is
**730 hours**. Extended-support SKUs (`…-ExtendedSupportYr1_Yr2-…`) are excluded.

Spot check, so the extraction can be trusted: `t4g.medium` in ap-south-1 carries rate code
`CZCTHCVW44E95CJ5.JRTCKXETXF.6YS6EN2CT7`, description "$0.0224 per On Demand Linux
t4g.medium Instance Hour", `effectiveDate` 2026-09-01 — the same value the EC2 pricing page
renders.

## 2. Unit prices, `measured` from the price list

**ElastiCache node hours, USD/hr.** Valkey is 20% under Redis OSS on every row.

| Node | `maxmemory`, B | Valkey aps3 | Redis aps3 | Valkey use1 | Redis use1 |
|---|---|---|---|---|---|
| `cache.t4g.small` | 1,471,026,299 | 0.0328 | 0.0410 | 0.0256 | 0.0320 |
| `cache.t4g.medium` | 3,317,862,236 | 0.0648 | 0.0810 | 0.0520 | 0.0650 |
| `cache.m7g.large` | 6,854,542,746 | 0.1312 | 0.1640 | 0.1264 | 0.1580 |
| `cache.r7g.large` | 14,037,181,030 | 0.1792 | 0.2240 | 0.1752 | 0.2190 |
| `cache.r7g.xlarge` | 28,261,849,702 | 0.3592 | 0.4490 | 0.3496 | 0.4370 |

Synchronous durability adds a `SyncDurability-NodeUsage` SKU — `cache.m7g.large` Valkey
ap-south-1 $0.0236/hr on top of $0.1312, the documented 18%.

**ElastiCache Serverless.**

| | ap-south-1 | us-east-1 |
|---|---|---|
| Valkey data stored | $0.054 /GB-hr | $0.084 /GB-hr |
| Redis OSS data stored | $0.081 /GB-hr | $0.125 /GB-hr |
| Valkey ECPU | $0.0015 /million | $0.0023 /million |
| Redis OSS ECPU | $0.0022 /million | $0.0034 /million |
| Snapshot storage | $0.085 /GB-month | $0.085 /GB-month |

**MemoryDB.**

| | Valkey aps3 | Redis aps3 | Valkey use1 | Redis use1 |
|---|---|---|---|---|
| `db.t4g.medium` (3,317,862,236 B) | 0.0847 | 0.1210 | 0.0679 | 0.0970 |
| `db.r7g.large` (14,037,181,030 B) | 0.2219 | 0.3170 | 0.2163 | 0.3090 |
| `db.r7g.xlarge` (28,261,849,702 B) | 0.4431 | 0.6330 | 0.4319 | 0.6170 |
| Data written | $0 to 10 TB/mo, then $0.04/GB | $0.20/GB | $0 to 10 TB/mo, then $0.04/GB | $0.20/GB |
| Snapshot storage over allowance | $0.023 /GB-mo | $0.023 /GB-mo | $0.021 /GB-mo | $0.021 /GB-mo |

**EC2 on-demand Linux, USD/hr**, and gp3 storage.

| | ap-south-1 | us-east-1 |
|---|---|---|
| `t4g.small` 2 GiB | 0.0112 | 0.0168 |
| `t4g.medium` 4 GiB | 0.0224 | 0.0336 |
| `t4g.large` 8 GiB | 0.0448 | 0.0672 |
| `t4g.xlarge` 16 GiB | 0.0896 | 0.1344 |
| `m7g.large` 8 GiB | 0.0583 | 0.0816 |
| `r7g.large` 16 GiB | 0.0751 | 0.1071 |
| gp3 provisioned storage | $0.0912 /GB-month | $0.08 /GB-month |

## 3. The arithmetic, cell by cell

**What is being sized.** `derived` 1,051.5 MiB (#58) = 1,102,577,664 B = 1.027 GiB =
1.103 GB decimal. Ten times: 10.269 GiB = 11.026 GB. Write volume `derived` 598.2 KiB/s =
612,556.8 B/s → 1,609.8 GB per 730-hour month; 16.10 TB at ten times.

**Container beside the services.** The memory is bought inside R4's compute (#68), so the
marginal cost is one instance-size step. `t4g.small`→`t4g.medium` adds 2 GiB for
+$0.0112/hr = $8.18/mo; `t4g.medium`→`t4g.large` adds 4 GiB for +$0.0224/hr = $16.35/mo.
At ten times, `t4g.large`→`t4g.xlarge` adds 8 GiB for +$0.0448/hr = $32.70/mo, and
`m7g.large`→`m7g.xlarge` adds 8 GiB for +$0.0583/hr = $42.56/mo; `r7g.large` standalone is
$54.82/mo. **$0** if R4's host already has the headroom.

**ElastiCache node.** $0.0648 × 730 = **$47.30** (Valkey, `cache.t4g.medium`, ap-south-1);
$0.3592 × 730 = **$262.22** at ten times (`cache.r7g.xlarge`). Redis OSS is the same
arithmetic 25% higher.

**ElastiCache Serverless, Valkey, ap-south-1.** Storage 1.103 GB × 730 × $0.054 = $43.46.
ECPU: "Reads and writes require 1 ECPU for each kilobyte (KB) of data transferred", and a
`measured` mean entry of 340.5 B is under 1 KB, so one operation is one ECPU. One `XADD`
plus two consumer-group reads per event: 3 × 1,849.8 = 5,549.4 ECPU/s × 2,628,000 s =
**14,584 million ECPU** × $0.0015 = $21.88. Total **$65.34**; ten times, **$653.39**. In
us-east-1 the same shape is $67.61 + $33.54 = **$101.15**, and $1,011.57 at ten times.
Above the 100 MB Valkey minimum in every case.

**MemoryDB.** Node plus data written. Valkey ap-south-1: $0.0847 × 730 = $61.83 and
1,609.8 GB written is inside the free 10 TB → **$61.83**. Ten times: $0.4431 × 730 =
$323.46 node, and 16.10 TB leaves 6,098 GB over the allowance at $0.04 = $243.92 →
**$567.38**. Redis OSS ap-south-1: $88.33 node + 1,609.8 × $0.20 = $321.96 → **$410.29**;
ten times $462.09 + $3,219.60 = **$3,681.69**.

**Self-managed EC2.** `t4g.medium` $0.0224 × 730 = $16.35 plus an 8 GB gp3 root volume at
8 × $0.0912 = $0.73 → **$17.08**. Ten times, `r7g.large` $54.82 + $0.73 = **$55.55**. In
us-east-1 gp3 is $0.08/GB-month, so the same two rows are **$25.17** and **$78.82**.
`t4g` is burstable: two vCPUs at a 20% baseline each, against a `measured` 5.9% of one core
for the publisher, so the baseline covers it — but the credit model is a risk `m7g.large`
at $42.56 removes.

**Sizing rule.** Usable = `maxmemory` × (1 − `reserved-memory-percent`/100), default 25.
`cache.t4g.small`: 1,471,026,299 × 0.75 = 1,103,269,724 B against 1,102,577,664 needed —
692,060 B of headroom, 0.06%. `cache.r7g.large`: 10,527,885,772 B against 11,025,776,640
needed at ten times — short by 497,890,868 B.

## 4. Sources

Primary only. Every claim in [0002-redis-hosting.md](0002-redis-hosting.md) traces here.

| Claim | Source |
|---|---|
| "**No persistence**: You can disable persistence completely." AOF is enabled with `appendonly yes`; RDB is disabled with `config set save ""` | [Redis persistence](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/) |
| Shipped defaults `save 3600 1 300 100 60 10000`, `appendonly no`, `maxmemory-policy noeviction`, `stream-node-max-entries 100` | [`redis.conf` 7.4.0](https://github.com/redis/redis/blob/7.4.0/redis.conf) L428–445, L1162, L1398, L2013–2014 |
| `noeviction` "will return an error when you try to execute commands that cache new data"; `allkeys-lru` and the rest "Evict … keys" — whole keys | [Key eviction](https://redis.io/docs/latest/develop/reference/eviction/) |
| `MAXLEN` evicts by count, `MINID` by id, `~` is approximate trimming | [XADD](https://redis.io/docs/latest/commands/xadd/) |
| ElastiCache: "Redis OSS configuration variables `appendonly` and `appendfsync` are not supported"; both listed `Default: off`, `Modifiable: No`; `reserved-memory-percent` default 25; `maxmemory-policy` default `volatile-lru`; the per-node `maxmemory` table | [Engine specific parameters](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/ParameterGroups.Engine.html) |
| ElastiCache backups: "If the backup retention limit is set to 0, automatic backups are disabled for the cache" | [Scheduling automatic backups](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/backups-automatic.html) |
| ElastiCache durability is opt-in, Multi-AZ transactional log, synchronous writes "increases write latency from microseconds to single-digit milliseconds" | [Durability in ElastiCache](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/durability.html), [Durability options](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/Durability.Options.html) |
| Serverless: "Reads and writes require 1 ECPU for each kilobyte (KB) of data transferred"; minimum metered storage 100 MB Valkey, 1 GB Redis OSS; synchronous durability priced "at 18% on top of the standard node-hour price"; backup $0.085/GiB-month; same-AZ EC2↔ElastiCache transfer free, cross-AZ $0.01/GiB charged on the EC2 side | [Amazon ElastiCache pricing](https://aws.amazon.com/elasticache/pricing/), read 2026-09-09 |
| MemoryDB: "stores data durably across multiple Availability Zones (AZs) using a Multi-AZ transactional log" | [What is MemoryDB](https://docs.aws.amazon.com/memorydb/latest/devguide/what-is-memorydb.html) |
| MemoryDB: a shard has "one primary write node and the other 5 serving as read replicas" — a one-node cluster is legal | [MemoryDB core components](https://docs.aws.amazon.com/memorydb/latest/devguide/components.html) |
| MemoryDB: "You pay only for the volume of data (in GB) you write to your MemoryDB cluster… There are no associated costs for reads"; Valkey data written free to 10 TB/month | [Pricing for Amazon MemoryDB](https://aws.amazon.com/memorydb/pricing/), read 2026-09-09 |
| MemoryDB `maxmemory-policy` default `noeviction`; no `appendonly` parameter; the per-node `maxmemory` table | [MemoryDB engine specific parameters](https://docs.aws.amazon.com/memorydb/latest/devguide/parametergroups.redis.html) |
| All node, ECPU, data-written, EC2 and gp3 unit prices | AWS Price List Bulk API, §1 above |
| WSL 2 "automatically resizes these VHD files to meet storage needs"; "the process of reducing a virtual disk size is much more complicated" | [How to manage WSL disk space](https://learn.microsoft.com/en-us/windows/wsl/disk-space) |
| `wsl --manage <Distro> --set-sparse <true\|false>`: "Set the VHD of distro to be sparse, allowing disk space to be automatically reclaimed" | `wsl.exe --help`, WSL 2.7.8.0, `measured` 2026-09-09 |
| Docker Desktop's **Disk usage limit** and **Disk image location** settings are listed for "Mac, Linux, Windows Hyper-V" — not the WSL 2 backend | [Change your Docker Desktop settings](https://docs.docker.com/desktop/settings-and-maintenance/settings/) |
| The port-forward caveat on every loopback figure | vault, `teach/message-bus/the-spike.md` |
| The three causes and the five-step diagnosis | vault, `teach/message-bus/the-disk-bloat.md` |
