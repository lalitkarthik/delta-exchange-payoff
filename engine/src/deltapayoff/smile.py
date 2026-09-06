"""Reading the stored volatility surface back out. `docs/smile-contract.md`.

The one module that turns table C — `computed-bars` — into what the volatility screen
plots. Pure but for the store handed to it: it solves nothing, calls nothing, and adds no
number that was not already written to disk or held in the writer's buffer.

**It reads a whole expiry at once, not a minute.** `measured`, three runs, minimum: one
minute for one expiry reads in 4.5 ms against 6.8 ms for 540 minutes and 18,676 rows.
Parquet prunes by partition directory and by column, so the fixed cost dominates and the
day costs 2.3 ms more than the minute. That measurement predates the five-minute flush
([#16](https://github.com/lalitkarthik/delta-exchange-payoff/issues/16)), which multiplies
the current day's file count by twelve; the re-measurement is owed and `derived` at
roughly 88 ms until it is taken.

**The columns are named rather than taken whole, and that is not tidiness.** Table C has
twenty columns and this screen reads eleven of them; naming them is what lets Parquet skip
the five Greeks' bytes entirely. The five are stored and are deliberately not served — the
smile plots volatility, and five figures nothing on the screen reads would be five more
chances for the client and the store to drift.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from .models import SmileMinute, SmilePoint, SmileResponse
from .store import BarStore

#: What the screen reads. Everything else in table C — the Greeks, the symbol, the
#: option type past the de-duplication below — stays on disk.
COLUMNS = (
    "minute",
    "strike",
    "option_type",
    "iv",
    "iv_leg",
    "iv_reason",
    "forward",
    "discount",
    "years_to_expiry",
    "forward_method",
    "model_version",
)

#: Categorical in the store, plain strings on the wire. Dictionary encoding is a storage
#: decision and JSON has no opinion about it.
STRINGS = ("option_type", "iv_leg", "iv_reason", "forward_method", "model_version")

#: The chain-level fields, repeated down every stored row of a minute and lifted to the
#: minute in the response. One value per curve, so one copy.
PER_MINUTE = ("forward", "discount", "years_to_expiry", "forward_method", "model_version")

#: ISO 8601, UTC, second precision. The store's minutes are microsecond datetimes on a
#: UTC clock, and a minute is exact, so the fractional part would always be zeros.
MINUTE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def read_smile(store: BarStore, underlying: str, expiry: str) -> SmileResponse:
    """Every stored minute for one expiry.

    Absence is an empty series and never an error. An underlying with no partitions, an
    expiry that was never stored and a store with no files at all are all "nothing yet".
    """
    rows = _rows(store, underlying, expiry)
    return SmileResponse(
        underlying=underlying,
        expiry=expiry,
        model_versions=_stamps(rows),
        minutes=_minutes(rows),
    )


def _rows(store: BarStore, underlying: str, expiry: str) -> list[dict[str, Any]]:
    """Disk **and** buffer for one expiry, de-duplicated to one row per strike, ascending.

    **The union is the point of this endpoint.** The store flushes every five minutes, so
    reading only the files hands the screen a right edge up to a full interval behind the
    live curve — `measured` this session at 07:38Z flushed against a clock of 08:05Z, a
    27-minute hole. The buffer already lives in this process, so the union costs a
    concatenation; skipping it would ship a gap nobody looking at the screen could
    explain, and the regression would look correct in every test that did not check for
    exactly this one thing.

    The two sources go through the same filter and the same projection, so they cannot
    disagree about what "this expiry" means or about which columns exist.

    **One point per strike, not per leg.** Table C's grain is the contract, so a paired
    strike holds two rows carrying the same volatility — parity gives the strike one
    number and `compute.enrich` writes it to both sides, with `iv_leg` naming the side it
    came from. The de-duplication is therefore not a choice between two values. It sorts
    on `option_type` first so the survivor is the same row on every run rather than
    whichever one the file happened to hold first.
    """
    sources = (store.scan(), store.pending())
    frame = pl.concat(
        [_filtered(source, underlying, expiry) for source in sources],
        how="vertical_relaxed",
    )
    ordered = frame.sort("minute", "strike", "option_type").unique(
        subset=("minute", "strike"), keep="first", maintain_order=True
    )
    return ordered.collect().to_dicts()


def _filtered(source: pl.LazyFrame, underlying: str, expiry: str) -> pl.LazyFrame:
    """One source, narrowed to one expiry and to the columns the screen reads.

    Pushed into the lazy frame rather than applied after collecting, so on the disk side
    `date=…/underlying=…` is answered by the directory names before a file is opened and
    the Greeks' bytes are never read. The categorical columns are widened to strings here,
    before the concatenation: dictionary encoding is a storage decision, JSON has no
    opinion about it, and two frames carrying two different dictionaries for one column
    would otherwise have to be reconciled to be stacked.
    """
    return (
        source.filter(
            (pl.col("underlying") == underlying) & (pl.col("expiry") == expiry)
        )
        .select(COLUMNS)
        .with_columns(*(pl.col(name).cast(pl.String) for name in STRINGS))
    )


def _stamps(rows: list[dict[str, Any]]) -> list[str]:
    """Every distinct model stamp present, ascending.

    A list rather than a field, because the model can change mid-day and every row says
    which one made it. A response spanning two stamps reports both; choosing one silently
    would put two differently computed curves on one axis with nothing to say so.
    """
    return sorted({row["model_version"] for row in rows if row["model_version"]})


def _minutes(rows: list[dict[str, Any]]) -> list[SmileMinute]:
    """Group the flat frame into one entry per minute, points ascending by strike."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["minute"].strftime(MINUTE_FORMAT), []).append(row)
    return [
        SmileMinute(
            minute=minute,
            points=[
                SmilePoint(
                    strike=row["strike"],
                    iv=row["iv"],
                    iv_leg=row["iv_leg"],
                    iv_reason=row["iv_reason"],
                )
                for row in group
            ],
            **{field: group[0][field] for field in PER_MINUTE},
        )
        for minute, group in sorted(grouped.items())
    ]
