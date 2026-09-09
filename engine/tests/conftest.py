"""Shared fixtures. Nothing in this suite touches the network.

The three JSON fixtures under `tests/fixtures/`:

* `tickers-btc-04-09-2026.json` — a verbatim `GET /v2/tickers` response for
  `contract_types=call_options,put_options&underlying_asset_symbols=BTC&expiry_date=04-09-2026`,
  captured from production on 2026-09-01. 128 tickers, spot 77568.2.
* `tickers-btc-all-expiries.json` — the same call without `expiry_date`, subsetted to
  one call and one put per listed expiry so the file stays small. Real rows, untouched.
* `tickers-btc-multi-expiry.json` — a verbatim `GET /v2/tickers` for BTC options with
  no `expiry_date` filter, captured from production on 2026-09-02T08:40:14Z. 588
  contracts across eight expiries, half a day to 85 days out, spot 77874.2. This is the
  fixture the agreement matrix slices by time to expiry; the chain capture above is one
  expiry and cannot.
* `ws-ticker-04-09-2026.json`, `ws-ob-l2-04-09-2026.json`, `rest-04-09-2026.json` —
  captured together on 2026-09-03 by `tools/capture_ws.py`. One verbatim websocket frame
  per symbol on each channel for the 04-09-2026 BTC chain, 136 symbols, plus the REST
  response for the same expiry taken alongside. The pairing is the point: it lets the
  same contracts be read two ways so the wire decoder can be checked rather than assumed.
* `tickers-absent-quotes.json` — three rows lifted from the chain capture and then
  hand-edited to carry the absent-value spellings Delta uses: `"0"`, `""` and `null`.
  Delta's live snapshots quote every strike, so the edge cases have to be constructed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(autouse=True)
def no_live_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's start-up subscribes every live BTC option over a websocket. No test may.

    Set before any `TestClient(app)` runs the lifespan, so the REST endpoints and
    `/ws/chain` are exercised against a hand-fed `ChainStream` instead of Delta.
    """
    monkeypatch.setenv("DELTA_LIVE_FEED", "0")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard stop: a test that opens a real client fails instead of dialling out."""
    from deltapayoff import delta_client

    def refuse() -> None:
        raise AssertionError("a test tried to open a live Delta client")

    monkeypatch.setattr(delta_client, "_new_async_client", refuse)


@pytest.fixture
def chain_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-btc-04-09-2026.json")["result"]


@pytest.fixture
def all_expiry_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-btc-all-expiries.json")["result"]


@pytest.fixture
def absent_quote_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-absent-quotes.json")["result"]


@pytest.fixture
def multi_expiry_tickers() -> list[dict[str, Any]]:
    return load_fixture("tickers-btc-multi-expiry.json")["result"]


@pytest.fixture
def ws_ticker_frames() -> dict[str, Any]:
    return load_fixture("ws-ticker-04-09-2026.json")["frames"]


@pytest.fixture
def ws_book_frames() -> dict[str, Any]:
    return load_fixture("ws-ob-l2-04-09-2026.json")["frames"]


@pytest.fixture
def rest_snapshot() -> list[dict[str, Any]]:
    return load_fixture("rest-04-09-2026.json")["result"]

@pytest.fixture
def ws_captured_at(ws_ticker_frames) -> datetime:
    """The instant the websocket fixtures were captured, read off the frames.

    A snapshot has to be priced as of when it was taken. `chain_from_frames` defaults
    `fetched_at` to the current clock, so a test that omits it silently re-dates a
    2026-09-03 capture to today — and `year_fraction` measures from `fetched_at` to
    settlement. The 04-09-2026 fixture then walks its own time to expiry down to nothing.

    That is not hypothetical. It fired on 2026-09-04: the fitted discount's implied rate
    reached 38.8% and `f1_parity_fit` refused the chain, failing a test that had asserted
    a trusted forward since T4 without anyone touching the code.

    Taken from the frames' own `ts` rather than the fixture's `captured` date string,
    because `ts` is microsecond-exact and cannot drift from the data beside it.
    """
    stamps = [frame["ts"] for frame in ws_ticker_frames.values() if frame.get("ts")]
    return datetime.fromtimestamp(max(stamps) / 1e6, tz=timezone.utc)


#: The test-only Redis port. **Not 6379**: a developer's own Redis, or a `dev` stack
#: running under Compose, is on the default port, and a suite that trimmed and deleted
#: keys there would eat a running system's streams. 6399 is the same port
#: `tools/measure_redis_hosting.py` uses, so one container serves both.
REDIS_TEST_PORT = 6399
REDIS_TEST_URL = f"redis://127.0.0.1:{REDIS_TEST_PORT}"
REDIS_CONTAINER = "deltapayoff-tests-redis-6399"
#: The pipe's own flags, so the suite runs against the configuration
#: `docs/design/cloud/redis-hosting.md` §1 fixes — with `maxmemory` at 1gb rather than
#: prod's 2gb, because this runs on a laptop.
REDIS_ARGS = (
    "redis-server --save '' --appendonly no --maxmemory 1gb "
    "--maxmemory-policy noeviction"
)


@pytest.fixture(scope="session")
def redis_server() -> Iterator[str]:
    """A throwaway Redis on 6399 for the session, started and removed here.

    **Skips loudly rather than passing quietly.** A Redis contract suite that silently
    became zero tests when Docker was not running would let the bus regress with a green
    suite over it, which is the plausible-and-wrong failure this project keeps refusing.
    The skip reason names the exact command, so a reader of the summary can turn the
    suite back on in one paste.

    Nothing here touches the network: the container is local, the port is loopback and
    the image is whatever Docker already has or pulls once.
    """
    docker = shutil.which("docker")
    hint = (
        f"docker run -d --rm --name {REDIS_CONTAINER} "
        f"-p {REDIS_TEST_PORT}:6379 redis:7-alpine {REDIS_ARGS}"
    )
    if docker is None:
        pytest.skip(f"SKIPPED LOUDLY: no docker on PATH. Start one by hand: {hint}")

    probe = subprocess.run(  # noqa: S603 - a fixed argv, no shell
        [docker, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            "SKIPPED LOUDLY: docker is installed but its daemon is not answering "
            f"({probe.stderr.strip() or probe.stdout.strip()}). Start it, or run: {hint}"
        )

    already = subprocess.run(  # noqa: S603
        [docker, "ps", "-q", "--filter", f"publish={REDIS_TEST_PORT}"],
        capture_output=True,
        text=True,
    )
    if already.stdout.strip():
        # Something is already serving the port — `tools/measure_redis_hosting.py`'s own
        # container, most likely. Use it and leave it alone; removing another process's
        # container would be a surprise this fixture has no business springing.
        yield REDIS_TEST_URL
        return

    run = subprocess.run(  # noqa: S603
        [docker, "run", "-d", "--rm", "--name", REDIS_CONTAINER,
         "-p", f"{REDIS_TEST_PORT}:6379", "redis:7-alpine", *REDIS_ARGS.split()],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(
            "SKIPPED LOUDLY: docker could not start the test Redis "
            f"({run.stderr.strip()}). By hand: {hint}"
        )
    try:
        _await_redis(REDIS_TEST_URL)
        yield REDIS_TEST_URL
    finally:
        subprocess.run(  # noqa: S603
            [docker, "rm", "-f", REDIS_CONTAINER], capture_output=True, text=True
        )


def _await_redis(url: str, attempts: int = 50) -> None:
    """Wait for the container's first `PING`. Not a clock dependency — a readiness poll
    with a bound, which fails the fixture rather than the tests underneath it."""
    import redis

    conn = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
    for _attempt in range(attempts):
        try:
            conn.ping()
            conn.close()
            return
        except Exception:  # noqa: BLE001 - any failure here is "not up yet"
            time.sleep(0.1)
    conn.close()
    pytest.skip(f"SKIPPED LOUDLY: the test Redis on {url} never answered PING")
