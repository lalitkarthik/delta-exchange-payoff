"""Reading the ladder back out at one stored minute. `docs/historical-chain-contract.md`.

The historical sibling of `smile.py`: it solves nothing and calls nothing, and adds no
number that was not already written to disk or held in a writer's buffer. Where
`smile.py` reads one table across a whole expiry, this reads **three** tables — quote,
reference and computed bars — joined on `symbol` at **one** minute, because a ladder is a
join across those three and a smile point is not.

**No forward-fill, enforced by construction rather than by a rule someone has to
remember.** `read_ladder_at` returns `None` when quote-bars holds no row for the exact
minute asked for. There is no fallback to a neighbouring minute anywhere in this module —
the caller (`main.chain_at`) turns `None` into the same "nothing here" shape `/ws/chain`
sends for `waiting`, and that is the only thing it may do with it.

**A fourth table, `spot-bars`, is read too, and that is a deliberate widening of the
three the ticket names.** `docs/chain-contract.md` types `spot` and `atm_strike`
`number | null` on `ChainResponse` (#47) for exactly this reason. Reading the one extra
table that already records the real spot for the minute is the honest answer; inventing
one from the forward would conflate two figures `docs/chain-contract.md` is explicit are
never the same number. When `spot-bars` has no row for the minute — possible for the
same reason a computed bar can be missing while a quote bar exists, see
`store.COMPUTED_SAMPLE_SECONDS` — `spot` and `atm_strike` are `None`, and
`web/components/ChainLadder.tsx`'s `inTheMoney` guard (#47) renders that as no highlight
on either side rather than the `strike < null` wash it used to be — see the LLD.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Any

import polars as pl

from .chain import QUOTE_CURRENCY, nearest_strike
from .models import ChainRow, ComputedLeg, HistoricalChain, Leg
from .store import BarStore, scan_and_pending

#: `2026-09-04T09:00:00Z` — the store's own spelling, matching `smile.MINUTE_FORMAT`
#: exactly so a stamp round-trips through the URL, the minutes route and the ladder
#: route with no reformatting anywhere in the stack.
MINUTE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def list_minutes(
    quote_store: BarStore, underlying: str, expiry: str, day: Date
) -> list[str]:
    """Every minute quote-bars holds for this underlying, expiry and date, ascending.

    **The domain is quote arrivals, not reference or computed ones.** `read_ladder_at`
    below answers "nothing here" exactly when quote-bars has no row, so the slider's
    domain has to agree with that gate — a minute this route omitted would be a hole the
    ladder route could still answer, and a minute it listed that the ladder route refused
    would be a control that lies about its own track.
    """
    frame = _union(quote_store, underlying, expiry, day=day).select("minute")
    minutes = frame.unique().sort("minute").collect()["minute"]
    return [minute.strftime(MINUTE_FORMAT) for minute in minutes]


def read_ladder_at(
    quote_store: BarStore,
    reference_store: BarStore,
    computed_store: BarStore,
    spot_store: BarStore,
    underlying: str,
    expiry: str,
    minute: datetime,
) -> HistoricalChain | None:
    """The ladder rebuilt from four tables at one minute. `None` when nothing was quoted.

    Quote bars give `bid`/`ask`; reference bars give `mark`, the venue's own IV and
    Greeks, and open interest; computed bars give ours. `spot-bars` gives the two
    chain-level fields the module docstring explains. Everything a table does not hold
    for this minute — `product_id` and `tick_size` are never stored at all,
    `oi_value_usd` only ever arrives over REST and this reads bars — is `None`, not a
    default: the columns are not on the schema, and there is nothing to fall back to.
    """
    day = minute.date()
    quotes = _at(quote_store, underlying, expiry, minute, day=day).collect()
    if quotes.is_empty():
        return None

    references = {
        row["symbol"]: row
        for row in _at(reference_store, underlying, expiry, minute, day=day)
        .collect()
        .iter_rows(named=True)
    }
    computed = {
        row["symbol"]: row
        for row in _at(computed_store, underlying, expiry, minute, day=day)
        .collect()
        .iter_rows(named=True)
    }

    legs: dict[float, dict[str, Leg]] = {}
    for row in quotes.iter_rows(named=True):
        side = "call" if row["option_type"] == "C" else "put"
        legs.setdefault(row["strike"], {})[side] = _leg(
            row, references.get(row["symbol"]), computed.get(row["symbol"])
        )

    rows = [
        ChainRow(strike=strike, call=sides.get("call"), put=sides.get("put"))
        for strike, sides in sorted(legs.items())
    ]

    spot = _spot_at(spot_store, underlying, minute, day=day)
    stamp = minute.strftime(MINUTE_FORMAT)
    return HistoricalChain(
        underlying=underlying,
        expiry=expiry,
        minute=stamp,
        # No second clock: `fetched_at` on the live path is "when we asked Delta", and
        # there is no such moment here. The minute this ladder describes is the only
        # honest answer, so the two fields carry the same stamp rather than one of them
        # being left to mean something it cannot.
        fetched_at=stamp,
        spot=spot,
        atm_strike=nearest_strike(list(legs), spot),
        # Every table this route reads was written by this codebase's own Delta
        # adapter — there is no venue field on a Parquet row to read a currency off,
        # only `symbol`, `strike`, `option_type` and the rest of the schema
        # `docs/design/lld/store.md` fixes. `QUOTE_CURRENCY` is therefore the same
        # constant `build_chain` uses for the same reason.
        quote_currency=QUOTE_CURRENCY,
        rows=rows,
        **_chain_fields(computed),
    )


def _union(
    store: BarStore, underlying: str, expiry: str, *, day: Date | None = None
) -> pl.LazyFrame:
    """Disk and buffer, filtered to one underlying and expiry, unioned by
    `store.scan_and_pending` — see that function for why the union matters. The buffer
    holds up to a flush interval nothing on disk yet describes, and a parquet-only read
    would answer "nothing here" for a minute the store already has."""
    clause = (pl.col("underlying") == underlying) & (pl.col("expiry") == expiry)
    if day is not None:
        clause = clause & (pl.col("date") == day)
    return scan_and_pending(store, clause)


def _at(
    store: BarStore, underlying: str, expiry: str, minute: datetime, *, day: Date
) -> pl.LazyFrame:
    """`_union`, narrowed to one exact minute. `day` is passed through so the partition
    directory answers most of the filter before a file is even opened."""
    return _union(store, underlying, expiry, day=day).filter(pl.col("minute") == minute)


def _spot_at(
    store: BarStore, underlying: str, minute: datetime, *, day: Date
) -> float | None:
    """`spot-bars` carries no `expiry` column at all — it is one row per underlying per
    minute, not per contract — so the filter here is narrower than `_union`'s."""
    clause = (
        (pl.col("underlying") == underlying)
        & (pl.col("date") == day)
        & (pl.col("minute") == minute)
    )
    rows = scan_and_pending(store, clause).select("spot_close").collect()
    if rows.is_empty():
        return None
    return rows["spot_close"][0]


def _leg(
    quote_row: dict[str, Any],
    reference_row: dict[str, Any] | None,
    computed_row: dict[str, Any] | None,
) -> Leg:
    """One contract's leg at one minute, joined by `symbol` across the three tables.

    `bid`/`ask` are the **close** of the minute's OHLC range — the last observation
    before the minute sealed, matching what "the ladder as it stood at that minute"
    means for every other close-valued field here. The open, high and low are stored and
    deliberately not served: the live ladder carries one number per field, not four.
    """
    return Leg(
        symbol=quote_row["symbol"],
        bid=quote_row["bid_close"],
        ask=quote_row["ask_close"],
        mark=reference_row["mark_close"] if reference_row else None,
        bid_iv=reference_row["venue_bid_iv"] if reference_row else None,
        ask_iv=reference_row["venue_ask_iv"] if reference_row else None,
        mark_iv=reference_row["venue_mark_iv"] if reference_row else None,
        delta=reference_row["venue_delta"] if reference_row else None,
        gamma=reference_row["venue_gamma"] if reference_row else None,
        theta=reference_row["venue_theta"] if reference_row else None,
        vega=reference_row["venue_vega"] if reference_row else None,
        rho=reference_row["venue_rho"] if reference_row else None,
        oi=reference_row["oi_contracts"] if reference_row else None,
        # Never stored: REST-only, and this reads bars. Absent, not derived — the
        # chain contract is explicit that contracts x size x spot is a calculation and
        # this field reports an observation.
        oi_value_usd=None,
        oi_change_usd_6h=reference_row["oi_change_usd_6h"] if reference_row else None,
        # Never stored at all, on any table.
        tick_size=None,
        computed=_computed_leg(computed_row) if computed_row else None,
    )


def _computed_leg(row: dict[str, Any]) -> ComputedLeg:
    """Ours, from table C. `iv_reason` is `""` when solved — `/chain`'s own spelling,
    not the store's `None` — because this response is typed as `ComputedLeg`, the same
    model the live path answers with."""
    return ComputedLeg(
        iv=row["iv"],
        iv_leg=row["iv_leg"],
        iv_reason=row["iv_reason"] or "",
        delta=row["delta"],
        gamma=row["gamma"],
        vega=row["vega"],
        theta=row["theta"],
        rho=row["rho"],
    )


#: The chain-level fields table C repeats down every row of a minute. `None` together
#: when computed-bars has nothing for this minute — the same rule `ChainResponse` already
#: states: a chain with no forward carries no volatility on any leg either.
_CHAIN_FIELDS = ("forward", "discount", "years_to_expiry", "forward_method")


def _chain_fields(computed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not computed:
        return dict.fromkeys(_CHAIN_FIELDS)
    first = next(iter(computed.values()))
    return {field: first[field] for field in _CHAIN_FIELDS}
