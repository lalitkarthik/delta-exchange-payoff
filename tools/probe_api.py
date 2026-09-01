#!/usr/bin/env python3
"""Measure what Delta Exchange's public REST API actually returns.

Regenerates every number in ``docs/delta-api-scope.md``. Standard library only,
no API key, production only. Read-only: this script issues GET requests and
writes nothing to the exchange.

    python tools/probe_api.py all
    python tools/probe_api.py depth cap
    python tools/probe_api.py expired --fast

Sections
    depth     history depth per contract type and per resolution
    cap       rows per response, and paging past the cap
    expired   the zero-volume carry-forward, incl. expired and future-dated bars
    option    every field a live option ticker carries, with type and example
    expiries  live expiries and strike counts for BTC and ETH
    limits    rate-limit weights and window, auth matrix, header requirements
    all       all of the above, in that order

Pacing
    --slow (default) sleeps 1.5s between requests. --fast sleeps 0.4s.
    Override with --sleep SECONDS. A 429 is honoured via X-RATE-LIMIT-RESET.
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

BASE = "https://api.india.delta.exchange"
USER_AGENT = "convex-hedge-probe/1.0 (+delta-exchange-payoff)"
TIMEOUT = 30
UTC = dt.timezone.utc

# Delta's carry-forward window: the endpoint never looks back more than this
# many buckets from `end`. Measured, see the `cap` section.
MAX_ROWS = 4000

SLEEP = 1.5
_REQUESTS = 0


# ---------------------------------------------------------------- transport


def _sleep() -> None:
    time.sleep(SLEEP)


def request(path: str, *, retries: int = 4) -> tuple[int, object]:
    """GET `path`. Returns (status, decoded body). Never raises on 4XX/5XX."""
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
        except Exception as exc:  # DNS blips, resets, read timeouts
            if attempt == retries - 1:
                return 0, f"transport error: {exc}"
            time.sleep(2 * (attempt + 1))
    return 0, "exhausted retries"


def candles(symbol: str, resolution: str, start: int, end: int) -> list[dict]:
    path = (
        "/v2/history/candles?resolution=" + urllib.parse.quote(resolution)
        + "&symbol=" + urllib.parse.quote(symbol)
        + f"&start={int(start)}&end={int(end)}"
    )
    status, body = request(path)
    if status != 200 or not isinstance(body, dict):
        print(f"    !! {symbol} {resolution} -> HTTP {status}: {body}")
        return []
    return body.get("result") or []


# ---------------------------------------------------------------- helpers


def now_ts() -> int:
    return int(time.time())


def ts(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def day(seconds: int) -> str:
    return dt.datetime.fromtimestamp(seconds, UTC).date().isoformat()


def epoch(year: int, month: int = 1, dayn: int = 1, hour: int = 0) -> int:
    return int(dt.datetime(year, month, dayn, hour, tzinfo=UTC).timestamp())


EPOCH_2014 = epoch(2014)


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def sub(title: str) -> None:
    print()
    print("-- " + title)


def describe(rows: list[dict]) -> str:
    if not rows:
        return "n=0"
    newest, oldest = rows[0]["time"], rows[-1]["time"]
    zero = sum(1 for c in rows if not c.get("volume"))
    return (
        f"n={len(rows)}  oldest={ts(oldest)}  newest={ts(newest)}  "
        f"zero_volume={zero}/{len(rows)}"
    )


def expiry_of(symbol: str) -> dt.date:
    """`C-BTC-77000-040926` -> date(2026, 9, 4). Expiry lives only in the suffix."""
    suffix = symbol.rsplit("-", 1)[-1]
    return dt.date(2000 + int(suffix[4:6]), int(suffix[2:4]), int(suffix[0:2]))


# ---------------------------------------------------------------- sections


def section_depth() -> None:
    head("DEPTH - how far back the history goes")
    now = now_ts()

    sub("daily candles, widest possible window (start=2014-01-01)")
    print(f"    {'symbol':30s} {'n':>6s}  oldest       newest")
    for symbol in [
        "BTCUSD", "ETHUSD", "SOLUSD",
        "MARK:BTCUSD", "MARK:ETHUSD",
        ".DEXBTUSD", ".DEETHUSD",
        "OI:BTCUSD", "FUNDING:BTCUSD",
    ]:
        rows = candles(symbol, "1d", EPOCH_2014, now)
        if rows:
            print(f"    {symbol:30s} {len(rows):6d}  {day(rows[-1]['time'])}   "
                  f"{day(rows[0]['time'])}")
        else:
            print(f"    {symbol:30s} {'0':>6s}  (empty)")
        _sleep()

    sub("live option series - depth is listing date to expiry, nothing more")
    status, body = request("/v2/tickers?contract_types=call_options"
                           "&underlying_asset_symbols=BTC")
    _sleep()
    live = sorted(
        (t["symbol"] for t in (body.get("result") or [])),
        key=lambda s: (expiry_of(s), s),
    ) if isinstance(body, dict) else []
    sample = [live[0], live[len(live) // 2], live[-1]] if live else []
    for symbol in sample:
        rows = candles(symbol, "1d", EPOCH_2014, now)
        listed = day(rows[-1]["time"]) if rows else "-"
        print(f"    {symbol:24s} expiry={expiry_of(symbol)}  n={len(rows):4d}  "
              f"first_bar={listed}")
        _sleep()

    sub("supported resolutions (400-day window on BTCUSD)")
    start = now - 400 * 86400
    for resolution in ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
                       "12h", "1d", "3d", "1w", "7d", "1M", "30d"]:
        rows = candles(symbol="BTCUSD", resolution=resolution, start=start, end=now)
        if rows:
            print(f"    {resolution:4s} ok    n={len(rows):5d}  "
                  f"oldest={ts(rows[-1]['time'])}")
        else:
            print(f"    {resolution:4s} rejected or empty")
        _sleep()


def section_cap() -> None:
    head("CAP - rows per response, and paging past it")
    now = now_ts()

    sub("1m candles, widening the requested window")
    print(f"    {'window':>10s}  {'n':>6s}  oldest returned")
    for days in [1, 2, 3, 4, 5, 10, 40, 200]:
        rows = candles("BTCUSD", "1m", now - days * 86400, now)
        oldest = ts(rows[-1]["time"]) if rows else "-"
        print(f"    {days:7d}d   {len(rows):6d}  {oldest}")
        _sleep()

    sub("the same cap across resolutions (400-day window)")
    start = now - 400 * 86400
    for resolution in ["1m", "5m", "15m", "1h", "4h", "1d"]:
        rows = candles("BTCUSD", resolution, start, now)
        print(f"    {resolution:4s}  n={len(rows):6d}  oldest={ts(rows[-1]['time'])}"
              if rows else f"    {resolution:4s}  n=0")
        _sleep()

    sub("paging backwards: end = oldest_returned - one interval, 3 pages")
    end, seen, page = now, {}, 0
    while page < 3:
        rows = candles("BTCUSD", "1m", EPOCH_2014, end)
        if not rows:
            break
        page += 1
        for candle in rows:
            seen[candle["time"]] = candle
        print(f"    page {page}: n={len(rows):5d}  {ts(rows[-1]['time'])} .. "
              f"{ts(rows[0]['time'])}  cumulative={len(seen)}")
        end = rows[-1]["time"] - 60
        _sleep()
    times = sorted(seen)
    gaps = sum(1 for a, b in zip(times, times[1:]) if b - a != 60)
    if times:
        print(f"    reassembled {len(times)} unique minutes, {gaps} gaps, "
              f"{ts(times[0])} .. {ts(times[-1])}")


def section_expired() -> None:
    head("EXPIRED - the zero-volume carry-forward")
    now = now_ts()
    dead = ["C-BTC-60000-270624", "C-BTC-60000-310726"]

    sub("product metadata for the expired symbols")
    for symbol in dead:
        status, body = request(f"/v2/products/{symbol}")
        if status == 200 and isinstance(body, dict) and body.get("result"):
            product = body["result"]
            specs = product.get("product_specs") or {}
            strike = float(product["strike_price"])
            settle = float(specs.get("settlement_index_price") or 0)
            print(f"    {symbol}  state={product.get('state')}  "
                  f"settlement_time={product.get('settlement_time')}")
            print(f"        strike={strike:g}  settlement_index_price={settle}  "
                  f"intrinsic={settle - strike:.6f}")
        else:
            print(f"    {symbol}  HTTP {status}: {body}")
        _sleep()

    sub("daily series - first bars, last bars, and the constant in between")
    for symbol in dead:
        rows = candles(symbol, "1d", EPOCH_2014, now)
        print(f"    {symbol}: {describe(rows)}")
        for candle in list(reversed(rows))[:4]:
            print(f"        {day(candle['time'])}  o={candle['open']} h={candle['high']}"
                  f" l={candle['low']} c={candle['close']} v={candle['volume']}")
        print("        ...")
        for candle in list(reversed(rows))[-2:]:
            print(f"        {day(candle['time'])}  o={candle['open']} h={candle['high']}"
                  f" l={candle['low']} c={candle['close']} v={candle['volume']}")
        _sleep()

    sub("hourly across the settlement boundary (C-BTC-60000-310726, 2026-07-31T12:00Z)")
    rows = candles("C-BTC-60000-310726", "1h",
                   epoch(2026, 7, 31, 8), epoch(2026, 8, 1, 2))
    for candle in reversed(rows):
        print(f"        {ts(candle['time'])}  o={candle['open']} h={candle['high']}"
              f" l={candle['low']} c={candle['close']} v={candle['volume']}")
    _sleep()

    sub("an option that expired worthless still quotes its last trade forever")
    for symbol in ["C-BTC-70000-310726", "C-BTC-80600-010926"]:
        rows = candles(symbol, "1d", EPOCH_2014, now)
        if rows:
            print(f"    {symbol}  {describe(rows)}")
            print(f"        last bar: {day(rows[0]['time'])}  close={rows[0]['close']}"
                  f"  volume={rows[0]['volume']}")
        else:
            print(f"    {symbol}  n=0")
        _sleep()

    sub("the response needs a real trade inside [start, end] - nothing else")
    for label, start in [
        ("2014-01-01", EPOCH_2014),
        ("2024-06-01", epoch(2024, 6, 1)),
        ("2024-06-24", epoch(2024, 6, 24)),
        ("2024-06-25", epoch(2024, 6, 25)),
        ("2024-07-01", epoch(2024, 7, 1)),
        ("2025-01-01", epoch(2025, 1, 1)),
    ]:
        rows = candles("C-BTC-60000-270624", "1d", start, now)
        oldest = day(rows[-1]["time"]) if rows else "-"
        print(f"    start={label}  n={len(rows):4d}  oldest={oldest}")
        _sleep()

    sub("the same carry-forward fabricates FUTURE bars for a live symbol")
    future = epoch(2027, 3, 1)
    rows = candles("BTCUSD", "1d", now - 5 * 86400, future)
    print(f"    BTCUSD end=2027-03-01: {describe(rows)}")
    for candle in rows[:3]:
        print(f"        {day(candle['time'])}  o={candle['open']} h={candle['high']}"
              f" l={candle['low']} c={candle['close']} v={candle['volume']}")
    _sleep()
    rows = candles("BTCUSD", "1d", now + 10 * 86400, now + 20 * 86400)
    print(f"    BTCUSD window entirely in the future: n={len(rows)} "
          "(no trade in window -> empty)")
    _sleep()

    sub("carry-forward also pads a LIVE option's own lifetime")
    rows = candles("C-BTC-60000-270624", "1m", EPOCH_2014, epoch(2024, 6, 28))
    if rows:
        zero = sum(1 for c in rows if not c.get("volume"))
        print(f"    C-BTC-60000-270624 1m up to expiry+12h: n={len(rows)}  "
              f"zero-volume bars={zero} ({100 * zero / len(rows):.1f}%)")
    _sleep()

    sub("the MARK: series is corrupted the same way, and has no volume to filter on")
    for symbol in ["MARK:C-BTC-60000-270624", "MARK:C-BTC-60000-310726",
                   "OI:C-BTC-60000-310726"]:
        rows = candles(symbol, "1d", EPOCH_2014, now)
        if rows:
            closes = {c["close"] for c in rows}
            print(f"    {symbol:28s} n={len(rows):4d}  "
                  f"{day(rows[-1]['time'])} .. {day(rows[0]['time'])}  "
                  f"last_close={rows[0]['close']}  volume={rows[0]['volume']!r}")
            print(f"        distinct closes={len(closes)}  "
                  "-> volume is null, so no field distinguishes real from padded")
        else:
            print(f"    {symbol:28s} n=0")
        _sleep()

    sub("control: symbols that never traded return nothing")
    for symbol in ["C-BTC-60000-999999", "NOT-A-SYMBOL", "C-BTC-99999999-040926"]:
        rows = candles(symbol, "1d", EPOCH_2014, now)
        print(f"    {symbol:28s} n={len(rows)}")
        _sleep()


def section_option() -> None:
    head("OPTION - what a live option ticker carries")
    status, body = request("/v2/tickers?contract_types=call_options"
                           "&underlying_asset_symbols=BTC")
    _sleep()
    tickers = (body.get("result") or []) if isinstance(body, dict) else []
    if not tickers:
        print("    no live BTC call tickers returned")
        return
    sample = next((t for t in tickers if t.get("greeks") and t.get("quotes")), tickers[0])

    sub(f"field inventory, from {sample['symbol']}")
    print(f"    {'field':34s} {'type':8s} example")

    def emit(prefix: str, mapping: dict) -> None:
        for key in sorted(mapping):
            value = mapping[key]
            if isinstance(value, dict) and key in ("greeks", "quotes", "price_band"):
                emit(prefix + key + ".", value)
                continue
            kind = type(value).__name__
            shown = json.dumps(value)
            if len(shown) > 34:
                shown = shown[:31] + "..."
            print(f"    {prefix + key:34s} {kind:8s} {shown}")

    emit("", sample)
    print()
    print("    NOTE: there is no expiry field. Expiry exists only as the DDMMYY "
          "symbol suffix.")
    print(f"    'expiry_date' present in ticker keys: "
          f"{'expiry_date' in sample}")

    sub("spot disagreement across one snapshot")
    status, body = request("/v2/tickers?contract_types=call_options,put_options"
                           "&underlying_asset_symbols=BTC")
    _sleep()
    rows = (body.get("result") or []) if isinstance(body, dict) else []
    top = {t.get("spot_price") for t in rows if t.get("spot_price")}
    greek = {t["greeks"]["spot"] for t in rows if t.get("greeks")}
    print(f"    tickers={len(rows)}")
    print(f"    distinct spot_price   = {len(top)}  {sorted(top)}")
    print(f"    distinct greeks.spot  = {len(greek)}  "
          f"range {min(map(float, greek))} .. {max(map(float, greek))}"
          if greek else "    greeks.spot absent")
    if greek:
        print(f"    spread across greeks.spot = "
              f"{max(map(float, greek)) - min(map(float, greek)):.2f} USD")

    sub(f"product-level fields, from /v2/products/{sample['symbol']}")
    status, body = request(f"/v2/products/{sample['symbol']}")
    _sleep()
    if status == 200 and isinstance(body, dict) and body.get("result"):
        product = body["result"]
        for key in ["id", "symbol", "state", "contract_type", "strike_price",
                    "settlement_time", "launch_time", "tick_size",
                    "contract_value", "trading_status"]:
            value = product.get(key)
            print(f"    {key:20s} {type(value).__name__:8s} {json.dumps(value)}")


def section_expiries() -> None:
    head("EXPIRIES - what is live right now")
    today = dt.datetime.now(UTC).date()
    for underlying in ["BTC", "ETH"]:
        for contract in ["call_options", "put_options"]:
            status, body = request(
                f"/v2/tickers?contract_types={contract}"
                f"&underlying_asset_symbols={underlying}"
            )
            rows = (body.get("result") or []) if isinstance(body, dict) else []
            counts: dict[dt.date, int] = {}
            for ticker in rows:
                counts[expiry_of(ticker["symbol"])] = \
                    counts.get(expiry_of(ticker["symbol"]), 0) + 1
            sub(f"{underlying} {contract}: {len(rows)} tickers, "
                f"{len(counts)} expiries")
            for expiry in sorted(counts):
                print(f"        {expiry}  ({(expiry - today).days:+4d}d)  "
                      f"{counts[expiry]:4d} strikes")
            _sleep()


def section_limits() -> None:
    head("LIMITS - weights, window, auth, headers")

    sub("/v2/rate_limits/quota answers without an API key")

    def quota() -> tuple[int, int]:
        status, body = request("/v2/rate_limits/quota")
        if status != 200 or not isinstance(body, dict):
            return -1, -1
        return body.get("current_quota", -1), \
            body.get("remaining_time_in_milliseconds", -1)

    used, remaining = quota()
    time.sleep(0.4)
    used2, remaining2 = quota()
    self_weight = used2 - used
    print(f"    current_quota={used2}  remaining_time_in_milliseconds={remaining2}")
    print(f"    weight of /v2/rate_limits/quota itself = {self_weight}")
    print(f"    implied window length = {remaining2 / 1000:.0f}s remaining "
          "(documented: fixed 5-minute window)")

    sub("measured weight per endpoint (quota call's own weight subtracted)")
    previous = used2
    for path in [
        "/v2/tickers/BTCUSD",
        "/v2/tickers",
        "/v2/history/candles?resolution=1d&symbol=BTCUSD&start=1788000000&end=1788200000",
        "/v2/products?page_size=1",
        "/v2/l2orderbook/BTCUSD",
        "/v2/trades/BTCUSD",
        "/v2/assets",
        "/v2/indices",
        "/v2/settings",
        "/v2/history/sparklines?symbols=BTCUSD",
        "/v2/wallet/balances",
    ]:
        request(path)
        time.sleep(0.4)
        current, _ = quota()
        print(f"    {path[:64]:64s} weight={current - previous - self_weight}")
        previous = current
        time.sleep(0.4)

    sub("which endpoints answer with no API key")
    for path in [
        "/v2/products", "/v2/products/BTCUSD", "/v2/tickers", "/v2/tickers/BTCUSD",
        "/v2/l2orderbook/BTCUSD", "/v2/trades/BTCUSD",
        "/v2/history/candles?resolution=1d&symbol=BTCUSD&start=1788000000&end=1788200000",
        "/v2/history/sparklines?symbols=BTCUSD", "/v2/settings", "/v2/assets",
        "/v2/indices", "/v2/rate_limits/quota",
        "/v2/wallet/balances", "/v2/orders", "/v2/positions/margined",
        "/v2/fills", "/v2/profile",
    ]:
        status, body = request(path)
        note = "ok" if status == 200 else json.dumps(body)[:60]
        print(f"    {status}  {path[:64]:64s} {note}")
        time.sleep(0.4)

    sub("the User-Agent header is not optional")
    import http.client
    try:
        conn = http.client.HTTPSConnection("api.india.delta.exchange", timeout=20)
        conn.putrequest("GET", "/v2/tickers/BTCUSD", skip_accept_encoding=True)
        conn.endheaders()
        response = conn.getresponse()
        print(f"    request with no User-Agent -> HTTP {response.status} "
              f"({response.reason})")
        response.read()
        conn.close()
    except Exception as exc:
        print(f"    could not run the no-User-Agent test: {exc}")


SECTIONS = {
    "depth": section_depth,
    "cap": section_cap,
    "expired": section_expired,
    "option": section_option,
    "expiries": section_expiries,
    "limits": section_limits,
}


def main(argv: list[str]) -> int:
    global SLEEP
    parser = argparse.ArgumentParser(
        description="Measure Delta Exchange's public REST API.",
        epilog="sections: " + " ".join(SECTIONS) + " all",
    )
    parser.add_argument("sections", nargs="*", default=["all"],
                        help="one or more section names, or 'all'")
    parser.add_argument("--fast", action="store_true",
                        help="0.4s between requests instead of 1.5s")
    parser.add_argument("--sleep", type=float, default=None,
                        help="explicit seconds between requests")
    args = parser.parse_args(argv)

    SLEEP = args.sleep if args.sleep is not None else (0.4 if args.fast else 1.5)

    names = args.sections or ["all"]
    if "all" in names:
        names = list(SECTIONS)
    unknown = [n for n in names if n not in SECTIONS]
    if unknown:
        parser.error(f"unknown section(s): {', '.join(unknown)}")

    started = dt.datetime.now(UTC)
    print(f"probe_api.py  base={BASE}")
    print(f"started       {started.isoformat().replace('+00:00', 'Z')}")
    print(f"sections      {' '.join(names)}")
    print(f"pacing        {SLEEP}s between requests")

    for name in names:
        SECTIONS[name]()

    finished = dt.datetime.now(UTC)
    print()
    print("=" * 78)
    print(f"finished      {finished.isoformat().replace('+00:00', 'Z')}")
    print(f"requests      {_REQUESTS} in "
          f"{(finished - started).total_seconds():.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
