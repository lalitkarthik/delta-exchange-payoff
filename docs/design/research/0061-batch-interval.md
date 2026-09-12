# 0061 — The batch interval, and thirty minutes of memory

The measurement run behind #61 (I2). What was run, on what, what came out, and what each
number does and does not describe. The design it feeds is
[../lld/redis-bus.md](../lld/redis-bus.md); the tool is `tools/measure_bus_live.py` and the
raw output is [0061-batch-interval-publisher.json](0061-batch-interval-publisher.json) and
[0061-batch-interval-consumer.json](0061-batch-interval-consumer.json).

## The run

2026-09-09, 20:50–21:40 IST. One continuous session, four ten-minute phases, 774 live BTC
and ETH contracts on both channels, 3,330,235 events. `control` publishes nothing — it is
the same feed with the bus switched off, so subtracting it isolates the publisher.

Two processes, on one laptop, against a `redis:7-alpine` container of its own on port 6398
started with `--save "" --appendonly no --maxmemory 1gb --maxmemory-policy noeviction`. The
engine on :8000 and the web app on :3000 kept running throughout and were not touched;
nothing here wrote to `data/`.

**The publisher is one process and the consumer is another, because one process is a lie.**
The first version of the tool did both and reported 445 µs an entry against #69's `measured`
31.8 µs. The difference was not Redis: decoding two consumers' worth of events at 1,850 a
second saturated the core, and `await pipe.execute()` was measuring how long the event loop
took to come back. The system being designed is three processes; measuring it as one
measures something else.

**Clock.** `time.perf_counter()` is `QueryPerformanceCounter` on Windows and
`CLOCK_MONOTONIC` on Linux, system-wide on both — `measured` here as agreeing between two
processes to well under a millisecond. The publisher stamps one event in twenty, chosen by
`int(event_id[:8], 16) % 20`, so the consumer knows which to expect without being told; the
stamps ride a stream of their own at `derived` 92 entries a second, 5% of the traffic under
test, on a key no consumer under test reads. `ts_received` is never used for this: it comes
off `time.time()`, which steps backwards under an NTP correction.

## What came out

| Phase | Events/s | Publisher CPU | Batches | Entries/batch | Achieved period | p50 | p90 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| control | 1,843.1 | 40.77% of a core | — | — | — | — | — | — | — |
| 10 ms | 1,851.6 | 65.10% | 8,611 | 129.1 | 69.7 ms | 230.1 | 616.9 | 842.4 | 1,107.0 |
| 50 ms | 1,835.1 | 70.73% | 6,233 | 177.1 | 96.4 ms | 195.2 | 632.8 | 847.8 | 1,156.8 |
| 100 ms | 1,854.1 | 70.33% | 4,239 | 262.5 | 141.7 ms | 235.3 | 607.2 | 830.6 | 1,157.9 |

All `measured`. Latency in milliseconds, publish → consumer receipt, ~55,000 samples per
phase. `Achieved period` is `derived`: elapsed ÷ batches.

Consumer side, whole run: 3,330,235 events, **0 dropped, 0 skipped, 0 undecodable, 0
over-capacity**, deepest backlog 1,668, 45.43% of a core. Publisher side: **0 failed
batches, 0 unroutable events, an empty outbox at every phase boundary**, 41,651 trims.

## 50 ms, and why

**The latencies do not order by interval** — 230, 195, 235 ms at p50; 842, 848, 831 at p99 —
which is the finding, not noise to be explained away. At this rate on this machine the
interval is not the dominant term in the latency; the feed's own decode work is. What does
order, cleanly, is the achieved period.

- **10 ms is not honoured.** The process cannot flush faster than `derived` 69.7 ms whatever
  it is asked for, so configuring 10 ms would be a setting that says something untrue about
  the system and leaves no headroom to tune when it matters.
- **100 ms costs 45 ms of period over 50 ms and bought nothing back.** 70.33% against 70.73%
  of a core is inside the spread between two phases of the same run, and there is one run
  per interval — this is not a difference worth 45 ms.
- **50 ms is the largest request still within about 1.4x of what the process achieves**, and
  177 entries a batch is the shape #69 `measured` in isolation at 37 µs an entry.

The bus costs the publisher `derived` **24.33 points of a core** at 10 ms and **29.96 at 50 ms**
— control 40.77% against 65.10% and 70.73% — for encoding, the pipeline and the trim over
1,850 events a second. **29.96 is the chosen interval's figure and the one to size `feed` from**
([../decisions/0007-load-profile.md](../decisions/0007-load-profile.md)); this line read 29.6
until #95, which is 70.73 − 40.77 rounded wrong rather than a different quantity.

**Flush wall time is not Redis's cost.** `measured` 301.8, 278.0 and 254.2 µs an entry
across the three phases against #69's isolated 31.8 µs: `await pipe.execute()` yields, the
socket reader and the decode run inside that await, and the wall clock counts them. It is
reported as the publisher's flush latency in situ and never as a Redis figure.

## Memory: 1,056.4 MiB at thirty minutes

`measured` **1,107,735,240 B**, `INFO memory` `used_memory`, after the three bus phases ran
back to back — thirty continuous minutes of live BTC+ETH — at 1,831,703 entries on the
busiest of the sixteen keys. #58 `derived` 1,051.5 **MiB** for this encoding at this rate
before any of it existed; the run came in **0.5% above it**. The unit is binary and the
arithmetic settles it: `derived` 598.2 KiB/s = 612,556.8 B/s over the 1,800 s window is
1,102,577,664 B, and 1,102,577,664 ÷ 1,048,576 = 1,051.5 exactly. This file wrote MB until #95.

**The container started at `--maxmemory 1gb` and was raised to 1.5gb mid-run**, at minute 18
with `used_memory` at 642.9 MiB, because the ten-minute reading of 354.5 MiB projected past
1 GiB before minute 30. That is the finding rather than a workaround: **1 GiB is not enough
for thirty minutes of BTC+ETH and #69's `maxmemory 2gb` is.** Under `noeviction` the ceiling
would have become `OOM` errors at the publisher at about minute 29 — loud, at the writer,
which is where `redis-hosting.md` §3 wants it.

Retention never bound it: the floor is thirty minutes, so the first entry aged out as the
run ended. Every one of the 41,651 trims before that was a no-op, at no measurable cost, as
#69 said it would be.

## Caveats, in the order they would mislead someone

1. **Docker Desktop for Windows routes loopback through a WSL2 VM.** Every figure here pays
   a port-forward hop: an upper bound on a co-located container and a floor for anything
   with a network hop, exactly as #69 records.
2. **A blocked reader on this host wakes late.** `measured`: an `XREAD` blocked on this
   setup returns about **24 ms** after the entry it was waiting for, against **1.1 ms** for a
   poll that finds one, and an idle `BLOCK n` costs about `2n`. At 1,850 events a second that
   is the difference between a reader that keeps up and one that never catches up, so the
   consumer ran with `read_block_ms=0` — polling. **The engine's default stays `BLOCK 500`**,
   because prod is Linux beside the Redis.
3. **One run per interval.** Differences of a few points of a core are not distinguishable
   from run-to-run variation, and the choice above does not lean on any of them.
4. **The publisher process was also the feed.** That is the shape `feed` will have, so it is
   the right process to measure — but it means every latency here carries the decode work of
   774 contracts, and a quieter venue would look very different.
5. **One socket drop during `control` and one during `50 ms`**, both redialled by the
   controller inside a second and visible in the run log. Neither phase's event rate moved
   outside 1,835–1,854/s, so neither is excluded.
6. **This run predates the default that keeps the consumer group off the retained window,
   and the run's own counters are what clear it — not the protocol (#102).** The tool
   subscribed losslessly while stating no `group_start`, which at this commit resolved to
   `"0"`: *deliver everything Redis still holds*. Against a non-empty stream that would
   have drained up to the thirty-minute window at Redis's replay speed before the first
   live entry. It did not happen here, and three `measured` figures above say so
   independently of anyone's memory of how the run was launched:
   - **`unjoined_receipts` is 20.** A replayed entry cannot become a latency sample —
     `read_stamps` reads the stamps key from `"$"`, so a replayed entry's stamp is behind
     the cursor and its receipt is never joined. Every sampled entry of a replay therefore
     lands in this counter. Thirty minutes at 1,850 events a second, sampled 1-in-20, is
     `derived` **~166,500**. Twenty is end-of-run raggedness.
   - **`received` 3,330,235 against `written` 3,328,455** — an excess of 1,780, about a
     second of traffic, where a replay would have shown up to 3.3 million.
   - **The consumer ran 2,416.9 s against the four phases' 2,402.1 s**, so it was reading
     14.8 s before the publisher began: a container of its own on 6398, an empty stream,
     nothing to replay.

   **No figure in this record is compromised.** The tool now states `group_start="$"` at
   its call site, so figures taken after #97 are comparable with these and no longer
   depend on which way the default happens to be pointing.

## What would change the choice

- **A native host.** The flush cost falls, the achieved period approaches the request, and
  10 ms becomes a real setting rather than a wish. Re-run this tool on the deployed `feed`
  before quoting any of these numbers as production figures.
- **`feed` alone in its process.** These numbers are one process doing the socket, the
  decode and the publish. After I5 that is exactly what `feed` is, so the CPU column should
  be re-read then; the split does not change the memory column at all.
- **Ten times the rate.** `derived` **10.269 GiB** at thirty minutes — ten times the
  1,102,577,664 B above — needs a different node before it needs a different interval.
  10.3 GiB, which [../decisions/0007-load-profile.md](../decisions/0007-load-profile.md)
  carries, is that rounded; `../cloud/compute.md` §8 now writes 10.269. **This line read 10.5 GiB until #95**: that
  was 10,515 "M" divided by 1,000, a decimal divide of a binary quantity, and it is the third
  figure #95 found for one number.
