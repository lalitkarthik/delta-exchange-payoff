"""I2: what does the Redis bus cost against the live feed, at three batch intervals?

Answers exactly the two questions #61 asks a measurement to settle and nothing else:

  1. **The batch interval.** At 10, 50 and 100 ms: what the publisher costs, and what the
     interval adds between an event being published and a consumer holding it. One of the
     three is chosen on this evidence.
  2. **The memory.** What Redis holds after thirty minutes of live BTC+ETH at the decided
     retention, read off `INFO memory` `used_memory`.

**Two processes, because one is a lie.** The first version of this tool published and
consumed in one process and reported 445 µs an entry against #69's `measured` 31.8 µs. The
difference was not Redis: decoding two consumers' worth of events at 1,700 events a second
saturated the core, and `await pipe.execute()` was then measuring how long the event loop
took to come back rather than how long Redis took to answer. The system this measures is
three processes; measuring it as one measures the wrong thing. So:

    # a Redis of its own, so the suite's container on 6399 is untouched
    docker run -d --rm --name i2-bus-live-6398 -p 6398:6379 redis:7-alpine \\
      redis-server --save "" --appendonly no --maxmemory 1gb --maxmemory-policy noeviction

    # the consumer first: it must be reading before the publisher starts
    engine/.venv/Scripts/python.exe tools/measure_bus_live.py --role consumer
    engine/.venv/Scripts/python.exe tools/measure_bus_live.py --role publisher

    docker rm -f i2-bus-live-6398

**`--maxmemory 1gb`, not the 2gb `redis-hosting.md` §3 fixes for prod**, because this
laptop does not have 2 GB to hand a container. Nothing here comes near either ceiling and
the number this run produces is what says so.

**How the two processes share a clock.** `time.perf_counter()` is
`QueryPerformanceCounter` on Windows and `CLOCK_MONOTONIC` on Linux — system-wide on both,
and `measured` here as agreeing between two processes to well under a millisecond. So the
publisher stamps and the consumer subtracts, and the latency is monotonic end to end.
It never uses `ts_received`, which comes off `time.time()` and steps backwards under an
NTP correction.

**How the join works without touching the wire.** One event in `--sample` is stamped, and
which one is decided by the event's own id — `int(event_id[:8], 16) % sample == 0` — so a
consumer knows which events to expect a stamp for without being told. The stamps travel on
a stream of their own, `{env}:stamps`, at `derived` 92 entries a second against the feed's
1,850: the measurement's own load is 5% of the traffic it measures and is on a key no
consumer under test reads.

**Its own process, its own socket, its own Redis.** The engine on :8000 keeps running and
is untouched. Nothing here writes to `data/`: there is no `BarWriter` in this file.

Every number printed is `measured` on this machine, this run, unless the line says
`derived`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))

from deltapayoff.adapters import DeltaAdapter, DeltaFeed  # noqa: E402
from deltapayoff.delta_client import DeltaClient  # noqa: E402
from deltapayoff.events import Event  # noqa: E402
from deltapayoff.redis_bus import BusConfig, RedisBus  # noqa: E402
from deltapayoff.supervisor import FeedSupervisor  # noqa: E402

DEFAULT_URL = "redis://127.0.0.1:6398"

#: The engine's own subscription shapes, so the readers under measurement are the readers
#: that will run: `store.QUEUE_WATERMARK` and `ChainStream.attach`'s default.
STORE_WATERMARK = 5_000
SCREEN_CAPACITY = 10_000

DONE = "done"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def sampled(event_id: str, sample: int) -> bool:
    """Whether this event carries a stamp. **Decided by the id**, so that a publisher and
    a consumer that never speak agree about which events are in the sample."""
    try:
        return int(event_id[:8], 16) % sample == 0
    except ValueError:  # pragma: no cover - an id that is not hex is not ours
        return False


# ------------------------------------------------------------------ the publisher


async def publish_role(args: argparse.Namespace) -> None:
    """The `feed` service, with a stopwatch: subscribe the venue, publish every event."""
    import redis.asyncio

    underlyings = tuple(name.strip().upper() for name in args.underlyings.split(","))
    client = DeltaClient()
    await client.__aenter__()
    adapter = DeltaAdapter(
        client=client, underlyings=underlyings, feed_factory=DeltaFeed
    )
    listed = 0
    for underlying in underlyings:
        instruments = await adapter.instruments(underlying)
        adapter.subscribe(instruments)
        listed += len(instruments)
    print(f"subscribed {listed} contracts over {'+'.join(underlyings)}, both channels")

    side = redis.asyncio.Redis.from_url(args.url)
    await side.set(f"{args.env}:phase", "control")

    bus: RedisBus | None = None
    stamps: list[tuple[str, str]] = []
    counters = {"published": 0}

    def sink(event: Event) -> None:
        counters["published"] += 1
        if bus is None:
            return
        if sampled(event.event_id, args.sample):
            stamps.append((event.event_id, repr(time.perf_counter())))
        bus.publish(event)

    supervisor = FeedSupervisor([adapter], sink)
    supervisor.start()

    async def drain_stamps() -> None:
        """Write the sample's stamps beside the batch that carried them."""
        while True:
            await asyncio.sleep(0.2)
            if not stamps:
                continue
            batch, stamps[:] = list(stamps), []
            pipe = side.pipeline(transaction=False)
            for event_id, at in batch:
                pipe.xadd(f"{args.env}:stamps", {"id": event_id, "t": at})
            pipe.xtrim(f"{args.env}:stamps", maxlen=200_000, approximate=True)
            await pipe.execute()

    stamp_task = asyncio.create_task(drain_stamps(), name="stamps")
    results: list[dict] = []

    try:
        for batch_ms in args.phases:
            label = "control" if batch_ms is None else str(batch_ms)
            await side.set(f"{args.env}:phase", label)
            if batch_ms is not None:
                config = BusConfig(
                    url=args.url,
                    env=args.env,
                    underlyings=underlyings,
                    batch_ms=batch_ms,
                )
                if bus is None:
                    bus = RedisBus(config)
                    await bus.start()
                else:
                    # Same connection, same streams, same accumulated retention. Only the
                    # interval moves, which is the one thing under test — and the flusher
                    # reads it once a tick, not once a task, for exactly this.
                    bus.config = config

            before = bus.publisher() if bus is not None else {}
            counters["published"] = 0
            started = time.perf_counter()
            cpu_started = time.process_time()

            for minute in range(int(args.minutes)):
                await asyncio.sleep(60)
                elapsed = time.perf_counter() - started
                print(
                    f"  {label:<8} minute {minute + 1:>2}/{int(args.minutes)}  "
                    f"{counters['published'] / elapsed:7.1f} events/s  "
                    f"cpu {(time.process_time() - cpu_started) / elapsed * 100:5.1f}% "
                    f"of a core",
                    flush=True,
                )
            # A fractional `--minutes` is for smoke runs; the phase still lasts exactly
            # what was asked for rather than the whole minutes inside it.
            remainder = args.minutes * 60 - (time.perf_counter() - started)
            if remainder > 0:
                await asyncio.sleep(remainder)
            elapsed = time.perf_counter() - started
            cpu = time.process_time() - cpu_started

            phase: dict = {
                "phase": label,
                "batch_ms": batch_ms,
                "elapsed_seconds": round(elapsed, 1),
                "published": counters["published"],
                "events_per_second": round(counters["published"] / elapsed, 1),
                "publisher_cpu_seconds": round(cpu, 2),
                "publisher_cpu_percent_of_a_core": round(cpu / elapsed * 100, 2),
            }
            if bus is not None:
                after = bus.publisher()
                written = after["written"] - before.get("written", 0)
                flushed = after["flush_seconds"] - before.get("flush_seconds", 0.0)
                batches = after["batches"] - before.get("batches", 0)
                phase.update(
                    written=written,
                    batches=batches,
                    trims=after["trims"] - before.get("trims", 0),
                    failures=after["failures"] - before.get("failures", 0),
                    unroutable=after["unroutable"] - before.get("unroutable", 0),
                    outbox_at_end=after["outbox"],
                    entries_per_batch=round(written / batches, 1) if batches else 0,
                    flush_seconds=round(flushed, 3),
                    flush_micros_per_entry=round(flushed / written * 1e6, 1)
                    if written
                    else 0.0,
                    flush_peak_ms=round(after["flush_peak_seconds"] * 1000, 2),
                )
                info = await bus.client.info("memory")
                phase["used_memory_bytes"] = int(info["used_memory"])
                phase["used_memory_mib"] = round(int(info["used_memory"]) / 1048576, 1)
            results.append(phase)
            print(json.dumps(phase, indent=2), flush=True)
            args.out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    finally:
        await side.set(f"{args.env}:phase", DONE)
        stamp_task.cancel()
        await asyncio.gather(stamp_task, return_exceptions=True)
        await supervisor.aclose()
        if bus is not None:
            await bus.aclose()
        await side.aclose()
        await client.aclose()

    print("\npublisher phases")
    print(
        f"{'phase':<10} {'events/s':>9} {'CPU %core':>10} {'batches':>8} "
        f"{'entries/batch':>14} {'µs/entry':>9} {'used_memory MiB':>16}"
    )
    for phase in results:
        print(
            f"{phase['phase']:<10} {phase['events_per_second']:>9.1f} "
            f"{phase['publisher_cpu_percent_of_a_core']:>10.2f} "
            f"{phase.get('batches', 0):>8} {phase.get('entries_per_batch', 0):>14.1f} "
            f"{phase.get('flush_micros_per_entry', 0):>9.1f} "
            f"{phase.get('used_memory_mib', 0):>16.1f}"
        )
    print(f"\nWritten to {args.out}")


# ------------------------------------------------------------------ the consumer


async def consume_role(args: argparse.Namespace) -> None:
    """The `store` and the `api` with a stopwatch: subscribe, decode, time arrival."""
    import redis.asyncio

    underlyings = tuple(name.strip().upper() for name in args.underlyings.split(","))
    config = BusConfig(
        url=args.url,
        env=args.env,
        underlyings=underlyings,
        read_block_ms=args.read_block_ms,
    )
    bus = RedisBus(config)
    await bus.start()
    wanted = [name.strip() for name in args.subscriptions.split(",") if name.strip()]
    store = (
        bus.subscribe(
            "store",
            maxsize=STORE_WATERMARK,
            lossless=True,
            # **An arrival-lag harness must never replay, so it says so (#102).** `"0"`
            # would create this group at the bottom and hand it the whole retained window
            # at Redis's replay speed — throughput no venue produces, and entries whose
            # age is up to thirty minutes. `"$"` has been the default since #97, but a
            # default is not a measurement protocol: the number this tool writes down has
            # to depend on what it stated, not on what it inherited.
            group_start="$",
        )
        if "store" in wanted
        else None
    )
    screen = (
        bus.subscribe("screen", maxsize=SCREEN_CAPACITY) if "screen" in wanted else None
    )
    await bus.ready()
    print(
        f"reading {len(config.streams())} streams under {args.env!r}; waiting",
        flush=True,
    )

    side = redis.asyncio.Redis.from_url(args.url)
    phase = {"label": "control"}
    #: Receipt times for sampled events not yet joined, and stamps not yet matched. Both
    #: are popped on the join, so neither grows: a stamp always has a receipt or the event
    #: was lost, and either way it leaves within one sweep.
    receipts: dict[str, tuple[float, str]] = {}
    pending: dict[str, float] = {}
    latencies: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    cpu = {"start": time.process_time(), "at": time.perf_counter()}

    def join(event_id: str) -> None:
        got = receipts.pop(event_id, None)
        published = pending.pop(event_id, None)
        if got is None or published is None:
            if got is not None:
                receipts[event_id] = got
            elif published is not None:
                pending[event_id] = published
            return
        received, label = got
        latencies.setdefault(label, []).append((received - published) * 1000)

    async def read_events(subscription, timed: bool) -> None:
        while True:
            event = await subscription.queue.get()
            counts[subscription.name] = counts.get(subscription.name, 0) + 1
            if timed and sampled(event.event_id, args.sample):
                receipts[event.event_id] = (time.perf_counter(), phase["label"])
                join(event.event_id)

    async def read_stamps() -> None:
        cursor = "$"
        key = f"{args.env}:stamps"
        while True:
            got = await side.xread({key: cursor}, count=2000, block=500)
            for _, entries in got or ():
                for entry_id, fields in entries:
                    cursor = entry_id.decode()
                    pending[fields[b"id"].decode()] = float(fields[b"t"])
                    join(fields[b"id"].decode())
            if not got:
                await asyncio.sleep(0.01)

    async def watch_phase() -> None:
        while True:
            raw = await side.get(f"{args.env}:phase")
            label = raw.decode() if raw else "control"
            if label != phase["label"]:
                print(
                    f"  phase {phase['label']} -> {label}; "
                    f"{len(latencies.get(phase['label'], [])):,} samples",
                    flush=True,
                )
                phase["label"] = label
            if label == DONE:
                return
            await asyncio.sleep(1.0)

    tasks = [asyncio.create_task(read_stamps(), name="c-stamps")]
    if store is not None:
        tasks.append(asyncio.create_task(read_events(store, True), name="c-store"))
    if screen is not None:
        tasks.append(
            asyncio.create_task(read_events(screen, store is None), name="c-screen")
        )
    try:
        await watch_phase()
    finally:
        elapsed = time.perf_counter() - cpu["at"]
        used = time.process_time() - cpu["start"]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stats = bus.stats()
        await bus.aclose()
        await side.aclose()

    summary = {
        "consumer_cpu_percent_of_a_core": round(used / elapsed * 100, 2),
        "elapsed_seconds": round(elapsed, 1),
        "received": counts,
        "unjoined_receipts": len(receipts),
        "unjoined_stamps": len(pending),
        "consumers": stats,
        "phases": {
            label: {
                "samples": len(values),
                "p50_ms": round(percentile(values, 0.50), 2),
                "p90_ms": round(percentile(values, 0.90), 2),
                "p99_ms": round(percentile(values, 0.99), 2),
                "max_ms": round(max(values), 2),
                "mean_ms": round(statistics.mean(values), 2),
            }
            for label, values in latencies.items()
            if values
        },
    }
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nWritten to {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("publisher", "consumer"), required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--env", default="i2", help="the key prefix for this run")
    parser.add_argument("--underlyings", default="BTC,ETH")
    parser.add_argument("--minutes", type=float, default=10.0, help="per phase")
    parser.add_argument(
        "--phases",
        default="control,10,50,100",
        help="comma-separated batch intervals in ms; `control` publishes nothing",
    )
    parser.add_argument(
        "--sample", type=int, default=20, help="one event in this many is stamped"
    )
    parser.add_argument(
        "--subscriptions",
        default="store",
        help="which consumers this process runs: store, screen, or both. **One per "
        "process by default**: two readers in one process is not the system under test, "
        "and on this laptop it is a reader that falls behind.",
    )
    parser.add_argument(
        "--read-block-ms",
        type=int,
        default=0,
        help="0 polls instead of blocking; see redis_bus.DEFAULT_READ_BLOCK_MS for the "
        "measurement behind the default here being 0 and the engine's being 500",
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    args.phases = [
        None if part.strip() == "control" else int(part)
        for part in args.phases.split(",")
        if part.strip()
    ]
    if args.out is None:
        research = ROOT / "docs" / "design" / "research"
        args.out = research / f"0061-batch-interval-{args.role}.json"
    if args.role == "publisher":
        asyncio.run(publish_role(args))
    else:
        asyncio.run(consume_role(args))


if __name__ == "__main__":
    main()
