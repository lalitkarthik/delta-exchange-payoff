#!/usr/bin/env python3
"""Backfill Delta's `.DEXBTUSD` index candles into the `index-bars` store (#54).

`tools/probe_index_history.py` established that `/v2/history/candles` serves the index
at 1-minute resolution without padding empty buckets — this tool is what actually reads
that history into a store `deltapayoff.store.read_index_bars` can serve realised
volatility from. It is deliberately **not** part of the live feed: `index-bars` has no
aggregator and nothing on the market-data bus produces it, so a bucket the venue never
returned stays absent rather than being forward-filled or interpolated (R4).

**Pages are requested newest-first**, each at most `PAGE_MINUTES` one-minute buckets wide,
so a run that is stopped partway through has already stored the most recent history —
the part a chart is most likely to need next. Writing is idempotent (R5): a `(minute,
symbol)` pair already on disk is never requested to be written again, so a rerun over a
fully-backfilled range fetches its pages, reports zero new rows and touches no file.

A page that does not finish with HTTP 200 aborts the whole run with a nonzero exit and
writes no rows from that page — earlier pages are already flushed and stay valid, so a
rerun resumes from where the failure happened rather than from scratch.

    python tools/backfill_index_bars.py --days 30
    python tools/backfill_index_bars.py --dry-run --days 90
    python tools/backfill_index_bars.py --symbol .DEXETHUSD --underlying ETH

Standard library only for the venue half; `request()` mirrors
`tools/probe_index_history.py`'s own three-attempt, 429-aware convention exactly, kept as
an independent copy rather than a shared import because that probe is out of this
ticket's scope. No API key, read-only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.bars import IndexBar  # noqa: E402
from deltapayoff.delta_client import USER_AGENT  # noqa: E402
from deltapayoff.store import (  # noqa: E402
    INDEX_DATASET,
    INDEX_SCHEMA,
    BarStore,
    default_root,
)

BASE = "https://api.india.delta.exchange"
CANDLES_PATH = "/v2/history/candles"
TIMEOUT = 30
UTC = timezone.utc

#: At most this many one-minute buckets per request. A 10,000-minute span therefore
#: plans three pages of 4,000, 4,000 and 2,000 buckets, newest-first.
PAGE_MINUTES = 4_000

#: `docs/delta-api-scope.md`'s weight for a candles call, and the quota it is weighed
#: against. Not enforced arithmetically here — `--sleep`'s default is paced against it,
#: the same way `tools/probe_index_history.py`'s `--slow`/`--fast` are, rather than this
#: tool counting down a budget it cannot see other callers spend from.
REQUEST_WEIGHT = 3
VENUE_BUDGET_PER_5_MINUTES = 20_000

DEFAULT_SYMBOL = ".DEXBTUSD"
DEFAULT_UNDERLYING = "BTC"
DEFAULT_DAYS = 30
DEFAULT_SLEEP = 0.4


# ---------------------------------------------------------------- transport


def request(path: str, *, retries: int = 3) -> tuple[int, object]:
    """GET `path`. Returns `(status, decoded body)`. Never raises on 4XX/5XX.

    Mirrors `tools/probe_index_history.py`'s `request()` exactly: at most three
    attempts; a 429 sleeps out `X-RATE-LIMIT-RESET` (default 60000ms when the header is
    missing **or unparsable**) and retries within the same budget; any other transport
    failure sleeps one second and retries; the third failure returns status `0` with a
    diagnostic. **Status `0` means the venue was never reached and must never be read as
    an empty result** — `run_backfill` checks it before looking at the body at all.
    """
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code == 429:
                try:
                    wait_ms = int(exc.headers.get("X-RATE-LIMIT-RESET", "60000"))
                except (TypeError, ValueError):
                    wait_ms = 60000
                time.sleep(wait_ms / 1000 + 1)
                continue
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, body[:200]
        except Exception as exc:  # noqa: BLE001 - any transport failure is status 0
            if attempt == retries - 1:
                return 0, f"transport error: {type(exc).__name__}: {exc}"
            time.sleep(1.0)
    return 0, "exhausted retries"


def _candles_path(symbol: str, start: datetime, end: datetime) -> str:
    return (
        f"{CANDLES_PATH}?resolution=1m"
        f"&symbol={urllib.parse.quote(symbol)}"
        f"&start={int(start.timestamp())}&end={int(end.timestamp())}"
    )


def fetch_page(symbol: str, start: datetime, end: datetime) -> tuple[int, object]:
    """One page's candles. `(200, list[dict])` on success — the list is `[]` for an
    empty `result`, never interpreted as anything else (a flat interval, an error) — or
    `(status, body)` for anything that is not a clean 200."""
    status, body = request(_candles_path(symbol, start, end))
    if status != 200:
        return status, body
    result = body.get("result") if isinstance(body, dict) else None
    return status, result if isinstance(result, list) else []


# ---------------------------------------------------------------- page planning


def plan_pages(
    start: datetime, end: datetime, *, page_minutes: int = PAGE_MINUTES
) -> list[tuple[datetime, datetime]]:
    """Newest-first, non-overlapping windows of at most `page_minutes` one-minute
    buckets, covering `[start, end]` exactly once.

    Newest-first so a run stopped partway through has already stored the history a chart
    is most likely to want next. `end <= start` — an empty or inverted span — plans
    nothing: there are no minutes to request.
    """
    if end <= start:
        return []
    width = timedelta(minutes=page_minutes)
    pages: list[tuple[datetime, datetime]] = []
    cursor = end
    while cursor > start:
        page_start = max(start, cursor - width)
        pages.append((page_start, cursor))
        cursor = page_start
    return pages


# ---------------------------------------------------------------- conversion


def _as_float(value: object) -> float | None:
    return None if value is None else float(value)


def bars_from_candles(
    candles: list[dict], *, symbol: str, underlying: str
) -> list[IndexBar]:
    """Only the candle objects actually present in `result`, R4: a bucket the venue did
    not return produces no `IndexBar` here and therefore no row — never forward-filled,
    never interpolated. Timestamps and OHLC values are carried through unchanged."""
    bars: list[IndexBar] = []
    for row in candles:
        try:
            minute = datetime.fromtimestamp(int(row["time"]), tz=UTC)
        except (KeyError, TypeError, ValueError):
            continue
        bars.append(
            IndexBar(
                underlying=underlying,
                minute=minute,
                symbol=symbol,
                index_open=_as_float(row.get("open")),
                index_high=_as_float(row.get("high")),
                index_low=_as_float(row.get("low")),
                index_close=_as_float(row.get("close")),
            )
        )
    return bars


def existing_keys(store: BarStore, underlying: str) -> set[tuple[datetime, str]]:
    """Every `(minute, symbol)` already stored for `underlying` — R5's idempotence key.
    A pair here is never written again; the same minute under a *different* symbol is
    not in this set and is written as new."""
    frame = (
        store.scan()
        .filter(pl.col("underlying") == underlying)
        .select("minute", "symbol")
        .collect()
    )
    return {(row["minute"], row["symbol"]) for row in frame.iter_rows(named=True)}


# ---------------------------------------------------------------- the run


def run_backfill(
    *,
    symbol: str,
    underlying: str,
    root: Path,
    pages: list[tuple[datetime, datetime]],
    sleep: float = DEFAULT_SLEEP,
) -> int:
    """Fetch and store every planned page. Returns the process exit status.

    A page that does not finish with HTTP 200 stops the run here, immediately, with a
    nonzero exit and writes no rows from that page. Every earlier page has already been
    added and flushed, so a rerun resumes from the failure rather than from scratch.
    """
    store = BarStore(root, dataset=INDEX_DATASET, schema=INDEX_SCHEMA)
    # `BarStore.flush()` names a file from its own flush ordinal plus the earliest
    # minute in the buffer, which is unique **within one process's lifetime** because
    # the live writer never restarts that counter. This tool is a fresh process on
    # every invocation, so two separate runs whose first pages happen to share an
    # earliest minute — plausible when the same day is backfilled for two symbols in
    # quick succession — would otherwise both produce ordinal 1 and collide on the same
    # file name, silently overwriting one run's row with the other's. Seeding the
    # counter from what is already on disk keeps every ordinal this run produces above
    # every ordinal any earlier run produced, without touching `BarStore` itself.
    # Seeded from what is on disk so a second run cannot reuse an ordinal and
    # overwrite the first run's file. It is a read-then-write count with no lock:
    # two backfills started at the same instant could still collide. Accepted -
    # this is a job a person runs, not a scheduled concurrent one.
    store.flushes = len(list(store.path.rglob("*.parquet")))
    held = existing_keys(store, underlying)

    total_new = 0
    for page_start, page_end in pages:
        status, candles = fetch_page(symbol, page_start, page_end)
        if status != 200:
            print(
                f"error: page {page_start.isoformat()} .. {page_end.isoformat()} "
                f"failed with HTTP {status}: {candles}",
                file=sys.stderr,
            )
            return 1

        bars = sorted(
            bars_from_candles(candles, symbol=symbol, underlying=underlying),
            key=lambda bar: bar.minute,
        )
        fresh = [bar for bar in bars if (bar.minute, bar.symbol) not in held]
        store.add(fresh)
        written = store.flush()
        held.update((bar.minute, bar.symbol) for bar in fresh)
        total_new += written
        print(
            f"page {page_start.isoformat()} .. {page_end.isoformat()}: "
            f"{len(candles)} candles, {len(fresh)} new, {written} written"
        )
        time.sleep(sleep)

    print(f"done: {total_new} new rows written across {len(pages)} pages")
    return 0


# ---------------------------------------------------------------- entry point


def _now() -> datetime:
    return datetime.now(tz=UTC)


def positive_days(value: str) -> int:
    """`argparse` type for `--days`: rejects anything that is not a positive integer
    with the standard `argparse` usage message and exit code 2."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"--days must be an integer, got {value!r}"
        ) from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"--days must be positive, got {value!r}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="venue index symbol")
    parser.add_argument(
        "--underlying", default=DEFAULT_UNDERLYING, help="partition underlying"
    )
    parser.add_argument(
        "--days", type=positive_days, default=DEFAULT_DAYS, help="how far back to reach"
    )
    parser.add_argument(
        "--root", type=Path, default=None, help="store root (default: <repo>/data)"
    )
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP, help="delay between pages, seconds"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="plan the pages and touch nothing"
    )
    return parser


def main(argv: list[str] | None = None, *, now: Callable[[], datetime] | None = None) -> int:
    clock = now or _now
    args = build_parser().parse_args(argv)

    root = args.root if args.root is not None else default_root()
    end = clock()
    start = end - timedelta(days=args.days)
    pages = plan_pages(start, end)

    if args.dry_run:
        print(f"symbol: {args.symbol}")
        print(f"underlying: {args.underlying}")
        print(f"root: {root}")
        print(f"requested range: {start.isoformat()} .. {end.isoformat()}")
        print("direction: newest-first")
        print(f"page count: {len(pages)}")
        print(f"page size: <= {PAGE_MINUTES} one-minute buckets")
        for index, (page_start, page_end) in enumerate(pages, start=1):
            buckets = int((page_end - page_start).total_seconds() // 60)
            print(
                f"  page {index}: {page_start.isoformat()} .. {page_end.isoformat()} "
                f"({buckets} buckets)"
            )
        return 0

    return run_backfill(
        symbol=args.symbol,
        underlying=args.underlying,
        root=root,
        pages=pages,
        sleep=args.sleep,
    )


if __name__ == "__main__":
    sys.exit(main())
