# HLD evidence

This document holds the measurements, assumptions, derived figures, run descriptions and
caveats that support [the high-level design](hld.md), plus its explicit out-of-scope decisions.
The default with `DELTA_BUS` unset remains the unchanged in-process FanOut monolith.

## 5. Numbers, and where each came from

| Number | Tag | Run |
|---|---|---|
| `ob_l2` refreshes every 508 ms per contract, `ticker` every 5,001 ms; both channels on BTC alone carry 1,322.9 msg/s at 636.5 KB/s | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| Both channels, every listed contract, **BTC alone**: 504 contracts, 1,095.1 msg/s, 547.3 KB/s. **BTC+ETH**: 782 contracts (BTC 504, ETH 278), 1,693.6 msg/s, 843.4 KB/s | `measured` | `tools/measure_feed.py --underlyings ... --seconds 60`, 60 s each, back to back, 2026-09-08 04:09–04:11 UTC (~09:39 IST) |
| BTC ladder unchanged within noise: push interval (nominal 1.0 s) median 1,011.7→1,015.0 ms, p95 1,027.7→1,063.1 ms with ETH also live; one-expiry solve pass (`enrich()`) median 0.966→0.983 ms | `measured` | live `/ws/chain` client (24 pushes each) + 200-run `time_it` on real data, 2026-09-08 |
| Staleness before `degraded` 15 s (three ticker refreshes); grace after the last viewer leaves an expiry 30 s | `assumed` | #33. The staleness half is now measured against a live hour and stands — longest quiet gap 44.785 s, `design/quiet-gap.md`. The grace half is still untested |
| Store gap 2026-09-04 09:38Z to 2026-09-07 09:45Z, unnoticed | `measured` | store file timestamps |

**The contested number is settled: never a subscription mismatch, only measured vs. not.** #33's
spec quoted `main.py`'s own comment, ~600 msg/s and ~300 KB/s for BTC alone — never actually run,
no probe output behind it anywhere in this repository's history. 1,322.9 msg/s at 636.5 KB/s
(2026-09-03) names the identical subscription, not a different one; #43's run above is a third
point on it, differing by the day's contract count and activity, exactly as §8 of
`docs/architecture.md` predicts. `main.py`'s comment now points here rather than repeat a number.

**One number stays absent on purpose:** #33 wants one expiry's solve cost measured against the
*running engine*, not a direct call — the solve-pass row above answers #43's narrower question
only, whether ETH changes it, and says nothing about event-loop contention under load.

## 6. Out of scope, and why

- **A message broker other than Redis.** Kafka and the rest stay out; #57 decided Redis Streams
  and #61 landed it behind the seam, off by default. What is still out of scope is *deploying* a
  process wall — the services split is I3 to I5, not [HLD §2](hld.md#2-the-boxes).
- **Order execution, positions, the sandbox** — the whiteboard's right half. The envelope is shaped
  so they can share it; nothing else here is designed for them.
- **An NSE adapter** and the instrument fields it needs — currency, lot size, tick size,
  settlement style, calendar — added with the adapter, against a real need.
- **The computed-bars rebuild tool**, specified as a batch job from quote bars; **backfilling the
  store's three-day hole**, not recoverable; **a web test runner**, where `typecheck` and `build`
  remain the gate; and **changing the four bar tables' schemas** beyond what the new routes read.

## 7. Adapter evidence moved from `lld/adapter.md`

All `measured` findings below come from the 2026-09-03 captures unless a later run is named.

- **The ticker frame carries no tick size.** `md.option_reference.tick_size` is `None` on
  all 136; REST carries one and the websocket does not. Absent rather than invented.
- **Every book frame carries `lts`, and every level is exactly `[price, size]`** — no empty
  book either side. Sizes and `lts` had never been decoded; the offsets went into
  `wire.decode_ob_l2_top`.
- **16 of 136 contracts have never traded** — `ohlc` all-null — so `last_price` is `None`,
  and 11 carry an open interest of exactly zero, so both halves of the null/zero rule are live.
- **Two ticker body fields reach no event**: `pb`, the price band, and `m24hc` — seen and
  skipped. **The decode is not the hot path**: `derived` 1.3% of a core.
- **ETH's `ob_l2` refreshes at BTC's own 502–508 ms** — `measured` 2026-09-08, no per-underlying
  branch needed. `ticker` reads 4,999 ms for BTC and 5,330 ms for ETH over a 60 s window, a gap a
  ~5 s period's noise at 12 samples cannot rule out; a longer run is what would tell the two apart.

| Number | Tag | Run |
|---|---|---|
| 136 book frames → 136 quotes; 136 ticker frames → 136 references + 136 index quotes | `measured` | `tests/fixtures/ws-*.json`, decoded 2026-09-07 |
| One distinct `sp` across all 136 ticker frames; `tick_size` absent on 136/136; `lts` present on 136/136 book frames | `measured` | same capture |
| 11 of 136 contracts at exactly zero open interest; 16 of 136 never traded | `measured` | same capture |
| Decode 30.6 µs per book frame, 43.6 µs per ticker frame | `measured` | 20 passes over the captures, this session |
| ≈1.3% of one core to decode the live BTC feed — 4.9% against the contested higher rate in `hld-evidence.md` §5. The wire decode inside it already ran in the socket reader before #36; only the event construction is new | `derived` | the two above against `docs/ingestion.md`'s 267.7 and 117.6 msg/s |
| Live: `/health` ok, `/ws/chain` a 21-row ladder after 5 `waiting` messages (spot 79634.8, forward 79642.12, 42 legs with our IV), 4 Parquet files at 412,100 bytes, 7,385 rows written | `measured` | engine run 2026-09-07T13:24:41Z, 410 s, BTC only |
| BTC alone 504 contracts, 1,095.1 msg/s, 547.3 KB/s; BTC+ETH 782 contracts, 1,693.6 msg/s, 843.4 KB/s, both channels, 60 s each | `measured` | `tools/measure_feed.py --underlyings`, 2026-09-08, full detail in `hld-evidence.md` §5 |
