# Deployment

**Two places, and one line between them.** Everything that touches the venue, the bus, the store and
the API runs on **one EC2 instance**. The front end is built and served by **AWS Amplify**.

```
                        DELTA EXCHANGE
                              |
   +--------------------------v---------------------------+
   |  EC2 -- one c7g.xlarge, ap-south-1, host networking   |
   |                                                       |
   |   feed  -->  redis  -->  store  -->  S3 (bar tables)  |
   |                 |                                     |
   |                 +----->  api                          |
   |                 +----->  discord-alerts               |
   |                                                       |
   |   proxy (TLS) -- the one public listener, for the api |
   +--------------------------^---------------------------+
                              | HTTPS, one rewrite rule
                    +---------+----------+
                    |  Amplify -- web    |
                    +---------+----------+
                              |
                           a browser
```

## What runs where

| Tier | Runs | Holds |
|---|---|---|
| **EC2** | ECS on EC2, one task definition, `host` network mode | `feed`, `redis`, `store`, `api`, `discord-alerts`, and a proxy that terminates TLS for the API |
| **Amplify** | Amplify Hosting, built from the repository on push | `web` -- the Next.js front end, its build, its CDN and its certificate |
| **S3** | one bucket per environment | the four bar tables, in the hive layout the engine already writes |

**The back end stays on one box because it is one pipeline.** `feed` publishes to Redis on
`127.0.0.1`, `store` and `api` read from the same loopback, and nothing between them crosses a
network. `host` network mode is what keeps that true.

**The front end is the one piece with no reason to be there.** It holds no state, talks to no
venue, and is a static build plus server-side rendering -- exactly what Amplify is. Moving it off
the instance takes a container, a build step and a certificate off the box and gives the browser a
CDN it would otherwise not have.

## The instance

| | Value |
|---|---|
| Region | **`ap-south-1`**, Mumbai -- in-country, and the CloudFront edge that serves the venue is in the same city |
| Instance | **`c7g.xlarge`** -- Graviton3, **4 vCPU, 8 GiB**, 30 GB gp3 root |
| Architecture | `linux/arm64`. The x86 twin of the same shape costs 82% more here |
| Orchestrator | **Amazon ECS, EC2 launch type** -- a restart is a platform event something can alarm on, and EC2 compute adds $0 over running Compose by hand |
| Network mode | **`host`** |
| Subnet | **public**, one in-use public IPv4 |
| NAT gateway | **none** |
| Inbound | **one HTTPS listener, for the API**, reached by Amplify's rewrite. Nothing else is public |

**No NAT gateway.** The feed pulls a `measured` 843.4 KB/s, which is `derived` 2,216.5 GB a month.
Inbound to AWS is free; a NAT gateway meters it, and the bill would be `derived` **$165.00 a
month** -- more than the compute it fronts. The instance sits in a public subnet so its own public
IPv4 is the egress. `awsvpc` network mode is not used for the same reason: task ENIs on EC2 get no
public IP, so it forces a private subnet and a NAT gateway with it.

## How the browser reaches the API

**One origin, one rewrite rule.** Amplify serves the app and rewrites `/api/<*>` to the instance's
HTTPS endpoint as a **200 rewrite**, so the browser makes no cross-origin request and no preflight.

| | |
|---|---|
| Browser origin | the Amplify domain, and nothing else |
| `/api/<*>` | rewritten to `https://<api endpoint>/<*>` |
| Everything else | served by Amplify |
| CORS | **not consulted** -- the browser never addresses the instance |
| `/ws/chain` | the same prefix; a websocket handshake, never subject to CORS |

This is the shape the local stack already runs behind nginx, with Amplify's rewrite standing in for
the `proxy` container. It is why `NEXT_PUBLIC_ENGINE_URL` is a **path** and not a host: the browser
asks its own origin for `/api/chain` in development and in production alike.

**The API endpoint is public but not advertised.** It answers Amplify's rewrite and health checks
and nothing else; the dashboard is the only client, and it never sees the address.

## Sizing

Six containers on the instance, with `web` no longer among them.

| Container | vCPU | Memory |
|---|---|---|
| `feed` | **1.0** (1,024 CPU units) | 1 GB |
| `store` | 0.5 | 1 GB |
| `api` | 1.0 | 2 GB |
| `redis` | 0.5 | **3 GB** -- the `maxmemory 2gb` ceiling plus overhead |
| `discord-alerts` | 0.05 | 0.25 GB |
| proxy | 0.25 | 0.5 GB |
| **Total** | **3.30** | **7.75 GB** |

**`feed` reserves a whole core** because one Python process is one core, and it is the process that
must never fall behind a socket. **The host overcommits vCPU**: the reservations total 3.30 against
4, and three one-core Python services cannot starve `feed` at its 1,024 units.

The reservations under-count CPU and over-count memory: the `derived` need at today's rate is
1.21-1.95 cores and 5.05 GiB, which is why the instance is 4 vCPU and not 2. **At ten times the
rate it is a `c7g.4xlarge` to `c7g.8xlarge`** -- that is the re-size trigger, and nothing else about
the topology changes with it.

## Amplify

| | Value |
|---|---|
| Source | this repository, `web/`, built on push to the deployment branch |
| Framework | Next.js, with server-side rendering |
| TLS and domain | Amplify's, managed |
| API routing | one 200 rewrite, `/api/<*>` to the instance |
| Build-time variable | `NEXT_PUBLIC_ENGINE_URL=/api` |

**`NEXT_PUBLIC_` values are inlined by `next build`.** Changing the API endpoint or the rewrite is a
**rebuild**, not a restart -- the same rule that holds in the Docker stack, for the same reason.

## Storage

**Amazon S3 Standard, one bucket per environment**, `s3://<env>-deltapayoff-bars/`, written by
`store` and read by the four historical routes. Versioning off, Block Public Access on, SSE-S3, and
one lifecycle rule that only aborts incomplete multipart uploads. Bars are kept indefinitely.
**Compaction is nightly**, every partition strictly before today. The layout, the write path and the
compaction contract are [Data store](data-store.md).

## What it costs

| Line | 1x | 10x |
|---|---|---|
| EC2, all-in | `derived` $78.07 | `derived` $295.72-582.39 |
| S3 Standard, compacted, BTC+ETH | `derived` $1.92 | `derived` $19.19 |
| Redis | $0 -- its memory is bought inside the instance | $0 |
| Amplify hosting | `assumed` small: build minutes and GB served, at one dashboard's traffic | `assumed` small |
| **Total** | **`derived` $79.99** plus Amplify | **`derived` $314.91-601.58** plus Amplify |

An unexpected line on a bill is almost always a NAT gateway or a forgotten public IPv4.

## The services this system buys

| Service | For |
|---|---|
| Amazon ECS, EC2 launch type | one task definition holding the six back-end containers |
| Amazon EC2 | one `c7g.xlarge`, `host` network mode |
| AWS Amplify Hosting | the front end: build, CDN, TLS, and the API rewrite |
| Amazon VPC | one public subnet, one in-use public IPv4, no NAT gateway |
| Amazon EBS gp3 | the instance's 30 GB root volume, and nothing else |
| Amazon S3 Standard | the four bar tables, one bucket per environment |
| Amazon ECR | the images a deploy registers |
| Amazon CloudWatch | alarms on a stopped task and on the box |

Two fallbacks are named in advance, so neither is a decision taken under pressure:
**ElastiCache for Valkey** replaces the Redis container, and it is one endpoint string.
**RDS for PostgreSQL** holds order-management state when an order path arrives -- beside the bars,
never holding them.

## Deploying

**The back end**: build the image, push to ECR, register a task definition revision,
`aws ecs update-service`. The platform holds the desired state, so a deploy is a revision rather
than an `ssh`.

**The front end**: push to the deployment branch. Amplify builds and promotes it.

**The images are identical in both environments.** One `Dockerfile` per service, built
`linux/arm64`, and the same tag runs on a laptop and on the instance.

## A month of operations

1. **Patch the AMI** -- replace the instance, do not patch in place. ~30 minutes.
2. **Read the ECS event stream** for task stops nobody noticed, and their stopped-reason strings.
3. **Check disk on the root volume** -- images, logs, anything left locally.
4. **Check the bill** against the `derived` $78.07.
5. **Check the Amplify build history** for a failed build nobody was watching.

**What the platform does instead of us**: restarts a container that exited, reports that it did,
replaces a task that fails its health check, and rebuilds the front end on every push.

## Related guides

[Architecture](architecture.md) | [Data store](data-store.md) | [Message bus](message-bus.md)
