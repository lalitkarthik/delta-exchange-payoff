"""T4 step 5: does the feed hit the baselines, and what does the fan-out cost?

Two separate measurements, and the second is the one that matters.

**Throughput** against the figures the ticket carries — 187 msg/s and 82 KB/s for
`ticker` over every live option, 270 msg/s and 131.7 KB/s for `ob_l2` over one chain. If
we cannot reproduce them, either that measurement or this one is wrong, and #6 sets a
latency target against them.

**The cost of the seam.** We chose an in-process fan-out over ZeroMQ. That choice needs
evidence rather than taste, so this times `publish` into N queues against calling N
consumers directly. Put beside the rest of the budget — Delta publishes every ~940 ms and
the whole chain recomputes in 1.1 ms — a hop in the microseconds means the indirection is
free and a broker would only add serialisation for nothing.

    python tools/measure_feed.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.fanout import FanOut  # noqa: E402
from deltapayoff.feed import DeltaFeed  # noqa: E402

REST = "https://api.india.delta.exchange"
RUN_SECONDS = 20.0


def symbols(expiry: str | None = None) -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    if expiry:
        url += f"&expiry_date={expiry}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


async def throughput(channel: str, names: list[str]) -> None:
    bus = FanOut()
    sink = bus.subscribe("measure", maxsize=100_000)
    feed = DeltaFeed(bus)
    feed.subscribe(channel, names)

    task = asyncio.create_task(feed.run())
    await asyncio.sleep(1.0)  # connect and subscribe before the clock starts
    start_messages, start_bytes = feed.messages, feed.bytes_read
    started = time.perf_counter()
    await asyncio.sleep(RUN_SECONDS)
    elapsed = time.perf_counter() - started
    messages = feed.messages - start_messages
    read = feed.bytes_read - start_bytes

    feed.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    seen: set[str] = set()
    while not sink.queue.empty():
        seen.add(sink.queue.get_nowait().symbol)

    per_symbol = len(seen) / (messages / elapsed) * 1000 if messages else 0.0
    print(
        f"{channel:8} {len(names):5} symbols  {messages / elapsed:7.1f} msg/s  "
        f"{read / 1024 / elapsed:7.1f} KB/s  {per_symbol:6.0f} ms/symbol  "
        f"{len(seen):4} distinct  dropped {sink.dropped}"
    )


async def fanout_cost() -> None:
    """Publish into N queues, against calling N consumers directly."""
    from deltapayoff.timing import time_it

    record = object()
    for consumers in (1, 3, 10):
        bus = FanOut()
        for n in range(consumers):
            bus.subscribe(f"c{n}", maxsize=10_000)
        drains = [bus._subscriptions[f"c{n}"].queue for n in range(consumers)]

        def publish_once():
            bus.publish(record)
            for queue in drains:
                queue.get_nowait()

        _, timing = time_it(publish_once, runs=20_000)

        sink: list = []

        def direct_once():
            for _ in range(consumers):
                sink.append(record)
            sink.clear()

        _, baseline = time_it(direct_once, runs=20_000)

        print(
            f"  {consumers:2} consumers   fan-out {timing.median_ms * 1000:7.3f} us"
            f"   direct call {baseline.median_ms * 1000:7.3f} us"
            f"   overhead {(timing.median_ms - baseline.median_ms) * 1000:7.3f} us"
        )



async def main() -> None:
    every = symbols()
    chain = symbols("04-09-2026")
    print(f"{len(every)} live BTC options, {len(chain)} in the 04-09-2026 chain\n")
    print("THROUGHPUT")
    await throughput("ticker", every)
    await asyncio.sleep(2.0)
    await throughput("ob_l2", chain)

    print("\nFAN-OUT COST, per published record")
    await fanout_cost()

    print("\nBUDGET")
    print(f"  {'Delta publishes (ob_l2, measured)':38} ~940     ms")
    print(f"  {'our full chain recompute':38}    1.095 ms")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
