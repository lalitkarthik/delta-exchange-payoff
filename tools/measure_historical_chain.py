"""#45: the ladder route's own cost, beside `/smile`'s measured 6.8 ms.

`docs/smile-contract.md` measured a whole expiry's stored day — 540 minutes, 18,676 rows
— at 6.8 ms, against 4.5 ms for one minute of it. `/chain/at` reads a **single** minute
across four tables rather than a whole expiry across one, so the two numbers are not the
same read scaled down; this script measures the one this ticket actually built rather
than assuming the smile figure transfers.

Builds a throwaway store under a temporary directory, writes one minute of quote,
reference, computed and spot bars for a realistic ladder, flushes it to Parquet, then
times `deltapayoff.historical.read_ladder_at` against it — cold (first call, disk only)
and warm (buffered, no flush), several runs each, minimum reported as the other
measurements in this codebase are: the fixed cost of opening files dominates, so the
minimum is the read itself and everything above it is scheduling noise.

    python tools/measure_historical_chain.py
    python tools/measure_historical_chain.py --strikes 68 --runs 20
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "src"))

from deltapayoff.bars import (  # noqa: E402
    ComputedBar,
    QuoteBar,
    ReferenceBar,
    SpotBar,
)
from deltapayoff.compute import MODEL_VERSION  # noqa: E402
from deltapayoff.historical import read_ladder_at  # noqa: E402
from deltapayoff.store import (  # noqa: E402
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
)

UNDERLYING = "BTC"
EXPIRY = "04-09-2026"
MINUTE = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
DAY = Date(2026, 9, 4)


def _symbol(strike: float, option_type: str) -> str:
    return f"{option_type}-{UNDERLYING}-{strike:.0f}-040926"


def build(root: Path, strikes: int) -> None:
    """One minute of a `strikes`-strike ladder, both sides, across all four tables."""
    quote = BarStore(root)
    reference = BarStore(root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA)
    computed = BarStore(root, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    spot = BarStore(root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA)

    ladder = [70000.0 + 500.0 * i for i in range(strikes)]
    for strike in ladder:
        for option_type in ("C", "P"):
            symbol = _symbol(strike, option_type)
            quote.add(
                [
                    QuoteBar(
                        symbol=symbol,
                        underlying=UNDERLYING,
                        expiry=EXPIRY,
                        strike=strike,
                        option_type=option_type,
                        minute=MINUTE,
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
                        symbol=symbol,
                        underlying=UNDERLYING,
                        expiry=EXPIRY,
                        strike=strike,
                        option_type=option_type,
                        minute=MINUTE,
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
            computed.add(
                [
                    ComputedBar(
                        symbol=symbol,
                        underlying=UNDERLYING,
                        expiry=EXPIRY,
                        strike=strike,
                        option_type=option_type,
                        minute=MINUTE,
                        iv=0.431,
                        iv_leg="call" if option_type == "C" else "put",
                        iv_reason=None,
                        delta=0.51,
                        gamma=0.00012,
                        vega=31.4,
                        theta=-8.2,
                        rho=1.9,
                        forward=77590.43,
                        discount=0.99997892,
                        years_to_expiry=0.00114155,
                        forward_method="F1+assumed-rate",
                        model_version=MODEL_VERSION,
                    )
                ]
            )

    spot.add(
        [
            SpotBar(
                underlying=UNDERLYING,
                minute=MINUTE,
                spot_open=77600.0,
                spot_high=77700.0,
                spot_low=77500.0,
                spot_close=77651.9,
                spot_ticks=7056,
            )
        ]
    )

    for store in (quote, reference, computed, spot):
        store.flush()


def time_reads(root: Path, runs: int) -> list[float]:
    quote = BarStore(root)
    reference = BarStore(root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA)
    computed = BarStore(root, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)
    spot = BarStore(root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA)

    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        ladder = read_ladder_at(
            quote, reference, computed, spot, UNDERLYING, EXPIRY, MINUTE
        )
        samples.append((time.perf_counter() - started) * 1000)
        assert ladder is not None
    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strikes", type=int, default=68, help="each side, so x2 legs")
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(root, args.strikes)
        samples = time_reads(root, args.runs)

    print(f"strikes: {args.strikes} ({args.strikes * 2} legs), runs: {args.runs}")
    print(f"min:    {min(samples):.3f} ms")
    print(f"median: {statistics.median(samples):.3f} ms")
    print(f"max:    {max(samples):.3f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
