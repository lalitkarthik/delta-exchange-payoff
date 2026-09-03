"""The live chain cache: latest frame per symbol, rebuilt into a chain on demand.

This computes nothing. It keeps the most recent frame each contract sent and hands the
collection to `wire.chain_from_frames`, which is the same decoder the REST path's tests
already cover. What it adds is *which* frames belong to the chain a browser asked for,
and the answer that there is no chain yet.

No network. Frames come from the captured fixtures, or are built inline where the test is
about the cache rather than the decoding.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

from deltapayoff.black76 import call_price, put_price
from deltapayoff.fanout import FanOut
from deltapayoff.feed import Quote
from deltapayoff.stream import ChainStream

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


def quote(frame, channel):
    return Quote(
        symbol=frame["sy"], channel=channel, received_at=0.0, frame=frame
    )


def test_a_chain_is_none_until_something_has_arrived() -> None:
    """An empty cache is not an empty chain.

    Returning a `ChainResponse` with no rows would render as a blank ladder and read as
    'Delta lists nothing', when the truth is that the socket has not spoken yet.
    """
    stream = ChainStream()

    assert stream.chain("BTC", EXPIRY) is None


def test_applying_frames_builds_the_chain_they_describe() -> None:
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("P-BTC-77600-040926", 120, 125), "ticker"))

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
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-BTC-80000-110926", 200, 210), "ticker"))

    front = stream.chain("BTC", EXPIRY)
    later = stream.chain("BTC", OTHER_EXPIRY)

    assert [r.strike for r in front.rows] == [77_600.0]
    assert [r.strike for r in later.rows] == [80_000.0]


def test_only_the_requested_underlying_is_included() -> None:
    """ETH strikes are three orders of magnitude below BTC's, so a leak here would not
    look like an error — it would look like a chain with a very wide ladder."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-ETH-4000-040926", 12, 14), "ticker"))

    chain = stream.chain("BTC", EXPIRY)

    assert [r.strike for r in chain.rows] == [77_600.0]
    assert [r.strike for r in stream.chain("ETH", EXPIRY).rows] == [4_000.0]


def test_the_latest_frame_wins() -> None:
    """The cache holds one frame per contract, not a history. A quote that arrived two
    seconds ago is not evidence of anything once a newer one exists."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-BTC-77600-040926", 601, 607), "ticker"))

    chain = stream.chain("BTC", EXPIRY)

    assert chain.rows[0].call.bid == 601.0
    assert chain.rows[0].call.ask == 607.0


def test_the_book_overrides_the_ticker_quote() -> None:
    """Both channels carry the top of book; `ob_l2` refreshes every 508 ms against
    `ticker`'s 5001 ms. Taking the book's copy is where the freshness comes from."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(book("C-BTC-77600-040926", 601, 607), "ob_l2"))

    chain = stream.chain("BTC", EXPIRY)

    assert chain.rows[0].call.bid == 601.0
    # The ticker frame still supplies everything the book does not carry.
    assert chain.rows[0].call.delta == 0.55
    assert chain.rows[0].call.oi == 100.0


def test_a_book_frame_alone_is_not_a_chain_row() -> None:
    """`ob_l2` carries no spot, no Greeks and no open interest. A row built from it
    alone would render as a mostly empty line rather than as a quote."""
    stream = ChainStream()
    stream.apply(quote(book("C-BTC-77600-040926", 601, 607), "ob_l2"))

    assert stream.chain("BTC", EXPIRY) is None


def test_the_stream_drains_the_bus_it_subscribes_to() -> None:
    """Wired to the `FanOut`, not to the socket. The feed publishes and never waits."""

    async def scenario():
        bus = FanOut()
        stream = ChainStream()
        stream.attach(bus, maxsize=100)

        bus.publish(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
        bus.publish(quote(ticker("P-BTC-77600-040926", 120, 125), "ticker"))

        task = asyncio.create_task(stream.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return stream.chain("BTC", EXPIRY)

    chain = asyncio.run(scenario())

    assert chain is not None
    assert chain.rows[0].call is not None
    assert chain.rows[0].put is not None


def test_symbols_for_an_expiry_are_reported_for_subscribing() -> None:
    """The feed needs a symbol list to subscribe. It comes from REST at start-up, but
    the stream knows what it has actually seen, which is what the screen can show."""
    stream = ChainStream()
    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))
    stream.apply(quote(ticker("C-BTC-80000-110926", 200, 210), "ticker"))

    assert stream.symbols("BTC", EXPIRY) == ["C-BTC-77600-040926"]


def test_a_real_captured_chain_rebuilds_from_the_cache(
    ws_ticker_frames, ws_book_frames
) -> None:
    """End to end on the 136-symbol capture: every frame in, one full chain out."""
    stream = ChainStream()
    for frame in ws_ticker_frames.values():
        stream.apply(quote(frame, "ticker"))
    for frame in ws_book_frames.values():
        stream.apply(quote(frame, "ob_l2"))

    chain = stream.chain("BTC", EXPIRY)

    assert len(chain.rows) == 69
    assert chain.spot is not None
    assert chain.atm_strike is not None


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
        stream.apply(quote(call_frame, "ticker"))
        stream.apply(quote(put_frame, "ticker"))


def test_an_arriving_quote_marks_its_expiry_dirty() -> None:
    """Arrival is what schedules work. Nothing else does."""
    stream = ChainStream()
    assert stream.dirty == set()

    stream.apply(quote(ticker("C-BTC-77600-040926", 579, 584), "ticker"))

    assert stream.dirty == {("BTC", EXPIRY)}


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

    stream.apply(quote(ticker("C-BTC-77600-040926", 590, 595), "ticker"))

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
    stream.apply(quote(ticker("C-BTC-77600-040927", 590, 595), "ticker"))
    assert stream.computed_chains() != []
    assert stream.dirty == {("BTC", FITTABLE_EXPIRY)}


def test_the_offered_chains_are_a_snapshot_the_loop_cannot_change_underneath() -> None:
    """A list, not the live dictionary. The writer walks it while the recompute task may
    be replacing entries, and mutating a dict during iteration raises."""
    stream = ChainStream()
    two_sided(stream)
    stream.recompute_dirty()

    held = stream.computed_chains()
    stream.apply(quote(ticker("C-BTC-77600-040926", 590, 595), "ticker"))
    stream.recompute_dirty()

    assert len(held) == 1, "the sampler's snapshot grew under it"
