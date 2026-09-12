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

import contextlib
import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
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


#: The pipe's own flags, so the suite runs against the configuration
#: `docs/design/cloud/redis-hosting.md` §1 fixes — with `maxmemory` at 1gb rather than
#: prod's 2gb, because this runs on a laptop.
REDIS_ARGS = (
    "redis-server --save '' --appendonly no --maxmemory 1gb "
    "--maxmemory-policy noeviction"
)

#: Never 6379. A developer's own Redis, or a `dev` stack running under Compose, is on
#: the default port, and a suite that trimmed and deleted keys there would eat a running
#: system's streams. #92: it used to be the *only* other rule — one fixed port (6399)
#: and one fixed container name (`deltapayoff-tests-redis-6399`) for every session. That
#: made every worktree share one Redis: a second `pytest` session's teardown deleted the
#: stream keys a first session's live reader was still reading from, 28 seconds after
#: its own fixture had started it. The fix below is per-session on every axis a second
#: session could otherwise collide on — container name, port, and (see
#: `RedisTestSession.session_id`) the docker label that gates removal — rather than
#: trading the collision for a slower suite, which running out of four worktrees at once
#: cannot afford.
REDIS_RESERVED_PORT = 6379

#: The docker label a container this fixture started carries, valued at its own
#: `session_id`. Read back before every `docker rm`, so "a session never removes a
#: container it did not start" holds even if two sessions' names ever collided, not only
#: while they happen not to.
SESSION_LABEL = "deltapayoff.test-session"

#: A third axis, deliberately not here: the stream *key* itself carries no per-session
#: component, because `events/redis_wire.stream_name()` is not allowed to grow one.
#: #74 removed the last environment section from that grammar on purpose, and
#: `test_redis_wire.py::test_stream_names_do_not_include_an_environment_section` pins
#: it — a key a production consumer builds from configuration must be the same key a
#: test publishes to, or the contract this suite exists to prove stops meaning anything.
#: `stream_name()` is out of this ticket's scope for the same reason. The isolation this
#: still needs comes from the other two axes instead: two sessions with different
#: containers on different ports are never reading or trimming the same keyspace, so a
#: shared key *string* between them never becomes a shared key.


@dataclass(frozen=True)
class RedisTestSession:
    """One session's slice of the test-only Redis namespace — never shared, never reused.

    `session_id` seeds the other two fields and doubles as the docker label value, so a
    container can always be asked "whose are you" rather than trusted by name alone.
    """

    session_id: str
    container: str
    port: int

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}"


def _free_port(*, avoid: frozenset[int] = frozenset()) -> int:
    """A TCP port the OS says is free right now, never `REDIS_RESERVED_PORT` and never
    one already claimed by another session built in this same process (`avoid`) — the
    OS alone will not repeat a port to us that fast, but two sibling calls asking for a
    free port in the same microsecond deserve a guarantee rather than a laptop's odds."""
    for _attempt in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port != REDIS_RESERVED_PORT and port not in avoid:
            return port
    raise RuntimeError("could not find a free test-Redis port after 20 attempts")


def new_redis_test_session(
    *, avoid_ports: frozenset[int] = frozenset()
) -> RedisTestSession:
    """A fresh, unique slice of the namespace. Called once per real session by
    `redis_server`; called twice in one process by `test_redis_test_session.py` to prove
    the two never coincide."""
    session_id = f"{uuid.uuid4().hex[:10]}"
    return RedisTestSession(
        session_id=session_id,
        container=f"deltapayoff-tests-redis-{session_id}",
        port=_free_port(avoid=avoid_ports),
    )


@contextlib.contextmanager
def redis_test_session(
    session: RedisTestSession | None = None,
) -> Iterator[RedisTestSession]:
    """Start (or adopt) one throwaway Redis and remove only what this call started.

    **Skips loudly rather than passing quietly.** A Redis contract suite that silently
    became zero tests when Docker was not running would let the bus regress with a green
    suite over it, which is the plausible-and-wrong failure this project keeps refusing.
    The skip reason names the exact command, so a reader of the summary can turn the
    suite back on in one paste.

    Nothing here touches the network: the container is local, the port is loopback and
    the image is whatever Docker already has or pulls once.
    """
    session = session or new_redis_test_session()
    docker = shutil.which("docker")
    hint = (
        f"docker run -d --rm --name {session.container} "
        f"--label {SESSION_LABEL}={session.session_id} "
        f"-p {session.port}:6379 redis:7-alpine {REDIS_ARGS}"
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
        [docker, "ps", "-q", "--filter", f"publish={session.port}"],
        capture_output=True,
        text=True,
    )
    if already.stdout.strip():
        # This session's own freshly-chosen port is already spoken for — a race against
        # something else binding it in the instant between our probe and this check, at
        # worst. Use it and leave it alone: removing a container this session did not
        # start is exactly the failure #92 exists to close off, so there is no "reuse
        # it" branch here that also owns it.
        yield session
        return

    run = subprocess.run(  # noqa: S603
        [docker, "run", "-d", "--rm", "--name", session.container,
         "--label", f"{SESSION_LABEL}={session.session_id}",
         "-p", f"{session.port}:6379", "redis:7-alpine", *REDIS_ARGS.split()],
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.skip(
            "SKIPPED LOUDLY: docker could not start the test Redis "
            f"({run.stderr.strip()}). By hand: {hint}"
        )
    try:
        _await_redis(session.url)
        yield session
    finally:
        _remove_own_container(docker, session)


def _owner_label(docker: str, container: str) -> tuple[bool, str | None]:
    """`(exists, label)`. `exists` is false when `docker inspect` cannot find the name at
    all — already gone, nothing to remove, not a foreign owner to report."""
    inspected = subprocess.run(  # noqa: S603
        [docker, "inspect", "-f", f'{{{{index .Config.Labels "{SESSION_LABEL}"}}}}',
         container],
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        return False, None
    label = inspected.stdout.strip()
    return True, (label or None)


def _remove_own_container(docker: str, session: RedisTestSession) -> None:
    """The rule that makes the failure impossible, not just unlikely: read the label
    back and refuse to remove a container this session did not start.

    The name already embeds `session_id`, so nothing else should ever be able to answer
    to it — this is the check that turns "should never" into "cannot", by naming the
    foreign owner instead of guessing whose container it is."""
    exists, owner = _owner_label(docker, session.container)
    if not exists:
        return  # already gone -- nothing this session owns is still there to remove
    if owner != session.session_id:
        raise RuntimeError(
            f"refusing to remove {session.container!r}: it is owned by "
            f"{owner!r}, not this session ({session.session_id!r})"
        )
    subprocess.run(  # noqa: S603
        [docker, "rm", "-f", session.container], capture_output=True, text=True
    )


@pytest.fixture(scope="session")
def redis_server() -> Iterator[str]:
    """A throwaway Redis, unique to this pytest session, started and removed here.

    See `redis_test_session` for the mechanics and `RedisTestSession` for what "unique"
    covers. Fixtures downstream (`test_bus_contract.py`, `test_alert_consumer.py`,
    `test_process_split.py`) only ever see the URL, exactly as before #92.
    """
    with redis_test_session() as session:
        yield session.url


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
