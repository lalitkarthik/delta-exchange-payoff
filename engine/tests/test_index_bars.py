"""`index-bars`: the venue's own index candles, kept apart from what we recorded.

**Why a fifth table rather than rows added to `spot-bars`.** R1 measured Delta's
per-minute range wider than ours on 16 of 16 overlapping minutes — 1.769e-04 against
1.341e-04 at the median — while the closes agree to a part in 10^5
(`docs/index-history.md` §5). Those are two different measurements of the same index, and
a range estimator reads whichever it is handed. Mixed into one table, a rolling window's
answer would depend on where it happened to fall across the seam, which is an artefact
nobody would ever see. `spot-bars` also has no column that could tell the two apart
afterwards, and its `spot_ticks` has no meaning for a venue candle, whose `volume` is null
on every bar — it could only be written as a null or as a lie.

The seams here are `write_index_bars`/`read_index_bars` and the pure conversion in
`tools/backfill_index_bars.py`. Nothing reaches into the store's file layout, which
`test_store.py` already pins.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from deltapayoff.bars import IndexBar
from deltapayoff.store import (
    INDEX_DATASET,
    INDEX_SCHEMA,
    BarStore,
    read_index_bars,
)

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from backfill_index_bars import (  # noqa: E402
    PAGE_BARS,
    candles_to_bars,
    new_rows,
    plan_pages,
)

UTC = timezone.utc
START = datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
SYMBOL = ".DEXBTUSD"


def bar(minute: datetime, close: float, *, underlying: str = "BTC") -> IndexBar:
    """One candle, its four prices spread either side of `close` by a fixed amount.

    The spread is deliberate and asymmetric so a test that transposed high and low, or
    open and close, would fail rather than pass on a square bar.
    """
    return IndexBar(
        underlying=underlying,
        minute=minute,
        symbol=SYMBOL,
        index_open=close - 3.0,
        index_high=close + 5.0,
        index_low=close - 7.0,
        index_close=close,
    )


@pytest.fixture
def store(tmp_path) -> BarStore:
    return BarStore(tmp_path, dataset=INDEX_DATASET, schema=INDEX_SCHEMA)


def test_index_bars_round_trip_as_the_estimators_bar_type(store: BarStore) -> None:
    """Written as `IndexBar`, read back as the `Bar` the estimators already take.

    The reader's job is to hand `realised_vol` something it can consume unchanged, so the
    return type is `realised_vol.Bar` and not a store row — the same disposition
    `read_spot_bars` takes, and the reason `volatility.py` needs no conversion step.
    """
    store.add([bar(START, 80_000.0), bar(START + timedelta(minutes=1), 80_010.0)])
    store.flush()

    bars = read_index_bars(store, "BTC")

    assert [b.at for b in bars] == [START, START + timedelta(minutes=1)]
    assert [b.close for b in bars] == [80_000.0, 80_010.0]
    assert [b.open for b in bars] == [79_997.0, 80_007.0]
    assert [b.high for b in bars] == [80_005.0, 80_015.0]
    assert [b.low for b in bars] == [79_993.0, 80_003.0]


def test_a_hole_in_the_record_survives_the_round_trip(store: BarStore) -> None:
    """Minutes 0, 1 and 5 in; minutes 0, 1 and 5 out. Never 2, 3 and 4.

    The never-forward-fill rule at the storage seam. Delta's own
    `/v2/history/candles` pads a bucket that saw no trade with the last trade and does
    not say so, which is the mistake this whole project was started by. The index does
    not pad (`docs/index-history.md` §4) — but a hole reaching the store from any other
    cause must still arrive at the estimators as a hole, because `_adjacent_pairs` drops
    a return that spans one and cannot drop what it cannot see.
    """
    minutes = [START, START + timedelta(minutes=1), START + timedelta(minutes=5)]
    store.add([bar(m, 80_000.0 + i) for i, m in enumerate(minutes)])
    store.flush()

    assert [b.at for b in read_index_bars(store, "BTC")] == minutes


def test_a_null_price_is_dropped_rather_than_defaulted(store: BarStore) -> None:
    """`null` is not `0`. A bar built on a zero would be a 100% return into and out.

    The venue has no reason to serve a null price on an index candle, so this is a
    defence against the store rather than against Delta — a partial write, a schema
    widened later, a compaction that filled a column. It costs one `drop_nulls` and it
    forecloses a class of realised-volatility spike that would look like a market event.
    """
    holed = IndexBar(
        underlying="BTC",
        minute=START + timedelta(minutes=1),
        symbol=SYMBOL,
        index_open=80_000.0,
        index_high=80_005.0,
        index_low=None,
        index_close=80_002.0,
    )
    store.add([bar(START, 80_000.0), holed, bar(START + timedelta(minutes=2), 80_020.0)])
    store.flush()

    bars = read_index_bars(store, "BTC")

    assert [b.at for b in bars] == [START, START + timedelta(minutes=2)]


def test_two_indices_in_one_store_are_told_apart_by_symbol(store: BarStore) -> None:
    """`.DEXBTUSD` and `.DEXBTUSDT_LTP` both serve BTC, and they disagree.

    One is priced from resting books and the other from last trades, so their bars are
    not interchangeable at any tolerance that matters to a range estimator. A store
    holding both must be asked which is wanted; unasked, it returns both interleaved,
    which a caller sees at once. The failure mode being foreclosed is the quiet one —
    receiving whichever happened to sort first and never knowing a choice was made.
    """
    other = IndexBar(
        underlying="BTC",
        minute=START,
        symbol=".DEXBTUSDT_LTP",
        index_open=80_100.0,
        index_high=80_105.0,
        index_low=80_095.0,
        index_close=80_101.0,
    )
    store.add([bar(START, 80_000.0), other])
    store.flush()

    assert [b.close for b in read_index_bars(store, "BTC", symbol=SYMBOL)] == [80_000.0]
    assert [b.close for b in read_index_bars(store, "BTC", symbol=".DEXBTUSDT_LTP")] == [
        80_101.0
    ]
    assert len(read_index_bars(store, "BTC")) == 2


# ---------------------------------------------------------------------------
# The backfill job's pure core. `tools/` is not on the package path, so it is
# imported by file — the same thing `test_measure_window.py` already does for
# `tools/_window.py`. Nothing below opens a socket: the network half of the job
# is a separate function that takes a fetcher, and no test supplies a real one.
# ---------------------------------------------------------------------------


def test_candles_become_rows_with_no_bucket_invented() -> None:
    """Three candles spanning five minutes produce three rows, not five.

    This is the founding lesson made executable one layer earlier than the store. The
    endpoint that serves these is the same one that returns 801 daily bars for
    `C-BTC-60000-270624` of which 797 are fabricated, and the conversion's only defence
    against inheriting that habit is to never create a row the payload did not contain.
    """
    candles = [
        {
            "time": int(START.timestamp()),
            "open": 1.0, "high": 4.0, "low": 0.5, "close": 3.0,
        },
        {
            "time": int((START + timedelta(minutes=1)).timestamp()),
            "open": 3.0, "high": 5.0, "low": 2.0, "close": 4.0,
        },
        {
            "time": int((START + timedelta(minutes=5)).timestamp()),
            "open": 4.0, "high": 6.0, "low": 3.5, "close": 5.5,
        },
    ]

    rows = candles_to_bars(candles, underlying="BTC", symbol=SYMBOL)

    assert [r.minute for r in rows] == [
        START,
        START + timedelta(minutes=1),
        START + timedelta(minutes=5),
    ]
    assert [r.index_close for r in rows] == [3.0, 4.0, 5.5]
    assert all(r.symbol == SYMBOL and r.underlying == "BTC" for r in rows)


def test_rerunning_over_a_stored_range_adds_nothing() -> None:
    """Idempotence. `BarStore.flush` appends a file and has no notion of a key.

    So a job re-run over a range it already fetched would double every row in it, and
    every estimator reading them would see each minute twice — a series whose returns
    alternate between the real move and exactly zero, which suppresses realised
    volatility in the same direction padding would. Filtering before the buffer is the
    only place this can be caught.
    """
    rows = candles_to_bars(
        [
            {
                "time": int(START.timestamp()),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            },
            {
                "time": int((START + timedelta(minutes=1)).timestamp()),
                "open": 1.5, "high": 3.0, "low": 1.0, "close": 2.5,
            },
        ],
        underlying="BTC",
        symbol=SYMBOL,
    )

    assert len(new_rows(rows, held=set())) == 2
    assert new_rows(rows, held={(START, SYMBOL)}) == rows[1:]
    assert new_rows(rows, held={(r.minute, r.symbol) for r in rows}) == []


def test_the_same_minute_under_two_symbols_is_two_rows() -> None:
    """The key is `(minute, symbol)`, not the minute alone.

    Backfilling `.DEXBTUSDT` into a store that already holds `.DEXBTUSD` must not be
    silently skipped as already-present — they are different series that happen to share
    a clock, which is the whole reason `symbol` is on the row.
    """
    rows = candles_to_bars(
        [
            {
                "time": int(START.timestamp()),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            }
        ],
        underlying="BTC",
        symbol=".DEXBTUSDT",
    )

    assert new_rows(rows, held={(START, SYMBOL)}) == rows


def test_pages_walk_backwards_and_never_overlap() -> None:
    """Newest first, each window inside the 4,000-bar cap, no minute asked for twice.

    The cap truncates the *oldest* rows of an over-wide window
    (`docs/delta-api-scope.md`), so a forward walk asking for more than it can get would
    lose the far end of each page and leave holes no later pass would look for. Walking
    backwards at a width the endpoint can answer whole means the truncation direction
    stops mattering.

    The expected page count comes from the arithmetic of the span, not from re-running
    the function's own loop: ten thousand buckets over a four-thousand-bucket page is
    three pages, and the oldest of them is the short one.
    """
    end = int(START.timestamp())
    start = end - 10_000 * 60

    pages = plan_pages(start=start, end=end, page_bars=PAGE_BARS)

    assert len(pages) == 3
    assert pages[0].end == end, "the first request must be the newest window"
    assert pages[-1].start == start, "the walk must reach the far end exactly"
    widths = [(p.end - p.start) // 60 for p in pages]
    assert widths == [4_000, 4_000, 2_000]
    for newer, older in zip(pages[:-1], pages[1:], strict=True):
        assert older.end == newer.start, "windows must abut, never overlap"


def test_an_empty_or_inverted_span_asks_for_nothing() -> None:
    """No window is not zero windows of width zero; it is no request at all."""
    now = int(START.timestamp())
    assert plan_pages(start=now, end=now) == []
    assert plan_pages(start=now, end=now - 3600) == []
