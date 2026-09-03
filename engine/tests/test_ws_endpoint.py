"""`/ws/chain`: the live read path, over FastAPI's TestClient. No network.

The browser opens one websocket and receives the complete `ChainResponse` — byte for byte
the shape `/chain` already returns — on a timer. The ladder component needs no changes,
because it is being handed the same object it already renders.

The feed is replaced with a stream that is fed by hand, so the whole endpoint is
exercised without a socket to Delta. `TestClient` is deliberately **not** entered as a
context manager, following `tests/test_api.py`: doing so runs the app lifespan, which
subscribes every live BTC option over a real websocket at start-up.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from deltapayoff.feed import Quote
from deltapayoff.main import MIN_PUSH_INTERVAL_SECONDS, app, get_chain_stream
from deltapayoff.stream import ChainStream

EXPIRY = "04-09-2026"


def ticker(symbol, bid, ask):
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": "77651.9",
        "ts": 1,
        "d": [
            {
                "s": symbol,
                "i": 1,
                "m": "580.6",
                "q": [str(ask), "10", str(bid), "20", None],
                "qiv": ["0.31", "0.29", "0.30"],
                "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
                "oi": ["100", "200"],
            }
        ],
    }


@pytest.fixture
def live_stream():
    """A stream primed with two contracts, wired in place of the real feed."""
    stream = ChainStream()
    for symbol, bid, ask in (
        ("C-BTC-77600-040926", 579, 584),
        ("P-BTC-77600-040926", 120, 125),
    ):
        stream.apply(
            Quote(
                symbol=symbol,
                channel="ticker",
                received_at=0.0,
                frame=ticker(symbol, bid, ask),
            )
        )
    app.dependency_overrides[get_chain_stream] = lambda: stream
    yield stream
    app.dependency_overrides.clear()


def test_the_socket_pushes_a_full_chain_without_being_asked(live_stream) -> None:
    """No request, no polling, no refresh — the first message arrives on connect.

    Waiting a whole tick before the first push would leave the page blank for a second
    on every load, so the chain is sent immediately and then on the timer.
    """
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=BTC&expiry={EXPIRY}"
    ) as socket:
        message = json.loads(socket.receive_text())

    assert message["type"] == "chain"
    assert message["data"]["underlying"] == "BTC"
    assert message["data"]["expiry"] == EXPIRY
    assert [row["strike"] for row in message["data"]["rows"]] == [77600.0]


def test_the_payload_is_the_same_shape_the_rest_endpoint_returns(live_stream) -> None:
    """The ladder component is not changed, so the object it receives cannot change.

    Every key `ChainResponse` declares must be present, and the decimals must be JSON
    numbers — `web/lib/engine.ts` refuses strings outright rather than parsing them.
    """
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=BTC&expiry={EXPIRY}"
    ) as socket:
        chain = json.loads(socket.receive_text())["data"]

    assert set(chain) == {
        "underlying",
        "expiry",
        "spot",
        "atm_strike",
        "fetched_at",
        "rows",
    }
    assert isinstance(chain["spot"], float)
    assert isinstance(chain["rows"][0]["call"]["bid"], float)


def test_updates_keep_arriving_and_carry_the_newest_prices(live_stream) -> None:
    """The point of the whole exercise: the screen changes without anyone touching it."""
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=BTC&expiry={EXPIRY}&interval=0.02"
    ) as socket:
        first = json.loads(socket.receive_text())["data"]

        live_stream.apply(
            Quote(
                symbol="C-BTC-77600-040926",
                channel="ticker",
                received_at=0.0,
                frame=ticker("C-BTC-77600-040926", 601, 607),
            )
        )
        second = json.loads(socket.receive_text())["data"]

    assert first["rows"][0]["call"]["bid"] == 579.0
    assert second["rows"][0]["call"]["bid"] == 601.0


def test_an_expiry_with_nothing_yet_says_so_rather_than_sending_an_empty_ladder(
    live_stream,
) -> None:
    """An empty chain and a chain that has not arrived look identical on screen and are
    not the same thing. The first means Delta lists nothing; the second means wait."""
    client = TestClient(app)
    with client.websocket_connect(
        "/ws/chain?underlying=BTC&expiry=25-12-2026&interval=0.02"
    ) as socket:
        message = json.loads(socket.receive_text())

    assert message["type"] == "waiting"
    assert "data" not in message


def test_a_bad_underlying_is_refused_at_the_handshake(live_stream) -> None:
    """The REST endpoints validate and return 400. A websocket cannot, so it closes —
    but it says why first, or the browser sees an unexplained disconnect and retries
    forever against a request that can never succeed."""
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=DOGE&expiry={EXPIRY}"
    ) as socket:
        message = json.loads(socket.receive_text())

    assert message["type"] == "error"
    assert "DOGE" in message["detail"]


def test_a_bad_expiry_format_is_refused_too(live_stream) -> None:
    client = TestClient(app)
    with client.websocket_connect(
        "/ws/chain?underlying=BTC&expiry=2026-09-04"
    ) as socket:
        message = json.loads(socket.receive_text())

    assert message["type"] == "error"
    assert "DD-MM-YYYY" in message["detail"]


def test_two_browsers_get_their_own_stream(live_stream) -> None:
    """Sockets are per connection; the cache behind them is shared. Opening a second tab
    must not disturb the first, and must not open a second connection to Delta."""
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=BTC&expiry={EXPIRY}"
    ) as first:
        with client.websocket_connect(
            f"/ws/chain?underlying=BTC&expiry={EXPIRY}"
        ) as second:
            a = json.loads(first.receive_text())["data"]
            b = json.loads(second.receive_text())["data"]

    assert a["rows"] == b["rows"]


def test_the_rest_endpoints_still_work(live_stream) -> None:
    """Adding a live path must not disturb the one the fixtures and tests rely on."""
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}


def test_the_push_interval_cannot_be_driven_below_its_floor(live_stream) -> None:
    """`interval` is a query parameter, so it is attacker-controlled.

    **Measured** before the floor existed: `?interval=0` pushed 207 chains in 3 seconds,
    69 a second against an intended one. Each push rebuilds a 69-strike ladder from 136
    cached frames and serialises it, so a hand-edited URL pegs a core and starves the
    event loop the Delta feed is running on. A negative value does the same, because
    `asyncio.sleep` treats it as zero.

    The parameter stays overridable — these tests drive it at 0.02 — so the fix is a
    floor, not removal.
    """
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=BTC&expiry={EXPIRY}&interval=0"
    ) as socket:
        started = time.perf_counter()
        socket.receive_text()
        socket.receive_text()
        elapsed = time.perf_counter() - started

    assert elapsed >= MIN_PUSH_INTERVAL_SECONDS


def test_a_negative_interval_is_floored_too(live_stream) -> None:
    """`asyncio.sleep(-1)` returns immediately, so a negative is a zero in disguise."""
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws/chain?underlying=BTC&expiry={EXPIRY}&interval=-5"
    ) as socket:
        started = time.perf_counter()
        socket.receive_text()
        socket.receive_text()
        elapsed = time.perf_counter() - started

    assert elapsed >= MIN_PUSH_INTERVAL_SECONDS
