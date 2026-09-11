# R6 — Which process is compute-heavy, and which is data-heavy

Findings for #75, under epic #57. The belief it produces is
[../decisions/0007-load-profile.md](../decisions/0007-load-profile.md); the feed engine
document's summary is [../cloud/compute.md](../cloud/compute.md) §8. R7 (#78) prices topologies
against §8's sizing table; I13 (#79) replaces every `derived` cell with `measured` after #65.

## The question

Which process is compute-bound, which memory-bound, which disk- or network-bound, and by how
much? Criteria as #57 orders them: invariants, ops, cost at 1× and 10×, path to OMS, venue latency.
**1×** is `measured` 1,693.6 msg/s, 843.4 KB/s, 782 contracts ([../hld.md](../hld.md) §5), or
`derived` 1,849.8 events/s ([0001-stream-naming-and-payload-format.md](0001-stream-naming-and-payload-format.md)).
**A core** is one laptop core (i7-12650H), `assumed` equal to one Graviton3 vCPU, probably
optimistically. AWS lists `m7g` and `c7g` at one thread per core
([gp](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html), [co](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html)).

## 1. What this ticket measured, 2026-09-11 UTC

The live engine, PID 13280 (the interpreter under launcher 6588), in-process fan-out, 893
contracts (556 BTC + 337 ETH, `feed.instruments` at 18:39:33Z). Nothing in it was changed.

| # | Number | Run |
|---|---|---|
| m1 | Engine **34.22%** of a core, 122.6 MiB working set, **no viewer** | `Get-Process` CPU delta, 60.41 s from 18:46:50Z, no profiler attached |
| m2 | Engine **39.70%** with **one viewer** on BTC 12-09-2026, 36 rows | same method, 54.35 s from 18:48:15Z |
| m3 | That viewer got **38,316 B a push**, 62 pushes in 62.1 s: **38.9 KB/s** | read-only `/ws/chain` client, 18:48:09–18:49:11Z |
| m4 | CPU split by module, §2 | `py-spy 0.4.2 record --nonblocking --rate 100`, 120 s from 18:44:07Z, temporary venv outside the repo |
| m5 | Imports alone, private: `feed`'s modules **29.4 MiB**, `store`'s **40.5**, `api`'s **50.4**; bare interpreter 7.3 | one process per set, engine venv, `GetProcessMemoryInfo` |
| m6 | `next dev`, both processes: **240.8 MiB** private (175.5 + 65.3), **0.08%** of a core | `Get-Process`, 121.31 s from 18:44:15Z |
| m7 | Store on disk, 2026-09-08 BTC+ETH, uncompacted: **182.7 MB**, 510 files per table | `du -sb` on `data/`, read only |

**m1 corroborates R4:** 34.22 × 782 / 893 = **29.97%** `derived`, against `measured` 30.89%
([0005-compute-and-region.md](0005-compute-and-region.md) §1). With py-spy attached the same meter
read **53.00%**, so the profiler supplies proportions only, never an absolute.

## 2. Where the monolith's CPU goes

py-spy caught 43.49 s of non-idle samples (4,349 samples, and **2,550 read errors**, the price of
`--nonblocking`, which never pauses the feed). Each sample is filed under the innermost frame from
our package; a stack that lost our frame is filed by what it was doing.

| Future service | What the samples were in | Seconds | Share |
|---|---|---|---|
| `feed` | `adapters/delta.py`, `events/instrument.py`, `delta_socket.py`, `wire.py`, the `websockets` reader, plus 5.12 s of TLS reads, `json` decode, `uuid` event ids and `strptime` with no frame of ours | 28.25 | **71.6%** |
| `store` | `bars.py` folding every tick into its minute; `store.py` | 7.08 | **17.9%** |
| `api` | `stream.py` holding the newest frame per contract; the minute pass | 4.13 | **10.5%** |
| the bus | `fanout.py` | 0.03 | 0.1% |
| shared | the event loop's own `_poll` and `_run_once`; empty stacks | 4.00 | pro rata |

**Applied to R4's 30.89%, `derived`: `feed` 22.1, `store` 5.5, `api` 3.2 points of a core.**

## 3. What the split adds, which the monolith never paid

Inside one process an event is an object handed to two queues. Across Redis it is encoded once
and **decoded once per consumer**. Both costs are from I2 ([0061-batch-interval.md](0061-batch-interval.md)):

- **Encode and publish: 29.96 points**. That is the 70.73% of the feed-shaped publisher at 50 ms,
  minus 40.77% with the bus off.
- **Decode: 22.1–45.4 points per consumer.** The upper bound is I2's consumer, `measured` 45.43%
  ([0061-batch-interval-consumer.json](0061-batch-interval-consumer.json)): one lossless reader
  decoding 3,330,235 events, **polling** (`read_block_ms=0`) through a phase with nothing to
  read, on Windows through WSL2. The lower bound is `derived`: the feed's own 22.1, the same work.

**The split costs 1.10–1.75 cores against the monolith's 0.31: 3.6 to 5.7 times as much.**

## 4. The profile at 1×

| | CPU, cores | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | **0.52–0.71** `derived` C1 | ~50 MiB `derived` M1; ≤ +95 MiB while Redis is away, M2 | none, `assumed`: logs only | in 859.9 KB/s, out 598.2 KB/s `derived` N1 | **CPU**, one core |
| `store` | **0.28–0.51** `derived` C3 | ~115 MiB `derived` M1 | **2.33 KB/s**, 8 files per 300 s `derived` D1 | in 598.2 KB/s, out 2.3 KB/s `derived` N2 | **CPU**, not disk |
| `api` | **0.25–0.49**, + 0.05–0.14 a watched expiry `derived` C4 | ~80 MiB `derived` M1 | on demand, `assumed` D2 | in 598.2 KB/s; out **38.9 KB/s a viewer** `derived` N3 | **CPU**, set by viewers |
| `web` | **~0** `derived` C5 | **240.8 MiB** `derived` M3 | none, `assumed`: an image | page loads only, `assumed`: the browser's socket is to `api` | **memory** |
| Redis | 0.05 `assumed` C6 | **1,056.4 MiB** at 30 min, 2 GiB ceiling `derived` M4 | **none** `derived` D3 | in 598.2, out 1,196.4 KB/s `derived` N4 | **memory** |

| Key | Arithmetic, and the measured input |
|---|---|
| C1 | 22.1 (§2) + 29.96 (§3) = **52.1%**; upper **70.73%**, I2's feed-shaped process `measured` at 774 contracts. Sizing uses the upper |
| C3, C4 | `store` 5.5 + decode 22.1–45.4 = **27.6–51.0%**; `api` 3.2 + decode = **25.3–48.7%** |
| C4 viewer | 39.70 − 34.22 = **5.48 points** (m1, m2), 36 rows; up to 14.368 ms per 100 ms pass = **14.4** on the largest ladder ([../lld/chain-cache.md](../lld/chain-cache.md) §9) |
| C5 | m6 on `next dev`, carried to `next start` unchanged |
| C6 | `assumed`: ~5,550 entry operations a second (1,849.8 written, twice that read) at ~10 µs of server time each. Only the client side has been measured |
| M1 | m5 plus state. State = R4's `measured` 178.6 MiB private − ~55 MiB of imports (`assumed`) = ~124 MiB, split `assumed` 60/25/15 to `store`/`api`/`feed`. `store` holds five minutes of sealed rows (11,740 a flush, [../lld/store.md](../lld/store.md) §8) plus every open minute; the cache holds one frame per contract |
| M2 | outbox bound 200,000 entries ([../lld/redis-bus.md](../lld/redis-bus.md) §4) × ~500 B `assumed`, only while Redis is unreachable |
| M3, M4 | m6. 1,056.4 MiB `measured` at thirty minutes (0061, *Memory*); ceilings 2 and 12 GiB (0005 §1) |
| D1 | 699,591 B a scheduled flush, BTC+ETH, `measured` (lld/store §8) × 288 = **201.5 MB/day** = 2.33 KB/s. m7 scaled from 255 of 288 flushes gives 206.3, within 2.4%. **143 MB/day was BTC alone** |
| D2, D3 | 82.0 ms for a compacted whole-day read, `measured` locally ([../decisions/0004-durable-store.md](../decisions/0004-durable-store.md) §5), at 0004's `assumed` 1,000 reads a day. Redis has no persistence, as decided (0002) |
| N1 | 843.4 KB/s `measured` (hld §5) + the re-list's 987 KB a minute (`main.py`, `RELIST_INTERVAL_SECONDS`) = 859.9 KB/s = **6.9 Mbit/s**; out 598.2 KB/s, encoding B (0001; 0002 writes KiB/s for the same figure) |
| N2–N4 | each consumer reads the whole 598.2 KB/s; Redis writes it once and serves it twice. N3 is m3; a 101-row ladder at ~2.8× is `assumed` ~109 KB/s |
| Hop | across two instances, 1,196.4 KB/s × 2,628,000 s = **3,144 GB/month**: $62.88 cross-AZ at 0005b §2's $0.01/GB, charged in each direction, $628.80 at 10× (corrected by R7, #78; first printed as half). Same-AZ is `assumed` $0; R7 confirms |

## 5. The profile at 10×

`derived` by ×10 from §4, `assumed` linear in the event rate. A viewer's cost follows its ladder's
size, not the rate.

| | CPU, cores | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | **5.2–7.1**, **8–11 processes** at ≤ 0.7 each (`assumed` ceiling) | ~0.4–0.5 GiB: state ×10 plus an import base per process | none | in 8.6 MB/s, **7.3% of a 0.937 Gbps baseline**; out 6.0 MB/s | **CPU, sharded** |
| `store` | **2.8–5.1**, 4–8 processes | ~0.9–1.1 GiB | 23.3 KB/s, 2.0 GB/day | in 6.0 MB/s | **CPU** |
| `api` | **2.5–4.9**, 4–7 processes, + viewers | ~0.5–0.7 GiB | on demand | in 6.0 MB/s | **CPU** |
| `web` | ~0 | 240.8 MiB | none | unchanged | memory |
| Redis | 0.5 `assumed` | **10.3 GiB**, 12 GiB ceiling (0001 table, 0005 §1) | none | in 6.0, out 12.0 MB/s | **memory** |

**One Python process is one core.** `feed` at 1× already spends 52–71% of the only core its one
asyncio loop can use, and saturates at 1.4–1.9× today's rate. At 10× the question is how many processes.

## 6. `oms` and `strategy`: named, not built, every cell `assumed`

**`oms`** is assumed to take order intents off the bus, place and cancel orders over the venue's
authenticated API, track each to a fill, and write every state change to 0004's Postgres.
**`strategy`** is assumed to read the solved chain and market events, evaluate signals over the
ladder, and publish order intents for `oms`.

| `assumed` | CPU | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `oms` 1× | 0.1 core: orders a second, not quotes | 0.2 GiB: open orders, positions | none local; KB/s to Postgres | KB/s to the venue | **latency, durability** |
| `oms` 10× | 0.1–0.5: scales with orders, which 10× market data need not bring | 0.2 GiB | as 1× | as 1× | as 1× |
| `strategy` 1× | 0.3–1.0: one decode (§3) if it reads the raw stream, plus an unwritten model | 0.5 GiB | none | 598.2 KB/s raw, KB/s if `computed.chain` only | **CPU** |
| `strategy` 10× | 2.5–10 | 1–2 GiB | none | 6.0 MB/s raw | **CPU** |

Independent of these rows: **every raw-stream consumer we add pays a 0.22–0.45 core decode at 1×.**

## 7. Dominant resource, and who contends first

**`feed` and `api` contend first, on CPU.** A starved `feed` falls behind the socket, the receive
buffer fills and the venue closes it: holes, criterion 1. `api` is the only service whose CPU
jumps while the rate stands still: up to 0.14 core per browser, instantly. `store` is as hungry
but tolerant, since Redis holds thirty minutes. At 10× the next pair is Redis and `store`, on memory.

**Nothing is disk- or network-bound.** The store writes 2.33 KB/s against gp3's baseline 3,000
IOPS and 125 MiB/s ([EBS](https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html)),
0.002% of it. The feed reads 6.9 Mbit/s against `m7g.large`'s and `c7g.large`'s 0.937 Gbps, 0.73%.

## 8. Sizing

On-demand, ap-south-1, instance only (EBS and the public IPv4 are R7's). Hourly prices `measured`,
AWS Price List Bulk API, 2026-09-09 ([0005b-prices-and-sources.md](0005b-prices-and-sources.md) §2);
monthly × 730, `derived`: `t4g.medium` $16.35, `t4g.large` $32.70, `c7g.large` $35.84, `m7g.large`
$42.56, `m7g.xlarge` $85.12, `m7g.2xlarge` $170.31. **Fit rule, `assumed`:** upper-bound CPU at
most 70% of the vCPUs; reservations + 0.5 GiB OS within 90% of RAM; burstable only below baseline
(0.4 and 0.6 core, 0005b §4) and only for a service that can lag without loss. Reservations:
`feed` 0.1 GiB, `store` 0.25, `api` 0.2, `web` 0.5, proxy 0.5 GiB and 0.05 core, Redis 3 GiB
(0005) and 13 GiB at 10×. CPU ranges include one viewer, on the smallest and largest ladder.

| Unit | CPU, 1× | 1×: smallest that fits | CPU, 10× | 10×: smallest that fits |
|---|---|---|---|---|
| `feed` alone | 0.52–0.71 | `c7g.large` **$35.84**; not burstable (criterion 1) | 5.2–7.1 | `m7g.2xlarge` $170.31 at the lower bound; **none priced** at the upper |
| `store` alone | 0.28–0.51 | `t4g.large` **$32.70**; the one service allowed credits | 2.8–5.1 | `m7g.xlarge` $85.12 → `m7g.2xlarge` $170.31 |
| `api` alone | 0.31–0.63 | `c7g.large` **$35.84**; viewers spike | 2.6–5.0 | `m7g.xlarge` → `m7g.2xlarge` |
| `web` alone | ~0 | `t4g.medium` **$16.35** | ~0 | `t4g.medium` $16.35 |
| Redis alone | 0.05 | `t4g.medium` **$16.35**, 3.5 of 4 GiB | 0.5 | `m7g.xlarge` **$85.12**, 13.5 of 16 GiB |
| **All six, one box** (R4) | 1.21–1.95 | `m7g.large` $42.56 lower; **`m7g.xlarge` $85.12** upper | 11.1–17.7 | **none priced**: the largest is 8 vCPU |
| `feed`+Redis ∣ the rest | 0.76 ∣ 1.19 | `c7g.large` ×2 **$71.68** (Redis at the memory limit) | 5.7–7.6 ∣ 5.4–10.2 | none ∣ `m7g.2xlarge` lower |
| `feed`+`store`+Redis ∣ `api`+`web`+proxy | 1.27 ∣ 0.68 | `m7g.large` + `c7g.large` **$78.40** | 8.5–12.7 ∣ 2.6–5.1 | none ∣ `m7g.xlarge` → `m7g.2xlarge` |
| One per service | | **$137.08** | | ≥ **$442.02**, `feed` unpriced at the upper |

**0005's own trigger is met.** It re-costs "if the split costs materially more than 30.89% of a
core"; the split is 1.10–1.75 cores, and its 10× `m7g.2xlarge` carries neither bound.

## 9. The share I least believe: `api`

**What drives it was zero when it was measured**: R4, m1 and py-spy all had nothing watched, and
m2 is one sample on the smallest ladder (36 rows; the largest has 101). **Nine tenths of it is the
decode**, known only to a factor of two. **Its py-spy share rests on the fewest samples**, 413 of
4,349, under a profiler that failed 2,550 reads that need not fall evenly across the code.
**Runner-up: `feed`.** Derived 0.52 disagrees with I2's `measured` 0.71, and I2's bus-off control
(40.77%) cost more than today's whole monolith. That is unexplained, so the sizing uses 0.71.

## 10. Left open for later

After #65, I13 (#79) replaces every `derived` cell in §4 and §5 with `measured` per container and
notes each that moved by more than 25%: `api` at 0, 1 and 3 viewers first, then the decode with
`BLOCK` reads on Linux, then `feed` against 0.71.

## What I learned

**Name a service by what it produces and you guess the wrong resource.** "The store" sounds like
a disk job. It writes 2.33 KB a second, which a disk never notices. What it spends is CPU:
folding 1,850 events a second into bars and, after the split, decoding every one of them first.
Ask what a service does *per event*, and multiply by the rate.

**Splitting a process can multiply its CPU, not divide it.** Inside one process an event is
handed over by reference. Across Redis it is written as text once and read back as text by
every reader. So 0.31 of a core becomes 1.1–1.75. The bus buys independent deploys and a store
that survives a restart, and it charges for them per event, per reader.

**A bigger box cannot help a program that uses one core.** Python runs one thread of Python code
at a time, and our services are one asyncio loop each. At 10× the feed has five to seven cores
of work and can use one core per process. So the first answer is more processes, split by
underlying, and instance size follows from how many. 0005 multiplied the monolith's core by ten;
this is the step that was missing.

**The least-believed number is the one whose driver you never varied.** Every monolith CPU figure
was taken with no browser open, and the browser drives `api`: one viewer added 5.5 points. Trust
a measurement for the conditions it saw. And a profiler can move its own reading: 34% without
py-spy, 53% with. So py-spy gave the *proportions* and the plain meter the *total*.
