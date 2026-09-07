"""#38 step: how long does the live feed actually go quiet?

The connection controller marks a connection `degraded` when nothing has arrived for a
bounded interval. That bound has to sit above the longest gap a **healthy** feed
produces, or the badge fires on a quiet market and means nothing; and below the point a
person would call the feed dead. `hld.md` §5 carries 15 s as `assumed`, chosen as three
ticker refreshes at `measured` 5001 ms. This measures the real thing.

It subscribes every listed BTC option on both channels — exactly what the engine
subscribes — and records the wall-clock gap between consecutive frames off the socket,
which is precisely what the controller's staleness timer measures. One hour by default.

    python tools/measure_quiet_gap.py             # one hour
    python tools/measure_quiet_gap.py 300         # five minutes

Writes a JSON summary to `tools/out/` so the number can be quoted with its run named.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.feed import BOOK_CHANNEL, TICKER_CHANNEL, DeltaFeed

REST = "https://api.india.delta.exchange"
DEFAULT_SECONDS = 3600.0


def symbols() -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


class GapRecorder:
    """A feed sink that keeps the gaps and not the frames.

    Holding 3.6 million `VenueMessage`s to measure the distance between them would be a
    different experiment. Only the gap, the channel and the instant are kept.
    """

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self.longest: list[tuple[float, float, str]] = []  # gap, at, channel
        self.channels: Counter[str] = Counter()
        self.messages = 0
        self.last: float | None = None

    def publish(self, message) -> None:
        now = time.monotonic()
        self.messages += 1
        self.channels[message.channel] += 1
        if self.last is not None:
            gap = now - self.last
            self.gaps.append(gap)
            self.longest.append((gap, now, message.channel))
            self.longest.sort(reverse=True)
            del self.longest[20:]
        self.last = now


async def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS
    names = symbols()
    recorder = GapRecorder()
    feed = DeltaFeed(recorder)
    feed.subscribe(TICKER_CHANNEL, names)
    feed.subscribe(BOOK_CHANNEL, names)

    started_at = datetime.now(UTC)
    task = asyncio.create_task(feed.run())
    started = time.monotonic()
    try:
        await asyncio.sleep(seconds)
    finally:
        feed.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    elapsed = time.monotonic() - started

    gaps = sorted(recorder.gaps)
    summary = {
        "run": "tools/measure_quiet_gap.py",
        "started_at": started_at.isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "symbols_subscribed": len(names),
        "channels": ["ticker", "ob_l2"],
        "messages": recorder.messages,
        "connections": feed.connections,
        "malformed": feed.malformed,
        "last_error": feed.last_error,
        "gap_seconds": {
            "max": round(max(gaps), 3) if gaps else None,
            "p99": round(gaps[int(len(gaps) * 0.99)], 3) if gaps else None,
            "p95": round(gaps[int(len(gaps) * 0.95)], 3) if gaps else None,
            "median": round(statistics.median(gaps), 3) if gaps else None,
            "mean": round(statistics.fmean(gaps), 4) if gaps else None,
        },
        "longest_20": [
            {"gap": round(gap, 3), "at_seconds": round(at - started, 1), "channel": ch}
            for gap, at, ch in recorder.longest
        ],
        "per_channel": dict(recorder.channels),
    }
    out = Path(__file__).resolve().parent / "out" / "measure_quiet_gap.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
