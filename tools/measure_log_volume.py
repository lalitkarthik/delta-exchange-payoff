"""#42: how many records a quiet hour produces, and how many a reconnect does.

**Two different questions, measured two different ways.** A reconnect's record count is
deterministic — six lines for one drop-and-recover cycle, pinned by
`engine/tests/test_controller.py::
test_a_reconnect_only_logging_scenario_produces_a_bounded_number_of_records` — so it does
not need a live run. What genuinely needs one is the *quiet* rate: how much a healthy,
uneventful stretch costs, which this script answers by reading whatever the live engine
has already written to `logs/<today>.log`.

**Read-only.** This never starts or stops the engine. Point it at a window while
`uvicorn deltapayoff.main:app` is running live, or at any past window in the day's file.

Rule 7 applies: a short window cannot see the tail of a rare event like a reconnect, so
the hourly figure this reports is a **quiet-market rate**, extrapolated linearly from
whatever window is given — it is not a promise about a bad hour. Say the window measured,
every time; this script does it for you in the output.

    python tools/measure_log_volume.py --since 2026-09-08T05:20:00Z --minutes 5
    python tools/measure_log_volume.py --minutes 60   # the whole of today's file so far
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parents[1] / "logs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=datetime.now(UTC).date().isoformat(),
        help="the log file's date, YYYY-MM-DD (default: today, UTC)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO timestamp to start the window at (default: the file's first line)",
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=60.0,
        help="window length in minutes (default: 60)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = LOGS_DIR / f"{args.date}.log"
    if not path.exists():
        print(f"no log file at {path}", file=sys.stderr)
        raise SystemExit(1)

    records = []
    malformed = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1

    if not records:
        print("the file has no records", file=sys.stderr)
        raise SystemExit(1)

    timestamps = [datetime.fromisoformat(r["ts"]) for r in records]
    start = (
        datetime.fromisoformat(args.since) if args.since else min(timestamps)
    )
    end = start + timedelta(minutes=args.minutes)
    windowed = [
        r for r, t in zip(records, timestamps, strict=True) if start <= t < end
    ]
    actual_end = max((t for t in timestamps if start <= t < end), default=start)
    actual_seconds = max((actual_end - start).total_seconds(), 1e-9)

    by_event = Counter(r.get("event") for r in windowed)
    by_level = Counter(r.get("level") for r in windowed)
    total_bytes = sum(len(line.encode("utf-8")) for line in map(json.dumps, windowed))

    scale = 3600.0 / actual_seconds
    result = {
        "file": str(path),
        "malformed_lines": malformed,
        "window": {
            "requested_minutes": args.minutes,
            "start": start.isoformat(),
            "observed_seconds": round(actual_seconds, 1),
        },
        "measured_in_window": {
            "records": len(windowed),
            "bytes": total_bytes,
            "by_event": dict(by_event),
            "by_level": dict(by_level),
        },
        "derived_hourly_rate": {
            "records_per_hour": round(len(windowed) * scale),
            "bytes_per_hour": round(total_bytes * scale),
            "note": (
                "linear extrapolation from the observed window only; see rule 7 - "
                "a short window cannot see the tail of a rare event"
            ),
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
