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
| Tables B/D bit-identical across two independent recorders: 10,564 and 19 common keys, 24/24 and 5/5 columns, `mark_ticks` included | `measured` | `tools/compare_store_runs.py`, 2026-09-12T05:52-06:12Z, BTC, #82 |
| Table A ticks conserve across two independent recorders: `abs(right-left) <= 1` on every one of 11,120 rows; window sum 1,333,097 both sides on all three tick columns; 1,215 rows favoured each side | `measured` | same run |
| Table A prices and `last_lts` differ between the two recorders even when ticks conserve -- socket jitter, not a defect; not gated by `tools/compare_store_runs.py` | `measured` | same run |
| Table C computed rows differ at 93.1% where the paired quote row is byte-identical and 93.9% where it is not -- the same rate | `measured` | same run |
| Table C `theta`: largest absolute gap 1.000 (ATM 0DTE straddle, -114.5 vs -115.5, 0.87% relative, from a 0.82% iv difference); the 21 rows with relative gap above 1 all have `\|theta\| <= 0.34` | `measured` | same run |
| Table A/B/D minimum window-end age before coverage is trustworthy: 308 s | `derived` | `FLUSH_SECONDS` (300 s, `deltapayoff.store`) + `QUOTE_GRACE_SECONDS` (8 s, `deltapayoff.bars`), #82 |
| I8 (#70): `boto3` and `fsspec`/`s3fs` are absent from `engine/requirements.txt` and `engine/requirements-dev.txt` | `measured` | `grep -c -e boto3 -e fsspec -e s3fs engine/requirements*.txt`, 2026-09-12: `0` in both files. This is what `StoreHomeUnavailable` names as missing; adding one is a decision for whoever holds AWS access, made loudly rather than silently |
| I8 (#70): 106 tests in `test_store.py` + `test_store_home.py` pass unchanged, and the host-mount write path (`_frame`, `_flush_generation`, `_flush_legacy`, `compact_partition`) has zero lines touched by this ticket's diff | `measured` | `pytest -q tests/test_store_home.py tests/test_store.py`, 2026-09-12 (106 passed); `git diff --stat` on `store.py` scoped to the functions named, same date |

## Why conservation, not equality

Table A and table C fold the same venue but by different mechanisms, and that difference
is why one tolerates two recorders disagreeing and the other does not.

Tables B and D fold a single broadcast channel with no arithmetic of their own, so a
value published once must read back identically everywhere: `tools/compare_store_runs.py`
holds them to byte-equality.

Table A folds the order book, and two recorders hold two separate sockets to it. A tick
that arrives one side of a minute boundary on one socket can arrive the other side on the
other, so the two recorders' bars for that minute differ in which ticks they hold --
sampling jitter, not loss. What does not depend on which socket a recorder held is
**conservation**: no tick the venue sent is created or destroyed, so the two recorders'
tick counts must agree to within the jitter each row can carry, and must sum to the same
total across a whole window regardless of which row the jitter moved a tick into.
`tools/compare_store_runs.py` checks exactly that -- per-row bound, window-total balance
-- rather than the value columns a jitter-affected tick actually produced.

Table C is not folded from the book at all. `ComputedBar` is a point sample of a 100 ms
recompute cache, taken independently by each process. Two independent samples of a cache
that itself changes every 100 ms disagree at a high, roughly constant rate whether or not
the underlying quote row the two recorders hold happens to agree -- there is no
conservation law for a point sample the way there is for a running count of ticks. Only
`theta`'s envelope has been measured; the rest of table C's Greeks are reported by the
tool for visibility but do not gate its exit code until their own envelope is measured
from a live run.
