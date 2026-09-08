#!/usr/bin/env python3
"""Backfill Delta's own BTC index candles into the store's `index-bars` table.

The IV-vs-RV screen (#24) has refused every lookback since it shipped, and not because
anything is broken: `min_days` is 7.24 — the shortest listed expiry, past the seven-day
floor — while `max_days` is the span of what we happened to record, which was 4.17 days
holding 66 distinct minutes. R1 (#25) removed the reason to wait. `.DEXBTUSD` serves
complete 1-minute candles back to about 2023-12-20, measured by narrow windows walked
back by bisection, 120 buckets of 120 at every age tested. It does not pad: six hours
returned 360 buckets in 360 bars with no gaps and a longest flat run of two, which the
`price_method: orderbook` field predicts — a book has a price whether or not anyone
trades. `docs/index-history.md` is the record.

**The network half and the pure half are separate on purpose.** `candles_to_bars` and
`plan_pages` take data and return data; `fetch_page` is the only function here that opens
a socket. `engine/tests/test_index_bars.py` tests the first two and never supplies a real
fetcher — no test in this repo may touch the network, and the split is what makes that
cost nothing.

    python tools/backfill_index_bars.py --days 30 --dry-run
    python tools/backfill_index_bars.py --days 30
    python tools/backfill_index_bars.py --symbol .DEXBTUSDT --days 7

Pacing
    Weight 3 per candles call against a 20,000 quota per five-minute window, so the
    default 0.4s sleep is far inside it. A 429 is honoured via `X-RATE-LIMIT-RESET`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UTC = dt.timezone.utc
BASE = "https://api.india.delta.exchange"
USER_AGENT = "convex-hedge-backfill/1.0 (+delta-exchange-payoff)"
TIMEOUT = 30

#: `docs/index-history.md` §2. The one `docs/settlement.md` reports on a settled product
#: record, and the only BTC index here priced from resting books rather than last trades.
DEFAULT_SYMBOL = ".DEXBTUSD"

#: Measured, not documented: one response carries 4,000 bars, which is 66.7 hours at 1m.
#: Delta's own docs say 2,000. `docs/index-history.md` §3.
PAGE_BARS = 4_000

RESOLUTION = "1m"
BUCKET = 60


@dataclass(frozen=True, slots=True)
class Page:
    """One request's window, as epoch seconds."""

    start: int
    end: int


def _minute(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, UTC)


def candles_to_bars(candles, *, underlying: str, symbol: str):
    """Delta's candle dicts to `IndexBar` rows. **No bucket is invented.**

    The founding lesson one layer earlier than the store: this is the same endpoint that
    returns 801 daily bars for `C-BTC-60000-270624` of which 797 are fabricated, and the
    only defence against inheriting that habit is to never produce a row the payload did
    not contain. A window with a hole in it yields rows either side of the hole and
    nothing across it, and the estimators' `_adjacent_pairs` then drops the return that
    would have spanned it.

    Rows are returned in ascending minute order regardless of the order they arrived in,
    because the store partitions on the minute and a caller reasoning about "the newest
    row I have" should not have to sort first.
    """
    from deltapayoff.bars import IndexBar

    rows = [
        IndexBar(
            underlying=underlying,
            minute=_minute(int(candle["time"])),
            symbol=symbol,
            index_open=_number(candle.get("open")),
            index_high=_number(candle.get("high")),
            index_low=_number(candle.get("low")),
            index_close=_number(candle.get("close")),
        )
        for candle in candles
    ]
    rows.sort(key=lambda row: row.minute)
    return rows


def _number(value) -> float | None:
    """A price as a number, or `None`. Never a string, and never `0` for absent.

    The engine's own boundary rule, applied here because this is a boundary: Delta
    spells some absent fields `"0"`, and a zero index price would be a 100% return into
    and out of the bar rather than a missing one.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def plan_pages(*, start: int, end: int, page_bars: int = PAGE_BARS) -> list[Page]:
    """The windows to ask for, newest first, each at most `page_bars` buckets wide.

    **Newest first, and it matters.** `docs/delta-api-scope.md` records the 4,000-bar cap
    as truncating the *oldest* rows of an over-wide window. Walking backwards means every
    window is asked for at a width the endpoint can answer whole, so truncation never
    silently removes the far end of a page and leaves a hole no later pass would look
    for. Verifying that truncation direction against the index specifically is listed in
    the ticket's *what to notice*; until it is verified, the walk is arranged so that the
    answer does not matter.

    Windows do not overlap. The store deduplicates on `(minute, symbol)` anyway, but a
    plan that overlapped would spend requests to produce rows that are then thrown away.
    """
    if end <= start:
        return []
    span = page_bars * BUCKET
    pages: list[Page] = []
    cursor = end
    while cursor > start:
        window_start = max(start, cursor - span)
        pages.append(Page(start=window_start, end=cursor))
        cursor = window_start
    return pages


def new_rows(rows, *, held: set) -> list:
    """The rows whose `(minute, symbol)` the store does not already hold.

    **Idempotence lives here, not in the writer.** `BarStore.flush` appends a file per
    call and has no notion of a key, so a job re-run over a range already stored would
    double every row in it and every estimator reading them would see a series with each
    minute twice. Filtering before the buffer is what makes re-running the job free.
    """
    return [row for row in rows if (row.minute, row.symbol) not in held]


def fetch_page(page: Page, *, symbol: str, base: str = BASE) -> list:
    """One request. **The only function in this file that opens a socket.**"""
    query = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "resolution": RESOLUTION,
            "start": page.start,
            "end": page.end,
        }
    )
    request = urllib.request.Request(
        f"{base}/v2/history/candles?{query}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            reset = exc.headers.get("X-RATE-LIMIT-RESET")
            wait = (int(reset) / 1_000_000) if reset and reset.isdigit() else 5.0
            print(f"    429; sleeping {wait:.1f}s", file=sys.stderr)
            time.sleep(wait)
            return fetch_page(page, symbol=symbol, base=base)
        raise
    return body.get("result") or []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--underlying", default="BTC")
    parser.add_argument(
        "--days", type=float, default=30.0, help="how far back to walk (default 30)"
    )
    parser.add_argument("--root", type=Path, help="store root (default <repo>/data)")
    parser.add_argument("--sleep", type=float, default=0.4)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan and fetch nothing",
    )
    args = parser.parse_args(argv)

    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "engine" / "src"))
    from deltapayoff.store import (  # noqa: PLC0415 - path set above
        INDEX_DATASET,
        INDEX_SCHEMA,
        BarStore,
    )

    now = int(time.time())
    start = now - int(args.days * 86_400)
    pages = plan_pages(start=start, end=now)

    print(f"backfill_index_bars - {args.symbol} - {args.days:g} days")
    print(f"  {len(pages)} pages of up to {PAGE_BARS} bars, newest first")
    if args.dry_run:
        for page in pages[:3]:
            first = f"{_minute(page.start):%Y-%m-%d %H:%M}Z"
            print(f"    {first} .. {_minute(page.end):%H:%M}Z")
        if len(pages) > 3:
            print(f"    ... and {len(pages) - 3} more")
        return 0

    store = BarStore(args.root, dataset=INDEX_DATASET, schema=INDEX_SCHEMA)
    held = _already_held(store, args.underlying)
    print(f"  store already holds {len(held)} minutes")

    written = 0
    for index, page in enumerate(pages, start=1):
        candles = fetch_page(page, symbol=args.symbol)
        rows = new_rows(
            candles_to_bars(candles, underlying=args.underlying, symbol=args.symbol),
            held=held,
        )
        held.update((row.minute, row.symbol) for row in rows)
        store.add(rows)
        written += len(rows)
        print(f"  page {index}/{len(pages)}: {len(candles)} candles, {len(rows)} new")
        store.flush()
        time.sleep(args.sleep)

    print(f"\n{written} rows written to {store.path}")
    return 0


def _already_held(store, underlying: str) -> set:
    """`(minute, symbol)` of every row the store holds for this underlying."""
    import polars as pl  # noqa: PLC0415 - the engine's path is set by `main`

    frame = store.scan()
    try:
        rows = (
            frame.filter(pl.col("underlying") == underlying)
            .select("minute", "symbol")
            .collect()
        )
    except Exception:  # noqa: BLE001 - an absent dataset is an empty one
        return set()
    return {(row["minute"], row["symbol"]) for row in rows.iter_rows(named=True)}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
