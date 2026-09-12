# The numbers behind the compute section

Evidence for [compute.md](compute.md), moved out of its §7 by #95 because that file sat at the
200-line bound. **Quote a number from this table, never from a sentence elsewhere** — the same
rule [message-bus.md](message-bus.md) §5 states for the bus. This is the third time a design
note here has been kept under the bound by moving its evidence to a sibling, after
`hld-evidence.md` (#62), `logging-catalogue.md` (#63) and `store-numbers.md` (#81).

---

## 1. The table

| Number | Tag | Source |
|---|---|---|
| Engine 30.89% of one core; 117.1 MiB resident, 178.6 MiB private | `measured` | 60.69 s `Get-Process` sample, 2026-09-09 13:18 UTC, **no viewer attached** |
| `web` 0.00% of a core, 203.1 MiB private | `measured` | same sample |
| 1,693.6 msg/s, 843.4 KB/s, BTC+ETH | `measured` | `tools/measure_feed.py`, 2026-09-08 (`../hld.md` §5) |
| Full solve pass 175.227 ms; pre-#44 loop ~64% of a core | `measured` / `derived` | `../lld/chain-cache.md` §9 |
| **Publisher inside `feed` at the chosen 50 ms: 70.73% of a core, against the bus-off control's 40.77% — the bus costs `derived` 29.96 points** | `measured` / `derived` | I2 (#61), `tools/measure_bus_live.py`, 2026-09-09, 774 live BTC+ETH contracts, 3,330,235 events; [../research/0061-batch-interval.md](../research/0061-batch-interval.md) |
| Redis publisher **alone, on loopback, at 100 ms batches**: 31.8 µs an entry, 5.88% of a core | `measured` | #69, `tools/measure_redis_hosting.py`, 2026-09-09, isolated. **This is Redis's own cost and not the feed's** |
| Redis working set 1,051.5 MiB at thirty minutes; `measured` 1,056.4 MiB on the run | `derived` / `measured` | #58 forecast, confirmed 0.5% high by I2's `INFO memory` `used_memory` 1,107,735,240 B |
| Inbound 2,216.5 GB/month; NAT gateway $165.00/month | `derived` | 843.4 KB/s × 730 h × $0.056/GB |
| `m7g.large` $48.94/mo; `m7g.2xlarge` $179.43/mo, ap-south-1 (0005's classes; 0008's are in compute.md §9) | `derived` | AWS Price List Bulk API, read 2026-09-09 |
| EKS control plane $73.00/month; ECS on EC2 $0 | `measured` | AWS Price List Bulk API; [ECS pricing](https://aws.amazon.com/ecs/pricing/) |
| Delta endpoints are CloudFront; POP `BOM78-P11`; edge↔origin ~167 ms | `measured` / `derived` | `tools/measure_venue_latency.py`, 2026-09-09 (**through a Cloudflare WARP tunnel**) |
| Latency from ap-south-1 and ap-northeast-1 | **unmeasured** | no AWS access; commands in `../research/0005a-venue-latency-run.md` §3 |
| Image sizes and per-container footprint | **unmeasured** | #65 has not landed; **re-cost compute.md when it does** |

**Nothing here is fixed against #65.** When it produces the images, re-run compute.md §2, §4 and
§9 against the `measured` image sizes and per-container CPU and memory, and note any change in
the decision records.

## 2. Two publisher figures, and which one sizes `feed`

**Both rows above are real measurements of the same publisher, and they differ by five times.**
They differ in the conditions they were taken under, not in their quality:

| | 5.88% of a core | 29.96 points of a core |
|---|---|---|
| Batch interval | **100 ms** | **50 ms — the interval I2 chose** |
| Process | the publisher **alone**, nothing else in it | the publisher **inside the feed**, doing the socket and the decode too |
| Path | loopback through Docker Desktop's WSL2 port forward | the same forward |
| Isolation | a synthetic writer at the measured entry shape | 3,330,235 live BTC+ETH events, against a bus-off control |
| Tag | `measured` (#69) | `derived` (#61): `measured` 70.73% − `measured` 40.77% |
| What it answers | *what does Redis cost to write to* | *what does the bus cost the feed* |

**Size `feed` from the second and read the first as Redis's own cost** — the rule
[message-bus.md](message-bus.md) §5 already states, and the one
[../decisions/0007-load-profile.md](../decisions/0007-load-profile.md) rests on: sizing from the
first is its rejected option B, which "under-counts by 3.6–5.7×. It is how 0005 reached 6 vCPU
at 10×".

**The tag was not the thing that went wrong.** `measured` is true of both figures and was no
help at all in choosing between them: what separated them was the batch interval, and a decision
(I2's 50 ms) had already made one of the two conditions obsolete. **A number's provenance tag
does not carry the conditions it was taken under, and it is the conditions that go stale.** The
convention lives in [nomenclature.md](nomenclature.md) and is not changed here; what #95 changes
is that every row above now names its interval, its process and its run beside the tag, so a
reader can tell which conditions a figure describes before quoting it. Whether the convention
itself should require that is a question for a ticket of its own.

## 3. Units: MiB, and why the arithmetic settles it

The Redis working set is **MiB**, everywhere, and the same figure is written `MB` in
[../decisions/0001-stream-naming-and-payload-format.md](../decisions/0001-stream-naming-and-payload-format.md)
and its research note. The arithmetic decides it and is not a matter of taste:

- The rate is `derived` **598.2 KiB/s = 612,556.8 B/s** (#58, encoding B; 0002b §3 writes the
  byte figure out). Over the 1,800 s retention window that is 1,102,602,240 B.
- **1,102,577,664 B ÷ 1,048,576 = 1,051.5 exactly.** ÷ 1,000,000 it is 1,102.6, which is not the
  figure anyone quotes. So the number that everyone carries is a binary one and `MiB` is its unit.
- Ten times the rate is therefore `derived` **10.269 GiB**, and **10.3 GiB is that rounded**.
  [../research/0061-batch-interval.md](../research/0061-batch-interval.md)'s 10.5 GiB was
  10,515 "M" divided by 1,000 — a decimal divide of a binary quantity — and is corrected there.

**This is what [../decisions/0002-redis-hosting.md](../decisions/0002-redis-hosting.md)'s
`cache.t4g.small` rejection rests on**, and it survives the check: 1,103,269,724 usable bytes
against 1,102,577,664 needed is **692,060 bytes, 0.0628%**. Read as decimal MB the same node
would have fitted by 4.9% and the rejection's reason would have had to change. It does not.
