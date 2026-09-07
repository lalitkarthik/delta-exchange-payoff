"""One contract's minute bars for one date. `docs/bars-contract.md`.

The contract chart's read path, and the third sibling of `smile.py` and `historical.py`:
it solves nothing, calls nothing, and adds no number that was not already written to
disk or held in a writer's buffer. Where `historical.py` joins three tables across every
listed strike at **one minute**, this joins **two** tables across every stored minute of
**one contract** — `quote-bars` for the mid, bid and ask candles the chart draws by
default, and `reference-bars` for the last-traded-price candle its toggle switches to.

**Addressed by canonical string, not by the store's own columns.** The route takes
`events.instrument.Instrument.from_canonical`, so the URL, the log line and the cache
key this project is unifying around agree here too. `venue` is parsed and then
deliberately not filtered on: no column in `store.py`'s schemas carries it — the store
predates the multi-venue instrument and holds one venue's data today, exactly as
`docs/design/events.md` decided for `underlying`/`expiry`/`strike`/`option_type`. A
canonical string naming a venue this store never recorded is not an error; it is simply a
contract this store has nothing to say about, and the response says so honestly with an
empty list rather than a 404 that would imply the route itself does not exist.

**No forward-fill, by construction rather than by a rule someone has to remember.** A
minute is in the response exactly when `quote-bars` holds a row for it — the same gate
`historical.list_minutes` uses for the slider's domain, so "stored" means one thing
across both read paths. `reference-bars` is **joined onto** that minute set, never used
to widen it: a minute where the book never quoted but the ticker's rolling LTP field
happened to republish would otherwise appear as a bar with no bid or ask at all, which is
not what "the book was quoted this minute" means. A minute `reference-bars` has nothing
for still appears, with its last-traded OHLC null — the leg the toggle needs simply has
nothing to show for that minute, and the chart's gap handling is what draws that, not
this module inventing a row to hide it in.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime

import polars as pl
from pydantic import BaseModel

from .events.instrument import Instrument
from .store import BarStore, scan_and_pending

#: `2026-09-04T09:00:00Z` — the same spelling `historical.MINUTE_FORMAT` and
#: `smile.MINUTE_FORMAT` use, so a stamp out of this route needs no reformatting either.
MINUTE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ContractBar(BaseModel):
    """One minute of one contract, in the two shapes the chart needs.

    **The store's own column names are not this response's field names**, and that
    translation happens here and nowhere else. `store.py`'s `REFERENCE_SCHEMA` spells the
    last-traded price `ltp_*` and open interest `oi_contracts`; this response spells the
    candle `ltp_*` too — the chart's toggle reads it under the name the store already
    uses, so only `mark_*` needed renaming, and it is not carried here at all: the chart
    draws mid, bid, ask and last-trade, never mark.

    Every field is nullable independently of the others. A stored bar's own OHLC can be
    partially null before this route ever sees it — a one-sided tick minute has a `bid`
    series and no `mid` series, `bars.QuoteBar`'s own docstring explains why — and this
    model carries that nullability through rather than smoothing it into a default.
    """

    #: ISO 8601 UTC, second precision, `Z`-suffixed.
    minute: str

    bid_open: float | None = None
    bid_high: float | None = None
    bid_low: float | None = None
    bid_close: float | None = None

    ask_open: float | None = None
    ask_high: float | None = None
    ask_low: float | None = None
    ask_close: float | None = None

    mid_open: float | None = None
    mid_high: float | None = None
    mid_low: float | None = None
    mid_close: float | None = None

    #: Last-traded-price OHLC, from `reference-bars`. `null` on a minute `quote-bars`
    #: holds but `reference-bars` does not — see the module docstring.
    ltp_open: float | None = None
    ltp_high: float | None = None
    ltp_low: float | None = None
    ltp_close: float | None = None


class ContractBarsResponse(BaseModel):
    """`GET /bars`. `docs/bars-contract.md`."""

    #: The canonical string this route was addressed by, echoed back rather than
    #: reconstructed from the parts below — the one spelling every part of this project
    #: agrees on, unchanged by a round trip through this route.
    instrument: str
    underlying: str
    #: `DD-MM-YYYY`, Delta's own spelling — matching `/chain` and `/chain/at`, not this
    #: route's own `date` parameter, which stays `YYYY-MM-DD`.
    expiry: str
    #: `YYYY-MM-DD` — the store's own partition spelling, and the day this response's
    #: bars were read for. Not necessarily the contract's expiry date: a contract trades
    #: on every day up to and including it.
    date: str
    #: Ascending by minute. A minute nobody quoted is **absent**, never a null row.
    bars: list[ContractBar]


def read_contract_bars(
    quote_store: BarStore,
    reference_store: BarStore,
    instrument: Instrument,
    day: Date,
) -> list[ContractBar]:
    """One contract's day, minute by minute.

    `[]` when `quote-bars` holds nothing for this contract on this day.
    """
    underlying = instrument.underlying
    expiry = instrument.expiry.strftime("%d-%m-%Y")
    strike = float(instrument.strike)
    option_type = instrument.right.value

    quote_lazy = _rows(
        quote_store,
        underlying,
        expiry,
        strike,
        option_type,
        day,
        (
            "minute",
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
            "mid_open",
            "mid_high",
            "mid_low",
            "mid_close",
        ),
    )
    reference_lazy = _rows(
        reference_store,
        underlying,
        expiry,
        strike,
        option_type,
        day,
        ("minute", "ltp_open", "ltp_high", "ltp_low", "ltp_close"),
    )
    # Two independent Parquet reads with no data dependency between them, collected
    # together rather than one after the other: `collect_all` lets Polars' thread pool
    # overlap the two scans instead of this route paying for both sequentially, on a
    # route already deliberately synchronous to keep the read off the feed's event loop.
    quote_frame, reference_frame = pl.collect_all([quote_lazy, reference_lazy])

    quotes = {row["minute"]: row for row in quote_frame.iter_rows(named=True)}
    references = {row["minute"]: row for row in reference_frame.iter_rows(named=True)}

    return [
        _bar(minute, quotes[minute], references.get(minute))
        for minute in sorted(quotes)
    ]


def _rows(
    store: BarStore,
    underlying: str,
    expiry: str,
    strike: float,
    option_type: str,
    day: Date,
    columns: tuple[str, ...],
) -> pl.LazyFrame:
    """Disk and buffer, filtered to one contract on one day, unioned by
    `store.scan_and_pending` — see that function for why the union matters: the buffer
    holds up to a flush interval nothing on disk yet describes, and a parquet-only read
    would answer "nothing here" for a minute the store already has. The projection to
    `columns` is applied after the union rather than before; Polars pushes it down into
    each lazy source either way, so this reads as one filter and one select rather than
    the same select written out twice."""
    clause = (
        (pl.col("underlying") == underlying)
        & (pl.col("expiry") == expiry)
        & (pl.col("strike") == strike)
        & (pl.col("option_type") == option_type)
        & (pl.col("date") == day)
    )
    return scan_and_pending(store, clause).select(columns)


def _bar(
    minute: datetime, quote_row: dict, reference_row: dict | None
) -> ContractBar:
    stamp = minute.strftime(MINUTE_FORMAT)
    return ContractBar(
        minute=stamp,
        bid_open=quote_row["bid_open"],
        bid_high=quote_row["bid_high"],
        bid_low=quote_row["bid_low"],
        bid_close=quote_row["bid_close"],
        ask_open=quote_row["ask_open"],
        ask_high=quote_row["ask_high"],
        ask_low=quote_row["ask_low"],
        ask_close=quote_row["ask_close"],
        mid_open=quote_row["mid_open"],
        mid_high=quote_row["mid_high"],
        mid_low=quote_row["mid_low"],
        mid_close=quote_row["mid_close"],
        ltp_open=reference_row["ltp_open"] if reference_row else None,
        ltp_high=reference_row["ltp_high"] if reference_row else None,
        ltp_low=reference_row["ltp_low"] if reference_row else None,
        ltp_close=reference_row["ltp_close"] if reference_row else None,
    )
