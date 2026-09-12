# The store -- the numbers behind the design

Split out of [store.md](store.md) by #81, which pushed that file past the 200-line
bound. Nothing here is new; every row moved unchanged. Each number carries its tag and
the run that produced it, and a number without a run behind it does not belong here.

## Numbers

| Number | Tag | Run |
|---|---|---|
| Table A/B/D grace 8.0 s | `derived` | 1.45x the 5,511 ms ceiling from `tools/measure_arrival_lag.py`, 2026-09-04 |
| Table C grace 0.0 s | `derived` | a sample has no stragglers; see §1 |
| Arrival lag: book p50 212.6 ms, max 510.3 ms; reference median 3,176 ms, max 5,298.8 ms | `measured` | `tools/measure_arrival_lag.py`, 2026-09-04 |
| Flush every 5 minutes, 288 files per table per day before compaction | `derived` | #16, from the measured hourly file sizes |
| ~7,056 spot observations a minute against ~118 for one contract's book | `measured` | `tools/measure_feed.py`, 2026-09-03 |
| 2,460 quote-bar rows, all carrying `last_lts`, over 410 s | `measured` | #36's live run, 2026-09-07T13:24:41Z |
| One production-interval (300 s) flush, BTC alone: 7,535 rows, 8 files, 877,975 bytes across the four tables | `measured` | #37's live run |
| The same flush shape, BTC+ETH: scheduled flush 11,740 rows/8 files/699,591 bytes; plus the trailing open-minute flush, 14,088 rows/16 files/909,237 bytes total. File count doubles cleanly — one `underlying=` partition per table per flush, per underlying; rows and bytes do not, because the two runs cover different minutes and market activity, not only a different recorded set | `measured` | `tools/measure_store.py`'s `capture()`, generalised to two underlyings, scratch root, 2026-09-08 — full breakdown in `docs/storage.md` |
| Every one of the four tables holds an ETH partition with rows after two flush intervals | `measured` | same run |
