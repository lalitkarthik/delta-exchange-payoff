#!/usr/bin/env python3
"""Establish what `/v2/history/candles` actually serves for Delta's BTC **index**.

Realised volatility needs a price series reaching back as far as the chart's lookback,
and the whole IV-vs-RV design (#24) assumes Delta serves 1-minute index history. Nobody
had checked. `docs/delta-api-scope.md` line 13 is explicit that perpetuals, index and
funding series were removed from its scope, so the one series RV depends on is the one
series this repo has never probed.

**And it is the same endpoint that produced this project's founding lesson.**
`/v2/history/candles` pads every bucket containing no trade with the last trade, without
saying so: `C-BTC-60000-270624` returns 801 daily bars of which 797 are fabricated. An
index is computed continuously and may never have an empty bucket — or it may pad exactly
like an option and look perfectly smooth while doing it. A padded bar has zero return, and
zero returns **suppress** realised volatility, so padding would make RV read low
everywhere, the chart would show a fat premium, and the conclusion would be wrong in the
direction that looks most interesting.

Standard library only for the venue half, no API key, production only, read-only. The
`compare` section additionally reads our own Parquet store through Polars — that half is
offline and runs without a network.

    python tools/probe_index_history.py all
    python tools/probe_index_history.py symbol
    python tools/probe_index_history.py compare          # offline, needs data/

Sections
    symbol       enumerate spot indices, then try every plausible candle spelling
    resolutions  which resolutions the index series accepts
    depth        how far back 1m and 1d actually reach
    padding      whether the series repeats a price where nothing happened
    compare      the venue's minutes against our `spot-bars` for minutes both cover
    all          all of the above, in that order

Pacing
    --slow (default) sleeps 1.5s between requests. --fast sleeps 0.4s.
    Weight 3 per candles call against a 20000 quota per 5-minute window; a 429 is
    honoured via X-RATE-LIMIT-RESET.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.india.delta.exchange"
USER_AGENT = "convex-hedge-probe/1.0 (+delta-exchange-payoff)"
TIMEOUT = 30
UTC = dt.timezone.utc

#: Measured in `docs/delta-api-scope.md` section 4, from the 400 error body. Reused here
#: rather than re-derived: this probe asks which of them the *index* serves, which is a
#: different question from which ones exist.
VALID_RESOLUTIONS = (
    "5s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w",
)

#: `docs/settlement.md` line 82 reports `spot_index = .DEXBTUSD` on a settled option's
#: product record, so that spelling is the leading candidate rather than a guess. The
#: rest are the shapes this venue uses elsewhere, tried so that a failure can be
#: attributed to the symbol rather than to the endpoint.
CANDIDATE_SYMBOLS = (
    ".DEXBTUSD",
    ".DEXBTUSDT",
    "DEXBTUSD",
    "BTCUSD",
    "BTCUSDT",
    "MARK:BTCUSD",
    "INDEX:BTCUSD",
    "SPOT:BTCUSD",
    ".BTCUSD",
)

SLEEP = 1.5
_REQUESTS = 0


# ---------------------------------------------------------------- transport


def _sleep() -> None:
    time.sleep(SLEEP)


def request(path: str, *, retries: int = 3) -> tuple[int, object]:
    """GET `path`. Returns `(status, decoded body)`. Never raises on 4XX/5XX.

    Status `0` means the request never reached the venue. That is deliberately
    distinguishable from a 404: "the endpoint refused us" and "we could not ask" are
    different findings, and this probe exists to write one of them down.
    """
    global _REQUESTS
    url = BASE + path
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            _REQUESTS += 1
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code == 429:
                wait_ms = int(exc.headers.get("X-RATE-LIMIT-RESET", "60000"))
                print(f"    [429] X-RATE-LIMIT-RESET={wait_ms}ms - sleeping")
                time.sleep(wait_ms / 1000 + 1)
                continue
            try:
                return exc.code, json.loads(body)
            except ValueError:
                return exc.code, body[:200]
        except Exception as exc:
            if attempt == retries - 1:
                return 0, f"transport error: {type(exc).__name__}: {exc}"
            time.sleep(1.0)
    return 0, "exhausted retries"


def candles(symbol: str, resolution: str, start: int, end: int) -> tuple[int, object]:
    path = (
        "/v2/history/candles?resolution=" + urllib.parse.quote(resolution)
        + "&symbol=" + urllib.parse.quote(symbol)
        + f"&start={start}&end={end}"
    )
    status, body = request(path)
    if status == 200 and isinstance(body, dict):
        return status, body.get("result") or []
    return status, body


# ---------------------------------------------------------------- helpers


def now_ts() -> int:
    return int(time.time())


def ts(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def sub(title: str) -> None:
    print()
    print(f"  {title}")
    print("  " + "-" * len(title))


def unreachable(status: int, body: object) -> bool:
    """A transport failure, not an answer from the venue."""
    return status == 0


def note_unreachable(body: object) -> None:
    print()
    print("  !! The venue was not reached. This is NOT evidence about the endpoint.")
    print(f"  !! {body}")
    print("  !! Record it as unreachable, never as absent.")


# ---------------------------------------------------------------- sections


def section_symbol() -> list[str]:
    """Name the symbol that serves BTC index history, and record what each try did."""
    head("SYMBOL - what Delta calls its BTC index, and which spelling candles accepts")

    sub("enumerate spot indices from /v2/products")
    status, body = request("/v2/products?contract_types=spot_index&page_size=200")
    _sleep()
    listed: list[str] = []
    if status == 200 and isinstance(body, dict):
        for product in body.get("result") or []:
            symbol = product.get("symbol")
            if symbol and "BT" in symbol.upper():
                listed.append(symbol)
                print(f"    {symbol:16s} {product.get('description', '')}")
        if not listed:
            print("    (no BTC spot_index products returned)")
    elif unreachable(status, body):
        note_unreachable(body)
    else:
        print(f"    HTTP {status}: {body}")

    sub("try each candidate against /v2/history/candles at 1m over the last hour")
    now = now_ts()
    start = now - 3600
    working: list[str] = []
    tried = list(dict.fromkeys([*listed, *CANDIDATE_SYMBOLS]))
    print(f"    {'symbol':16s} {'status':>6s} {'bars':>6s}  note")
    for symbol in tried:
        status, result = candles(symbol, "1m", start, now)
        if status == 200 and isinstance(result, list) and result:
            working.append(symbol)
            print(f"    {symbol:16s} {status:6d} {len(result):6d}  serves data")
        elif status == 200:
            print(f"    {symbol:16s} {status:6d} {0:6d}  200 but empty")
        elif unreachable(status, result):
            print(f"    {symbol:16s} {'-':>6s} {'-':>6s}  UNREACHABLE: {result}")
        else:
            detail = result
            if isinstance(result, dict):
                detail = result.get("error") or result.get("message") or result
            print(f"    {symbol:16s} {status:6d} {'-':>6s}  {str(detail)[:60]}")
        _sleep()

    print()
    print(f"    serving symbols: {working or '(none)'}")
    return working


def section_resolutions(symbol: str) -> None:
    head(f"RESOLUTIONS - which of the twelve the index serves ({symbol})")
    now = now_ts()
    start = now - 40 * 86400
    print(f"    {'res':5s} {'status':>6s} {'bars':>7s}  oldest")
    for resolution in VALID_RESOLUTIONS:
        status, result = candles(symbol, resolution, start, now)
        if status == 200 and isinstance(result, list) and result:
            oldest = min(row["time"] for row in result)
            print(f"    {resolution:5s} {status:6d} {len(result):7d}  {ts(oldest)}")
        elif unreachable(status, result):
            print(f"    {resolution:5s} {'-':>6s} {'-':>7s}  UNREACHABLE")
        else:
            print(f"    {resolution:5s} {status:6d} {'-':>7s}  {str(result)[:50]}")
        _sleep()


def section_depth(symbol: str) -> None:
    """How far back the series truly reaches — asked by widening, not by asserting.

    The 4000-bar cap means a wide 1m window answers "the cap" rather than "the depth",
    so depth is read off the daily series and the 1m reach is stated as what one
    response covers.
    """
    head(f"DEPTH - how far back the index actually goes ({symbol})")
    now = now_ts()

    sub("daily, widest possible window")
    epoch_2014 = int(dt.datetime(2014, 1, 1, tzinfo=UTC).timestamp())
    status, result = candles(symbol, "1d", epoch_2014, now)
    if status == 200 and isinstance(result, list) and result:
        times = sorted(row["time"] for row in result)
        print(f"    bars={len(result)}  oldest={ts(times[0])}  newest={ts(times[-1])}")
        if len(result) >= 4000:
            print("    at or above the 4000 cap - this is the cap, not the depth")
    elif unreachable(status, result):
        note_unreachable(result)
    else:
        print(f"    HTTP {status}: {str(result)[:120]}")
    _sleep()

    sub("1m, one response - what a single page covers")
    status, result = candles(symbol, "1m", now - 10 * 86400, now)
    if status == 200 and isinstance(result, list) and result:
        times = sorted(row["time"] for row in result)
        span_h = (times[-1] - times[0]) / 3600
        print(f"    bars={len(result)}  span={span_h:.1f}h  oldest={ts(times[0])}")
        expected = int((times[-1] - times[0]) / 60) + 1
        print(f"    buckets in that span={expected}  missing={expected - len(result)}")
    elif unreachable(status, result):
        note_unreachable(result)
    else:
        print(f"    HTTP {status}: {str(result)[:120]}")
    _sleep()


def _runs_of_identical(values: list[float]) -> tuple[int, int]:
    """`(longest run, number of repeats)` of consecutive identical values.

    This is the padding tell. A continuously computed index should essentially never
    print the same price twice in a row at 1-minute resolution; a padded series prints
    long flat runs wherever nothing traded.
    """
    longest = 1
    current = 1
    repeats = 0
    for previous, value in itertools.pairwise(values):
        if value == previous:
            current += 1
            repeats += 1
            longest = max(longest, current)
        else:
            current = 1
    return longest, repeats


def section_padding(symbol: str) -> None:
    head(f"PADDING - does the index repeat a price where nothing happened ({symbol})")
    now = now_ts()
    status, result = candles(symbol, "1m", now - 6 * 3600, now)
    if unreachable(status, result):
        note_unreachable(result)
        return
    if status != 200 or not isinstance(result, list) or not result:
        print(f"    HTTP {status}: {str(result)[:120]}")
        return

    rows = sorted(result, key=lambda row: row["time"])
    closes = [float(row["close"]) for row in rows]
    longest, repeats = _runs_of_identical(closes)
    flat_bars = sum(1 for row in rows if len({
        float(row["open"]), float(row["high"]),
        float(row["low"]), float(row["close"]),
    }) == 1)
    volumes = [row.get("volume") for row in rows]
    volume_note = "null on every bar" if all(v is None for v in volumes) else "present"

    sub("six hours of 1m bars")
    print(f"    bars                 {len(rows)}")
    print(f"    distinct closes      {len(set(closes))}")
    print(f"    repeated closes      {repeats}  (longest flat run {longest} bars)")
    print(f"    OHLC all four equal  {flat_bars}")
    print(f"    volume field         {volume_note}")
    expected = int((rows[-1]["time"] - rows[0]["time"]) / 60) + 1
    print(f"    buckets in span      {expected}  returned {len(rows)}  "
          f"gaps {expected - len(rows)}")
    print()
    if flat_bars > len(rows) * 0.05 or longest > 3:
        print("    => LOOKS PADDED. Treat flat bars as fabricated; keep them out of RV.")
    elif expected == len(rows):
        print("    => No gaps and no flat runs: the series is dense and looks computed,")
        print("       not traded. Confirm against `compare` before trusting it.")
    else:
        print("    => Gaps present and no flat runs: absent buckets are omitted rather")
        print("       than padded, which is the behaviour RV wants.")


def _spot_bars(root: Path):
    """Our own `spot-bars`, or `None` with a reason printed."""
    try:
        import polars as pl
    except ImportError:
        print("    polars not importable - run under engine/.venv")
        return None
    path = root / "spot-bars"
    if not any(path.rglob("*.parquet")):
        print(f"    no spot-bars under {path}")
        return None
    return pl.scan_parquet(
        path / "**" / "*.parquet", hive_partitioning=True
    ).collect().sort("minute")


def section_compare(symbol: str, root: Path) -> None:
    """The venue's minutes against ours, for the minutes both cover.

    Our `spot_high`/`spot_low` come from roughly 7,056 ticker frames a minute; a candle's
    come from trades. If they differ materially the range estimators in R2 give different
    answers depending on which source they read, and that is worth knowing before it
    turns up as an unexplained line on a chart.
    """
    head(f"COMPARE - the venue's index candles against our spot-bars ({symbol})")
    ours = _spot_bars(root)
    if ours is None or ours.is_empty():
        print("    nothing stored to compare against")
        return

    first = ours["minute"][0]
    last = ours["minute"][-1]
    print(f"    our spot-bars: {len(ours)} minutes, {first} .. {last}")

    start = int(first.timestamp()) - 60
    end = int(last.timestamp()) + 60
    status, result = candles(symbol, "1m", start, end)
    if unreachable(status, result):
        note_unreachable(result)
        return
    if status != 200 or not isinstance(result, list) or not result:
        print(f"    HTTP {status}: {str(result)[:120]}")
        return

    theirs = {int(row["time"]): row for row in result}
    matched = []
    for row in ours.iter_rows(named=True):
        minute = int(row["minute"].timestamp())
        candle = theirs.get(minute)
        if candle is not None:
            matched.append((row, candle))

    sub(f"{len(matched)} minutes covered by both")
    if not matched:
        print("    no overlap - the venue's window and ours do not intersect")
        return

    def relative(ours_value: float, theirs_value: float) -> float:
        return abs(ours_value - theirs_value) / ours_value if ours_value else 0.0

    for field, mine, yours in (
        ("close", "spot_close", "close"),
        ("high", "spot_high", "high"),
        ("low", "spot_low", "low"),
        ("open", "spot_open", "open"),
    ):
        errors = [relative(row[mine], float(candle[yours])) for row, candle in matched]
        print(
            f"    {field:6s} median {statistics.median(errors):.3e}   "
            f"max {max(errors):.3e}"
        )

    sub("range width - ours from ~7k ticker frames, theirs from trades")
    our_range = [
        (row["spot_high"] - row["spot_low"]) / row["spot_close"] for row, _ in matched
    ]
    their_range = [
        (float(c["high"]) - float(c["low"])) / float(c["close"]) for _, c in matched
    ]
    print(f"    ours   median {statistics.median(our_range):.3e}")
    print(f"    theirs median {statistics.median(their_range):.3e}")
    wider = sum(1 for a, b in zip(our_range, their_range, strict=True) if a > b)
    print(f"    ours wider on {wider}/{len(matched)} minutes")
    print()
    print("    A materially wider range on our side is expected and is not an error:")
    print("    a range estimator reads whichever source it is given, so R2's Parkinson")
    print("    and Garman-Klass will disagree across sources by roughly this much.")


# ---------------------------------------------------------------- entry point


SECTIONS = ("symbol", "resolutions", "depth", "padding", "compare", "all")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sections", nargs="*", default=["all"], choices=SECTIONS)
    parser.add_argument("--symbol", help="skip discovery and probe this symbol")
    parser.add_argument("--root", type=Path, help="store root (default <repo>/data)")
    pace = parser.add_mutually_exclusive_group()
    pace.add_argument("--fast", action="store_true")
    pace.add_argument("--slow", action="store_true")
    parser.add_argument("--sleep", type=float)
    parser.add_argument(
        "--timeout", type=float,
        help="socket timeout in seconds (default 30). Lower it to characterise an "
             "unreachable venue without waiting three times thirty seconds per symbol.",
    )
    args = parser.parse_args(argv)

    global SLEEP, TIMEOUT
    if args.timeout is not None:
        TIMEOUT = args.timeout
    if args.sleep is not None:
        SLEEP = args.sleep
    elif args.fast:
        SLEEP = 0.4

    root = args.root or Path(__file__).resolve().parents[1] / "data"
    wanted = args.sections or ["all"]
    if "all" in wanted:
        wanted = ["symbol", "resolutions", "depth", "padding", "compare"]

    started = time.time()
    print(f"probe_index_history - {ts(int(started))} - base {BASE}")
    print(f"store root {root}")

    symbol = args.symbol
    if "symbol" in wanted or symbol is None:
        serving = section_symbol()
        symbol = symbol or (serving[0] if serving else None)

    if symbol is None:
        print()
        print("No symbol served index candles. Every section below needs one, so they")
        print("are skipped. If the failures above read UNREACHABLE this says nothing")
        print("about the endpoint - rerun from a machine that can reach the venue.")
        return 1

    print()
    print(f"probing with symbol {symbol!r}")
    for section in wanted:
        if section == "symbol":
            continue
        if section == "resolutions":
            section_resolutions(symbol)
        elif section == "depth":
            section_depth(symbol)
        elif section == "padding":
            section_padding(symbol)
        elif section == "compare":
            section_compare(symbol, root)

    print()
    print(f"{_REQUESTS} requests in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
