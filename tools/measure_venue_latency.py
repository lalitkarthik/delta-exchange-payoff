"""R4: how far away is Delta, and what is between us and it?

Answers the region half of #68 with the only measurement this machine can take, and
names what it cannot take. Nothing here subscribes to anything: the websocket half opens
a connection, does the handshake, sends protocol-level pings and closes. It never sends
`subscribe`, so it cannot compete with the running engine for the venue's frames.

Run from `engine/`:

    .venv/Scripts/python.exe ../tools/measure_venue_latency.py

Useful flags:

    --label ap-south-1      what to call this vantage point in the findings table
    --samples 30            connects, requests and pings per figure (default 20)
    --offline               skip the AWS ip-ranges.json fetch

What it measures, in order:

  1. **DNS.** The CNAME chain and the addresses behind each endpoint. This is the
     finding, not the preamble: if the name ends at a CDN, every latency figure below
     is a distance to an edge and not to the venue.
  2. **Where those addresses belong**, against AWS's own published
     `https://ip-ranges.amazonaws.com/ip-ranges.json` — service and region, from the
     primary source rather than from a whois guess.
  3. **TCP connect** to port 443, `samples` times, one fresh socket each.
  4. **TLS handshake**, the same sockets, from `connect` returning to `do_handshake`.
  5. **One REST round trip**, `samples` times, reporting the CDN POP that served it and
     the origin's own `Request-In-Time`/`Request-Out-Time` header pair where present.
  6. **The websocket**: handshake time, then `samples` protocol pings, each timed from
     the frame going out to its pong coming back. No subscription.

**Read this before quoting any absolute number.** Every figure is the round trip *from
the machine that ran it*, including whatever tunnel, VPN or proxy that machine's routing
puts in the path — the tool prints what it can detect of that, and the run pasted into
`docs/design/research/0005a-venue-latency-run.md` was taken through an active Cloudflare
WARP tunnel. The comparison the ticket actually wants is this same tool run from an EC2
instance in two AWS regions; the exact commands for that are in that file's §3, and they
are the reason every flag above exists.

Every number printed is `measured` on the machine that ran it, that run.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import ssl
import statistics
import sys
import time
import urllib.request
from typing import Any

WS_HOST = "public-socket.india.delta.exchange"
WS_URL = f"wss://{WS_HOST}"
REST_HOST = "api.india.delta.exchange"
#: The cheapest public GET on Delta's REST API that still reaches the origin: one
#: product, no auth. Chosen so a `--samples 20` run costs the venue almost nothing.
REST_PATH = "/v2/products?page_size=1"

IP_RANGES_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"
CF_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

DEFAULT_SAMPLES = 20
PORT = 443


def pct(values: list[float], q: float) -> float:
    """The `q`th percentile by nearest rank, so a 20-sample p95 is a real sample."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(q * len(ordered) + 0.5))))
    return ordered[rank - 1]


def summarise(name: str, values_ms: list[float]) -> str:
    if not values_ms:
        return f"  {name:<26} no samples"
    return (
        f"  {name:<26} n={len(values_ms):<3} "
        f"min {min(values_ms):7.2f}  p50 {statistics.median(values_ms):7.2f}  "
        f"p95 {pct(values_ms, 0.95):7.2f}  max {max(values_ms):7.2f}   ms"
    )


# --------------------------------------------------------------------------- DNS


def resolve(host: str) -> dict[str, Any]:
    """The CNAME chain and every address, both families, from the system resolver."""
    out: dict[str, Any] = {"host": host, "chain": [], "addresses": []}
    try:
        canonical, aliases, ipv4 = socket.gethostbyname_ex(host)
        out["chain"] = [host, *aliases, canonical] if canonical != host else [host]
        out["chain"] = list(dict.fromkeys(out["chain"]))
        out["addresses"] += ipv4
    except OSError as exc:  # pragma: no cover - network shape
        out["error"] = repr(exc)
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(
            host, PORT, socket.AF_INET6, socket.SOCK_STREAM
        ):
            if family == socket.AF_INET6 and sockaddr[0] not in out["addresses"]:
                out["addresses"].append(sockaddr[0])
    except OSError:
        pass
    return out


def aws_ranges(offline: bool) -> dict[str, Any] | None:
    """AWS's published prefix list. Primary source for *where an address belongs*."""
    if offline:
        return None
    try:
        with urllib.request.urlopen(IP_RANGES_URL, timeout=60) as response:
            return json.load(response)
    except Exception as exc:  # pragma: no cover - network shape
        print(f"  (ip-ranges.json unavailable: {exc!r})")
        return None


def classify(address: str, ranges: dict[str, Any] | None) -> str:
    if ranges is None:
        return "unclassified"
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return "unparseable"
    key = "prefixes" if addr.version == 4 else "ipv6_prefixes"
    field = "ip_prefix" if addr.version == 4 else "ipv6_prefix"
    hits = [
        (p["service"], p["region"])
        for p in ranges.get(key, [])
        if addr in ipaddress.ip_network(p[field])
    ]
    services = sorted({service for service, _ in hits if service != "AMAZON"})
    regions = sorted({region for _, region in hits})
    if not hits:
        return "not an AWS address"
    return f"AWS {'/'.join(services) or 'AMAZON'} in {'/'.join(regions)}"


# ------------------------------------------------------------------- TCP and TLS


def connect_and_handshake(
    host: str, samples: int
) -> tuple[list[float], list[float], str]:
    """Fresh TCP connect and TLS handshake, `samples` times. Returns ms lists."""
    context = ssl.create_default_context()
    tcp_ms: list[float] = []
    tls_ms: list[float] = []
    peer = ""
    for _ in range(samples):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(15)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            start = time.perf_counter()
            raw.connect((host, PORT))
            connected = time.perf_counter()
            wrapped = context.wrap_socket(raw, server_hostname=host)
            done = time.perf_counter()
            peer = wrapped.getpeername()[0]
            tcp_ms.append((connected - start) * 1000)
            tls_ms.append((done - connected) * 1000)
            wrapped.close()
        except OSError as exc:  # pragma: no cover - network shape
            print(f"  connect failed: {exc!r}")
            raw.close()
        time.sleep(0.05)
    return tcp_ms, tls_ms, peer


# -------------------------------------------------------------------------- REST


def rest_round_trips(
    host: str, path: str, samples: int, bust_cache: bool = False
) -> tuple[list[float], dict[str, str]]:
    """One GET per sample on a fresh connection. Returns ms and the last headers.

    With `bust_cache`, each request carries a unique query parameter so the CDN cannot
    serve it from the edge. **The gap between the two modes is the only handle this
    machine has on where the origin is**: a hit stops at the edge, a miss goes all the
    way, and half the difference is the edge-to-origin leg.
    """
    context = ssl.create_default_context()
    times: list[float] = []
    headers: dict[str, str] = {}
    base = f"https://{host}{path}"
    for index in range(samples):
        url = base
        if bust_cache:
            separator = "&" if "?" in base else "?"
            url = f"{base}{separator}_cb={time.time_ns()}-{index}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "delta-payoff-measure-venue-latency/1",
                "Accept": "application/json",
            },
        )
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                response.read()
                times.append((time.perf_counter() - start) * 1000)
                headers = {k.lower(): v for k, v in response.headers.items()}
        except Exception as exc:  # pragma: no cover - network shape
            print(f"  REST failed: {exc!r}")
        time.sleep(0.05)
    return times, headers


# --------------------------------------------------------------------- websocket


async def websocket_pings(url: str, samples: int) -> tuple[float, list[float]]:
    """Handshake once, then time `samples` protocol pings. **No subscription is sent.**

    `websockets`' `ping()` returns a future that resolves when the matching pong
    arrives, which is the round trip the venue actually answers on the same socket the
    engine uses. Sending nothing else is what keeps this tool off the engine's toes.
    """
    import asyncio

    import websockets

    start = time.perf_counter()
    async with websockets.connect(url, open_timeout=20, ping_interval=None) as socket_:
        handshake_ms = (time.perf_counter() - start) * 1000
        pings: list[float] = []
        for _ in range(samples):
            sent = time.perf_counter()
            pong = await socket_.ping()
            await pong
            pings.append((time.perf_counter() - sent) * 1000)
            await asyncio.sleep(0.05)
        return handshake_ms, pings


def vantage_point() -> None:
    """What is between this machine and the internet, as far as it can tell."""
    try:
        with urllib.request.urlopen(CF_TRACE_URL, timeout=15) as response:
            trace = dict(
                line.split("=", 1)
                for line in response.read().decode().splitlines()
                if "=" in line
            )
        print(
            f"  egress seen by Cloudflare : ip={trace.get('ip')} "
            f"colo={trace.get('colo')} loc={trace.get('loc')} "
            f"warp={trace.get('warp')} gateway={trace.get('gateway')}"
        )
        if trace.get("warp") == "on":
            print("  *** WARP IS ON. Every figure includes a Cloudflare tunnel hop. ***")
    except Exception as exc:  # pragma: no cover - network shape
        print(f"  (vantage point unknown: {exc!r})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", default="this machine", help="name for the vantage point"
    )
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--ws-host", default=WS_HOST)
    parser.add_argument("--rest-host", default=REST_HOST)
    parser.add_argument("--rest-path", default=REST_PATH)
    parser.add_argument("--offline", action="store_true", help="skip the ip-ranges fetch")
    args = parser.parse_args()

    print(f"# venue latency, vantage point: {args.label}")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"# {stamp}, {args.samples} samples per figure")
    print()

    print("## 0. Vantage point")
    vantage_point()
    print()

    ranges = aws_ranges(args.offline)
    if ranges is not None:
        stamped = ranges.get("createDate")
        print(f"## 1. DNS and ownership   (ip-ranges.json createDate {stamped})")
    else:
        print("## 1. DNS and ownership")
    for host in (args.ws_host, args.rest_host):
        info = resolve(host)
        print(f"  {host}")
        print(f"    CNAME chain : {' -> '.join(info['chain'])}")
        for address in info["addresses"]:
            print(f"    {address:<42} {classify(address, ranges)}")
    print()

    print("## 2. TCP connect and TLS handshake")
    for host in (args.ws_host, args.rest_host):
        tcp_ms, tls_ms, peer = connect_and_handshake(host, args.samples)
        print(f"  {host}  (peer {peer})")
        print(summarise("TCP connect", tcp_ms))
        print(summarise("TLS handshake", tls_ms))
    print()

    print("## 3. REST round trip, edge cache hit and forced miss")
    hits, hit_headers = rest_round_trips(args.rest_host, args.rest_path, args.samples)
    misses, miss_headers = rest_round_trips(
        args.rest_host, args.rest_path, args.samples, bust_cache=True
    )
    print(summarise("GET, cacheable", hits))
    print(summarise("GET, cache-busted", misses))
    for label, headers in (("cacheable", hit_headers), ("cache-busted", miss_headers)):
        served = headers.get("x-cache", "?")
        pop = headers.get("x-amz-cf-pop", "?")
        origin = headers.get("server", "?")
        print(f"  {label:<26} {served}, POP {pop}, origin {origin}")
        if "request-in-time" in headers and "request-out-time" in headers:
            try:
                out_us = int(headers["request-out-time"])
                in_us = int(headers["request-in-time"])
                spent = (out_us - in_us) / 1000
                print(f"  {'':<26} origin self-reported {spent:.2f} ms inside itself")
            except ValueError:
                pass
    if hits and misses:
        gap = statistics.median(misses) - statistics.median(hits)
        print(
            f"  {'miss minus hit':<26} {gap:7.2f} ms   "
            f"-> edge<->origin ~{gap / 2:.1f} ms one way"
        )
    print()

    print("## 4. Websocket handshake and protocol ping  (no subscription sent)")
    try:
        import asyncio

        handshake_ms, pings = asyncio.run(
            websocket_pings(f"wss://{args.ws_host}", args.samples)
        )
        print(f"  {'handshake (connect+TLS+HTTP upgrade)':<26} {handshake_ms:.2f} ms")
        print(summarise("ping -> pong", pings))
    except Exception as exc:
        print(f"  websocket failed: {exc!r}")
    print()

    print("# Every figure above is `measured` on this machine, this run. A figure from a")
    print("# different vantage point is a different number; run this there with --label.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
