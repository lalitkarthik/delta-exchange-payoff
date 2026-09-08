# Low-level design: re-listing instruments

**What is built inside `main.relist_instruments`, `main.relist_forever` and
`DeltaFeed.subscribe`.** The connection those subscriptions ride on is
[reconnect.md](reconnect.md); what a subscription *means* to an adapter is
[adapter.md](adapter.md). Landed by #51.

## 1. What it is for

`start_feed_stack` listed instruments once, at start-up, and nothing ever asked the venue
again. The reconnect path replays the subscription registry — which is deliberately never
cleared — so it re-subscribed **the same set** for the life of the process. Every contract
Delta listed afterwards was never subscribed, never stored, and absent from every
historical screen.

The live path was unaffected, which is what made it survive: a fresh `/chain` request
lists all 24 strikes over REST and solves implied volatility on every one. **Only the
recorded history was damaged**, and that is the one thing this project cannot rebuild.

`measured` 2026-09-08 against the real store. An engine started 2026-09-07 recorded
strikes 77600 and 78000 of the 08-09-2026 expiry from 00:00:00Z (824 rows each), while
77200, 77400, **77800** and 78200 first appear at 06:32:00Z (20 rows each) — 06:32 being
when a second, freshly started engine listed instruments. 77800 sits *between* two strikes
recorded from midnight; a venue does not list two and skip the one between them for six
and a half hours. Downstream, 1,250 of 1,272 stored minutes hold 20 strikes and only 12
hold 24, which is what broke the volatility smile's left wing into disconnected dots.

## 2. The two halves, and why one alone fixes nothing

**Re-listing** is `relist_forever` → `relist_instruments`: ask
`adapter.instruments(underlying)`, subtract what `FeedStack.listed` already holds, and
`adapter.subscribe` the difference. Only the difference, because Delta answers a subscribe
with the current book — re-sending the whole registry would answer for every contract
already on the socket, several hundred snapshot frames to say nothing new.

**Reaching the open connection** is `DeltaFeed.subscribe`. Registering was never the same
as subscribing: the registry was sent to the venue exactly once per connection, inside
`_pump`, so a symbol added afterwards sat there unsubscribed until the next reconnect —
and a healthy feed does not reconnect. `DeltaFeed` now holds the socket it is pumping and
sends a subscribe frame for the newly registered symbols when one is open.

Either half alone is a fix that does nothing. `tests/test_relisting.py` fails on the
removal of either.

## 3. The cadence

`RELIST_INTERVAL_SECONDS = 60.0`, **`assumed`**.

One minute because the store's resolution is one minute: the cadence is exactly the
worst-case hole at the start of a newly listed contract's history, and 60 s makes that
hole one bar — the smallest gap this store can express. Five minutes would lose five bars
of every new strike to save four REST reads.

Not faster, either. `instruments()` is `/v2/tickers` with no expiry filter, the heaviest
read this engine makes — `measured` 2026-09-08, BTC 520 contracts in 644.8 KB and 735 ms,
ETH 278 in 342.2 KB and 737 ms — against a listing that changes a few times a day. At this
cadence that is `derived` 987 KB a minute beside the feed's own `measured` 843.4 KB/s,
about 2% more traffic. Below a minute it re-reads the same answer several times per bar it
could not have improved.

**It is not on any hot path.** The feed runs at `measured` 1,693.6 msg/s with BTC+ETH
(`tools/measure_feed.py`, 2026-09-08); this is a REST call on its own task, the slowest of
the five `start_feed_stack` runs, and the socket reader never waits on it.

**A second subscribe is additive, and that is measured rather than assumed.** If Delta
replaced a connection's subscription set instead of adding to it, sending the difference
would silently unsubscribe everything it did not name — #51 again, and worse.
`tools/probe_relist.py`, 2026-09-08: two contracts subscribed and delivering over a 15 s
window, two more subscribed on the same open socket, and over the next 15 s **all four**
delivered (33, 33, 32, 32 frames).

## 4. Failure modes

| Failure | What happens |
|---|---|
| The listing cannot be read at start-up | `start_feed_stack` propagates it and **nothing is started**. A feed that connected with an empty registry is the silent failure the socket owner exists to prevent, so an unanswerable venue is fatal here. |
| The listing cannot be read later | `feed.instruments` at warning, and the next cycle retries. The feed is delivering; ending it over a REST failure would turn a one-minute gap into an outage. |
| The live subscribe frame fails to send | A warning from `DeltaFeed._send_subscribe`. The symbols are already in the registry, so the next open replays them regardless. |
| The venue lists nothing for an underlying | No record and no subscribe — the same case a typo'd underlying produces, which `live_underlyings` already logs at error. |
| The re-list loop raises something unexpected | Caught and logged; the loop continues. A loop that died would leave the engine recording the set it started with and saying nothing, which is #51 again. |

**Nothing here can empty the registry.** `subscribe` is additive, the registry is never
cleared, and no path removes a symbol. That matters because an empty registry is
deliberately *not* announced as `OPENED` (#38, #39): a re-list that briefly emptied it
would put the connection badge through a false reconnect until it filled again.

## 5. Settled contracts — kept, and the threshold to revisit it

A contract that has expired drops out of the venue's listing but stays in `FeedStack.listed`
and in the socket registry, and is replayed on every reconnect for the life of the process.
**This is a decision, not an oversight.**

Dropping it would mean unsubscribing on a cadence, and the cadence is the problem: a
contract leaves the listing at settlement, while its last book updates are the most
valuable and least repeatable rows in the record. A re-list firing in that window would
take the subscription away mid-settlement to save a few hundred bytes of subscribe frame.
Removal would also mean a ninth member on an adapter protocol whose eight are the whole
test of the abstraction, and a removal path inside the one module whose entire purpose is
that the replay carries everything — where the failure mode is a short replay, silent by
construction.

The cost of keeping is a replay that grows by roughly one day's expired contracts per day
the process runs. It is made visible rather than merely assumed: every `feed.instruments`
record carries `subscribed`, the registry's current size, so the growth is in the log.

**The threshold.** The largest subscribe frame anyone has measured Delta accepting is
**520 symbols per channel** — the live re-list in §6, 2026-09-08 — over `probe_ws.py`'s
earlier 300. Both are floors: neither run found where the limit is, and a BTC+ETH engine
already replays 798 symbols per channel on every reconnect. **Revisit this decision when
either the ceiling is measured, or `subscribed` is seen materially above the live
listing's own size.** The right fix then is a prune inside `relist_instruments`, where the
current listing is already in hand, guarded so the registry can never be emptied.

## 6. Numbers

| Number | Tag | Run |
|---|---|---|
| `RELIST_INTERVAL_SECONDS` = 60 s | `assumed` | §3 |
| Feed throughput the re-list must not disturb, BTC+ETH: 1,693.6 msg/s | `measured` | `tools/measure_feed.py`, 2026-09-08 |
| Symbols accepted in one subscribe message, both channels: ≥ 300 | `measured` | `tools/probe_ws.py`, 2026-09-03 |
| The hole #51 left on 2026-09-07: 4 strikes of one expiry missing 06:32Z of 824 minutes | `measured` | the store, 2026-09-08 |
| One listing: BTC 520 contracts, 644.8 KB, 735 ms; ETH 278, 342.2 KB, 737 ms | `measured` | `tools/probe_relist.py`, 2026-09-08 |
| A second subscribe on a live socket is additive | `measured` | `tools/probe_relist.py`, 2026-09-08, two 15 s windows |
| Symbols accepted in one **live** subscribe frame, per channel: ≥ 520 | `measured` | the live re-list below, 2026-09-08 |
| A live re-list subscribing 518 contracts on an open socket: 482 ms, and 521 symbols delivering 4,240 frames in the next 20 s against 3 before | `measured` | live against `api.india.delta.exchange`, 2026-09-08 07:09Z |

**The existing hole is unrecoverable and is not backfilled.** Delta's own history pads
with the last trade and does not say so; this project's rule is that a minute with no
arrivals produces no row. Filling those minutes from the venue's padded history would put
invented rows in the one table this engine exists to be trusted about.

## 7. The seam the tests drive

`tests/test_relisting.py`, at seam 1 — the app under `TestClient` — with the real adapter,
the real socket owner and the real controller over a scripted connection. The connection
**delivers only what has been subscribed on it**, which is what makes the assertions able
to fail: the frames on the wire name a contract that appears only in the second listing,
so with either half of the fix removed the socket is never told about it and all four
tables stay empty. The socket owner's own half is `tests/test_feed.py`, section
"subscribing".
