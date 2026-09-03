"""T5.1 step 2: how late is a tick, measured — so the watermark is not a guess.

Bars are bucketed on **Delta's** clock and discovered on **ours**. Those are different
clocks, so a tick stamped 12:00:59.900 by the venue can reach this process at
12:01:00.240 — after the minute it belongs to has closed. Sealing a bar is therefore a
decision about lateness, not about time: wait too little and real ticks are counted as
late and thrown away; wait too much and every bar is delayed for nothing.

**The wait is a property of this connection, so it has to be measured on it.** This
project has been caught three times by a plausible constant taken on trust — OpenAlgo's
one-symbol frame limit, Delta's 60 s idle disconnect, the inverse-settlement premise —
and picking five seconds because it sounds reasonable would be the fourth.

What this measures, per frame:

    lag = our wall clock at receipt  -  the venue's `ts` on the frame

and reports the distribution. The grace period is read off the tail, not the median: the
median says what a typical tick costs, the tail says what the slowest *real* tick costs,
and the tail is the one a bar has to survive.

**Two caveats that this number carries and cannot remove.** The lag is
clock-skew-plus-transit, not transit: `time.time()` here and Delta's `ts` are two
unsynchronised clocks, and an NTP offset of tens of milliseconds is ordinary. And a lag
can therefore come back *negative* if our clock runs behind the venue's. Neither
invalidates the choice — a watermark has to absorb skew as well as transit, because both
move a tick across a boundary the same way — but it means this figure is not a network
latency and must not be quoted as one.

`lts` on `ob_l2` is reported alongside and **is not used for anything**. Its meaning is
unverified; it is measured here only so the gap between the two stamps is on the record.

    python tools/measure_arrival_lag.py [expiry DD-MM-YYYY] [seconds]
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.fanout import FanOut  # noqa: E402
from deltapayoff.feed import DeltaFeed  # noqa: E402

REST = "https://api.india.delta.exchange"
RUN_SECONDS = 60.0
DEFAULT_EXPIRY = None

#: Percentiles worth printing. p99.9 is here because the watermark is a tail decision:
#: at ~268 msg/s on one chain, one frame in a thousand is one every four seconds, which
#: is often enough to matter to a bar and rare enough to be invisible in a p95.
PERCENTILES = (0.5, 0.9, 0.95, 0.99, 0.999, 1.0)


def symbols(expiry: str | None) -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    if expiry:
        url += f"&expiry_date={expiry}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


def quantile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation, so every figure printed is a real
    observation rather than a number that never happened."""
    if not sorted_values:
        return float("nan")
    index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def report(label: str, lags_ms: list[float]) -> None:
    if not lags_ms:
        print(f"{label:8} no frames")
        return
    ordered = sorted(lags_ms)
    print(f"{label:8} n={len(ordered):6}  mean {statistics.fmean(ordered):8.1f} ms")
    for fraction in PERCENTILES:
        name = "max" if fraction == 1.0 else f"p{fraction * 100:g}"
        print(f"           {name:>6}  {quantile(ordered, fraction):8.1f} ms")
    print(f"           {'min':>6}  {ordered[0]:8.1f} ms")


async def measure(channel: str, names: list[str], seconds: float) -> list[float]:
    """Collect one lag per frame on `channel` for `seconds`.

    Subscribes losslessly on purpose: a drop-oldest queue would discard exactly the
    frames that arrived in a burst, which is where the tail this is trying to find lives.
    Measuring lateness through a policy that evicts the late ones would be circular.
    """
    bus = FanOut()
    sink = bus.subscribe("lag", maxsize=100_000, lossless=True)
    feed = DeltaFeed(bus)
    feed.subscribe(channel, names)

    task = asyncio.create_task(feed.run())
    await asyncio.sleep(2.0)  # connect and subscribe before the clock starts
    while not sink.queue.empty():  # discard the subscribe burst
        sink.queue.get_nowait()

    await asyncio.sleep(seconds)
    feed.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    lags: list[float] = []
    lts_gaps: list[float] = []
    while not sink.queue.empty():
        quote = sink.queue.get_nowait()
        frame = quote.frame or {}
        exchange_us = frame.get("ts")
        if exchange_us is None:
            continue
        lags.append(quote.received_at * 1000.0 - float(exchange_us) / 1000.0)
        aux = frame.get("lts")
        if aux is not None:
            lts_gaps.append((float(exchange_us) - float(aux)) / 1000.0)

    report(channel, lags)
    if lts_gaps:
        ordered = sorted(lts_gaps)
        print(
            f"           lts is a median {quantile(ordered, 0.5):.1f} ms before ts "
            f"(min {ordered[0]:.1f}, max {ordered[-1]:.1f}) — NOT USED, meaning "
            "unverified"
        )
    print(f"           feed: {feed.messages} messages, malformed {feed.malformed}, "
          f"backlog peak {sink.backlog_peak}, dropped {sink.dropped}")
    return lags


async def main() -> None:
    expiry = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXPIRY
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else RUN_SECONDS

    chain = symbols(expiry)
    every = symbols(None)
    print(
        f"{len(every)} live BTC options, {len(chain)} in the "
        f"{expiry or 'all-expiries'} set, {seconds:.0f} s per channel\n"
    )
    print("ARRIVAL LAG  (our wall clock at receipt) - (the frame's exchange ts)")
    book = await measure("ob_l2", chain, seconds)
    await asyncio.sleep(2.0)
    await measure("ticker", every, seconds)

    if book:
        ordered = sorted(book)
        tail = quantile(ordered, 0.999)
        print(
            "\nGRACE PERIOD: read off ob_l2's p99.9 and rounded up to the next round "
            f"number above it.\n  p99.9 = {tail:.1f} ms, max = {ordered[-1]:.1f} ms"
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
