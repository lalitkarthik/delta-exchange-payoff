"""`GET /bars` — one contract's minute bars for one date, addressed by canonical string.

Driven over HTTP through `TestClient`, the same seam `test_historical.py` and
`test_smile.py` use. The two stores under it are built here, in `tmp_path`: nothing in
this file reads `data/`.

**The first test in this file is the one the ticket names as the one to write first**:
three minutes written, the middle one empty, and the route must answer with two bars and
no null row for the missing minute — never a fabricated candle. Every other test in this
file exists to make that guarantee harder to break by accident.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deltapayoff.bars import QuoteBar, ReferenceBar
from deltapayoff.main import HistoricalSource, app, get_historical_source
from deltapayoff.store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
)

MINUTE = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
DAY = Date(2026, 9, 4)
INSTRUMENT = "DELTA-BTC-20260904-77600-C-USD"


def quote_bar(
    *,
    minute: datetime = MINUTE,
    strike: float = 77600.0,
    option_type: str = "C",
    underlying: str = "BTC",
    expiry: str = "04-09-2026",
    bid: float | None = 73.0,
    ask: float | None = 74.0,
    mid: float | None = 73.5,
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
        mid_open=mid,
        mid_high=mid,
        mid_low=mid,
        mid_close=mid,
        mid_ticks=1 if mid is not None else 0,
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
    ltp: float | None = 1082.0,
) -> ReferenceBar:
    """One row of table B. Symbol matches `quote_bar`'s so the join in
    `contract_bars.py` lands on the same contract."""
    return ReferenceBar(
        symbol=f"{option_type}-{underlying}-{strike:.0f}-{expiry.replace('-', '')[:6]}",
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        minute=minute,
        mark_open=1060.0,
        mark_high=1060.0,
        mark_low=1060.0,
        mark_close=1060.0,
        mark_ticks=12,
        ltp_open=ltp,
        ltp_high=ltp,
        ltp_low=ltp,
        ltp_close=ltp,
        ltp_ticks=6 if ltp is not None else 0,
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


@pytest.fixture
def stores(tmp_path: Path) -> HistoricalSource:
    return HistoricalSource(
        quote=BarStore(tmp_path),
        reference=BarStore(tmp_path, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA),
        # Unused by `/bars` — the dependency's shape is four stores, shared with the
        # historical chain routes, and this route reads two of them. See main.py's
        # `HistoricalSource` and the LLD's note on the shared seam.
        computed=BarStore(tmp_path, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
        spot=BarStore(tmp_path, dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
    )


@pytest.fixture
def make_client(stores: HistoricalSource) -> Iterator[Callable[[], TestClient]]:
    """A `TestClient` whose `/bars` reads the two stores above and nothing else.

    The lifespan never runs — `TestClient` is not entered as a context manager — so the
    app has no `BarWriter` and the override is the only thing that can answer.
    """

    def factory() -> TestClient:
        app.dependency_overrides[get_historical_source] = lambda: stores
        return TestClient(app)

    yield factory
    app.dependency_overrides.clear()


def bars_of(
    client: TestClient,
    *,
    instrument: str = INSTRUMENT,
    date: str = "2026-09-04",
):
    response = client.get("/bars", params={"instrument": instrument, "date": date})
    assert response.status_code == 200, response.text
    return response.json()


# --- the one to write first: a hole in the middle --------------------------------


def test_the_empty_middle_minute_is_absent_not_a_null_row(
    make_client, stores: HistoricalSource
) -> None:
    """Three minutes, the middle one never quoted. The route must return two bars, and
    the missing minute must not appear at all — never a row of nulls standing in for it,
    which would be exactly the forward-fill this store was built to refuse."""
    early = MINUTE
    late = datetime(2026, 9, 4, 9, 2, tzinfo=timezone.utc)
    stores.quote.add(
        [quote_bar(minute=early, bid=73.0), quote_bar(minute=late, bid=90.0)]
    )
    stores.quote.flush()

    body = bars_of(make_client())

    assert [bar["minute"] for bar in body["bars"]] == [
        "2026-09-04T09:00:00Z",
        "2026-09-04T09:02:00Z",
    ]
    assert body["bars"][0]["bid_close"] == 73.0
    assert body["bars"][1]["bid_close"] == 90.0


def test_no_stored_minutes_at_all_is_an_empty_list_not_an_error(
    make_client, stores: HistoricalSource
) -> None:
    assert bars_of(make_client())["bars"] == []


# --- the four series: mid, bid, ask, last-trade -----------------------------------


def test_mid_bid_ask_come_from_quote_bars(make_client, stores: HistoricalSource) -> None:
    stores.quote.add([quote_bar(bid=73.0, ask=74.0, mid=73.5)])
    stores.quote.flush()

    bar = bars_of(make_client())["bars"][0]

    assert bar["bid_close"] == 73.0
    assert bar["ask_close"] == 74.0
    assert bar["mid_close"] == 73.5


def test_last_trade_comes_from_reference_bars(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar()])
    stores.reference.add([reference_bar(ltp=1082.0)])
    for store in (stores.quote, stores.reference):
        store.flush()

    bar = bars_of(make_client())["bars"][0]

    assert bar["ltp_close"] == 1082.0


def test_last_trade_is_null_when_reference_bars_has_nothing_for_this_minute(
    make_client, stores: HistoricalSource
) -> None:
    """A quoted minute the ticker channel never fed reference bars for — plausible per
    `bars.ReferenceAggregator`'s own grace. The candle series still answers; the toggle
    simply has nothing to draw for this minute."""
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    bar = bars_of(make_client())["bars"][0]

    assert bar["ltp_close"] is None


def test_a_quiet_book_minute_with_a_republished_ltp_is_not_invented_into_existence(
    make_client, stores: HistoricalSource
) -> None:
    """The gap the module docstring names: `reference-bars` can hold a row for a minute
    `quote-bars` never quoted, because the ticker channel's rolling LTP field republishes
    on its own cadence. That minute must not appear in `/bars` at all — `quote-bars` gates
    "stored" for this route, exactly as it gates the historical ladder's slider."""
    stores.reference.add([reference_bar(ltp=1082.0)])
    stores.reference.flush()

    assert bars_of(make_client())["bars"] == []


def test_a_one_sided_tick_minute_has_no_mid_but_still_has_a_bid(
    make_client, stores: HistoricalSource
) -> None:
    """`bars.QuoteBar`'s own rule: a tick with a bid and no ask advances only the bid
    series, so the mid series never ticked this minute and is null on the bar — a real
    partial observation, not an error in this route."""
    stores.quote.add([quote_bar(bid=73.0, ask=None, mid=None)])
    stores.quote.flush()

    bar = bars_of(make_client())["bars"][0]

    assert bar["bid_close"] == 73.0
    assert bar["ask_close"] is None
    assert bar["mid_close"] is None


# --- addressed by canonical string --------------------------------------------------


def test_the_instrument_and_expiry_echo_back_what_was_asked(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    body = bars_of(make_client())

    assert body["instrument"] == INSTRUMENT
    assert body["underlying"] == "BTC"
    assert body["expiry"] == "04-09-2026"
    assert body["date"] == "2026-09-04"


def test_the_underlying_in_the_instrument_string_is_normalised(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(underlying="BTC")])
    stores.quote.flush()

    body = bars_of(make_client(), instrument="DELTA-btc-20260904-77600-C-USD")

    assert body["underlying"] == "BTC"
    assert body["bars"][0]["bid_close"] == 73.0


def test_a_different_strike_answers_nothing(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(strike=77600.0)])
    stores.quote.flush()

    body = bars_of(make_client(), instrument="DELTA-BTC-20260904-80000-C-USD")

    assert body["bars"] == []


def test_a_put_and_a_call_at_the_same_strike_are_different_contracts(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add(
        [
            quote_bar(option_type="C", bid=73.0),
            quote_bar(option_type="P", bid=5.0),
        ]
    )
    stores.quote.flush()

    call = bars_of(make_client(), instrument="DELTA-BTC-20260904-77600-C-USD")["bars"][0]
    put = bars_of(make_client(), instrument="DELTA-BTC-20260904-77600-P-USD")["bars"][0]

    assert call["bid_close"] == 73.0
    assert put["bid_close"] == 5.0


def test_venue_is_parsed_but_not_filtered_on(
    make_client, stores: HistoricalSource
) -> None:
    """The store carries no venue column — see the module docstring. A canonical string
    naming a venue other than the one this store happens to hold data for still answers
    from the same rows, because there is nothing in the schema to disagree with it."""
    stores.quote.add([quote_bar()])
    stores.quote.flush()

    body = bars_of(make_client(), instrument="NSE-BTC-20260904-77600-C-USD")

    assert body["instrument"] == "NSE-BTC-20260904-77600-C-USD"
    assert body["bars"][0]["bid_close"] == 73.0


# --- the union of disk and buffer, exactly as /chain/at takes it -------------------


def test_a_minute_still_in_the_buffer_reaches_the_wire_unflushed(
    make_client, stores: HistoricalSource
) -> None:
    stores.quote.add([quote_bar(bid=73.0)])
    assert stores.quote.buffered == 1, "unflushed, or this test proves nothing"

    bar = bars_of(make_client())["bars"][0]

    assert bar["bid_close"] == 73.0


# --- the error table ---------------------------------------------------------------


@pytest.mark.parametrize(
    "instrument",
    [
        "not-a-canonical-string",
        "DELTA-BTC-2026-09-04-77600-C",
        "DELTA-BTC-20260904-77600-X",
        "DELTA-BTC-nonsense-77600-C",
        # #60 (I1): the pre-I1 five-part shape, with no currency token — now
        # rejected loudly rather than accepted with a guessed currency.
        "DELTA-BTC-20260904-77600-C",
        # A six-part string whose currency token is not upper-case ISO 4217 shaped.
        "DELTA-BTC-20260904-77600-C-usd",
    ],
)
def test_a_malformed_instrument_is_400(make_client, instrument: str) -> None:
    response = make_client().get(
        "/bars", params={"instrument": instrument, "date": "2026-09-04"}
    )
    assert response.status_code == 400


def test_an_underlying_this_engine_does_not_know_is_400(make_client) -> None:
    response = make_client().get(
        "/bars",
        params={"instrument": "DELTA-SOL-20260904-77600-C", "date": "2026-09-04"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("date", ["04-09-2026", "2026/09/04", "nonsense"])
def test_a_malformed_date_is_400(make_client, date: str) -> None:
    response = make_client().get(
        "/bars", params={"instrument": INSTRUMENT, "date": date}
    )
    assert response.status_code == 400


def test_missing_parameter_is_422_from_fastapi(make_client) -> None:
    response = make_client().get("/bars", params={"instrument": INSTRUMENT})
    assert response.status_code == 422


# --- the wiring, with nothing overridden ------------------------------------------


def test_the_route_reads_the_running_writers_own_stores(
    monkeypatch, tmp_path: Path
) -> None:
    """No dependency override here — the one test that checks the seam itself, exactly
    as `test_historical.py`'s equivalent does. Every other test in this file would keep
    passing if the route were wired to fresh stores over the same directory; that store
    would have an empty buffer and would silently drop whatever had not yet been
    flushed."""
    from deltapayoff.store import BarWriter

    writer = BarWriter(BarStore(tmp_path))
    writer.store.add([quote_bar(minute=MINUTE, bid=73.0)])
    assert writer.store.buffered == 1
    monkeypatch.setattr(app.state, "writer", writer, raising=False)

    body = bars_of(TestClient(app))
    assert body["bars"][0]["bid_close"] == 73.0
