"""`GET /chain/minutes` and `GET /chain/at` — the ladder at a stored minute.

Driven over HTTP through `TestClient`, the same seam `test_smile.py` uses. The four
stores under it are built here, in `tmp_path`: nothing in this file reads `data/`.

**The first test in this file is the one the ticket names as the one to write first**:
three minutes written, the middle one empty, and the ladder route for the empty minute
must answer "nothing here" rather than the minute either side of it. Every other test
in this file exists to make that guarantee harder to break by accident.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltapayoff.bars import ComputedBar, QuoteBar, ReferenceBar, SpotBar
from deltapayoff.compute import MODEL_VERSION
from deltapayoff.main import HistoricalSource, app, get_historical_source
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
)

MINUTE = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
DAY = Date(2026, 9, 4)


def quote_bar(
    *,
    minute: datetime = MINUTE,
    strike: float = 77600.0,
    option_type: str = "C",
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    bid: float | None = 73.0,
    ask: float | None = 74.0,
) -> QuoteBar:
    """One row of table A. `symbol` is derived exactly as `bars._parse_symbol` expects
    to be able to read it back: `C-BTC-77600-040926`."""
    return QuoteBar(
        symbol=f"{option_type}-{underlying}-{strike:.0f}-{expiry.replace('-', '')[:6]}",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute,
        bid_open=bid,
        bid_high=bid,
        bid_low=bid,
        bid_close=bid,
        bid_ticks=1 if bid is not None else 0,
        ask_open=ask,
        ask_high=ask,
        ask_low=ask,
        ask_close=ask,
        ask_ticks=1 if ask is not None else 0,
        mid_open=None,
        mid_high=None,
        mid_low=None,
        mid_close=None,
        mid_ticks=0,
        from_book=True,
        last_lts=None,
    )


def reference_bar(
    *,
    minute: datetime = MINUTE,
    strike: float = 77600.0,
    option_type: str = "C",
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    mark: float | None = 1060.125,
) -> ReferenceBar:
    """One row of table B. Symbol matches `quote_bar`'s so the join in `historical.py`
    lands on the same contract."""
    return ReferenceBar(
        symbol=f"{option_type}-{underlying}-{strike:.0f}-{expiry.replace('-', '')[:6]}",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute,
        mark_open=mark,
        mark_high=mark,
        mark_low=mark,
        mark_close=mark,
        mark_ticks=12,
        ltp_open=1082.0,
        ltp_high=1082.0,
        ltp_low=1082.0,
        ltp_close=1082.0,
        ltp_ticks=6,
        oi_contracts=1997.0,
        oi_change_usd_6h=-41302.35,
        turnover=411134.7807,
        venue_delta=-0.73938982,
        venue_gamma=0.00024511,
        venue_rho=-1.7038088,
        venue_theta=-202.29182089,
        venue_vega=13.60933495,
        venue_bid_iv=0.3110054,
        venue_ask_iv=0.32129313,
        venue_mark_iv=0.31623765,
    )


def computed_bar(
    *,
    minute: datetime = MINUTE,
    strike: float = 77600.0,
    option_type: str = "C",
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    iv: float | None = 0.4321,
    iv_leg: str | None = "call",
    iv_reason: str | None = None,
    forward: float | None = 77590.43,
    model_version: str = MODEL_VERSION,
) -> ComputedBar:
    """One row of table C. Greeks travel with the volatility or not at all."""
    greeks: dict[str, float | None] = dict(
        delta=0.51234567, gamma=0.00012345, vega=31.41592653, theta=-8.2, rho=1.9
    )
    if iv is None:
        greeks = dict.fromkeys(greeks)
    return ComputedBar(
        symbol=f"{option_type}-{underlying}-{strike:.0f}-{expiry.replace('-', '')[:6]}",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute,
        iv=iv,
        iv_leg=iv_leg,
        iv_reason=iv_reason,
        forward=forward,
        discount=0.99997892,
        years_to_expiry=0.00114155,
        forward_method="F1+assumed-rate",
        model_version=model_version,
        **greeks,
    )


def spot_bar(
    *, minute: datetime = MINUTE, underlying: str = "BTC", close: float = 77651.9
) -> SpotBar:
    """One row of table D. No contract identity at all — see `bars.SpotBar`."""
    return SpotBar(
        underlying=underlying,
        minute=minute,
        spot_open=77600.0,
        spot_high=77700.5,
        spot_low=77590.25,
        spot_close=close,
        spot_ticks=7056,
    )


@pytest.fixture
def stores(tmp_path: Path) -> HistoricalSource:
    return HistoricalSource(
        quote=BarStore(tmp_path),
        reference=BarStore(tmp_path, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA),
        computed=BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
        spot=BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
    )


@pytest.fixture
def make_client(stores: HistoricalSource) -> Iterator[Callable[[], TestClient]]:
    """A `TestClient` whose two routes read the four stores above and nothing else.

    The lifespan never runs — `TestClient` is not entered as a context manager — so the
    app has no `BarWriter` and the override is the only thing that can answer.
    """

    def factory() -> TestClient:
        app.dependency_overrides[get_historical_source] = lambda: stores
        return TestClient(app)

    yield factory
    app.dependency_overrides.clear()


def minutes_of(
    client: TestClient,
    *,
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    date: str = "2026-09-04",
):
    response = client.get(
        "/chain/minutes",
        params={"underlying": underlying, "expiry": expiry, "date": date},
    )
    assert response.status_code == 200, response.text
    return response.json()


def ladder_at(
    client: TestClient,
    minute: str,
    *,
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
):
    response = client.get(
        "/chain/at",
        params={"underlying": underlying, "expiry": expiry, "minute": minute},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- the one to write first: a hole in the middle --------------------------------


def test_the_empty_middle_minute_is_nothing_here_not_a_neighbours_ladder(
    make_client, stores: HistoricalSource
) -> None:
    """Three minutes, the middle one never quoted. The minutes route must list two, and
    the ladder route for the missing one must never answer with 09:00's or 09:02's rows —
    that would be exactly the forward-fill this store was built to refuse, made visible
    on this one screen for the first time."""
    early = MINUTE
    late = datetime(2026, 9, 4, 9, 2, tzinfo=timezone.utc)
    stores.quote.add(
        [quote_bar(minute=early, bid=73.0), quote_bar(minute=late, bid=90.0)]
    )
    stores.quote.flush()

    client = make_client()

    listed = minutes_of(client)
    assert listed["minutes"] == ["2026-09-04T09:00:00Z", "2026-09-04T09:02:00Z"]

    empty = ladder_at(client, "2026-09-04T09:01:00Z")
    assert empty["type"] == "waiting"
    assert "detail" in empty and "data" not in empty

    stood_early = ladder_at(client, "2026-09-04T09:00:00Z")
    assert stood_early["type"] == "chain"
    assert stood_early["data"]["rows"][0]["call"]["bid"] == 73.0

    stood_late = ladder_at(client, "2026-09-04T09:02:00Z")
    assert stood_late["data"]["rows"][0]["call"]["bid"] == 90.0


def test_a_minute_nobody_quoted_at_all_is_also_nothing_here(
    make_client, stores: HistoricalSource
) -> None:
    """Asking for a minute outside every stored run, not just one inside a gap."""
    stores.quote.add([quote_bar(minute=MINUTE)])
    stores.quote.flush()

    body = ladder_at(make_client(), "2026-09-04T08:00:00Z")

    assert body == {
        "type": "waiting",
        "detail": (
            "no stored quotes for BTC expiring 04-09-2026 at 2026-09-04T08:00:00Z"
        ),
    }


# --- every column the live ladder has --------------------------------------------


def test_the_ladder_carries_the_venues_figures_and_ours_side_by_side(
    make_client, stores: HistoricalSource
) -> None:
    """Reference bars give Delta's own IV and Greeks; computed bars give ours. Both
    travel on the same leg, exactly as the live path puts them side by side."""
    stores.quote.add([quote_bar(bid=73.0, ask=74.0)])
    stores.reference.add([reference_bar(mark=1060.125)])
    stores.computed.add([computed_bar(iv=0.4321, iv_leg="call")])
    for store in (stores.quote, stores.reference, stores.computed):
        store.flush()

    call = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]["call"]

    assert call["bid"] == 73.0
    assert call["ask"] == 74.0
    assert call["mark"] == 1060.125
    assert call["mark_iv"] == 0.31623765
    assert call["oi"] == 1997.0
    assert call["computed"]["iv"] == 0.4321
    assert call["computed"]["iv_leg"] == "call"
    assert call["computed"]["delta"] == 0.51234567


def test_computed_is_null_when_table_c_has_nothing_for_this_minute(
    make_client, stores: HistoricalSource
) -> None:
    """The gap `docs/storage.md` records: quote bars exist, computed bars do not. `null`,
    never a default — reporting Greeks at no volatility would be five invented figures."""
    stores.quote.add([quote_bar()])
    stores.reference.add([reference_bar()])
    for store in (stores.quote, stores.reference):
        store.flush()

    call = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]["call"]

    assert call["computed"] is None
    assert call["mark"] is not None, "reference bars still answer even without table C"


def test_reference_fields_are_null_when_table_b_has_nothing_for_this_minute(
    make_client, stores: HistoricalSource
) -> None:
    """The book was busy and the ticker channel never fed reference bars this minute —
    plausible per `bars.ReferenceAggregator`'s own grace. Quotes still answer."""
    stores.quote.add([quote_bar(bid=73.0, ask=74.0)])
    stores.quote.flush()

    call = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]["call"]

    assert call["bid"] == 73.0
    assert call["mark"] is None
    assert call["oi"] is None
    assert call["computed"] is None


def test_never_stored_fields_are_null_not_a_default(
    make_client, stores: HistoricalSource
) -> None:
    """`product_id`, `tick_size` and `oi_value_usd` are not on any bar schema. `null`,
    because there is nothing to fall back to — never `0` and never a guess."""
    stores.quote.add([quote_bar()])
    stores.reference.add([reference_bar()])
    for store in (stores.quote, stores.reference):
        store.flush()

    call = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]["call"]

    assert call["product_id"] is None
    assert call["tick_size"] is None
    assert call["oi_value_usd"] is None


def test_a_paired_strike_carries_both_legs(make_client, stores: HistoricalSource) -> None:
    stores.quote.add(
        [quote_bar(option_type="C", bid=73.0), quote_bar(option_type="P", bid=5.0)]
    )
    stores.quote.flush()

    row = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]
    assert row["call"]["bid"] == 73.0
    assert row["put"]["bid"] == 5.0


def test_an_unlisted_side_is_null_and_the_row_still_exists(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(option_type="C")])
    stores.quote.flush()

    row = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]
    assert row["call"] is not None
    assert row["put"] is None


def test_the_minute_field_and_fetched_at_both_name_the_minute(
    make_client, stores: HistoricalSource
) -> None:
    """No second clock: there is no "when we fetched it" for a historical read, so
    `fetched_at` carries the same stamp `minute` does rather than meaning nothing."""
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    data = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]

    assert data["minute"] == "2026-09-04T09:00:00Z"
    assert data["fetched_at"] == "2026-09-04T09:00:00Z"


def test_the_chain_level_fields_come_from_table_c(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar()])
    stores.computed.add([computed_bar(forward=77590.43)])
    for store in (stores.quote, stores.computed):
        store.flush()

    data = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]

    assert data["forward"] == 77590.43
    assert data["discount"] == 0.99997892
    assert data["forward_method"] == "F1+assumed-rate"


def test_the_chain_level_fields_are_null_with_no_computed_rows(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    data = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]

    assert data["forward"] is None
    assert data["forward_method"] is None


# --- spot: the fourth table, read for two fields ChainLadder cannot render null -----


def test_spot_and_atm_strike_come_from_spot_bars(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add(
        [quote_bar(strike=77000.0), quote_bar(strike=77600.0), quote_bar(strike=78000.0)]
    )
    stores.spot.add([spot_bar(close=77651.9)])
    for store in (stores.quote, stores.spot):
        store.flush()

    data = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]

    assert data["spot"] == 77651.9
    assert data["atm_strike"] == 77600.0, "the nearest listed strike to spot"


def test_spot_is_null_when_table_d_has_nothing_for_this_minute(
    make_client, stores: HistoricalSource
) -> None:
    """A real gap: the book was busy and the ticker channel, which is what feeds
    spot-bars, never fed this minute. Honest absence, not an invented spot."""
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    data = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]

    assert data["spot"] is None
    assert data["atm_strike"] is None


# --- the union of disk and buffer, exactly as /smile takes it --------------------


def test_a_minute_still_in_the_buffer_reaches_the_wire_unflushed(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(bid=73.0)])
    assert stores.quote.buffered == 1, "unflushed, or this test proves nothing"

    call = ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0]["call"]

    assert call["bid"] == 73.0


def test_the_minutes_list_is_also_the_union_of_disk_and_buffer(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(minute=MINUTE)])
    stores.quote.flush()
    stores.quote.add(
        [quote_bar(minute=datetime(2026, 9, 4, 9, 1, tzinfo=timezone.utc))]
    )

    listed = minutes_of(make_client())

    assert listed["minutes"] == ["2026-09-04T09:00:00Z", "2026-09-04T09:01:00Z"]


# --- absence is 200 and empty, exactly as /smile treats it -----------------------


def test_no_stored_minutes_at_all_is_an_empty_list_not_an_error(
    make_client, stores: HistoricalSource
) -> None:
    assert minutes_of(make_client())["minutes"] == []


def test_a_different_underlying_is_not_in_the_list(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(underlying="BTC")])
    stores.quote.flush()

    assert minutes_of(make_client(), underlying="ETH")["minutes"] == []


def test_a_different_expiry_is_not_in_the_list(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(expiry="04-09-2026")])
    stores.quote.flush()

    assert minutes_of(make_client(), expiry="11-09-2026")["minutes"] == []


def test_a_different_date_is_not_in_the_list(
    make_client, stores: HistoricalSource
) -> None:
    """The store's whole reason to take `date` at all: an expiry's history can span more
    than the one day the slider is looking at."""
    stores.quote.add([quote_bar(minute=MINUTE)])
    stores.quote.flush()

    assert minutes_of(make_client(), date="2026-09-05")["minutes"] == []


# --- the error table ---------------------------------------------------------------


@pytest.mark.parametrize("underlying", ["SOL", "BTCUSD", "xyz"])
def test_minutes_route_bad_underlying_is_400(make_client, underlying: str) -> None:
    response = make_client().get(
        "/chain/minutes",
        params={"underlying": underlying, "expiry": "04-09-2026", "date": "2026-09-04"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("expiry", ["2026-09-04", "4-9-2026", "nonsense"])
def test_minutes_route_bad_expiry_is_400(make_client, expiry: str) -> None:
    response = make_client().get(
        "/chain/minutes",
        params={"underlying": "BTC", "expiry": expiry, "date": "2026-09-04"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("date", ["04-09-2026", "2026/09/04", "nonsense"])
def test_minutes_route_bad_date_is_400(make_client, date: str) -> None:
    response = make_client().get(
        "/chain/minutes",
        params={"underlying": "BTC", "expiry": "04-09-2026", "date": date},
    )
    assert response.status_code == 400


def test_minutes_route_missing_parameter_is_422_from_fastapi(make_client) -> None:
    response = make_client().get(
        "/chain/minutes", params={"underlying": "BTC", "expiry": "04-09-2026"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "minute", ["2026-09-04 09:00:00", "2026-09-04T09:00:00", "nonsense"]
)
def test_ladder_route_bad_minute_is_400(make_client, minute: str) -> None:
    response = make_client().get(
        "/chain/at",
        params={"underlying": "BTC", "expiry": "04-09-2026", "minute": minute},
    )
    assert response.status_code == 400


def test_ladder_route_bad_underlying_is_400(make_client) -> None:
    response = make_client().get(
        "/chain/at",
        params={
            "underlying": "xyz",
            "expiry": "04-09-2026",
            "minute": "2026-09-04T09:00:00Z",
        },
    )
    assert response.status_code == 400


def test_the_underlying_is_normalised_before_the_store_is_asked(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(underlying="BTC")])
    stores.quote.flush()

    body = ladder_at(make_client(), "2026-09-04T09:00:00Z", underlying="btc")

    assert body["data"]["underlying"] == "BTC"


def test_historical_routes_read_flushed_rows_from_underlying_first_paths(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    partition = stores.quote.path / "underlying=BTC" / "date=2026-09-04"
    assert list(partition.glob("*.parquet"))
    assert minutes_of(make_client())["minutes"] == ["2026-09-04T09:00:00Z"]
    assert ladder_at(make_client(), "2026-09-04T09:00:00Z")["data"]["rows"][0][
        "call"
    ]["bid"] == 73.0


# --- the wiring, with nothing overridden ------------------------------------------


def test_both_routes_read_the_running_writers_own_stores(
    monkeypatch, tmp_path: Path
) -> None:
    """No dependency override here — the one test that checks the seam itself, exactly
    as `test_smile.py`'s equivalent does. Every other test in this file would keep
    passing if the routes were wired to fresh stores over the same directory; that
    store would have an empty buffer and would silently drop whatever had not yet been
    flushed."""
    writer = BarWriter(BarStore(tmp_path))
    writer.store.add([quote_bar(minute=MINUTE, bid=73.0)])
    assert writer.store.buffered == 1
    monkeypatch.setattr(app.state, "writer", writer, raising=False)

    listed = minutes_of(TestClient(app))
    assert listed["minutes"] == ["2026-09-04T09:00:00Z"]

    body = ladder_at(TestClient(app), "2026-09-04T09:00:00Z")
    assert body["data"]["rows"][0]["call"]["bid"] == 73.0
