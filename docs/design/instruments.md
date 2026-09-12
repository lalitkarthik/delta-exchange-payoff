# The instruments, and who checks them

**Nothing checks the instruments, so every tool under `tools/` is read once against the
question "does its denominator come from the data or from the request?", and the answer is
written down here beside the tool rather than left in a reviewer's head.**

That sentence exists because of one day. On 2026-09-12 three instrument defects were found:
[#100](https://github.com/lalitkarthik/delta-exchange-payoff/issues/100), the container
collector sampling at 16.4 s when told 5 s and dividing by the 5;
[#102](https://github.com/lalitkarthik/delta-exchange-payoff/issues/102),
`measure_bus_live.py` replaying thirty minutes of retention into its own arrival-lag
numbers; and [#104](https://github.com/lalitkarthik/delta-exchange-payoff/issues/104), the
gap probe gridding from the first to the last minute it saw, so a store that stopped
shortened the grid instead of putting holes in it. **Each was found by accident while doing
something else, and each one made the system look better than it was.** Three for three in
one direction is not a coincidence: a defect that makes a measurement worse gets
investigated, and a defect that makes it better gets quoted.

`tools/_window.py` is the prior art and the pattern. It exists because the measurement
tools kept reporting an hour they had not observed; it reports `requested_seconds` beside a
`measured` `elapsed_seconds`, sets `complete` false when the window ended early, and names
the ending. Five tools use it. This note is the same discipline applied to the other
twenty-six.

## The rule

**A denominator from the request can only be wrong loudly. A denominator from the data can
be wrong silently**, because the same event that removes observations from the numerator
removes them from the denominator and the ratio improves. Neither is right in every case —
a loss rate *should* divide by the minutes that had a quote — so the rule is not "always
divide by the request". It is:

1. Say which one it is, in the output, beside the figure.
2. Where the denominator comes from the data, report **coverage against the request** next
   to it, so a shrinking denominator cannot read as an improving system.
3. Where the denominator comes from the request, report what was **actually** observed
   against it, so an unreachable request cannot read as a completed one.

## The sweep

Every `.py` under `tools/`, `measured` 2026-09-12 at branch `dxp-104`, by reading each file.
**request** means an argument the operator gave or a constant expectation; **data** means
something the run observed; **elapsed** means a wall clock that was measured rather than
requested, which is the honest form of a duration denominator.

| Tool | The figure it divides | Denominator from | Changing? |
|---|---|---|---|
| `_window.py` | none — it is the module that keeps the two apart | request and elapsed, side by side | no, it is the pattern |
| `backfill_index_bars.py` | none; counts only | n/a | no |
| `capture_ws.py` | frames captured per symbol | data — the REST listing it just read | no |
| `compact_store.py` | per cent smaller | data — bytes before, this run | no |
| `compare_store_runs.py` | relative difference per field | data — `max(abs(left), abs(right))` | no; coverage is a raw count, never a fraction |
| `loadgen.py` | none; raised against reaped | data on both sides, re-read from the process table | no — #99 already made this the rule |
| `measure_arrival_lag.py` | percentiles, mean lag | data — the frames observed | no; uses `_window` and prints elapsed beside requested |
| `measure_bars.py` | none; durations only | request — `--runs`, printed | no |
| `measure_bus.py` | events per second | elapsed | no |
| `measure_bus_live.py` | events/s, CPU %, per-entry cost | elapsed, and data for per-entry | no — #102 fixed its other defect |
| `measure_computed_gaps.py` | quote bars with no computed bar | data — and **that is the defect #104 fixed** | **yes, this ticket** |
| `measure_containers.py` | per-second rates; "% moved" | elapsed for rates; **request** for "% moved" | no — #100 fixed the rates and the "% moved" reference is named in the output |
| `measure_feed.py` | msg/s, KB/s, per-symbol ms | elapsed | no; uses `_window` |
| `measure_greeks.py` | relative error per Greek | data — the reference value | no; the excluded count is always reported |
| `measure_historical_chain.py` | none; durations only | request — `--runs`, printed | no |
| `measure_log_volume.py` | records and bytes per hour | data — the observed span, and it prints `requested_minutes` beside it | no |
| `measure_open_partition.py` | CPU per call; the S3 bill | **request** — `--runs`, price constants, an `assumed` `--reads-per-day` | no; every one is labelled `derived` or `assumed` in the output |
| `measure_payload_size.py` | bytes per entry; 30-minute memory | data for bytes; **request** for the projection | no; the frame-rate constants carry their own runs |
| `measure_quiet_gap.py` | gap percentiles | data — the gaps observed | no; uses `_window` and writes `elapsed` into the filename |
| `measure_redis_hosting.py` | cost per entry in a batch | **request** — a constant events/s and the chosen batch interval | no; the batch size is the experiment, not an observation |
| `measure_solve.py` | CPU fraction, msg/s | elapsed | no; uses `_window` |
| `measure_store.py` | bytes/s, bytes/day, % of ceiling | elapsed; **request** for the ceiling | no — it already prints the ceiling twice, once against the constant and once against the contract count seen |
| `measure_store_cloud.py` | objects under the cliff; write requests | data; **request** for `--flushes` | no; the header says `derived: N flushes x ...` |
| `measure_venue_latency.py` | latency percentiles | data — the samples, with `n=` printed | no |
| `migrate_store.py` | none; counts only | n/a | no |
| `observe_reconnect.py` | none; counts and offsets | n/a | no |
| `probe_api.py` | zero-volume bars, gaps | data | no |
| `probe_index_history.py` | missing buckets, relative error | data — its own first and last timestamp | no |
| `probe_relist.py` | symbols still arriving | **request** — the two sets it chose to subscribe | no; that is the experiment, and it refuses to conclude when it cannot tell |
| `probe_ws.py` | msg/s, KB/s, per-symbol ms | elapsed | no; `requested` is printed as a column beside `acked` |
| `smoke_stack.py` | none; start-to-healthy only | n/a | no |

## What the sweep found

**One tool needed changing and it is the one this ticket was about.** The other thirty are
already honest about where their denominator comes from, and the reason is visible in the
git history: `_window.py`, #99's reaped-versus-raised rule, #100's cadence fix and
`measure_store.py`'s twice-printed ceiling are four separate people arriving at the same
discipline after being bitten. **The discipline exists; what did not exist was a list.**

**The seven worth re-reading before quoting**, because their denominator is a request or a
constant rather than anything the run observed, are `measure_open_partition.py`,
`measure_containers.py`'s "% moved", `measure_store.py`'s "% of ceiling",
`measure_payload_size.py`'s memory projection, `measure_store_cloud.py`'s write requests,
`measure_redis_hosting.py`'s per-entry cost and `probe_relist.py`'s coverage. None of them
is wrong. Each one answers "what would this cost at the rate we assume?" and not "what did
this cost?", and a reader who takes the first for the second gets a number that no run
produced. Every one of the seven says so in its own output today.

## What still has no owner

Nothing runs this sweep again. It is a `measured` reading of one commit, and the next tool
added to `tools/` will not be in it. A test that fails when a new `tools/*.py` reports a
percentage without also reporting what it was asked for is the thing that would close this,
and it is not written. See #104's report for the ticket that would write it.
