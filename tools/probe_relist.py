"""Measure the two things #51's re-listing rests on. A probe, not engine code.

**Question 1 — is a second `subscribe` on a live socket additive?** `DeltaFeed.subscribe`
sends only the newly registered symbols to a connection that is already open. That is
correct only if Delta adds them to what the socket has rather than replacing it; if it
replaced, the re-list would silently unsubscribe everything it did not name — #51 again,
and worse. This subscribes set A, waits for frames, subscribes set B, and then checks
that frames for **both** keep arriving.

**Question 2 — what does one listing cost?** `RELIST_INTERVAL_SECONDS` trades the hole at
the start of a new contract's history against the weight of `/v2/tickers` with no expiry
filter. This times the call and reports its size and row count.

Nothing here writes to the engine or to the store. Run it, read the table, record the
numbers.

    python tools/probe_relist.py
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import websockets

PUBLIC_WS = "wss://public-socket.india.delta.exchange"
REST = "https://api.india.delta.exchange"
CHANNELS = ("ticker", "ob_l2")

#: How long to listen after each subscribe before deciding a symbol is silent. `ob_l2`
#: refreshes every ~508 ms per contract on a live chain, so 15 s is ~30 refreshes.
LISTEN_SECONDS = 15.0


def listing(underlying: str) -> tuple[list[str], float, int]:
    """Every option symbol Delta lists for one underlying, with what the call cost."""
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        f"&underlying_asset_symbols={underlying}"
    )
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read()
    elapsed = time.perf_counter() - started
    rows = json.loads(raw)["result"]
    return [row["symbol"] for row in rows], elapsed, len(raw)


def subscribe_message(channel: str, symbols: list[str]) -> str:
    return json.dumps(
        {
            "type": "subscribe",
            "payload": {"channels": [{"name": channel, "symbols": symbols}]},
        }
    )


async def collect(socket, seconds: float) -> dict[str, int]:
    """Frames per symbol over a window, on any channel."""
    counts: dict[str, int] = {}
    deadline = time.perf_counter() + seconds
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return counts
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
        except TimeoutError:
            return counts
        message = json.loads(raw)
        if message.get("type") not in CHANNELS:
            continue
        symbol = message.get("sy") or ""
        counts[symbol] = counts.get(symbol, 0) + 1


async def additive(first: list[str], second: list[str]) -> None:
    print("\n--- is a second subscribe additive? ---")
    print(f"    first  {first}")
    print(f"    second {second}")
    async with websockets.connect(PUBLIC_WS, open_timeout=20) as socket:
        for channel in CHANNELS:
            await socket.send(subscribe_message(channel, first))
        before = await collect(socket, LISTEN_SECONDS)
        print(f"\n    after subscribe #1, {LISTEN_SECONDS:g}s:")
        print("       ", dict(sorted(before.items())))

        for channel in CHANNELS:
            await socket.send(subscribe_message(channel, second))
        after = await collect(socket, LISTEN_SECONDS)
        print(f"    after subscribe #2, {LISTEN_SECONDS:g}s:")
        print("       ", dict(sorted(after.items())))

    kept = [s for s in first if after.get(s, 0) > 0]
    gained = [s for s in second if after.get(s, 0) > 0]
    print("\n    the first set still arriving :", f"{len(kept)}/{len(first)}", kept)
    print("    the second set now arriving  :", f"{len(gained)}/{len(second)}", gained)
    if len(kept) == len(first) and len(gained) == len(second):
        print("\n    ADDITIVE: the second subscribe added to the first.")
    elif not kept and gained:
        print("\n    REPLACING: the second subscribe dropped the first set. The live")
        print("    subscribe must send the whole registry, not the difference.")
    else:
        print("\n    INCONCLUSIVE — a silent contract is not a dropped subscription.")
        print("    Re-run against contracts that are certainly trading.")


async def main() -> None:
    for underlying in ("BTC", "ETH"):
        symbols, elapsed, size = listing(underlying)
        print(
            f"listing {underlying}: {len(symbols)} contracts, "
            f"{size / 1024:.1f} KB, {elapsed * 1000:.0f} ms"
        )

    symbols, _, _ = listing("BTC")
    # Contracts most likely to be quoting: Delta lists near-dated at-the-money first in
    # its own ordering, and a silent far-dated strike would make this inconclusive.
    await additive(symbols[:2], symbols[2:4])


if __name__ == "__main__":
    asyncio.run(main())
