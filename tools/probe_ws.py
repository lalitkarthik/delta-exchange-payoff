"""Measure Delta's public websocket. A probe, not engine code.

Settles one question T4 (#3) is built on: **how many symbols does Delta accept in a single
`subscribe` message on each channel?**

OpenAlgo's `broker/deltaexchange/streaming/delta_websocket.py` states, verified live on
2026-08-12:

    MAX_SYMBOLS_PER_FRAME = {CHANNEL_TICKER: None, CHANNEL_OB_L2: 1}

with the comment that `ob_l2` "rejects anything above one symbol outright". A previous
session on this project subscribed **136** symbols to `ob_l2` successfully. One of those
two measurements is stale, and the answer changes how reconnection has to work: at a
limit of one, restoring a 136-strike chain needs 136 separate messages and can partially
fail, which forces the subscription registry to be keyed per symbol rather than per
message.

Nothing here writes to the engine. Run it, read the table, record the numbers.

    python tools/probe_ws.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import websockets

PUBLIC_WS = "wss://public-socket.india.delta.exchange"
REST = "https://api.india.delta.exchange"

#: Delta retired `v2/ticker`, `l1_orderbook` and `l2_orderbook` on 31 July 2026. The
#: public endpoint takes these names now, and the old ones are rejected as invalid.
CHANNELS = ("ticker", "ob_l2")

#: Sizes to try. Chosen to bracket both claims — 1 is OpenAlgo's, 136 is ours — and to
#: keep going past both so the actual ceiling shows rather than just "our number worked".
SIZES = (1, 2, 10, 50, 136, 300, 600)

#: **The per-symbol interval this probe reports is not the chain's interval.** It takes
#: the first N symbols of the all-expiries list, which is mostly far-dated contracts that
#: rarely change, and Delta publishes on change rather than on a metronome. That made 136
#: symbols look like 940 ms per symbol. `tools/measure_feed.py` on a real single-expiry
#: chain of the same size measures **508 ms**, matching #3's figure. Read the frame limit
#: here; read the interval there.

#: How long to wait for data after subscribing before calling a channel silent.
LISTEN_SECONDS = 12.0


async def option_symbols(limit: int = 600) -> list[str]:
    """Live BTC option symbols, over REST. The websocket cannot enumerate contracts."""
    import urllib.request

    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.load(response)
    return [row["symbol"] for row in payload["result"]][:limit]


def subscribe_message(channel: str, symbols: list[str]) -> str:
    return json.dumps(
        {
            "type": "subscribe",
            "payload": {"channels": [{"name": channel, "symbols": symbols}]},
        }
    )


async def probe_one(channel: str, symbols: list[str]) -> dict[str, Any]:
    """Subscribe `symbols` in ONE message and report what actually arrives.

    Delta acknowledges a subscribe with a `subscriptions` message listing what it
    accepted. That acknowledgement is the direct answer, and counting the distinct
    symbols that then send data is the corroboration — an exchange can acknowledge a
    subscription and quietly never send it.
    """
    result: dict[str, Any] = {
        "channel": channel,
        "requested": len(symbols),
        "error": None,
        "acked": None,
        "distinct_senders": 0,
        "messages": 0,
        "bytes": 0,
        "seconds": 0.0,
    }
    seen: set[str] = set()
    started = time.perf_counter()
    try:
        async with websockets.connect(PUBLIC_WS, open_timeout=20) as socket:
            await socket.send(subscribe_message(channel, symbols))
            # The clock starts AFTER the connection is up. Including the handshake --
            # about ten seconds here -- halved every rate in the first run of this probe
            # and made a matching measurement look like a contradiction.
            started = time.perf_counter()
            deadline = started + LISTEN_SECONDS
            while time.perf_counter() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        socket.recv(), timeout=max(deadline - time.perf_counter(), 0.1)
                    )
                except TimeoutError:
                    break
                result["messages"] += 1
                result["bytes"] += len(raw)
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "subscriptions":
                    for entry in message.get("channels", []) or []:
                        if entry.get("name") == channel:
                            result["acked"] = len(entry.get("symbols") or [])
                elif kind == "error":
                    result["error"] = str(message)[:200]
                    break
                elif kind == channel and message.get("sy"):
                    # `sy`, not `symbol`. The first run of this probe counted zero
                    # senders on every row because it guessed the field name.
                    seen.add(message["sy"])
    except Exception as exc:  # a refused subscribe can close the socket outright
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]

    result["distinct_senders"] = len(seen)
    result["seconds"] = time.perf_counter() - started
    return result


async def main() -> None:
    symbols = await option_symbols()
    print(f"{len(symbols)} live BTC option symbols from REST\n")
    print(f"{'channel':8} {'asked':>6} {'acked':>6} {'senders':>8} {'msgs':>6} "
          f"{'msg/s':>7} {'KB/s':>7} {'ms/sym':>8}  note")
    print("-" * 88)

    for channel in CHANNELS:
        for size in SIZES:
            if size > len(symbols):
                continue
            row = await probe_one(channel, symbols[:size])
            rate = row["messages"] / row["seconds"] if row["seconds"] else 0.0
            kbs = row["bytes"] / 1024 / row["seconds"] if row["seconds"] else 0.0
            # How often one contract speaks. This is the number the trigger design needs;
            # the message rate is just this divided into the number of subscriptions.
            per_symbol = (row["distinct_senders"] / rate * 1000) if rate else 0.0
            note = row["error"] or ""
            print(
                f"{channel:8} {row['requested']:6} {str(row['acked']):>6} "
                f"{row['distinct_senders']:8} {row['messages']:6} {rate:7.1f} "
                f"{kbs:7.1f} {per_symbol:8.0f}  {note[:28]}"
            )
            await asyncio.sleep(1.0)  # stay well inside the connection budget


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
