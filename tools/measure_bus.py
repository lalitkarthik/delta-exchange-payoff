"""Spike: our in-process FanOut against Redis Streams, on real decoded frames.

Answers three questions and nothing else:

  1. What does one message cost, end to end, on each transport?
  2. What sustained rate can each hold?
  3. What happens when the consumer falls behind?

Run from `engine/` with its venv. Redis is expected on localhost:6399.
Every number printed is `measured` on this machine, this run.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

functools_print = print
from pathlib import Path

sys.path.insert(0, "src")

import redis

from deltapayoff.fanout import FanOut

REDIS_URL = "redis://127.0.0.1:6399"
STREAM = "spike:md.option_quotes"


def load_frames() -> list[dict]:
    root = Path("tests/fixtures/ws-ob-l2-04-09-2026.json")
    raw = json.loads(root.read_text())["frames"]
    frames = list(raw.values()) if isinstance(raw, dict) else list(raw)
    return frames


def pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


# ---------------------------------------------------------------- 1. per message

async def fanout_latency(frames: list[dict], n: int) -> list[float]:
    """Publish -> consumer has the object. No serialisation: the object is shared."""
    bus = FanOut()
    sub = bus.subscribe("spike", maxsize=1000)
    lat = []
    for i in range(n):
        f = frames[i % len(frames)]
        t0 = time.perf_counter()
        bus.publish(f)
        got = await sub.queue.get()
        lat.append((time.perf_counter() - t0) * 1e6)
        assert got is f
    return lat


def redis_latency(r: redis.Redis, frames: list[dict], n: int) -> list[float]:
    """XADD -> XREAD returns it. Serialise, hop to Redis, hop back, deserialise."""
    r.delete(STREAM)
    lat = []
    last = "0-0"
    for i in range(n):
        f = frames[i % len(frames)]
        t0 = time.perf_counter()
        r.xadd(STREAM, {"p": json.dumps(f)}, maxlen=10_000, approximate=True)
        out = r.xread({STREAM: last}, count=1, block=1000)
        _, entries = out[0]
        last = entries[0][0]
        json.loads(entries[0][1][b"p"])
        lat.append((time.perf_counter() - t0) * 1e6)
    return lat


# ---------------------------------------------------------------- 2. sustained rate

async def fanout_throughput(frames: list[dict], n: int) -> float:
    bus = FanOut()
    # Both lossless: this measures transport cost, not the overflow policy.
    # A drop-oldest queue would discard most of `n` and the drain would never finish.
    sub = bus.subscribe("screen", maxsize=1000, lossless=True)
    writer = bus.subscribe("bars", maxsize=1000, lossless=True)
    t0 = time.perf_counter()
    for i in range(n):
        bus.publish(frames[i % len(frames)])
    for _ in range(n):
        await sub.queue.get()
        await writer.queue.get()
    return n / (time.perf_counter() - t0)


def redis_throughput(r: redis.Redis, frames: list[dict], n: int) -> tuple[float, float]:
    """Pipelined writes, then drain. Two numbers: publish-only, and round trip."""
    r.delete(STREAM)
    payloads = [{"p": json.dumps(frames[i % len(frames)])} for i in range(n)]
    t0 = time.perf_counter()
    pipe = r.pipeline(transaction=False)
    for i, p in enumerate(payloads, 1):
        pipe.xadd(STREAM, p, maxlen=100_000, approximate=True)
        if i % 1000 == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)
    pipe.execute()
    write_rate = n / (time.perf_counter() - t0)

    t1 = time.perf_counter()
    last, seen = "0-0", 0
    while seen < n:
        out = r.xread({STREAM: last}, count=1000, block=1000)
        if not out:
            break
        _, entries = out[0]
        for eid, fields in entries:
            json.loads(fields[b"p"])
            last = eid
            seen += 1
    return write_rate, n / (time.perf_counter() - t1)


# ---------------------------------------------------------------- 3. falling behind

async def fanout_slow_consumer(frames: list[dict], n: int, cap: int) -> dict:
    """Producer runs; consumer never reads. What does each policy report?"""
    bus = FanOut()
    lossy = bus.subscribe("screen", maxsize=cap)
    lossless = bus.subscribe("bars", maxsize=cap, lossless=True)
    for i in range(n):
        bus.publish(frames[i % len(frames)])
    return {
        "lossy_holds": lossy.queue.qsize(),
        "lossy_dropped": lossy.dropped,
        "lossless_holds": lossless.queue.qsize(),
        "lossless_dropped": lossless.dropped,
        "lossless_over_capacity": lossless.over_capacity,
    }


def redis_slow_consumer(r: redis.Redis, frames: list[dict], n: int, cap: int) -> dict:
    """Same shape: a capped stream, a consumer that never reads, then count."""
    r.delete(STREAM)
    try:
        r.xgroup_create(STREAM, "bars", id="0", mkstream=True)
    except redis.ResponseError:
        pass
    pipe = r.pipeline(transaction=False)
    for i in range(n):
        pipe.xadd(STREAM, {"p": json.dumps(frames[i % len(frames)])},
                  maxlen=cap, approximate=False)
        if (i + 1) % 1000 == 0:
            pipe.execute()
            pipe = r.pipeline(transaction=False)
    pipe.execute()
    held = r.xlen(STREAM)
    readable = len(r.xrange(STREAM, "-", "+"))
    return {"published": n, "stream_holds": held, "readable": readable,
            "unreadable_forever": n - readable}


# ---------------------------------------------------------------- main

async def main() -> None:
    frames = load_frames()
    size = len(json.dumps(frames[0]).encode())
    r = redis.Redis.from_url(REDIS_URL)
    r.ping()

    print(f"frames in fixture: {len(frames)}   one frame as JSON: {size} bytes")
    print(f"redis: {r.info('server')['redis_version']} at {REDIS_URL}\n")

    N = 3000
    print("=" * 68)
    print("1. ONE MESSAGE, END TO END  (publish -> consumer has it)")
    print("=" * 68)
    fo = await fanout_latency(frames, N)
    rd = redis_latency(r, frames, N)
    print(f"{'transport':<22}{'median':>12}{'p95':>12}{'p99':>12}")
    for name, lat in (("in-process FanOut", fo), ("Redis Streams", rd)):
        print(f"{name:<22}{statistics.median(lat):>11.1f}u{pct(lat,0.95):>11.1f}u"
              f"{pct(lat,0.99):>11.1f}u")
    print(f"\nRedis is {statistics.median(rd)/statistics.median(fo):.0f}x the "
          f"in-process cost, per message.")

    N2 = 20000
    print("\n" + "=" * 68)
    print("2. SUSTAINED RATE  (how many frames a second can it take?)")
    print("=" * 68)
    fo_rate = await fanout_throughput(frames, N2)
    w_rate, r_rate = redis_throughput(r, frames, N2)
    print(f"{'in-process FanOut (2 consumers)':<38}{fo_rate:>12,.0f} /s")
    print(f"{'Redis Streams, pipelined write':<38}{w_rate:>12,.0f} /s")
    print(f"{'Redis Streams, batched read':<38}{r_rate:>12,.0f} /s")
    print(f"\nour live feed needs {1323:,} /s")

    print("\n" + "=" * 68)
    print("3. WHEN THE CONSUMER FALLS BEHIND  (10,000 published, cap 1,000)")
    print("=" * 68)
    a = await fanout_slow_consumer(frames, 10_000, 1_000)
    b = redis_slow_consumer(r, frames, 10_000, 1_000)
    print("FanOut, drop-oldest policy :", a["lossy_holds"], "held,",
          a["lossy_dropped"], "dropped and COUNTED")
    print("FanOut, lossless policy    :", a["lossless_holds"], "held,",
          a["lossless_dropped"], "dropped,",
          a["lossless_over_capacity"], "over-capacity offers COUNTED")
    print("Redis Stream, maxlen 1000  :", b["stream_holds"], "held,",
          b["unreadable_forever"], "trimmed away and UNREADABLE")
    r.delete(STREAM)


if __name__ == "__main__":
    import functools
    print = functools.partial(functools_print, flush=True)
    asyncio.run(main())
