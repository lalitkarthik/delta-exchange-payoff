"""R5: what does `XADD` cost over loopback, and what does the shipped Redis persist?

Answers exactly the two questions #69 can answer without cloud access, and nothing else:

  1. What does the official `redis:7-alpine` image persist to disk if nobody tells it
     not to — and what does turning persistence off look like?
  2. What does one `XADD` cost over loopback, unpipelined and pipelined at the batch
     shapes I2 (#61) will choose between, at R1's decided encoding?

Run from `engine/`:

    .venv/Scripts/python.exe ../tools/measure_redis_hosting.py

Needs a throwaway Redis on `127.0.0.1:6399`:

    docker run -d --rm --name r5-redis-6399 -p 6399:6379 redis:7-alpine
    docker rm -f r5-redis-6399

**Read this caveat before quoting any absolute number.** Docker Desktop on Windows routes
localhost through a WSL2 VM, so every Redis call pays a port-forward hop. The vault's
spike
write-up says it first: "Native Linux Redis over loopback or a unix socket is typically
several times faster. The ratios and the batching lesson transfer; the microsecond figures
do not." Every figure below is therefore an **upper bound** on a co-located container's
cost, and a floor no managed Redis over a VPC hop can beat.

The encoding is R1's decision B, `json:envelope-flat`: the envelope flat, one Redis field
per key, the type's own keys as one JSON `payload` field. The encoder is imported from
`measure_payload_size.py` rather than restated, so the two runs cannot drift.

Every number printed is `measured` on this machine, this run.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import redis  # noqa: E402
from measure_payload_size import (  # noqa: E402
    REDIS_URL,
    encode_json_envelope_flat,
    real_events,
)

#: `derived`, #58: 1,849.8 events/s over BTC+ETH — 1,537.4 book frames, 156.2 ticker
#: frames, and a ticker frame is two events. The mix decides the entry sizes we send.
EVENTS_PER_SECOND = 1849.8
MIX = {"md.option_quote": 1537.4, "md.option_reference": 156.2, "md.index_quote": 156.2}

#: The batch intervals #61 names, in milliseconds, and the entries each holds at our rate.
BATCH_INTERVALS_MS = (10, 50, 100)

RETENTION_SECONDS = 30 * 60
KEY = "r5:md.option_quote:DELTA:BTC"
UNPIPELINED_N = 2000
BATCHES_PER_SHAPE = 200


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def entry_stream() -> list[dict[str, bytes]]:
    """One second of our real traffic, encoded, in the measured type mix."""
    events = real_events()
    out: list[dict[str, bytes]] = []
    for name, per_second in MIX.items():
        pool = events[name]
        for i in range(round(per_second)):
            out.append(encode_json_envelope_flat(pool[i % len(pool)]))
    # Interleave so a batch is never all one type, which is how the feed emits.
    out.sort(key=lambda fields: fields["event_id"])
    return out


def connect() -> redis.Redis:
    conn = redis.Redis.from_url(REDIS_URL)
    conn.ping()
    return conn


# ------------------------------------------------------ 1. what it persists as shipped


def shipped_persistence(conn: redis.Redis) -> None:
    print("\n1. What `redis:7-alpine` persists if nobody says otherwise")
    print("   " + "-" * 66)
    for name in ("save", "appendonly", "appendfsync", "maxmemory", "maxmemory-policy"):
        value = conn.config_get(name).get(name, "")
        print(f"   as shipped   {name:<18} {value!r}")
    info = conn.info("persistence")
    print(f"   as shipped   rdb_bgsave_in_progress  {info['rdb_bgsave_in_progress']}")
    print(f"   as shipped   aof_enabled             {info['aof_enabled']}")

    print("\n   The pipe's configuration - no AOF, no RDB - applied live:")
    conn.config_set("save", "")
    conn.config_set("appendonly", "no")
    for name in ("save", "appendonly"):
        value = conn.config_get(name).get(name, "")
        print(f"   as a pipe    {name:<18} {value!r}")


# ------------------------------------------------------------- 2. unpipelined XADD


def unpipelined(conn: redis.Redis, entries: list[dict[str, bytes]]) -> list[float]:
    print("\n2. One `XADD`, unpipelined, over loopback")
    print("   " + "-" * 66)
    conn.delete(KEY)
    latencies: list[float] = []
    for i in range(UNPIPELINED_N):
        fields = entries[i % len(entries)]
        start = time.perf_counter()
        conn.xadd(KEY, fields)
        latencies.append((time.perf_counter() - start) * 1e6)
    core = statistics.median(latencies) * EVENTS_PER_SECOND / 1e6 * 100
    print(f"   n                    {UNPIPELINED_N}")
    print(f"   p50                  {statistics.median(latencies):>9.1f} us")
    print(f"   p95                  {pct(latencies, 0.95):>9.1f} us")
    print(f"   p99                  {pct(latencies, 0.99):>9.1f} us")
    print(f"   ceiling              {1e6 / statistics.median(latencies):>9.0f} entries/s")
    print(f"   at 1,849.8 events/s  {core:>9.1f}% of a core")
    conn.delete(KEY)
    return latencies


# --------------------------------------------------------------- 3. pipelined batches


def pipelined(conn: redis.Redis, entries: list[dict[str, bytes]], trim: bool) -> None:
    label = "with `XTRIM MINID ~` on every batch" if trim else "no trim"
    section = '4' if trim else '3'
    print(f"\n{section}. Pipelined at the batch shapes #61 will choose between - {label}")
    print("   " + "-" * 66)
    print(f"   {'interval':>9} {'entries':>8} {'batch p50':>11} {'batch p99':>11}"
          f" {'per entry':>10} {'core':>8}")
    for interval_ms in BATCH_INTERVALS_MS:
        size = round(EVENTS_PER_SECOND * interval_ms / 1000)
        conn.delete(KEY)
        cursor = 0
        batch_us: list[float] = []
        for _ in range(BATCHES_PER_SHAPE):
            batch = [entries[(cursor + i) % len(entries)] for i in range(size)]
            cursor += size
            start = time.perf_counter()
            pipe = conn.pipeline(transaction=False)
            for fields in batch:
                pipe.xadd(KEY, fields)
            if trim:
                floor = int((time.time() - RETENTION_SECONDS) * 1000)
                pipe.xtrim(KEY, minid=floor, approximate=True)
            pipe.execute()
            batch_us.append((time.perf_counter() - start) * 1e6)
        per_entry = statistics.median(batch_us) / size
        core = per_entry * EVENTS_PER_SECOND / 1e6 * 100
        p50_ms = statistics.median(batch_us) / 1000
        p99_ms = pct(batch_us, 0.99) / 1000
        print(
            f"   {interval_ms:>7} ms {size:>8} {p50_ms:>8.2f} ms"
            f" {p99_ms:>8.2f} ms {per_entry:>7.1f} us {core:>7.2f}%"
        )
    conn.delete(KEY)


# ------------------------------------------------------------------ 5. trim behaviour


def trim_behaviour(conn: redis.Redis, entries: list[dict[str, bytes]]) -> None:
    print("\n5. `MINID ~` actually deletes, and `XACK` does not")
    print("   " + "-" * 66)
    conn.delete(KEY)
    pipe = conn.pipeline(transaction=False)
    for i in range(5000):
        pipe.xadd(KEY, entries[i % len(entries)], id=f"{1000 + i}-0")
    pipe.execute()
    rows = [("5,000 entries written", conn.memory_usage(KEY), conn.xlen(KEY))]

    conn.xgroup_create(KEY, "store", id="0")
    read = conn.xreadgroup("store", "store-1", {KEY: ">"}, count=5000)
    ids = [entry_id for entry_id, _ in read[0][1]]
    rows.append(
        ("after XREADGROUP (5,000 pending)", conn.memory_usage(KEY), conn.xlen(KEY))
    )

    conn.xack(KEY, "store", *ids)
    rows.append(("after XACK of all 5,000", conn.memory_usage(KEY), conn.xlen(KEY)))

    conn.xtrim(KEY, minid=4000, approximate=False)
    rows.append(("after XTRIM MINID 4000-0", conn.memory_usage(KEY), conn.xlen(KEY)))

    for label, used, length in rows:
        print(f"   {label:<34} {used:>11,} B  XLEN {length:>6,}")
    conn.delete(KEY)


def main() -> None:
    try:
        conn = connect()
    except redis.exceptions.ConnectionError:
        print(f"No Redis on {REDIS_URL}. Start one:")
        print("  docker run -d --rm --name r5-redis-6399 -p 6399:6379 redis:7-alpine")
        raise SystemExit(1) from None

    server = conn.info("server")
    print(f"Redis {server['redis_version']} on {REDIS_URL}, "
          f"{server['os']}, {server['arch_bits']}-bit")
    print("Encoding: R1 decision B, json:envelope-flat.")
    print("CAVEAT: Docker Desktop on Windows routes localhost through a WSL2 VM; every")
    print("        call below pays a port-forward hop. Upper bounds, not native figures.")

    entries = entry_stream()
    sizes = [sum(len(k) + len(v) for k, v in e.items()) for e in entries]
    print(f"\nOne second of traffic: {len(entries):,} entries, "
          f"{statistics.mean(sizes):.1f} B mean, {sum(sizes) / 1024:.1f} KiB")

    shipped_persistence(conn)
    unpipelined(conn, entries)
    pipelined(conn, entries, trim=False)
    pipelined(conn, entries, trim=True)
    trim_behaviour(conn, entries)
    print("\nDone. Remove the container: docker rm -f r5-redis-6399")


if __name__ == "__main__":
    main()
