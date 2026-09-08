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
sys.path.insert(0, str(Path(__file__).resolve().parent))


from _window import run_window  # noqa: E402
from deltapayoff.fanout import FanOut  # noqa: E402
from deltapayoff.adapters import DeltaFeed  # noqa: E402
from deltapayoff.adapters.delta_socket import BOOK_CHANNEL, TICKER_CHANNEL  # noqa: E402

REST = "https://api.india.delta.exchange"
RUN_SECONDS = 20.0


def symbols(underlying: str = "BTC", expiry: str | None = None) -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        f"&underlying_asset_symbols={underlying}"
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

    # **One window that redials, not one connection.** Since #39 `feed.run()` returns
    # at the first drop, so the old `create_task(run())` + `sleep` reported a full
    # twenty seconds of throughput off however much of it preceded the drop.
    # `tools/_window.py` carries the whole story.
    warmup = 1.0  # connect and subscribe before the clock starts
    started = time.perf_counter()
    counters: dict[str, int] = {}

    async def note_start() -> None:
        await asyncio.sleep(warmup)
        counters["messages"] = feed.messages
        counters["bytes"] = feed.bytes_read
        counters["at"] = time.perf_counter()

    marker = asyncio.create_task(note_start())
    window = await run_window(feed, warmup + RUN_SECONDS)
    await asyncio.gather(marker, return_exceptions=True)

    start_messages = counters.get("messages", 0)
    start_bytes = counters.get("bytes", 0)
    elapsed = time.perf_counter() - counters.get("at", started)
    messages = feed.messages - start_messages
    read = feed.bytes_read - start_bytes
    window.warn_if_truncated()

    seen: set[str] = set()
    while not sink.queue.empty():
        seen.add(sink.queue.get_nowait().symbol)

    per_symbol = len(seen) / (messages / elapsed) * 1000 if messages else 0.0
    print(
        f"{channel:8} {len(names):5} symbols  {messages / elapsed:7.1f} msg/s  "
        f"{read / 1024 / elapsed:7.1f} KB/s  {per_symbol:6.0f} ms/symbol  "
        f"{len(seen):4} distinct  dropped {sink.dropped}  "
        f"{window.attempts} connection(s)"
        + ("" if window.complete else "  ** WINDOW ENDED EARLY, see above **")
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



async def production_throughput(underlyings: list[str], seconds: float) -> None:
    """What `main.py` actually subscribes, for the given recorded set: **both** channels,
    over **every** listed contract, on one connection — not the two separate,
    differently-scoped runs `throughput()` above checks against ticket #4's baseline.

    #43 runs this once per recorded set, on the same day, to settle which subscription a
    quoted msg/s and KB/s figure describes — `docs/design/hld.md` §5 named the two
    disagreeing by ~2.2x with neither run's subscription set stated.
    """
    per_underlying = {name: symbols(name) for name in underlyings}
    names = sorted({s for group in per_underlying.values() for s in group})

    bus = FanOut()
    bus.subscribe("measure", maxsize=200_000)
    feed = DeltaFeed(bus)
    feed.subscribe(TICKER_CHANNEL, names)
    feed.subscribe(BOOK_CHANNEL, names)

    # **Two markers, timed off the wall clock, not off `run_window` returning.**
    # Measured while building this: a plain `note_start()` + "read the counters once
    # `run_window` is back" over-counts elapsed by a fixed ~10 s on every run (20 s asked
    # became 30 s, 60 s became 70 s) — the `websockets` close handshake that runs during
    # `feed.run()`'s teardown after cancellation, not receiving time. Dividing by that
    # inflated denominator would under-report every rate in this function by roughly a
    # sixth. Sampling `feed.messages`/`feed.bytes_read` at fixed offsets from our own
    # start, rather than after the window's cleanup completes, sidesteps it. `throughput()`
    # above has the same shape and is likely to share this bias; not touched here because
    # it validates against a different, already-published baseline.
    warmup = 1.0
    started = time.perf_counter()
    start_counts: dict[str, float] = {}
    end_counts: dict[str, float] = {}

    async def mark(delay: float, dest: dict[str, float]) -> None:
        await asyncio.sleep(delay)
        dest["messages"] = feed.messages
        dest["bytes"] = feed.bytes_read
        dest["at"] = time.perf_counter()

    start_marker = asyncio.create_task(mark(warmup, start_counts))
    end_marker = asyncio.create_task(mark(warmup + seconds, end_counts))
    window = await run_window(feed, warmup + seconds)
    await asyncio.gather(start_marker, end_marker, return_exceptions=True)

    if "at" not in end_counts:
        # The window ended early enough that our own end marker never fired — a drop,
        # not the close handshake. Fall back to reading the feed now; `window.complete`
        # below already says this window is not what was asked for.
        end_counts = {
            "messages": feed.messages,
            "bytes": feed.bytes_read,
            "at": time.perf_counter(),
        }

    elapsed = end_counts["at"] - start_counts.get("at", started)
    messages = end_counts["messages"] - start_counts.get("messages", 0)
    read = end_counts["bytes"] - start_counts.get("bytes", 0)
    window.warn_if_truncated()

    label = "+".join(underlyings)
    per_underlying_counts = ", ".join(
        f"{name} {len(group)}" for name, group in per_underlying.items()
    )
    print(
        f"{label:8} {len(names):5} contracts ({per_underlying_counts}), both channels  "
        f"{messages / elapsed:7.1f} msg/s  {read / 1024 / elapsed:7.1f} KB/s  "
        f"{elapsed:6.1f} s elapsed  {window.attempts} connection(s)"
        + ("" if window.complete else "  ** WINDOW ENDED EARLY, see above **")
    )


async def channel_cadence(channel: str, underlying: str, seconds: float) -> None:
    """One underlying, one channel, every listed contract — how often each symbol
    actually refreshes, with the same unbiased window `production_throughput` uses.

    #43's ticket asks whether ETH refreshes at BTC's 508 ms book / 5,001 ms ticker
    cadence. `throughput()` above answers a related question over a 20 s window, too
    short against a ~5 s ticker period to give more than four samples per symbol; this
    runs long enough (60 s asked) to average that noise out, one channel at a time.
    """
    names = symbols(underlying)
    channel_const = TICKER_CHANNEL if channel == "ticker" else BOOK_CHANNEL

    bus = FanOut()
    sink = bus.subscribe("measure", maxsize=200_000)
    feed = DeltaFeed(bus)
    feed.subscribe(channel_const, names)

    warmup = 1.0
    started = time.perf_counter()
    start_counts: dict[str, float] = {}
    end_counts: dict[str, float] = {}

    async def mark(delay: float, dest: dict[str, float]) -> None:
        await asyncio.sleep(delay)
        dest["messages"] = feed.messages
        dest["at"] = time.perf_counter()

    start_marker = asyncio.create_task(mark(warmup, start_counts))
    end_marker = asyncio.create_task(mark(warmup + seconds, end_counts))
    window = await run_window(feed, warmup + seconds)
    await asyncio.gather(start_marker, end_marker, return_exceptions=True)
    if "at" not in end_counts:
        end_counts = {"messages": feed.messages, "at": time.perf_counter()}

    elapsed = end_counts["at"] - start_counts.get("at", started)
    messages = end_counts["messages"] - start_counts.get("messages", 0)
    window.warn_if_truncated()

    seen: set[str] = set()
    while not sink.queue.empty():
        seen.add(sink.queue.get_nowait().symbol)

    per_symbol_ms = len(seen) / (messages / elapsed) * 1000 if messages else 0.0
    print(
        f"{underlying:4} {channel:6} {len(names):5} symbols  "
        f"{messages / elapsed:7.1f} msg/s  {per_symbol_ms:7.1f} ms/symbol  "
        f"{len(seen):4} distinct  {elapsed:5.1f} s elapsed  "
        f"{window.attempts} connection(s)"
        + ("" if window.complete else "  ** WINDOW ENDED EARLY, see above **")
    )


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--underlyings",
        default="",
        help="comma-separated, e.g. BTC or BTC,ETH — runs production_throughput and "
        "exits, skipping the per-channel and fan-out measurements below",
    )
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument(
        "--cadence",
        default="",
        help="CHANNEL:UNDERLYING, e.g. ticker:ETH — runs channel_cadence and exits",
    )
    args = parser.parse_args()

    if args.cadence:
        channel, _, underlying = args.cadence.partition(":")
        await channel_cadence(channel.strip(), underlying.strip().upper(), args.seconds)
        return

    if args.underlyings:
        wanted = [name.strip().upper() for name in args.underlyings.split(",") if name]
        await production_throughput(wanted, args.seconds)
        return

    every = symbols()
    chain = symbols(expiry="04-09-2026")
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
