"""`POST /analyse` — the route, over `TestClient`, against both read paths. No network.

The seam this feature agreed for the route: the app under `TestClient` with
`DELTA_LIVE_FEED=0`, a hand-fed `ChainStream` standing in for the live cache and four
`BarStore`s on a `tmp_path` standing in for the store. `tests/test_historical.py` is the
prior art for the second half and `tests/test_ws_endpoint.py` for the first.

**Every expected number here is a literal with a source beside it** — a price this file
fed in, a figure from `docs/payoff-contract.md`, or a hand-worked subtraction. Nothing
recomputes an expectation the way the route computes it.

**The live tests assert only what is independent of the wall clock.** The captured
expiry is a past date, so whether `compute.enrich` can fit a forward for it depends on
what day the suite runs; the entry prices, the corners, the breakevens and the premium
do not, and those are what the live tests pin. The fitted case — implied volatility,
Greeks, forward and discount — is pinned on the stored path instead, where every one of
those numbers is a row this file wrote rather than a solve against today's date.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deltapayoff.analyse import AnalyseRefusal, strategy_series
from deltapayoff.main import (
    HistoricalSource,
    app,
    get_historical_source,
    get_watched_stream,
)
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
)
from deltapayoff.stream import ChainStream
from fakes.decoder import events_from_frame
from test_historical import computed_bar, quote_bar, reference_bar, spot_bar

EXPIRY = "04-09-2026"
MINUTE = "2026-09-04T09:00:00Z"
CALL_77600 = "DELTA-BTC-20260904-77600-C-USD"
PUT_77600 = "DELTA-BTC-20260904-77600-P-USD"
CALL_78000 = "DELTA-BTC-20260904-78000-C-USD"


def ticker(symbol: str, bid: float | None, ask: float | None) -> dict[str, Any]:
    """One `ticker` frame, the shape `tests/test_ws_endpoint.py` sends. `None` on a side
    is Delta's own absent quote — the venue spells it `null` and this engine keeps it
    `null` rather than turning it into a zero."""
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
                "q": [
                    None if ask is None else str(ask),
                    "10",
                    None if bid is None else str(bid),
                    "20",
                    None,
                ],
                "qiv": ["0.31", "0.29", "0.30"],
                "g": ["0.55", "0.0003", "1.23", "-234.2", "16.58"],
                "oi": ["100", "200"],
            }
        ],
    }


def feed(stream: ChainStream, symbol: str, bid: float | None, ask: float | None) -> None:
    """One frame through the real decoder, and every event it produced into the cache."""
    for event in events_from_frame("ticker", ticker(symbol, bid, ask)):
        stream.apply(event)


@pytest.fixture
def live() -> ChainStream:
    """The live chain cache, in place of the one a running feed would fill."""
    return ChainStream()


def source_on(root: Path) -> HistoricalSource:
    """The store's four tables, under one root. Nothing here reads `data/`."""
    return HistoricalSource(
        quote=BarStore(root),
        reference=BarStore(root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA),
        computed=BarStore(root, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
        spot=BarStore(root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
    )


@pytest.fixture
def stores(tmp_path: Path) -> HistoricalSource:
    return source_on(tmp_path)


@pytest.fixture
def client(live: ChainStream, stores: HistoricalSource) -> Iterator[TestClient]:
    """Both read paths wired to the two fixtures above, and nothing else able to answer.

    `TestClient` deliberately not entered as a context manager, following `test_api.py`:
    doing so runs the lifespan, which subscribes every live BTC option over a socket.
    """
    app.dependency_overrides[get_watched_stream] = lambda: live
    app.dependency_overrides[get_historical_source] = lambda: stores
    yield TestClient(app)
    app.dependency_overrides.clear()


def analyse(client: TestClient, **body: Any):
    return client.post("/analyse", json=body)


def test_a_bought_leg_is_entered_at_the_ask(
    client: TestClient, live: ChainStream
) -> None:
    """The spread is crossed, and crossed the way `docs/payoff-contract.md` says.

    The book quotes 579 bid, 584 ask. A buyer pays 584 — never the mid, which is a trade
    nobody can make, and never the mark, which is Delta's own model output. So the leg
    comes back priced at the ask and the strategy is a 584 debit, `net_premium` positive
    because that is what the contract means by paid out.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)

    response = analyse(client, legs=[{"instrument": CALL_77600, "direction": 1}])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legs"][0]["entry_price"] == 584.0
    assert body["metrics"]["net_premium"] == 584.0
    assert body["underlying"] == "BTC"
    assert body["expiry"] == EXPIRY


def test_a_sold_leg_is_entered_at_the_bid(client: TestClient, live: ChainStream) -> None:
    """The other half of crossing the spread, and the sign that comes with it.

    The same 579/584 book. A seller is hit on the bid and takes 579 in, so `net_premium`
    is -579: negative is received, which is `docs/payoff-contract.md`'s convention and
    the one thing on the panel a trader cannot recover from by squinting.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)

    response = analyse(client, legs=[{"instrument": CALL_77600, "direction": -1}])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legs"][0]["entry_price"] == 579.0
    assert body["metrics"]["net_premium"] == -579.0


def test_a_supplied_entry_price_overrides_the_book(
    client: TestClient, live: ChainStream
) -> None:
    """"What if I were filled at 900" is a question about a quoted leg too.

    The book says 584 on the ask. The request says 900, so the leg is priced at 900 and
    the whole strategy costs 900 — the engine does not second-guess a price the trader
    typed, and does not average it with the one on screen.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)

    response = analyse(
        client,
        legs=[{"instrument": CALL_77600, "direction": 1, "entry_price": 900.0}],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["legs"][0]["entry_price"] == 900.0
    assert body["metrics"]["net_premium"] == 900.0


def test_a_vertical_spread_comes_back_whole(
    client: TestClient, live: ChainStream
) -> None:
    """The acceptance case: two legs in, the contract's whole response out.

    Bought the 77,600 call at its 584 ask and sold the 78,000 call at its 400 bid, so the
    spread is a **184 debit**. Every figure below was worked by hand from those two
    numbers and nothing else:

      * at 77,600 both calls expire worthless and the loss is the 184 paid;
      * at 78,000 the long call is worth 400 and the short one nothing, so 400 - 184
        = 216;
      * above 78,000 the two legs move together and the P&L stops changing — hence 216 as
        the maximum and **zero on both end slopes**;
      * the line crosses zero 184 above the long strike, at 77,784;
      * 216 over 184 is a reward-to-risk of 1.1739...

    The two rays are zero because one call was bought and one sold: `end_slopes` reads
    that off the leg mix rather than sampling, which is what makes "capped" a fact rather
    than an observation at some distance out.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)
    feed(live, "C-BTC-78000-040926", 400, 405)

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1, "quantity": 1},
            {"instrument": CALL_78000, "direction": -1, "quantity": 1},
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert [leg["entry_price"] for leg in body["legs"]] == [584.0, 400.0]
    assert [leg["instrument"] for leg in body["legs"]] == [CALL_77600, CALL_78000]
    assert [leg["direction"] for leg in body["legs"]] == [1, -1]

    corners = {corner["price"]: corner["pnl"] for corner in body["curve"]["corners"]}
    assert corners[77_600.0] == -184.0
    assert corners[78_000.0] == 216.0
    assert body["curve"]["slope_left"] == 0.0
    assert body["curve"]["slope_right"] == 0.0
    # And zero rather than **minus** zero on the wire, which `== 0.0` cannot tell apart:
    # this strategy has no puts at all, which is the case that produces one.
    assert math.copysign(1.0, body["curve"]["slope_left"]) == 1.0
    assert body["curve"]["window"]["low"] < body["curve"]["window"]["high"]

    assert body["metrics"] == {
        "max_profit": 216.0,
        "max_loss": -184.0,
        "breakevens": [77_784.0],
        "net_premium": 184.0,
        "reward_risk": pytest.approx(1.1739130434782608),
    }

    # One quantity sampled twice: wherever the table and the corners share a price they
    # must agree, or the picture and the figures under it describe different trades.
    rows = {row["price"]: row["pnl"] for row in body["table"]}
    assert rows[77_600.0] == -184.0
    assert rows[78_000.0] == 216.0

    assert body["contract_value"] == 0.001
    assert body["as_of"].endswith("Z")
    assert len(body["as_of"]) == len("2026-09-04T09:00:00Z")


# --- the refusals, one per row of the contract's table ------------------------------


def test_a_leg_with_nothing_on_its_side_refuses_the_whole_analysis(
    client: TestClient, live: ChainStream
) -> None:
    """Not dropped, not inferred from the other side: **asked about**.

    The 77,600 call is bid 579 with nothing offered. A buyer has nothing to lift, so the
    strategy has no price — and a strategy quietly missing a leg is a different strategy,
    drawn with nothing on screen to say so. The refusal names the instrument and the side
    that was empty, which is exactly what the trader needs in order to type a price.

    The second leg is quoted on both sides and would have priced perfectly well. It is
    here to prove the refusal is about the **whole** analysis rather than the one leg.
    """
    feed(live, "C-BTC-77600-040926", 579, None)
    feed(live, "C-BTC-78000-040926", 400, 405)

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1},
            {"instrument": CALL_78000, "direction": -1},
        ],
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert CALL_77600 in detail
    assert "ask" in detail


def test_a_sold_leg_with_no_bid_names_the_bid(
    client: TestClient, live: ChainStream
) -> None:
    """The mirror of the case above, and the reason the side is named rather than
    implied: the empty side depends on which way the leg was traded, and a message
    saying only "no quote" would send the trader to look at the wrong half of the book."""
    feed(live, "C-BTC-77600-040926", None, 584)

    response = analyse(client, legs=[{"instrument": CALL_77600, "direction": -1}])

    assert response.status_code == 422, response.text
    assert "bid" in response.json()["detail"]


def test_an_instrument_the_chain_does_not_list_is_a_404(
    client: TestClient, live: ChainStream
) -> None:
    """A thing that does not exist, answered with the code this engine already gives one.

    Two ways to be absent, and both are the same fact to the trader: a strike nobody
    lists at all, and a strike listed on one side only. The message quotes the whole
    canonical string back, because a strike and a right do not name a contract — 77,600 C
    trades in every series at once — so nothing shorter would tell the reader which leg
    of theirs was the problem.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)

    missing_strike = analyse(client, legs=[{"instrument": CALL_78000, "direction": 1}])
    assert missing_strike.status_code == 404, missing_strike.text
    assert CALL_78000 in missing_strike.json()["detail"]

    missing_side = analyse(client, legs=[{"instrument": PUT_77600, "direction": 1}])
    assert missing_side.status_code == 404, missing_side.text
    assert PUT_77600 in missing_side.json()["detail"]


def test_a_leg_that_is_not_a_canonical_string_is_a_400(client: TestClient) -> None:
    """`Instrument.from_canonical` is the single validator, and it names the part.

    Two malformations, both refused before any ladder is read: the pre-I1 five-part
    string with no currency, and a right that is neither C nor P. The message is the
    parser's own, so the reader is told which of the six parts was wrong rather than
    that "the leg was invalid" — a field validator on the request model would have
    buried exactly that inside FastAPI's own 422 envelope.
    """
    no_currency = analyse(
        client, legs=[{"instrument": "DELTA-BTC-20260904-77600-C", "direction": 1}]
    )
    assert no_currency.status_code == 400, no_currency.text
    assert "DELTA-BTC-20260904-77600-C" in no_currency.json()["detail"]

    bad_right = analyse(
        client, legs=[{"instrument": "DELTA-BTC-20260904-77600-X-USD", "direction": 1}]
    )
    assert bad_right.status_code == 400, bad_right.text
    assert "right" in bad_right.json()["detail"]


def test_legs_spanning_two_expiries_are_refused_naming_both(
    client: TestClient, live: ChainStream
) -> None:
    """One expiry per strategy, and the message says which two it was handed.

    With two series there is no date on which every leg has finished: the surviving leg
    has a price rather than a payoff, and the line could only be drawn by assuming a
    volatility. Calendar and diagonal spreads are #2.

    Both legs are quoted, so nothing else could have refused this — and the check runs
    before any ladder is read, because the expiry is what decides which ladder to read.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)
    feed(live, "C-BTC-77600-110926", 700, 705)

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1},
            {"instrument": "DELTA-BTC-20260911-77600-C-USD", "direction": -1},
        ],
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "04-09-2026" in detail
    assert "11-09-2026" in detail


def test_an_empty_strategy_is_refused_rather_than_drawn_blank(
    client: TestClient,
) -> None:
    """No legs is a refusal, not an empty chart. The rule lives on `AnalyseRequest`
    itself — `legs` is `min_length=1` — so no route can forget it and the message names
    the field that was empty."""
    response = analyse(client, legs=[])

    assert response.status_code == 422, response.text
    # **The one refusal on this contract that is not a string.** It is caught by
    # `AnalyseRequest` before the route is entered, so FastAPI's request-validation
    # envelope answers — a list under `detail`, naming the field — where the six semantic
    # refusals answer `{"detail": "..."}`. `docs/payoff-contract.md` marks the split; a
    # client reading `detail` has to expect either shape, so it is pinned here.
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body", "legs"]


def test_a_live_cache_that_has_not_warmed_is_a_503(
    client: TestClient, live: ChainStream
) -> None:
    """The answer exists; it does not exist **yet**.

    Nothing has been fed, so the cache has no ladder for this series. That is not a
    request the caller can fix by changing it — it is this process not being ready — so
    it is the 503 `get_bar_writer` reasons its way to for `/recording`, naming the
    underlying and the expiry that were asked for. Deliberately not `/chain/at`'s
    200-with-`waiting`: a one-shot POST whose only product is an analysis would make
    every consumer branch on a union to represent a state with nothing to draw.
    """
    response = analyse(client, legs=[{"instrument": CALL_77600, "direction": 1}])

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "BTC" in detail
    assert EXPIRY in detail


def test_a_process_with_no_chain_cache_at_all_is_the_same_503() -> None:
    """The lifespan never ran, so there is no cache to ask — which is the same fact to
    the caller as a cache that has not warmed, and gets the same code rather than the
    500 an unguarded attribute lookup would give."""
    response = analyse(TestClient(app), legs=[{"instrument": CALL_77600, "direction": 1}])

    assert response.status_code == 503, response.text
    assert EXPIRY in response.json()["detail"]


# --- the stored minute -------------------------------------------------------------


def store_a_minute(stores: HistoricalSource, **venue: float) -> None:
    """One minute of the 04-09-2026 chain: two calls, quoted, referenced and computed.

    The Greeks are round numbers this file chose rather than anything solved, so the
    weighted sums below are checkable by eye. The volatility, forward, discount and time
    to expiry are `test_historical.computed_bar`'s own — read back off disk exactly as
    written, which is what makes this the clock-free half of the suite: no fit runs, so
    nothing here depends on what day it is.
    """
    at = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    stores.quote.add(
        [
            quote_bar(minute=at, strike=77_600.0, bid=500.0, ask=510.0),
            quote_bar(minute=at, strike=78_000.0, bid=300.0, ask=305.0),
        ]
    )
    stores.reference.add(
        [
            replace(reference_bar(minute=at, strike=77_600.0), **venue),
            replace(reference_bar(minute=at, strike=78_000.0), **venue),
        ]
    )
    stores.computed.add(
        [
            replace(
                computed_bar(minute=at, strike=77_600.0),
                delta=0.5, gamma=0.0005, vega=30.0, theta=-8.0, rho=2.0,
            ),
            replace(
                computed_bar(minute=at, strike=78_000.0, iv=0.3907),
                delta=0.25, gamma=0.0001, vega=20.0, theta=-4.0, rho=1.0,
            ),
        ]
    )
    stores.spot.add([spot_bar(minute=at)])
    for store in (stores.quote, stores.reference, stores.computed, stores.spot):
        store.flush()


def test_a_named_minute_is_priced_off_the_store_and_not_the_live_cache(
    client: TestClient, live: ChainStream, stores: HistoricalSource
) -> None:
    """`as_of` present is a stored minute, and it is the stored minute that answers.

    The live cache is primed with a different book for the same contract — 579/584
    against the minute's 500/510 — so a route that reached for the wrong one would price
    the same legs at the wrong numbers rather than fail. `as_of` comes back as the minute
    that was asked for, which is how a static tab proves it stayed static.
    """
    store_a_minute(stores)
    feed(live, "C-BTC-77600-040926", 579, 584)

    stored = analyse(
        client, legs=[{"instrument": CALL_77600, "direction": 1}], as_of=MINUTE
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["as_of"] == MINUTE
    assert stored.json()["legs"][0]["entry_price"] == 510.0

    now = analyse(client, legs=[{"instrument": CALL_77600, "direction": 1}])
    assert now.status_code == 200, now.text
    assert now.json()["as_of"] != MINUTE
    assert now.json()["legs"][0]["entry_price"] == 584.0


def test_a_stored_minute_carries_the_volatility_and_the_weighted_greeks(
    client: TestClient, stores: HistoricalSource
) -> None:
    """The fitted half of the contract, off rows this file wrote rather than a solve.

    Bought one 77,600 call at its 510 ask and sold **three** 78,000 calls at their 300
    bid, so 900 comes in against 510 paid: `net_premium` is -390, a credit.

    Hand-worked from the two prices and the strikes:

      * at 77,600 nothing is exercised, so the P&L is the 390 taken in;
      * at 78,000 the long call is worth 400 and the shorts nothing: 900 - 510 + 400 - 900
        ... that is 390 + 400 = **790**, the peak, and it is a corner rather than a
        sampled point;
      * above 78,000 three shorts against one long lose 2 for every 1 the underlying
        rises — `slope_right` -2 — so the loss is **unbounded** and `max_loss` is `null`,
        never an infinity and never a large sentinel;
      * that ray crosses zero 790/2 = 395 above 78,000, at **78,395**;
      * `reward_risk` is `null` because a ratio against unlimited has no meaning.

    The Greeks are the store's own, **signed by direction and scaled by quantity exactly
    once**: the long leg's delta is 0.5 and the three short ones come to -0.75, summing
    to -0.25. Weighting twice — the mistake no type in this system could catch — would
    make the second row -2.25 and the total -1.75, so this is the assertion that pins it.
    """
    store_a_minute(stores)

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1, "quantity": 1},
            {"instrument": CALL_78000, "direction": -1, "quantity": 3},
        ],
        as_of=MINUTE,
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["spot"] == 77_651.9
    assert body["forward"] == 77_590.43
    assert body["discount"] == 0.99997892

    long_leg, short_leg = body["legs"]
    # **Per one unit, and not scaled by the three.** `entry_price` is what one unit cost
    # rather than what the leg cost, so it stays comparable with the bid and ask on the
    # ladder it was taken from — 300, never 900. The Greeks beside it *are* scaled, which
    # is the asymmetry `docs/payoff-contract.md` is explicit about.
    assert long_leg["entry_price"] == 510.0
    assert short_leg["entry_price"] == 300.0
    assert long_leg["iv"] == 0.4321
    assert short_leg["iv"] == 0.3907
    assert long_leg["greeks"] == {
        "delta": 0.5, "gamma": 0.0005, "vega": 30.0, "theta": -8.0, "rho": 2.0
    }
    assert short_leg["greeks"] == {
        "delta": -0.75,
        # `approx` on this one alone: three times a ten-thousandth is not exact in binary
        # and lands on -0.00030000000000000003. Every other figure here is exact.
        "gamma": pytest.approx(-0.0003),
        "vega": -60.0,
        "theta": 12.0,
        "rho": -3.0,
    }
    assert body["total_greeks"]["delta"] == -0.25
    assert body["total_greeks"]["vega"] == -30.0
    assert body["total_greeks"]["gamma"] == pytest.approx(0.0002)

    assert body["metrics"]["net_premium"] == -390.0
    assert body["metrics"]["max_profit"] == 790.0
    assert body["metrics"]["max_loss"] is None
    assert body["metrics"]["reward_risk"] is None
    assert body["metrics"]["breakevens"] == [78_395.0]
    assert body["curve"]["slope_right"] == -2.0
    assert body["curve"]["slope_left"] == 0.0

    corners = {corner["price"]: corner["pnl"] for corner in body["curve"]["corners"]}
    assert corners[77_600.0] == 390.0
    assert corners[78_000.0] == 790.0

    # The window is centred on the **forward**, not on spot: the two ends are the anchor
    # multiplied and divided by the same factor, so their geometric mean is the anchor
    # itself — 77,590.43 here and not the 77,651.9 spot a strike away from it.
    window = body["curve"]["window"]
    assert math.sqrt(window["low"] * window["high"]) == pytest.approx(77_590.43)


def test_a_minute_the_store_does_not_hold_is_a_404(
    client: TestClient, stores: HistoricalSource
) -> None:
    """A minute with no arrivals produces no row, so that minute genuinely does not
    exist — and a thing that does not exist is the 404 this engine already gives for an
    adapter it does not run or an underlying the venue lists nothing for.

    09:00 is stored and 09:01 is not. Answering 09:01 with 09:00's rows would be exactly
    the forward-fill the store was built to refuse, and answering it 200 with an empty
    envelope would make every caller branch on a union to represent nothing to draw. The
    message names all three parts of what was looked for, because any of them could be
    the one that was wrong.
    """
    store_a_minute(stores)

    response = analyse(
        client,
        legs=[{"instrument": CALL_77600, "direction": 1}],
        as_of="2026-09-04T09:01:00Z",
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert "BTC" in detail
    assert EXPIRY in detail
    assert "2026-09-04T09:01:00Z" in detail


def test_an_as_of_that_is_not_a_minute_is_refused_at_the_boundary(
    client: TestClient,
) -> None:
    """One spelling of a minute across the whole stack. `/chain/minutes` publishes it,
    `/chain/at` accepts it and this route reads it with the same validator, so a stamp
    taken off the slider travels here unchanged — and anything else is a 400 naming the
    format, exactly as it is one route up."""
    response = analyse(
        client, legs=[{"instrument": CALL_77600, "direction": 1}], as_of="4th September"
    )

    assert response.status_code == 400, response.text
    assert "YYYY-MM-DDTHH:MM:SSZ" in response.json()["detail"]


# --- what survives when there is no model --------------------------------------------


def test_a_minute_with_no_computed_rows_still_draws_a_curve(
    client: TestClient, stores: HistoricalSource
) -> None:
    """An unfitted chain is a market condition, not a bug, and the chart survives it.

    Quotes, references and a spot for the minute, but nothing in table C — which the
    store's own computed sampling makes an ordinary occurrence rather than a contrived
    one. With no forward there is nothing to invert a volatility against, so `forward`,
    `discount`, every `iv` and every Greek are `null` together. The curve and the metrics
    are untouched: a P&L at expiry is intrinsic value and a subtraction, and needs no
    model at all.
    """
    at = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    stores.quote.add([quote_bar(minute=at, strike=77_600.0, bid=500.0, ask=510.0)])
    stores.reference.add([reference_bar(minute=at, strike=77_600.0)])
    stores.spot.add([spot_bar(minute=at)])
    for store in (stores.quote, stores.reference, stores.spot):
        store.flush()

    response = analyse(
        client, legs=[{"instrument": CALL_77600, "direction": 1}], as_of=MINUTE
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["forward"] is None
    assert body["discount"] is None
    assert body["spot"] == 77_651.9
    assert body["legs"][0]["iv"] is None
    assert body["legs"][0]["greeks"] is None
    assert body["total_greeks"] is None
    assert body["legs"][0]["entry_price"] == 510.0
    assert body["metrics"]["max_loss"] == -510.0
    assert body["metrics"]["breakevens"] == [78_110.0]


def test_a_spot_that_came_back_zero_still_opens_a_chart(
    client: TestClient, stores: HistoricalSource
) -> None:
    """`null` is not `0`, and a zero price is not an anchor.

    A minute whose `spot-bars` row closed at zero is an upstream number that came out
    wrong, and `suggested_window` rightly refuses to centre a chart on it. Refusing the
    whole request would report a bad observation as a broken route, so the window falls
    back to the strikes instead and the analysis — which never needed spot for anything
    else — is answered in full.
    """
    at = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    stores.quote.add([quote_bar(minute=at, strike=77_600.0, bid=500.0, ask=510.0)])
    stores.reference.add([reference_bar(minute=at, strike=77_600.0)])
    stores.spot.add([spot_bar(minute=at, close=0.0)])
    for store in (stores.quote, stores.reference, stores.spot):
        store.flush()

    response = analyse(
        client, legs=[{"instrument": CALL_77600, "direction": 1}], as_of=MINUTE
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["spot"] == 0.0
    window = body["curve"]["window"]
    assert 0.0 < window["low"] < 77_600.0 < window["high"]


# --- against the committed capture ---------------------------------------------------


def test_a_spread_off_the_captured_chain_prices_at_the_real_book(
    client: TestClient, live: ChainStream, ws_ticker_frames, ws_book_frames
) -> None:
    """The whole 136-symbol capture in, one two-leg spread out.

    The toy books above prove the rules; this proves they survive a real ladder — 69
    strikes rather than two, both channels layered the way the live path layers them,
    every frame through the producer's own decoder.

    The two prices are read straight off `ws-ob-l2-04-09-2026.json`: the 77,600 call is
    535 / 530 and the 78,000 call is 368 / 362. Buying the first and selling the second
    therefore costs 535 - 362 = **173**, the strategy is worth -173 at 77,600 and
    400 - 173 = **227** above 78,000, and it turns a profit at **77,773**.

    That the book's prices are what appear — rather than the ticker channel's, which
    carried the same contracts in the same capture — is the freshness the whole project
    is built on: the book republishes every 508 ms against the ticker's 5,001 ms.
    """
    for frame in ws_ticker_frames.values():
        for event in events_from_frame("ticker", frame):
            live.apply(event)
    for frame in ws_book_frames.values():
        for event in events_from_frame("ob_l2", frame):
            live.apply(event)

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1},
            {"instrument": CALL_78000, "direction": -1},
        ],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [leg["entry_price"] for leg in body["legs"]] == [535.0, 362.0]
    assert body["metrics"]["net_premium"] == 173.0
    assert body["metrics"]["max_profit"] == 227.0
    assert body["metrics"]["max_loss"] == -173.0
    assert body["metrics"]["breakevens"] == [77_773.0]

    corners = {corner["price"]: corner["pnl"] for corner in body["curve"]["corners"]}
    assert corners[77_600.0] == -173.0
    assert corners[78_000.0] == 227.0


def test_delta_own_greeks_and_volatility_change_nothing_here(
    client: TestClient, tmp_path: Path
) -> None:
    """The project's strictest rule, checked at its newest consumer.

    `/analyse` is a new reader of `Leg`, which carries Delta's `mark_iv`, its two quote
    IVs and all five of its Greeks right beside the bid and ask this route legitimately
    uses. One stray `listed.delta` where `listed.computed.delta` was meant and the screen
    would be showing the venue's opinion under our heading, with every number still
    plausible.

    So the same minute is written into two stores — one as it stands, one with every one
    of Delta's own figures replaced by nonsense — and the two responses must be
    identical. Behavioural rather than textual, exactly as `test_no_delta_inputs.py`
    argues: grepping the source for `mark_iv` proves nothing about what runs.
    """
    plain = source_on(tmp_path / "plain")
    store_a_minute(plain)
    poisoned = source_on(tmp_path / "poisoned")
    store_a_minute(
        poisoned,
        venue_delta=-99.0,
        venue_gamma=-99.0,
        venue_vega=-99.0,
        venue_theta=-99.0,
        venue_rho=-99.0,
        venue_bid_iv=9.9,
        venue_ask_iv=9.9,
        venue_mark_iv=9.9,
        mark_close=-99.0,
    )

    legs = [{"instrument": CALL_77600, "direction": 1}]
    app.dependency_overrides[get_historical_source] = lambda: plain
    honest = analyse(client, legs=legs, as_of=MINUTE)
    app.dependency_overrides[get_historical_source] = lambda: poisoned
    nonsense = analyse(client, legs=legs, as_of=MINUTE)

    assert honest.status_code == 200, honest.text
    assert nonsense.status_code == 200, nonsense.text
    assert nonsense.json() == honest.json()


def test_legs_spanning_two_underlyings_are_refused_naming_both(
    client: TestClient, live: ChainStream
) -> None:
    """The mixed-expiry refusal's sibling, and it fails the same silent way.

    Only the BTC ladder is fed, and both legs name the strike it lists — so with no guard
    the ETH leg **resolves on the BTC chain**, and the response comes back 200 echoing
    `underlying: "BTC"` and `contract_value: 0.001` for a contract whose lot size is 0.01.
    The screen then multiplies a tenth of the legs by the wrong number with nothing on the
    page saying so.

    That this is normally a 404 instead is an accident of arithmetic — BTC strikes are
    around 77,000 and ETH's around 4,000, so they do not collide today — and an accident
    is not a refusal. One underlying per strategy, named in the message, exactly as the
    two expiries are.
    """
    feed(live, "C-BTC-77600-040926", 579, 584)

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1},
            {"instrument": "DELTA-ETH-20260904-77600-C-USD", "direction": -1},
        ],
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "BTC" in detail
    assert "ETH" in detail


def test_a_leg_solved_without_all_five_greeks_is_published_as_unfitted(
    client: TestClient, stores: HistoricalSource
) -> None:
    """A partially-solved leg is a **market and store condition**, not a server error.

    Table C's five Greek columns are nullable independently of `iv`, so a row carrying a
    volatility and only four Greeks is a shape the store can hold. There are three things
    that could happen to it and only one of them is honest:

      * publish the four and a `null` — the models refuse it, and rightly: `iv` and
        `greeks` are paired exactly, and a Greek missing from a row of five would be read
        as a zero exposure by anyone skimming the column;
      * let it reach the models and answer **500** — which is what
        `docs/payoff-contract.md` used to promise, and it is an inversion: a condition
        that arose in the data would leave the building disguised as our bug;
      * publish the leg as unfitted, `iv` and `greeks` both `null`.

    The third. **Dropping a number is not fabricating one** — the leg still says "no
    volatility here", which is true of what can be reported rather than a default sigma
    invented to fill the gap — and the whole-response invariant follows: with one leg
    unfitted there is no `total_greeks` either, because a sum over the legs that happened
    to solve describes a different position from the one on screen.

    The chain's own `forward` and `discount` survive: the fit succeeded, and it is this
    one leg that did not.
    """
    at = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
    stores.quote.add(
        [
            quote_bar(minute=at, strike=77_600.0, bid=500.0, ask=510.0),
            quote_bar(minute=at, strike=78_000.0, bid=300.0, ask=305.0),
        ]
    )
    stores.reference.add(
        [
            reference_bar(minute=at, strike=77_600.0),
            reference_bar(minute=at, strike=78_000.0),
        ]
    )
    stores.computed.add(
        [
            # A volatility, and vega missing from beside it.
            replace(computed_bar(minute=at, strike=77_600.0), vega=None),
            replace(computed_bar(minute=at, strike=78_000.0, iv=0.3907), vega=20.0),
        ]
    )
    stores.spot.add([spot_bar(minute=at)])
    for store in (stores.quote, stores.reference, stores.computed, stores.spot):
        store.flush()

    response = analyse(
        client,
        legs=[
            {"instrument": CALL_77600, "direction": 1},
            {"instrument": CALL_78000, "direction": -1},
        ],
        as_of=MINUTE,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    partial, whole = body["legs"]
    assert partial["iv"] is None
    assert partial["greeks"] is None
    assert whole["iv"] == 0.3907
    assert whole["greeks"]["vega"] == -20.0
    assert body["total_greeks"] is None
    assert body["forward"] == 77_590.43
    assert body["metrics"]["net_premium"] == 210.0


def test_strategy_series_refuses_an_empty_list_rather_than_indexing_it() -> None:
    """The one test here that skips the transport, because the route cannot reach this.

    `AnalyseRequest.legs` is `min_length=1`, so an empty strategy is refused by the type
    before `strategy_series` is called — but the signature says `Sequence[LegRequest]`,
    and a direct caller (the pure core building a request in a test, the next route that
    wants a strategy) would get an `IndexError` off `instruments[0]` instead of the
    refusal every other malformed strategy gets. A function that is total for its declared
    argument type costs one line.
    """
    with pytest.raises(AnalyseRefusal) as refusal:
        strategy_series([])

    assert refusal.value.status_code == 422
    assert "at least one leg" in str(refusal.value)
