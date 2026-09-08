"""#44: what one expiry's solve actually costs on the live ladder, and what a pass costs.

The figure this replaces is `measured` 7.25 ms per expiry on the 69-row websocket fixture
from a previous session, and it has been carried since as `assumed` for the live path with
`derived` 58 ms for a full eight-expiry tick. The fixture is one expiry of one underlying
captured on 2026-09-03; the live board is BTC and ETH since #43. This measures the live
one.

Three things, in one process, off one socket:

1. **Per expiry** — `ChainStream._compute(pair)`, the whole solve: fold the cached events
   into a ladder, fit the forward, solve every implied volatility, price every Greek.
   Repeated, with the median and p95 reported per pair, beside the row count that drives
   the cost.
2. **A full pass** — `recompute_dirty()` over every listed expiry, which is what the
   100 ms loop did on every tick before this ticket.
3. **A watched pass** — `recompute_watched()` with one pair registered, which is what it
   does now with one browser open.

`--mode cpu` instead runs the whole engine pipeline for a window and reports the
process's own CPU time, with `--pass all` reproducing the pre-#44 live loop (every dirty
expiry, every 100 ms) and `--pass watched` running what the engine now does. Two runs of
that, one of each, is the before-and-after the ticket asks for.

    python tools/measure_solve.py --fill 60
    python tools/measure_solve.py --mode cpu --pass all --seconds 600
    python tools/measure_solve.py --mode cpu --pass watched --seconds 600

Every number printed is `measured` on this machine, this run, over the window the summary
states. Nothing here is written to the store: `--mode cpu` uses a scratch root it removes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _window import run_window  # noqa: E402
from deltapayoff.adapters.delta import DeltaAdapter  # noqa: E402
from deltapayoff.fanout import FanOut  # noqa: E402
from deltapayoff.store import BarStore, BarWriter  # noqa: E402
from deltapayoff.stream import (  # noqa: E402
    ChainStream,
    recompute_every_minute,
    recompute_forever,
)

REST = "https://api.india.delta.exchange"
DEFAULT_UNDERLYINGS = ("BTC", "ETH")


def listed(underlying: str) -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        f"&underlying_asset_symbols={underlying}"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


async def fill(
    stream: ChainStream, underlyings: tuple[str, ...], seconds: float
) -> tuple[object, object]:
    """Run the live socket into `stream` for `seconds`. Returns the feed and the window.

    The events go through `DeltaAdapter`, not `DeltaFeed` straight onto the bus, because
    since #37 the cache reads canonical events and would make nothing of a venue frame.
    """
    bus = FanOut()
    stream.attach(bus, maxsize=200_000)
    adapter = DeltaAdapter(underlyings=underlyings)
    feed = adapter.feed
    for underlying in underlyings:
        names = listed(underlying)
        feed.subscribe("ticker", names)
        feed.subscribe("ob_l2", names)
        print(f"  {underlying}: {len(names)} listed contracts")

    pump = asyncio.create_task(stream.run(), name="stream")
    window = await run_window(
        feed, seconds, dial=lambda: adapter.stream(bus.publish), stop=adapter.stop
    )
    pump.cancel()
    await asyncio.gather(pump, return_exceptions=True)
    return feed, window


def pairs_of(stream: ChainStream) -> list[tuple[str, str]]:
    return sorted(
        (underlying, expiry)
        for underlying, expiries in stream._expiries.items()
        for expiry in expiries
    )


def timings(fn, repeats: int) -> dict[str, float]:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1e3)
    samples.sort()
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
        "max_ms": round(samples[-1], 3),
        "repeats": repeats,
    }


async def solve_costs(underlyings: tuple[str, ...], seconds: float, repeats: int):
    stream = ChainStream()
    print(f"filling the cache from the live socket for {seconds:.0f}s")
    feed, window = await fill(stream, underlyings, seconds)
    window.warn_if_truncated()

    pairs = pairs_of(stream)
    print(
        f"\n{len(pairs)} live (underlying, expiry) pairs, "
        f"{feed.messages} messages in {window.elapsed_seconds:.1f}s "
        f"({window.attempts} connection(s))\n"
    )

    print("PER EXPIRY — ChainStream._compute(pair), measured, this run")
    print(f"  {'pair':26} {'rows':>5} {'median':>9} {'p95':>9} {'max':>9}")
    per_expiry = {}
    for pair in pairs:
        rows = len(stream.instruments(*pair))
        stats = timings(lambda pair=pair: stream._compute(pair), repeats)
        per_expiry[f"{pair[0]} {pair[1]}"] = {"rows": rows, **stats}
        print(
            f"  {pair[0] + ' ' + pair[1]:26} {rows:5d} "
            f"{stats['median_ms']:8.3f}ms {stats['p95_ms']:8.3f}ms "
            f"{stats['max_ms']:8.3f}ms"
        )

    medians = [v["median_ms"] for v in per_expiry.values()]
    print(
        f"\n  {len(medians)} expiries: median of medians "
        f"{statistics.median(medians):.3f} ms, "
        f"sum of medians {sum(medians):.3f} ms"
    )

    def full_pass() -> None:
        stream.dirty = set(pairs)
        stream.recompute_dirty()

    def watched_pass() -> None:
        stream.dirty = set(pairs)
        stream.recompute_watched()

    print("\nA WHOLE PASS — measured, this run")
    before = timings(full_pass, repeats)
    print(f"  every expiry (the pre-#44 live tick): {before}")

    stream.watch(*pairs[0])
    after = timings(watched_pass, repeats)
    print(f"  one browser on {pairs[0][0]} {pairs[0][1]}: {after}")

    stream._watch.clear()
    idle = timings(watched_pass, repeats)
    print(f"  no browser open at all:                {idle}")

    return {
        "window": window.summary(),
        "pairs": len(pairs),
        "messages": feed.messages,
        "per_expiry_ms": per_expiry,
        "pass_every_expiry_ms": before,
        "pass_one_watched_ms": after,
        "pass_nothing_watched_ms": idle,
    }


async def recompute_all_forever(stream: ChainStream, interval: float = 0.1) -> None:
    """The pre-#44 live loop, kept here so the two can be run against one socket."""
    while True:
        await asyncio.sleep(interval)
        stream.recompute_dirty()


async def cpu_cost(
    underlyings: tuple[str, ...], seconds: float, which: str, root: Path
) -> dict:
    """The whole engine pipeline for a window, with **no browser open**, CPU measured.

    `which` selects the live loop: `all` is what ran before this ticket — every dirty
    expiry every 100 ms — and `watched` is what runs now, beside the minute pass. The
    rest of the process is identical, which is what makes the difference attributable.
    """
    if root.exists():
        shutil.rmtree(root)

    bus = FanOut()
    stream = ChainStream()
    stream.attach(bus, maxsize=200_000)
    writer = BarWriter(
        BarStore(root),
        chains=stream.live_computed_chains
        if which == "watched"
        else stream.computed_chains,
        flush_seconds=300.0,
    )
    writer.attach(bus)
    adapter = DeltaAdapter(underlyings=underlyings)
    feed = adapter.feed
    for underlying in underlyings:
        names = listed(underlying)
        feed.subscribe("ticker", names)
        feed.subscribe("ob_l2", names)

    tasks = [
        asyncio.create_task(stream.run(), name="stream"),
        asyncio.create_task(writer.run(), name="writer"),
    ]
    if which == "watched":
        tasks.append(asyncio.create_task(recompute_forever(stream), name="live"))
        tasks.append(
            asyncio.create_task(
                recompute_every_minute(stream, writer.sample_chains), name="minute"
            )
        )
    else:
        tasks.append(asyncio.create_task(recompute_all_forever(stream), name="live"))

    warmup = 5.0
    marks: dict[str, float] = {}

    async def note_start() -> None:
        await asyncio.sleep(warmup)
        marks["cpu"] = time.process_time()
        marks["wall"] = time.perf_counter()
        marks["messages"] = feed.messages

    marker = asyncio.create_task(note_start())
    window = await run_window(
        feed,
        warmup + seconds,
        dial=lambda: adapter.stream(bus.publish),
        stop=adapter.stop,
    )
    cpu = time.process_time() - marks.get("cpu", 0.0)
    wall = time.perf_counter() - marks.get("wall", 0.0)
    messages = feed.messages - marks.get("messages", 0)

    marker.cancel()
    for task in tasks:
        task.cancel()
    await asyncio.gather(marker, *tasks, return_exceptions=True)
    await writer.aclose()
    window.warn_if_truncated()
    if root.exists():
        shutil.rmtree(root)

    return {
        "live_pass": which,
        "window": window.summary(),
        "observed_seconds": round(wall, 1),
        "cpu_seconds": round(cpu, 2),
        "cpu_fraction_of_one_core": round(cpu / wall, 4) if wall else None,
        "messages": messages,
        "msg_per_second": round(messages / wall, 1) if wall else None,
        "recomputes": stream.recomputes,
        "minute_passes": stream.minute_passes,
        "computed_ticks": writer.computed.stats()["ticks"],
        "computed_late": writer.computed.stats()["late"],
        "computed_bars": writer.computed.stats()["bars_emitted"],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("solve", "cpu"), default="solve")
    parser.add_argument("--fill", type=float, default=60.0)
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--pass", dest="which", choices=("all", "watched"), default="all")
    parser.add_argument("--underlyings", default=",".join(DEFAULT_UNDERLYINGS))
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    underlyings = tuple(
        name.strip().upper() for name in args.underlyings.split(",") if name.strip()
    )

    if args.mode == "cpu":
        root = Path(__file__).resolve().parents[1] / ".measure-solve-scratch"
        result = await cpu_cost(underlyings, args.seconds, args.which, root)
    else:
        result = await solve_costs(underlyings, args.fill, args.repeats)

    print("\n" + json.dumps(result, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
