# The numbers behind the message bus

Evidence for [message-bus.md](message-bus.md), moved out of its §5 by #72 so that file stays
under the 200-line bound. **Quote a number from this table, never from a sentence elsewhere.**
It is the move this repository already made in #62 (`hld-evidence.md`), #63
(`logging-catalogue.md`), #81 (`store-numbers.md`) and #95 (`compute-numbers.md` and
`0001-units-reconciliation.md`): the design stays, the evidence moves to a sibling, and a pointer
stands where the table was.

---

## 1. The table

| Number | Tag | Run behind it |
|---|---|---|
| Retention thirty minutes; `maxmemory 2gb` | `assumed` | chosen in [0002](../decisions/0002-redis-hosting.md); see the judged tags in #72 |
| 1,849.8 events/s on the bus | `derived` | [../research/0001-stream-naming-and-payload-format.md](../research/0001-stream-naming-and-payload-format.md) §5: 1,537.4 book + 156.2 ticker × 2, from `measured` 1,693.6 venue frames/s and the channels' 508 ms and 5,001 ms refresh intervals |
| 1,835–1,854 events/s, 3,330,235 events | `measured` | `tools/measure_bus_live.py`, 2026-09-09, 774 live BTC+ETH contracts |
| 1,056.4 MiB after thirty continuous minutes | `measured` | same run, `INFO memory` `used_memory` 1,107,735,240 B |
| 1,051.5 MiB at thirty minutes, forecast | `derived` | #58, before any of it was built; the run came in 0.5% above |
| Batch interval **50 ms** | `assumed` | I2 (#61) chose it against 10, 50 and 100 ms on the live feed: 10 ms is not honoured, 100 ms costs 45 ms of period |
| Achieved period 96.4 ms at that 50 ms interval | `measured` | `tools/measure_bus_live.py`, 2026-09-09, same run |
| Publish to consumer receipt, p50 195.2 ms, p99 847.8 ms | `measured` | same run, 50 ms phase, ~55,000 samples |
| Publisher cost 29.96 points of a core at 50 ms | `derived` | same run, `measured` 70.73% against the bus-off control's `measured` 40.77% |
| `XADD` pipelined at 100 ms: 31.8 µs an entry, 5.88% of a core | `measured` | `tools/measure_redis_hosting.py`, 2026-09-09, loopback, isolated |
| The outbox bound (`max_outbox`), 200,000 entries | `assumed` | `redis_bus.py` |
| About 108 s of traffic at that bound | `derived` | 200,000 ÷ 1,849.8 events/s |
| Dropped, skipped, undecodable, failed batches: 0 | `measured` | `tools/measure_bus_live.py`, whole run |
| Latency to a managed endpoint | **unmeasured** | no AWS account; [services.md](services.md) §5 |

**Every loopback figure passes through Docker Desktop's WSL2 port forward.** It is an upper bound
for a co-located container and a floor for anything with a network hop.

## 2. Two publisher figures, and which one sizes `feed`

**Read 5.88% of a core and 29.96 points of a core as two conditions, not as a disagreement.**
Both describe the same publisher and they differ five-fold.

| | 5.88% of a core | 29.96 points of a core |
|---|---|---|
| Batch interval | **100 ms** | **50 ms — the interval I2 (#61) chose** |
| What is running | the publisher alone, on loopback, isolated | the publisher inside the feed process |
| Tag | `measured` | `derived`, from two `measured` samples |
| Run | #69, `tools/measure_redis_hosting.py`, 2026-09-09 | I2, `tools/measure_bus_live.py`, 2026-09-09 |

**Size `feed` from 29.96 points** ([0007](../decisions/0007-load-profile.md),
[compute.md](compute.md) §4), and read 5.88% as Redis's own cost at a batch interval this system
does not run at. `compute-numbers.md` §2 states the same pair for the compute document, and
`engine/tests/test_cloud_numbers.py` pins it.

**#95 corrected this table and left the prose behind it.** Until #72 the sentence here said both
figures were `measured`, which stopped being true when the 29.96 row became `derived`. A tag says
where a number came from; it never says whether the number still applies.
