"""Compute follows the page: interest, the grace window, and the two cadences — #44.

The live 100 ms pass solves the `(underlying, expiry)` pairs a browser has registered and
nothing else; a separate once-a-minute pass solves whatever had a frame in the minute it
is closing and hands the result to the bar writer, so the store covers the whole board
with no browser open anywhere.

Two seams. The chain cache directly, where the counting and the minute filter can be
asserted exactly on an injected clock; and the application under `TestClient` with
`DELTA_LIVE_FEED=0`, where real websocket clients open and close and `/health` is read
back over HTTP — which is the only place the handler's own register-and-release is
exercised rather than assumed.

No network: `conftest.py` refuses it and the feed is off.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltapayoff import main
from deltapayoff.stream import GRACE_SECONDS, ChainStream
from fakes.decoder import events_from_frame

#: Far-dated on purpose. `compute.enrich` prices from now to settlement, so a 2026
#: expiry would have run out and every `iv` here would be null — the ladders would still
#: be produced and the test would still pass while asserting nothing about the numbers.
EXPIRY = "04-09-2027"
OTHER_EXPIRY = "11-09-2027"
THIRD_EXPIRY = "18-09-2027"


def ticker(symbol: str, bid: float = 579, ask: float = 584) -> dict:
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


def feed(stream: ChainStream, symbol: str, bid: float = 579, ask: float = 584) -> None:
    """One frame through the real decoder, and every event it produced into the cache."""
    for event in events_from_frame("ticker", ticker(symbol, bid, ask)):
        stream.apply(event)


@pytest.fixture
def clocked() -> ChainStream:
    """A cache on clocks a test drives. Grace is a duration; the minute filter is not.

    `clock` is the monotonic reading the grace window is measured on and `wall` is the
    wall clock the minute pass buckets against, and both are attributes precisely so a
    test asserts the boundary rather than sleeping across it.
    """
    stream = ChainStream()
    stream.clock = lambda: _monotonic[0]
    stream.wall = lambda: _wall[0]
    return stream


_monotonic = [1_000.0]
#: An exact wall-clock minute boundary: `1_757_000_040 % 60 == 0`. The minute pass
#: buckets on the wall clock, so a test that started mid-minute would be asserting
#: an arithmetic accident rather than the rule.
_wall = [1_757_000_040.0]


@pytest.fixture(autouse=True)
def _reset_clocks():
    _monotonic[0] = 1_000.0
    _wall[0] = 1_757_000_040.0
    yield


# --------------------------------------------------------------- the count itself


def test_two_viewers_on_one_pair_and_one_on_another_are_counted_apart(clocked) -> None:
    """The count is per pair. Nothing here is a global "somebody is watching" flag."""
    clocked.watch("BTC", EXPIRY)
    clocked.watch("BTC", EXPIRY)
    clocked.watch("BTC", OTHER_EXPIRY)

    assert clocked.watching() == [
        ("BTC", EXPIRY, 2, None),
        ("BTC", OTHER_EXPIRY, 1, None),
    ]


def test_one_viewer_leaving_does_not_stop_the_solve_for_the_other(clocked) -> None:
    """**Why a count and not a flag.** Two tabs on one expiry is the ordinary case, and
    the first of them closing must leave the second's ladder moving."""
    clocked.watch("BTC", EXPIRY)
    clocked.watch("BTC", EXPIRY)

    clocked.unwatch("BTC", EXPIRY)

    assert clocked.watching() == [("BTC", EXPIRY, 1, None)]
    feed(clocked, "C-BTC-77600-040927")
    assert clocked.recompute_watched() == 1, "the surviving viewer stopped being solved"


def test_a_release_with_no_matching_watch_cannot_drive_the_count_negative(
    clocked,
) -> None:
    """The handler's `finally` runs on paths that never registered. A negative count
    would then swallow the next real `watch` and stop the solve silently."""
    clocked.unwatch("BTC", EXPIRY)
    clocked.unwatch("BTC", EXPIRY)

    clocked.watch("BTC", EXPIRY)

    assert clocked.watching() == [("BTC", EXPIRY, 1, None)]


# --------------------------------------------------------------- the grace window


def test_a_released_pair_keeps_being_solved_for_the_grace_and_then_stops(
    clocked,
) -> None:
    """The whole point of the grace: flipping back to an expiry is instant."""
    feed(clocked, "C-BTC-77600-040927")
    clocked.watch("BTC", EXPIRY)
    clocked.unwatch("BTC", EXPIRY)

    _monotonic[0] += GRACE_SECONDS - 1
    assert clocked.watching() == [("BTC", EXPIRY, 0, 1.0)]
    feed(clocked, "C-BTC-77600-040927", 580, 585)
    assert clocked.recompute_watched() == 1, "the grace did not keep the pair solved"

    _monotonic[0] += 2
    feed(clocked, "C-BTC-77600-040927", 581, 586)
    assert clocked.recompute_watched() == 0, "the grace never elapsed"
    assert clocked.watching() == []


def test_returning_inside_the_grace_clears_it_rather_than_stacking_a_second_timer(
    clocked,
) -> None:
    """Leaving and coming back must not leave a release armed behind the new viewer."""
    clocked.watch("BTC", EXPIRY)
    clocked.unwatch("BTC", EXPIRY)
    _monotonic[0] += GRACE_SECONDS - 1

    clocked.watch("BTC", EXPIRY)
    _monotonic[0] += 5

    assert clocked.watching() == [("BTC", EXPIRY, 1, None)]


def test_the_expired_pair_is_dropped_by_the_pass_and_not_by_the_report(clocked) -> None:
    """A monitor polling `/health` must not be the thing driving the recompute set, and
    one that stops polling must not leave expired entries alive."""
    clocked.watch("BTC", EXPIRY)
    clocked.unwatch("BTC", EXPIRY)
    _monotonic[0] += GRACE_SECONDS + 1

    assert clocked.watching() == [], "an elapsed grace was reported as live"
    assert clocked._watch, "the report dropped the entry itself"

    assert clocked.prune_watches() == 1
    assert clocked._watch == {}


# --------------------------------------------------------------- the live pass


def test_the_live_pass_solves_only_the_watched_pair(clocked) -> None:
    """The ticket's own probe: the count of solves per tick, with three expiries listed
    and one browser open. Before #44 this was three."""
    feed(clocked, "C-BTC-77600-040927")
    feed(clocked, "C-BTC-77600-110927")
    feed(clocked, "C-BTC-77600-180927")
    assert len(clocked.dirty) == 3

    clocked.watch("BTC", OTHER_EXPIRY)
    solved = clocked.recompute_watched()

    assert solved == 1, "the live pass solved something nobody asked for"
    assert clocked.dirty == {("BTC", EXPIRY), ("BTC", THIRD_EXPIRY)}


def test_an_unwatched_dirty_pair_is_left_dirty_rather_than_cleared(clocked) -> None:
    """Skipping is not the same as clearing. A cleared pair would leave `_computed`
    holding a stale ladder that `chain()` would then serve as current, and would leave
    the minute pass nothing to find."""
    feed(clocked, "C-BTC-77600-040927")

    assert clocked.recompute_watched() == 0
    assert clocked.dirty == {("BTC", EXPIRY)}
    assert clocked.computed_chains() == []

    # A reader still gets a fresh ladder: `chain()` recomputes a dirty pair itself.
    assert clocked.chain("BTC", EXPIRY) is not None


def test_the_writer_takes_only_the_ladders_the_live_pass_keeps_fresh(clocked) -> None:
    """`live_computed_chains` is what `BarWriter` samples six times a minute. An expiry
    solved once a minute must not be in it, or five of those six samples would meet the
    same ladder in a minute already sealed and each would be counted as `late`."""
    feed(clocked, "C-BTC-77600-040927")
    feed(clocked, "C-BTC-77600-110927")
    clocked.watch("BTC", EXPIRY)
    clocked.recompute_watched()
    clocked.recompute_closing_minute()

    assert len(clocked.computed_chains()) == 2, "the minute pass solved nothing"
    assert [c.expiry for c in clocked.live_computed_chains()] == [EXPIRY]


# --------------------------------------------------------------- the minute pass


def test_the_minute_pass_solves_the_board_with_nothing_watched(clocked) -> None:
    """It runs whether or not a browser is open. That is its entire reason to exist."""
    feed(clocked, "C-BTC-77600-040927")
    feed(clocked, "C-BTC-77600-110927")
    feed(clocked, "C-BTC-77600-180927")

    produced = clocked.recompute_closing_minute()

    assert sorted(chain.expiry for chain in produced) == sorted(
        [EXPIRY, OTHER_EXPIRY, THIRD_EXPIRY]
    )
    assert clocked.dirty == set()


def test_an_expiry_with_no_frames_this_minute_gets_no_ladder_from_the_minute_pass(
    clocked,
) -> None:
    """**The no-forward-fill rule, kept.** A ladder is bucketed on the instant it was
    computed, so solving a pair whose newest frame arrived in an earlier minute would
    write a computed bar into a minute that had no arrivals — one manufactured row per
    expiry every time a feed goes quiet. It is skipped, counted, and left dirty."""
    feed(clocked, "C-BTC-77600-040927")

    _wall[0] += 61  # the feed went silent; a whole minute passed with no frame

    assert clocked.recompute_closing_minute() == []
    assert clocked.minute_pass_skipped == 1
    assert clocked.dirty == {("BTC", EXPIRY)}, "the arrival was silently discarded"


def test_the_minute_pass_produces_one_ladder_per_expiry_per_minute(clocked) -> None:
    """One row per expiry per minute, and no more: a second pass inside the same minute
    with nothing new arriving produces nothing at all."""
    feed(clocked, "C-BTC-77600-040927")
    feed(clocked, "C-BTC-77600-110927")

    _wall[0] += 59.5
    first = clocked.recompute_closing_minute()

    _wall[0] += 0.1
    second = clocked.recompute_closing_minute()

    assert len(first) == 2
    assert second == []


def test_a_quiet_expiry_never_re_enters_the_minute_pass_on_its_own(clocked) -> None:
    """A pair skipped for having no frame this minute stays skipped, minute after
    minute, until a frame of its own arrives. The store therefore simply stops having
    anything to say about a dead expiry, rather than repeating its last ladder."""
    feed(clocked, "C-BTC-77600-040927")
    _wall[0] += 61

    for _ in range(5):
        assert clocked.recompute_closing_minute() == []
        _wall[0] += 60

    assert clocked.minute_pass_skipped == 5

    feed(clocked, "C-BTC-77600-040927", 590, 595)
    assert len(clocked.recompute_closing_minute()) == 1


# --------------------------------------------------------------- the HTTP seam


class _StubDeltaClient:
    """Enough of `DeltaClient` for the lifespan to open and close one. No socket."""

    async def __aenter__(self) -> _StubDeltaClient:
        return self

    async def aclose(self) -> None:
        return None


@pytest.fixture
def live_app(monkeypatch: pytest.MonkeyPatch):
    """The real application, lifespan run, feed off. `app.state.stream` is the real one.

    `TestClient` **is** entered as a context manager here, unlike `test_ws_endpoint.py`,
    because the point is the cache the running application holds rather than a hand-fed
    one — `/health` reports what the engine is actually solving, and a dependency
    override would make that assertion about the test's own object. `DELTA_LIVE_FEED` is
    already `0` from `conftest.py`, so nothing subscribes; the HTTP client is stubbed
    because the lifespan opens one before it looks at that switch.
    """
    monkeypatch.setattr(main, "DeltaClient", _StubDeltaClient)
    with TestClient(main.app) as client:
        yield client


def watched(client: TestClient) -> list[dict]:
    return client.get("/health").json()["watched"]


def test_the_health_report_counts_the_browsers_on_each_pair(live_app) -> None:
    """Two sockets on one pair and one on another: counts 2 and 1, over HTTP."""
    url = "/ws/chain?underlying=BTC&expiry={}&interval=0.02"
    with live_app.websocket_connect(url.format(EXPIRY)) as one:
        one.receive_text()
        with live_app.websocket_connect(url.format(EXPIRY)) as two:
            two.receive_text()
            with live_app.websocket_connect(url.format(OTHER_EXPIRY)) as three:
                three.receive_text()

                assert watched(live_app) == [
                    {
                        "underlying": "BTC",
                        "expiry": EXPIRY,
                        "viewers": 2,
                        "grace_remaining_seconds": None,
                    },
                    {
                        "underlying": "BTC",
                        "expiry": OTHER_EXPIRY,
                        "viewers": 1,
                        "grace_remaining_seconds": None,
                    },
                ]


def test_the_pairs_survive_the_grace_after_the_last_socket_closes_and_then_go(
    live_app,
) -> None:
    """Close every socket: the pairs stay, in grace, with the time remaining on the
    report — and then disappear once it elapses."""
    stream = main.app.state.stream
    stream.clock = lambda: _monotonic[0]

    url = "/ws/chain?underlying=BTC&expiry={}&interval=0.02"
    with live_app.websocket_connect(url.format(EXPIRY)) as one:
        one.receive_text()
        with live_app.websocket_connect(url.format(OTHER_EXPIRY)) as two:
            two.receive_text()
            assert len(watched(live_app)) == 2

    _wait_until(lambda: all(row["viewers"] == 0 for row in watched(live_app)))

    in_grace = watched(live_app)
    assert len(in_grace) == 2, "a closed pair vanished instead of entering its grace"
    assert all(row["grace_remaining_seconds"] == 30.0 for row in in_grace), in_grace

    _monotonic[0] += GRACE_SECONDS + 1

    assert watched(live_app) == []


def test_a_refused_handshake_registers_no_interest(live_app) -> None:
    """A pair spelled wrongly is not a pair. Registering before validating would pin a
    nonexistent expiry into the watched set for its whole grace."""
    with live_app.websocket_connect("/ws/chain?underlying=XRP&expiry=" + EXPIRY) as ws:
        assert json.loads(ws.receive_text())["type"] == "error"

    assert watched(live_app) == []


def test_the_recompute_set_changing_is_logged_at_debug(
    live_app, caplog: pytest.LogCaptureFixture
) -> None:
    """#42's rule: routine and bracketed, once per connection per end, never per
    message. The event is `compute.recompute_set` — the set the live pass solves."""
    caplog.set_level(10, logger="deltapayoff.stream")

    url = f"/ws/chain?underlying=BTC&expiry={EXPIRY}&interval=0.02"
    with live_app.websocket_connect(url) as socket:
        socket.receive_text()
    _wait_until(
        lambda: all(row[2] == 0 for row in main.app.state.stream.watching())
        and main.app.state.stream.watching() != []
    )

    records = [r for r in caplog.records if r.event == "compute.recompute_set"]
    messages = [r.getMessage() for r in records]

    assert any("watched" in message for message in messages), messages
    assert any("released" in message for message in messages), messages


def test_the_minute_pass_writes_the_board_with_no_socket_open(tmp_path: Path) -> None:
    """The acceptance line, at the seam: **no browser anywhere**, and the store still
    gains computed rows for every expiry that had a frame — and none for the one that
    did not.

    Driven through `BarWriter` on a `tmp_path` with the real hand-over the application
    wires: `stream.recompute_closing_minute()` into `writer.sample_chains`. The writer's
    own periodic sample is deliberately *not* the path, because by design it never sees
    an unwatched expiry, and nothing here is watched.

    **Row counts per minute are asserted on the ladders handed over, not on the stored
    rows.** A ladder is stamped by `raw_chain` from the real clock, so three simulated
    minutes all land in one real one and the store folds them to a row apiece — which
    says nothing either way. What the store is asked is the question it can answer: that
    rows exist at all with no socket open, and for which contracts.
    """
    from deltapayoff.store import (
        COMPUTED_DATASET,
        COMPUTED_SCHEMA,
        BarStore,
        BarWriter,
    )

    stream = ChainStream()
    stream.wall = lambda: _wall[0]
    writer = BarWriter(BarStore(tmp_path), chains=stream.live_computed_chains)
    assert writer.recording
    assert stream.watching() == [], "something is watching; this test is about nothing"

    # Both sides of each strike: `compute.enrich` recovers the forward from put-call
    # parity, and a ladder of calls alone yields a `computed` block with a null `iv`.
    handed: list[list[str]] = []
    for minute in range(3):
        for right in ("C", "P"):
            feed(stream, f"{right}-BTC-77600-040927", 579 + minute, 584 + minute)
            feed(stream, f"{right}-BTC-77600-110927", 100 + minute, 105 + minute)
            # The silent one: in the cache, and never quoted again after minute 0.
            if minute == 0:
                feed(stream, f"{right}-BTC-77600-180927", 20, 25)
        _wall[0] += 59.5
        produced = stream.recompute_closing_minute()
        writer.sample_chains(produced)
        handed.append(sorted(chain.expiry for chain in produced))
        _wall[0] += 0.5

    # Minute 0 closes three expiries; minutes 1 and 2 close the two that had frames in
    # them. The third is skipped both times — **not** re-solved into a minute it was
    # silent through, which is the no-forward-fill rule this table is held to.
    assert handed == [
        [EXPIRY, OTHER_EXPIRY, THIRD_EXPIRY],
        [EXPIRY, OTHER_EXPIRY],
        [EXPIRY, OTHER_EXPIRY],
    ]
    # Nothing was *skipped*: the third expiry went clean when minute 0 closed it, so it
    # is not even a candidate afterwards. The skip path — dirty, but from an earlier
    # minute — is the one `test_an_expiry_with_no_frames_this_minute...` drives.
    assert stream.minute_pass_skipped == 0
    assert stream.live_computed_chains() == [], "the live pass solved something"

    writer._seal(time.time() + 120)
    writer._flush_all()

    frame = (
        BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
        .scan()
        .collect()
    )

    assert frame.height > 0, "no browser open and the store stayed empty"
    assert set(frame["symbol"].to_list()) == {
        f"{right}-BTC-77600-{suffix}"
        for right in ("C", "P")
        for suffix in ("040927", "110927", "180927")
    }
    assert set(frame["iv"].to_list()) != {None}, "rows carry no implied volatility"


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never held")
