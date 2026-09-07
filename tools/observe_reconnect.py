"""#39 step: watch a real connection go down and come back, under a real controller.

**Every transition this project has recorded so far was inferred from message gaps.** #38
built the state machine and deliberately did not wire it into `main`, so no controller had
ever run over a live socket and said what it saw. #39 wires it, and this watches the
result.

It builds exactly what the engine builds — `DeltaAdapter` over `DeltaFeed`, wrapped in a
`ConnectionController` under a `FeedSupervisor` — subscribes every listed BTC option, and
prints the supervisor's report every second alongside every `feed.connection`,
`heartbeat` and `alert` that reaches the bus.

    python tools/observe_reconnect.py           # 180 s, with a simulated outage
    python tools/observe_reconnect.py 60 --no-outage

## The outage, and what it is not

The acceptance test asks for a network interface disabled for thirty seconds. **This
simulates that at the dial instead**, and says so rather than pretending otherwise,
because disabling an interface is a system setting and this machine is shared. What it
reproduces is the *shape* a disabled interface produces, in two phases, because the two
halves of the walk have different causes:

1. **Silent but open** — frames stop arriving and the socket stays up. This is the only
   thing that produces `connected -> degraded`, and it is what the first seconds of a
   dropped interface look like before the OS notices.
2. **Torn down and unreachable** — the read raises and every dial is refused. This is
   what the OS does once it notices, and it is what produces
   `degraded -> reconnecting -> connecting`.

Then the outage lifts and the walk finishes at `connected`. The timings printed are
`measured` on this run and named by its start time.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.adapters import DeltaAdapter, DeltaFeed
from deltapayoff.events import Alert, FeedConnection, Heartbeat
from deltapayoff.supervisor import FeedSupervisor

REST = "https://api.india.delta.exchange"
USER_AGENT = "convex-hedge-probe/1.0 (+delta-exchange-payoff)"

DEFAULT_SECONDS = 180.0
#: When the socket goes quiet, and when it is torn down. Both after the connection has
#: settled, so `connected` is reached first and the walk starts from a real state.
SILENT_AT = 40.0
TORN_DOWN_AT = 60.0
#: When dials start succeeding again. Twenty seconds of silence crosses `degraded_after`
#: (15 s) and the rest crosses `reconnect_after` (45 s) measured from the last frame.
OUTAGE_ENDS_AT = 100.0


class Outage:
    """The scripted interruption, on one monotonic clock everything else reads."""

    def __init__(self, enabled: bool, started: float) -> None:
        self.enabled = enabled
        self.started = started

    def at(self) -> float:
        return time.monotonic() - self.started

    def silent(self) -> bool:
        return self.enabled and SILENT_AT <= self.at() < OUTAGE_ENDS_AT

    def unreachable(self) -> bool:
        return self.enabled and TORN_DOWN_AT <= self.at() < OUTAGE_ENDS_AT


class WrappedSocket:
    """A real websocket that stops speaking, then raises, on the outage's schedule."""

    def __init__(self, socket, outage: Outage) -> None:
        self._socket = socket
        self._outage = outage

    async def send(self, payload):
        return await self._socket.send(payload)

    async def ping(self):
        return await self._socket.ping()

    async def recv(self):
        while self._outage.silent():
            if self._outage.unreachable():
                # What the OS does once it notices the interface is gone.
                raise ConnectionResetError("simulated interface down")
            # Silent but open: nothing arrives and nothing raises, which is the case
            # `degraded` exists for and the one TCP cannot tell from a quiet market.
            await asyncio.sleep(0.2)
        return await self._socket.recv()


def symbols() -> list[str]:
    url = (
        f"{REST}/v2/tickers?contract_types=call_options,put_options"
        "&underlying_asset_symbols=BTC"
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return [row["symbol"] for row in json.load(response)["result"]]


def connector(outage: Outage) -> Callable[[str], object]:
    """`DeltaFeed`'s `connect`, refusing while the interface is 'down'."""

    class Dial:
        def __init__(self, url: str) -> None:
            self._url = url
            self._socket = None

        async def __aenter__(self):
            if outage.unreachable():
                raise ConnectionRefusedError("simulated interface down")
            import websockets

            self._socket = await websockets.connect(self._url, open_timeout=20)
            return WrappedSocket(self._socket, outage)

        async def __aexit__(self, *_):
            if self._socket is not None:
                await self._socket.close()
            return False

    return Dial


async def main() -> None:
    seconds = DEFAULT_SECONDS
    enabled = True
    for argument in sys.argv[1:]:
        if argument == "--no-outage":
            enabled = False
        else:
            seconds = float(argument)

    names = symbols()
    started_at = datetime.now(UTC)
    started = time.monotonic()
    outage = Outage(enabled, started)

    events: list[tuple[float, str]] = []

    def record(event) -> None:
        at = time.monotonic() - started
        if isinstance(event, FeedConnection):
            was = "start" if event.from_state is None else event.from_state.value
            events.append((at, f"{was} -> {event.to_state.value} ({event.reason})"))
            print(f"  {at:7.1f}s  {was} -> {event.to_state.value}  ({event.reason})")
        elif isinstance(event, Alert):
            events.append((at, f"alert {event.code}: {event.detail}"))
            print(f"  {at:7.1f}s  ALERT {event.code}: {event.detail}")
        elif isinstance(event, Heartbeat):
            age = event.last_message_age_seconds
            print(
                f"  {at:7.1f}s  heartbeat {event.state.value}, "
                f"age {'unknown' if age is None else f'{age:.1f}s'}"
            )

    adapter = DeltaAdapter(
        feed_factory=lambda sink, **kw: DeltaFeed(sink, connect=connector(outage), **kw)
    )
    instruments = [
        instrument
        for name in names
        if (instrument := _instrument(name)) is not None
    ]
    adapter.subscribe(instruments)
    supervisor = FeedSupervisor([adapter], record, heartbeat_every=10.0)

    print(f"watching {len(instruments)} BTC contracts, {seconds:.0f}s", flush=True)
    if enabled:
        print(
            f"outage: silent at {SILENT_AT:.0f}s, torn down at {TORN_DOWN_AT:.0f}s, "
            f"reachable again at {OUTAGE_ENDS_AT:.0f}s",
            flush=True,
        )
    supervisor.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await supervisor.aclose()

    row = supervisor.report().adapters[0]
    print("\nthe walk, measured:")
    for at, line in events:
        print(f"  {at:7.1f}s  {line}")
    print(
        f"\nfinal: reconnects={row.reconnects} budget_remaining={row.budget_remaining} "
        f"transitions={row.transitions} empty_opens={row.empty_opens} "
        f"connections={adapter.feed.connections} messages={adapter.feed.messages}"
    )
    print(f"run started {started_at.isoformat()}")


def _instrument(name: str):
    from deltapayoff.adapters import instrument_from_symbol

    return instrument_from_symbol(name)


if __name__ == "__main__":
    asyncio.run(main())
