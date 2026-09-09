"""The live chain cache: latest events per instrument, rebuilt into a chain on demand.

This computes nothing. It keeps the most recent `md.option_quote` and the most recent
`md.option_reference` each contract sent, and the latest spot per underlying, and folds
them into the same `ChainResponse` the REST path returns. What it adds is *which* events
belong to the chain a browser asked for, and the answer that there is no chain yet.

**The events are the producer's, not this file's.** Captured frames go through the real
`DeltaAdapter.events_from_frame`, so a cache test cannot pass against a catalogue the
adapter has stopped filling. Since #37 nothing here names a venue channel except as the
argument that selects which decode to run.

No network. Frames come from the captured fixtures, or are built inline where the test is
about the cache rather than the decoding.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

import pytest

from deltapayoff.black76 import call_price, put_price
from deltapayoff.events import IndexQuote
from deltapayoff.fanout import FanOut
from deltapayoff.stream import ChainStream
from fakes.decoder import ARRIVED_AT, events_from_frame

#: An arrival stamp, aware, because the envelope refuses a naive one.
ARRIVED = datetime.fromtimestamp(ARRIVED_AT, tz=timezone.utc)

EXPIRY = "04-09-2026"
OTHER_EXPIRY = "11-09-2026"

#: A far-dated expiry for the T8 tests below. It has to stay in the future for the
#: year fraction to be positive, and far enough out that the implied rate the fit
#: recovers lands inside the plausible band rather than exploding as T goes to zero.
FITTABLE_SUFFIX = "040927"
FITTABLE_EXPIRY = "04-09-2027"
FITTABLE_FORWARD = 77_600


def ticker(symbol, bid, ask, spot="77651.9"):
    return {
        "type": "ticker",
        "sy": symbol,
        "sp": spot,
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


def book(symbol, bid, ask):
    return {
        "type": "ob_l2",
        "sy": symbol,
        "ts": 1,
        "lts": 1,
        "a": [[str(ask), "10"]],
        "b": [[str(bid), "10"]],
    }


def feed(stream, frame, channel):
    """Decode one frame the way the live path does and apply every event it produced.

    A ticker frame is two events — the contract's reference and the underlying's spot —
    and both reach the cache, which is exactly what the running engine publishes.
    """
    for event in events_from_frame(channel, frame):
        stream.apply(event)


def test_a_chain_is_none_until_something_has_arrived() -> None:
    """An empty cache is not an empty chain.

    Returning a `ChainResponse` with no rows would render as a blank ladder and read as
    'Delta lists nothing', when the truth is that the socket has not spoken yet.
    """
    stream = ChainStream()

    assert stream.chain("BTC", EXPIRY) is None


def test_applying_frames_builds_the_chain_they_describe() -> None:
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("P-BTC-77600-040926", 120, 125), "ticker")

    chain = stream.chain("BTC", EXPIRY)

    assert chain is not None
    assert [row.strike for row in chain.rows] == [77_600.0]
    assert chain.rows[0].call.bid == 579.0
    assert chain.rows[0].put.ask == 125.0
    assert chain.spot == 77_651.9


def test_only_the_requested_expiry_is_included() -> None:
    """The feed carries every listed expiry on one connection. A chain screen shows one.

    Without this filter every strike of every expiry would be folded onto the same
    ladder by strike, silently mixing contracts that settle weeks apart.
    """
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-BTC-80000-110926", 200, 210), "ticker")

    front = stream.chain("BTC", EXPIRY)
    later = stream.chain("BTC", OTHER_EXPIRY)

    assert [r.strike for r in front.rows] == [77_600.0]
    assert [r.strike for r in later.rows] == [80_000.0]


def test_only_the_requested_underlying_is_included() -> None:
    """ETH strikes are three orders of magnitude below BTC's, so a leak here would not
    look like an error — it would look like a chain with a very wide ladder."""
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-ETH-4000-040926", 12, 14), "ticker")

    chain = stream.chain("BTC", EXPIRY)

    assert [r.strike for r in chain.rows] == [77_600.0]
    assert [r.strike for r in stream.chain("ETH", EXPIRY).rows] == [4_000.0]


def test_the_latest_frame_wins() -> None:
    """The cache holds one frame per contract, not a history. A quote that arrived two
    seconds ago is not evidence of anything once a newer one exists."""
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-BTC-77600-040926", 601, 607), "ticker")

    chain = stream.chain("BTC", EXPIRY)

    assert chain.rows[0].call.bid == 601.0
    assert chain.rows[0].call.ask == 607.0


def test_the_book_quote_overrides_the_reference_quote() -> None:
    """Both events carry the top of book; the book's refreshes every 508 ms against the
    reference's 5001 ms. Taking the book's copy is where the freshness comes from, and it
    is taken **wholesale** — one side from each would be a spread nobody quoted."""
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, book("C-BTC-77600-040926", 601, 607), "ob_l2")

    chain = stream.chain("BTC", EXPIRY)

    assert chain.rows[0].call.bid == 601.0
    # The ticker frame still supplies everything the book does not carry.
    assert chain.rows[0].call.delta == 0.55
    assert chain.rows[0].call.oi == 100.0


def test_a_quote_event_alone_is_not_a_chain_row() -> None:
    """`md.option_quote` carries no mark, no Greeks and no open interest. A row built from
    it alone would render as a mostly empty line rather than as a quote."""
    stream = ChainStream()
    feed(stream, book("C-BTC-77600-040926", 601, 607), "ob_l2")

    assert stream.chain("BTC", EXPIRY) is None


def test_the_stream_drains_the_bus_it_subscribes_to() -> None:
    """Wired to the `FanOut`, not to the socket. The feed publishes and never waits."""

    async def scenario():
        bus = FanOut()
        stream = ChainStream()
        stream.attach(bus, maxsize=100)

        for frame in (
            ticker("C-BTC-77600-040926", 579, 584),
            ticker("P-BTC-77600-040926", 120, 125),
        ):
            for event in events_from_frame("ticker", frame):
                bus.publish(event)

        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return stream.chain("BTC", EXPIRY)

    chain = asyncio.run(scenario())

    assert chain is not None
    assert chain.rows[0].call is not None
    assert chain.rows[0].put is not None


def test_instruments_for_an_expiry_are_reported_for_subscribing() -> None:
    """The feed needs a contract list to subscribe. It comes from REST at start-up, but
    the stream knows what it has actually seen, which is what the screen can show.

    **Canonical strings and not the venue's symbols**, since #37: this is the cache's own
    key, and a second venue's ladder is enumerated by the same call.
    """
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-BTC-80000-110926", 200, 210), "ticker")

    assert stream.instruments("BTC", EXPIRY) == ["DELTA-BTC-20260904-77600-C-USD"]


def test_a_real_captured_chain_rebuilds_from_the_cache(
    ws_ticker_frames, ws_book_frames
) -> None:
    """End to end on the 136-symbol capture: every frame in, one full chain out."""
    stream = ChainStream()
    for frame in ws_ticker_frames.values():
        feed(stream, frame, "ticker")
    for frame in ws_book_frames.values():
        feed(stream, frame, "ob_l2")

    chain = stream.chain("BTC", EXPIRY)

    assert len(chain.rows) == 69
    assert chain.spot is not None
    assert chain.atm_strike is not None
    # #60 (I1): read off the instruments the frames decoded to, not a constant
    # sitting in `stream.py` — see `raw_chain`'s own comment on why.
    assert chain.quote_currency == "USD"


# --- T8: dirty tracking and the coalesced recompute ---------------------------------
#
# The cache now computes. These tests are about *when* it does so, not about the
# arithmetic — that lives in `test_compute.py`, reachable as a pure function.


def two_sided(stream: ChainStream, expiry_suffix: str = FITTABLE_SUFFIX) -> None:
    """Seven paired strikes, priced by this project's own Black-76 at 40%.

    Two constraints have to be met before `f1_parity_fit` will call a fit trusted, and
    both are easy to miss:

      * **at least `MIN_PAIRS` paired strikes** — hence seven, not three;
      * **an implied rate strictly inside (0, 30%)** — so the discount cannot be 1.0,
        which implies a rate of exactly zero and is rejected.

    The discount is therefore derived from the *actual* time to expiry at run time, at a
    plausible 8%. Pricing both legs from one forward also makes parity hold exactly, so
    the fit has a real answer to find rather than a noisy one.
    """
    settles = datetime.strptime(expiry_suffix, "%d%m%y").replace(
        hour=12, tzinfo=timezone.utc
    )
    years = (settles - datetime.now(timezone.utc)).total_seconds() / (365.0 * 86_400)
    discount = math.exp(-0.08 * years)

    for offset in (-3000, -2000, -1000, 0, 1000, 2000, 3000):
        strike = FITTABLE_FORWARD + offset
        call = call_price(FITTABLE_FORWARD, strike, years, 0.40, discount)
        put = put_price(FITTABLE_FORWARD, strike, years, 0.40, discount)
        call_frame = ticker(f"C-BTC-{strike}-{expiry_suffix}", call - 0.5, call + 0.5)
        put_frame = ticker(f"P-BTC-{strike}-{expiry_suffix}", put - 0.5, put + 0.5)
        feed(stream, call_frame, "ticker")
        feed(stream, put_frame, "ticker")


def test_an_arriving_quote_marks_its_expiry_dirty() -> None:
    """Arrival is what schedules work. Nothing else does."""
    stream = ChainStream()
    assert stream.dirty == set()

    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")

    assert stream.dirty == {("BTC", EXPIRY)}


def test_a_new_expiry_logs_the_recompute_set_changing_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#42's `compute.recompute_set`, at debug: a new `(underlying, expiry)` pair joining
    the cache is rare and worth a quiet line — but ~1,323 frames a second on an expiry
    already known must not repeat it, or the "debug, not per-message" promise breaks."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.stream")
    stream = ChainStream()

    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-BTC-77600-040926", 580, 585), "ticker")
    feed(stream, ticker("C-BTC-78000-040926", 600, 605), "ticker")

    records = [r for r in caplog.records if r.event == "compute.recompute_set"]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert "BTC" in records[0].getMessage()
    assert EXPIRY in records[0].getMessage()


def test_a_second_expiry_logs_a_second_time(caplog: pytest.LogCaptureFixture) -> None:
    """The set changing twice is logged twice — this is not a once-per-process latch."""
    caplog.set_level(logging.DEBUG, logger="deltapayoff.stream")
    stream = ChainStream()

    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-BTC-77600-110926", 579, 584), "ticker")

    records = [r for r in caplog.records if r.event == "compute.recompute_set"]
    assert len(records) == 2, [r.getMessage() for r in caplog.records]


def test_nothing_arriving_leaves_nothing_to_recompute() -> None:
    """A quiet market costs nothing. This is the whole point of the dirty set.

    At ~1,323 frames a second a timer that recomputed regardless would burn a core to
    reproduce numbers that had not changed.
    """
    stream = ChainStream()

    assert stream.recompute_dirty() == 0


def test_recomputing_clears_the_dirty_set() -> None:
    stream = ChainStream()
    two_sided(stream)
    assert stream.dirty == {("BTC", FITTABLE_EXPIRY)}

    recomputed = stream.recompute_dirty()

    assert recomputed == 1
    assert stream.dirty == set()


def test_only_dirty_expiries_are_recomputed() -> None:
    """One connection carries every expiry. A frame on one must not recompute the rest."""
    stream = ChainStream()
    two_sided(stream, "040926")
    two_sided(stream, "110926")
    stream.recompute_dirty()

    feed(stream, ticker("C-BTC-77600-040926", 590, 595), "ticker")

    assert stream.dirty == {("BTC", EXPIRY)}
    assert stream.recompute_dirty() == 1


def test_the_served_chain_carries_our_computed_values() -> None:
    """The reason the whole ticket exists: the ladder gets our numbers, not Delta's."""
    stream = ChainStream()
    two_sided(stream)
    stream.recompute_dirty()

    chain = stream.chain("BTC", FITTABLE_EXPIRY)

    assert chain is not None
    assert chain.forward is not None
    assert chain.rows[0].call.computed is not None
    assert chain.rows[0].call.computed.iv is not None


def test_a_chain_is_computed_on_demand_before_the_first_recompute() -> None:
    """Opening a new expiry must not show an uncomputed ladder while a timer catches up.

    The recompute loop refreshes what is cached; a cache miss computes there and then.
    """
    stream = ChainStream()
    two_sided(stream)

    chain = stream.chain("BTC", FITTABLE_EXPIRY)

    assert chain is not None
    assert chain.rows[0].call.computed.iv is not None


def test_delta_reference_columns_survive_the_stream() -> None:
    """The fixture's ticker carries Delta's Greeks. They must still be there."""
    stream = ChainStream()
    two_sided(stream)
    stream.recompute_dirty()

    chain = stream.chain("BTC", FITTABLE_EXPIRY)

    assert chain.rows[0].call.mark_iv == 0.30
    assert chain.rows[0].call.delta == 0.55


def test_a_chain_that_fails_to_enrich_is_retried_rather_than_dropped(monkeypatch) -> None:
    """One bad expiry must not silently strand the others on a stale cache.

    Clearing the dirty set up front and letting an exception escape would drop every
    remaining expiry in the pass: they leave `dirty` while `_computed` still holds their
    old chains, so `chain()` keeps serving those. On an expiry receiving no further
    frames that is a screen frozen at last minute's prices with nothing to say so.
    """
    stream = ChainStream()
    two_sided(stream, "040927")
    two_sided(stream, "110927")
    assert len(stream.dirty) == 2

    exploded: list[tuple[str, str]] = []

    def explode_on_the_first(key):
        exploded.append(key)
        if len(exploded) == 1:
            raise RuntimeError("this chain cannot be enriched")
        return ChainStream._compute(stream, key)

    monkeypatch.setattr(stream, "_compute", explode_on_the_first)

    recomputed = stream.recompute_dirty()

    # The survivor was computed; the failure was counted and put back for the next pass.
    assert recomputed == 1
    assert stream.recompute_errors == 1
    assert stream.dirty == {exploded[0]}


def test_a_failed_expiry_is_not_served_from_a_stale_cache() -> None:
    """Still dirty means `chain()` recomputes rather than handing back the old one."""
    stream = ChainStream()
    two_sided(stream, "040927")
    stream.recompute_dirty()
    first = stream.chain("BTC", FITTABLE_EXPIRY)
    assert first is not None

    # A new frame arrives, so the cached chain is now out of date.
    two_sided(stream, "040927")
    assert ("BTC", FITTABLE_EXPIRY) in stream.dirty

    again = stream.chain("BTC", FITTABLE_EXPIRY)

    assert again is not None
    assert again.fetched_at >= first.fetched_at
    assert ("BTC", FITTABLE_EXPIRY) not in stream.dirty


def test_the_computed_chains_are_offered_for_sampling_without_recomputing() -> None:
    """What table C reads. The store samples the chains the recompute loop has **already**
    produced; it never asks for one to be built.

    That distinction is the whole reason this is a separate method from `chain()`, which
    recomputes a dirty expiry synchronously so a reader never sees a stale ladder. Doing
    that from the writer's drain loop would move a chain build onto the pass that has to
    stay short, and would duplicate work the recompute task is already doing.
    """
    stream = ChainStream()
    two_sided(stream)

    assert stream.computed_chains() == [], "a chain was built for a sampler"

    stream.recompute_dirty()
    chains = stream.computed_chains()

    assert [chain.expiry for chain in chains] == [FITTABLE_EXPIRY]
    assert chains[0].rows[0].call.computed is not None
    # A dirty expiry stays dirty: sampling must not pretend the loop has run.
    feed(stream, ticker("C-BTC-77600-040927", 590, 595), "ticker")
    assert stream.computed_chains() != []
    assert stream.dirty == {("BTC", FITTABLE_EXPIRY)}


def test_the_offered_chains_are_a_snapshot_the_loop_cannot_change_underneath() -> None:
    """A list, not the live dictionary. The writer walks it while the recompute task may
    be replacing entries, and mutating a dict during iteration raises."""
    stream = ChainStream()
    two_sided(stream)
    stream.recompute_dirty()

    held = stream.computed_chains()
    feed(stream, ticker("C-BTC-77600-040926", 590, 595), "ticker")
    stream.recompute_dirty()

    assert len(held) == 1, "the sampler's snapshot grew under it"


# --- spot, which belongs to no contract and moves every ladder ---------------------


def index_quote(spot, underlying="BTC"):
    """One `md.index_quote`, built by the real decoder off a reference frame."""
    frame = ticker("C-BTC-77600-040926", 579, 584, spot=str(spot))
    (quote,) = [
        event
        for event in events_from_frame("ticker", frame)
        if type(event).__name__ == "IndexQuote"
    ]
    return quote


def test_a_spot_that_moves_marks_every_expiry_of_its_underlying() -> None:
    """**Spot belongs to no contract, so nothing else marks it dirty.**

    It sets the ATM strike, the forward and therefore every implied volatility on the
    ladder. An expiry left clean would go on serving a chain priced against the previous
    spot for as long as none of its own contracts ticked — last minute's volatility on
    screen with nothing saying so, which is the failure this project keeps refusing.
    """
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("C-BTC-80000-110926", 200, 210), "ticker")
    stream.recompute_dirty()
    assert stream.dirty == set()

    stream.apply(index_quote(90_000.0))

    assert stream.dirty == {("BTC", EXPIRY), ("BTC", OTHER_EXPIRY)}


def test_the_moved_spot_reaches_the_ladder_it_marked() -> None:
    """Not "it was marked dirty" — the number a reader is served actually changes."""
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    feed(stream, ticker("P-BTC-77600-040926", 120, 125), "ticker")
    before = stream.chain("BTC", EXPIRY)
    assert before.spot == 77_651.9

    stream.apply(index_quote(90_000.0))

    assert stream.chain("BTC", EXPIRY).spot == 90_000.0


def test_an_unchanged_spot_schedules_no_work() -> None:
    """The event arrives once per reference frame — `derived` ~118 a second — while spot
    moves far less often. Marking on arrival rather than on change would make every expiry
    dirty on every pass and burn a core reproducing numbers that had not moved."""
    stream = ChainStream()
    feed(stream, ticker("C-BTC-77600-040926", 579, 584), "ticker")
    stream.recompute_dirty()
    assert stream.dirty == set()

    stream.apply(index_quote(77_651.9))

    assert stream.dirty == set(), "an unchanged spot scheduled a recompute"
    assert stream.applied, "the observation was not counted at all"


def test_an_index_quote_carrying_no_spot_is_counted() -> None:
    """An absent spot is not an observation of absence — and `skipped` claims to count
    every record this cache made nothing of, so it has to count this one."""
    stream = ChainStream()
    stream.apply(IndexQuote(source="DELTA", ts_received=ARRIVED, underlying="BTC"))

    assert stream._spot == {}
    assert stream.skipped == 1
