"""#46: the bars route's own cost, beside `/smile`'s measured 6.8 ms and `/chain/at`'s
measured 15.3 ms (`docs/design/lld/historical-read-path.md`).

Builds a throwaway store under a temporary directory, writes one contract's minute bars
across `quote-bars` and `reference-bars` for a realistic trading day, flushes to Parquet,
then times `deltapayoff.contract_bars.read_contract_bars` against it, several runs,
minimum reported as the other measurements in this codebase are: the fixed cost of
opening files dominates, so the minimum is the read itself and everything above it is
scheduling noise.

    python tools/measure_bars.py
    python tools/measure_bars.py --minutes 660 --runs 20
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.bars import QuoteBar, ReferenceBar  # noqa: E402
from deltapayoff.contract_bars import read_contract_bars  # noqa: E402
from deltapayoff.events.instrument import Instrument, Right  # noqa: E402
from deltapayoff.store import (  # noqa: E402
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    BarStore,
)

UNDERLYING = "BTC"
EXPIRY = "04-09-2026"
STRIKE = 77600.0
SYMBOL = f"C-{UNDERLYING}-{STRIKE:.0f}-040926"
START = datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc)
DAY = Date(2026, 9, 4)

INSTRUMENT = Instrument(
    venue="DELTA",
    underlying=UNDERLYING,
    expiry=Date(2026, 9, 4),
    strike=Decimal(str(STRIKE)),
    right=Right.CALL,
)


def build(root: Path, minutes: int) -> None:
    """`minutes` of one contract's day, both tables — a realistic full trading day is
    ~1,440; the default below is smaller so `--runs 20` stays fast, matching
    `measure_historical_chain.py`'s own default-vs-realistic split."""
    quote = BarStore(root)
    reference = BarStore(root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA)

    for i in range(minutes):
        minute = START + timedelta(minutes=i)
        quote.add(
            [
                QuoteBar(
                    symbol=SYMBOL,
                    underlying=UNDERLYING,
                    expiry=EXPIRY,
                    strike=STRIKE,
                    option_type="C",
                    minute=minute,
                    bid_open=100.0,
                    bid_high=105.0,
                    bid_low=95.0,
                    bid_close=101.0,
                    bid_ticks=40,
                    ask_open=102.0,
                    ask_high=107.0,
                    ask_low=97.0,
                    ask_close=103.0,
                    ask_ticks=40,
                    mid_open=101.0,
                    mid_high=106.0,
                    mid_low=96.0,
                    mid_close=102.0,
                    mid_ticks=40,
                    from_book=True,
                    last_lts=None,
                )
            ]
        )
        reference.add(
            [
                ReferenceBar(
                    symbol=SYMBOL,
                    underlying=UNDERLYING,
                    expiry=EXPIRY,
                    strike=STRIKE,
                    option_type="C",
                    minute=minute,
                    mark_open=102.0,
                    mark_high=107.0,
                    mark_low=97.0,
                    mark_close=102.5,
                    mark_ticks=12,
                    ltp_open=102.0,
                    ltp_high=102.0,
                    ltp_low=102.0,
                    ltp_close=102.0,
                    ltp_ticks=6,
                    oi_contracts=1200.0,
                    oi_change_usd_6h=-4200.0,
                    turnover=98000.0,
                    venue_delta=0.5,
                    venue_gamma=0.0001,
                    venue_rho=1.1,
                    venue_theta=-30.0,
                    venue_vega=25.0,
                    venue_bid_iv=0.42,
                    venue_ask_iv=0.44,
                    venue_mark_iv=0.43,
                )
            ]
        )

    for store in (quote, reference):
        store.flush()


def time_reads(root: Path, runs: int) -> list[float]:
    quote = BarStore(root)
    reference = BarStore(root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA)

    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        bars = read_contract_bars(quote, reference, INSTRUMENT, DAY)
        samples.append((time.perf_counter() - started) * 1000)
        assert bars, "the built fixture must read back non-empty"
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=660)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, args.minutes)
        samples = time_reads(root, args.runs)

    print(f"minutes: {args.minutes}, runs: {args.runs}")
    print(f"min:    {min(samples):.3f} ms")
    print(f"median: {statistics.median(samples):.3f} ms")
    print(f"max:    {max(samples):.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
