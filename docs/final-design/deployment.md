# Deployment

**What this page contains.** Where the system runs when it is not on somebody's laptop: which Amazon
services are used, how the parts are divided between them, how the machine is sized, what it costs,
how a new version is released, and what routine attention it needs.

**How to read it.** Start with the picture and the two sections after it, which explain the shape
and the reasoning. The sizing, cost and operations sections are reference material to come back to.

**One thing to know first: none of this is built yet.** There is no Amazon account attached to this
project at the time of writing. These are the decisions that have been made, not a description of
something currently running. What *is* running is the Docker setup described in
[Getting started](getting-started.md), which mirrors this shape closely.

## The shape, in one picture

The back end runs together on one rented computer. The web page is served separately by a service
that specialises in exactly that.

```
                        DELTA EXCHANGE
                              |
   +--------------------------v---------------------------+
   |  One EC2 computer, in the Mumbai region               |
   |                                                       |
   |   feed  -->  redis  -->  store  -->  S3 (the files)   |
   |                 |                                     |
   |                 +----->  api                          |
   |                 +----->  alert forwarder              |
   |                                                       |
   |   a small proxy, the only thing reachable from        |
   |   outside, and only for the API                       |
   +--------------------------^---------------------------+
                              | one forwarding rule, over HTTPS
                    +---------+----------+
                    |  Amplify: the web  |
                    |  page              |
                    +---------+----------+
                              |
                          a browser
```

## Why it is divided this way

**The back end is on one machine because it is one pipeline.** The feed publishes to Redis, and the
store and the API read from that same Redis. Putting them on one computer means those messages never
travel over a network at all -- they go through the machine's own internal loopback, which is as
fast as it is possible to be, and cannot fail independently of the machine itself. The API in
particular belongs here rather than anywhere else, because the calculations it performs are driven
by the same stream of messages.

**The web page is elsewhere because it has no reason to be here.** It holds no data, connects to no
venue, and does nothing but display what the API sends it. Served by Amplify it gains a worldwide
delivery network, a managed security certificate, and an automatic rebuild whenever the code
changes -- and the back-end machine loses a container, a build step and one more thing to secure.

## How the browser reaches the API

Amplify serves the page, and it is configured with one forwarding rule: anything the page requests
beginning with `/api` is passed through to the back-end machine, and the answer is returned as
though Amplify had produced it.

The consequence is that **the browser only ever talks to one address**. It never makes a request to a
second server, so the browser safety rules about cross-address requests never come into play, and
nothing has to be configured to permit them. This is the same arrangement the local Docker setup
uses, with the nginx container replaced by Amplify's forwarding rule.

The back-end machine therefore has exactly one thing reachable from outside: the API, behind a small
proxy that handles encryption. Nothing else on it accepts connections from the internet.

## The machine

The table below lists what is rented and why each choice was made.

| Decision | Choice | Reasoning |
|---|---|---|
| Region | `ap-south-1` (Mumbai) | The cheapest of those compared, and in the same city as the network point the venue is served from |
| Computer | One `c7g.xlarge`: 4 processors, 8 GB memory, 30 GB disk | Sized below |
| Processor type | ARM | The equivalent Intel machine costs 82 percent more here for the same capability |
| Who starts the containers | Amazon ECS | It restarts a container that stops **and records that it did**, which running them by hand would not |
| Networking | Containers share the machine's own network | This is what keeps Redis on the internal loopback |
| Internet connection | A directly connected machine, with its own address | Explained below |
| Files | Amazon S3 | One bucket per environment |

**There is deliberately no NAT gateway.** A NAT gateway is the usual way to give machines internet
access without exposing them, and it bills for every byte passing through it. The feed downloads
roughly 2,200 GB a month, so routing it through a NAT gateway would cost `derived` **$165 a month**
-- more than the computer it would be protecting. Connecting the machine directly, with a single
address and only the API reachable, costs a few dollars and achieves the same isolation.

## Sizing

Six containers share the machine. The table below lists what each is allocated.

| Container | Processors | Memory |
|---|---|---|
| feed | **1.0** | 1 GB |
| store | 0.5 | 1 GB |
| api | 1.0 | 2 GB |
| redis | 0.5 | 3 GB |
| alert forwarder | 0.05 | 0.25 GB |
| proxy | 0.25 | 0.5 GB |
| **Total** | **3.30** | **7.75 GB** |

**The feed is given a whole processor** because a Python program cannot use more than one anyway,
and this is the program that must never fall behind the venue's connection. The others cannot crowd
it out even if they are all busy at once.

The allocations add up to 3.30 out of 4 processors, which is intentional: containers can borrow
capacity from each other, and reserving every last processor would leave nothing for the operating
system. The current measured usage is comfortably inside these numbers. **If the amount of data ever
grows tenfold, the answer is a larger machine of the same type** -- nothing else about the
arrangement has to change, which is the main practical benefit of keeping the pipeline together.

## What it costs

Every figure below is `derived` from Amazon's published prices and our own measured data volumes.

| Item | At today's volume | At ten times today's volume |
|---|---|---|
| The computer, all in | $78.07 a month | $295 to $582 a month |
| File storage | $1.92 a month | $19.19 a month |
| Redis | $0 -- it runs on the computer already paid for | $0 |
| Amplify | `assumed` small: charged per build and per gigabyte delivered | `assumed` small |
| **Total** | **about $80 a month** | **about $315 to $600 a month** |

If a bill ever comes in noticeably higher than this, the cause is almost always one of two things: a
NAT gateway that somebody added, or a spare internet address left allocated and forgotten.

## The services used

The table below lists every Amazon service involved.

| Service | What it does here |
|---|---|
| EC2 | Rents the computer |
| ECS | Starts the containers on it and restarts them if they stop |
| Amplify | Builds and serves the web page, and forwards `/api` to the computer |
| VPC | The network the computer sits on: one directly connected subnet, one address |
| EBS | The computer's own 30 GB disk |
| S3 | Permanent storage for the data files |
| ECR | Stores the container images that a release uses |
| CloudWatch | Raises an alarm if a container stops or the machine misbehaves |

Two replacements have been chosen in advance, so that neither has to be decided in a hurry.
If the Redis container ever becomes unsuitable, **ElastiCache** replaces it and the change is one
connection string. When an order-management system eventually needs somewhere to keep its state,
**a managed PostgreSQL database** goes alongside the files rather than replacing them.

## Releasing a new version

**The back end.** Build the container image, upload it to ECR, register the new version with ECS,
and tell ECS to update the service. ECS handles replacing the running containers. Releasing is
therefore a recorded change rather than somebody logging into a machine.

**The web page.** Push the code. Amplify notices, builds it, and publishes it.

The container images are **identical in every environment**. The same image that runs on a laptop
runs on the machine; only the settings differ.

## What a month of looking after it involves

1. **Update the machine image.** Replace the machine with one running the current image rather than
   patching the running one. About half an hour.
2. **Read the container event history** for anything that stopped when nobody was watching, and why.
3. **Check the disk** on the machine for accumulated images and logs.
4. **Check the bill** against the figures above.
5. **Check the web page's build history** for a failed build nobody noticed.

Everything else the platform does by itself: restarting a container that stopped, recording that it
did so, replacing one that stops answering its health check, and rebuilding the page on every code
change.

## Where to go next

[Architecture](architecture.md) explains what each container actually does.
[Configuration](configuration.md) lists the settings applied to them.
