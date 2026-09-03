"""Capture real websocket frames plus the matching REST snapshot, as test fixtures.

The decoder in `deltapayoff.wire` has to turn Delta's abbreviated websocket payloads into
the same `Leg` and `ChainRow` the REST path already builds. The only honest way to test
that is to read the same contracts **two ways at the same moment** and demand the numbers
agree: position is meaning in these payloads, and a transposed array index produces
numbers that are all wrong and all plausible.

So this writes three files:

* `ws-ticker-<expiry>.json`  — one `ticker` frame per symbol, verbatim
* `ws-ob-l2-<expiry>.json`   — one `ob_l2` frame per symbol, verbatim
* `rest-<expiry>.json`       — `GET /v2/tickers` for the same expiry, taken alongside

One frame per symbol rather than a window of them: the decoder is a pure function of a
single frame, and a megabyte of repeats would test nothing the first frame does not.

    python tools/capture_ws.py 04-09-2026
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets

PUBLIC_WS = "wss://public-socket.india.delta.exchange"
REST = "https://api.india.delta.exchange"
FIXTURES = Path(__file__).resolve().parents[1] / "engine" / "tests" / "fixtures"

#: Long enough for every symbol to speak once at the ~940 ms per-symbol interval
#: measured by `probe_ws.py`, with headroom for the illiquid wings that stay silent.
COLLECT_SECONDS = 25.0


def rest_snapshot(expiry: str) -> dict:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        f"&underlying_asset_symbols=BTC&expiry_date={expiry}"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


async def collect(symbols: list[str]) -> tuple[dict, dict]:
    """First frame per symbol on each channel. Both channels on one connection."""
    ticker: dict[str, dict] = {}
    book: dict[str, dict] = {}

    async with websockets.connect(PUBLIC_WS, open_timeout=20) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "payload": {
                        "channels": [
                            {"name": "ticker", "symbols": symbols},
                            {"name": "ob_l2", "symbols": symbols},
                        ]
                    },
                }
            )
        )
        deadline = time.perf_counter() + COLLECT_SECONDS
        while time.perf_counter() < deadline:
            if len(ticker) >= len(symbols) and len(book) >= len(symbols):
                break
            try:
                raw = await asyncio.wait_for(
                    socket.recv(), timeout=max(deadline - time.perf_counter(), 0.1)
                )
            except TimeoutError:
                break
            message = json.loads(raw)
            symbol = message.get("sy")
            if not symbol:
                continue
            if message.get("type") == "ticker":
                ticker.setdefault(symbol, message)
            elif message.get("type") == "ob_l2":
                book.setdefault(symbol, message)

    return ticker, book


async def main(expiry: str) -> None:
    # REST first, so its timestamp precedes every frame rather than trailing them.
    rest = rest_snapshot(expiry)
    symbols = [row["symbol"] for row in rest["result"]]
    print(f"{expiry}: {len(symbols)} symbols from REST")

    ticker, book = await collect(symbols)
    print(f"  ticker frames {len(ticker):4}/{len(symbols)}")
    print(f"  ob_l2 frames  {len(book):4}/{len(symbols)}")

    for name, payload in (
        (f"rest-{expiry}.json", rest),
        (f"ws-ticker-{expiry}.json", {"captured": expiry, "frames": ticker}),
        (f"ws-ob-l2-{expiry}.json", {"captured": expiry, "frames": book}),
    ):
        path = FIXTURES / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        print(f"  wrote {name}  {path.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "04-09-2026"))
