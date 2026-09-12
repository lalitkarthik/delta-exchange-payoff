# 0007a - Container measurement run for I13 (#79)

Status landed, #79, 2026-09-12. Replaces the `derived` cells of
[../decisions/0007-load-profile.md](../decisions/0007-load-profile.md) (R6) section 4 with
`measured`. Tool `tools/measure_containers.py`, entry points `collect` and `summarize`.
Compose project `dxp`. The per-cell ledger is
[0007b-container-measurement-numbers.md](0007b-container-measurement-numbers.md) and the
start-up and instrument detail is
[0007c-container-startup-and-instrument.md](0007c-container-startup-and-instrument.md);
**quote a number from those, never from a sentence here.** This file uses ASCII `x` and `-`
where the rest of the repository writes the multiplication sign and the en dash.

## The window, and why it is not a day

**`measured` 2026-09-12T05:50:07Z-11:02:38Z, 5h12m, 1,130 samples a container, six
containers.** Criterion 1 asks for one full day. **It is not met and is not ticked.** The
collection was cut three times, each time on evidence, and the last cut is the one that
binds: the stack stopped storing at 11:02:38Z (#103), so every sample after it describes a
stalled system rather than this one. **The window ends because the system stopped, not
because the day ended.**

A full-day re-collection would add the 16:00-05:00Z hours and nothing else that is known to
differ. Seven days of recorded `quote-bars` put every hour between 2.57M and 5.03M ticks with
no session structure, and the busiest hour of the day, 07:00Z, is already inside this window.
**It is not worth re-running for criterion 1 alone.** It is worth re-running for the two
things this collection genuinely lacks: `discord-alerts`, and a store that consumes for the
whole window.

## Two qualifiers that belong with the figures, not under them

1. **Every p95 below is the p95 of ~1.9 s-averaged CPU, not of instantaneous CPU.** Each
   sample is a window average and the window is part of the number.
2. **The per-viewer cost carries +/-0.6-0.8.** `4.66` and `1.68` points of a core are not
   exact to the second decimal. The ordering claim is solid; the decimal is not.

## 4. The profile at 1x, `measured`

Means over the window above. `cores` is `cpu_percent / 100`; one core is one laptop core.
Disk for `store` is **not** the container's block counter - see the note under the table.

| | CPU, cores | Memory, MiB | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | **0.3592** mean, 0.5179 p95 | 97.1 mean, 105.8 p95 | none observed | in 724.8 KB/s, out 719.5 KB/s | **CPU** |
| `store` | **0.3048** mean, 0.4951 p95 | 113.0 mean, 183.4 p95 | **1.49 KB/s**, 4 files per 302 s | in 671.6 KB/s, out 45.0 KB/s | **CPU**, not disk |
| `api` | **0.3045** mean, 0.4719 p95 | 80.2 mean, 81.5 p95 | none observed | in 671.6 KB/s, out 21.2-48.7 KB/s by viewers | **CPU**, set by viewers |
| `web` | **0.0388** mean, 0.1361 p95 | 110.6 mean, 112.5 p95 | none observed | **in 0.1 B/s, out 1.1 B/s** | memory |
| Redis | **0.0706** mean, 0.1070 p95 | 761.8 mean, 802.9 p95 | none observed | in 767.4 KB/s, out 1,380.2 KB/s | memory |
| `proxy` | **0.0109** mean, 0.0389 p95 | 17.1 mean, 17.7 p95 | none observed | in 6.3 KB/s, out 6.3 KB/s | - |
| `discord-alerts` | **0.0463** mean, 0.1384 p95 | 46.9 mean, 52.7 p95 | none observed | in 258 B/s, out 403 B/s | - |

**`discord-alerts` is the one row from a different run.** It was not running during the I13
collection - the stack predated #66 - so its cell is `measured` over a separate 10-minute
sample, 2026-09-12T16:36:31Z-16:46:26Z, 120 sweeps at a `measured` 5.000 s cadence on the
restarted stack. **Do not read it as one window with the other six.**

**"none observed" is not a `measured` zero.** `docker stats` BlockIO does not observe a bind
mount on this host: it reported `block_read` 0.0 B/s for all six containers across the whole
window while `store` was writing Parquet into one the entire time. `summarize` now names
every counter that never advanced. **`store`'s 1.49 KB/s is measured at the source** - 248
flush files, 27,965,139 B, written into `.stack-data/` inside the window.

**`web` received 1,810 bytes in 5h12m.** The viewer driver drove `/api/ws/chain` through the
proxy and never fetched a page, so the `web` row is an **idle** `next start` container. Its
CPU and memory are a floor, not a serving cost. R6's own `assumed` for this row - "page loads
only; the browser's socket is to `api`" - is confirmed exactly.

**The run sat at `derived` 0.86x of R6's 1x**, by `feed` ingress bytes: 724.8 KB/s `measured`
against R6's 843.4 KB/s. One underlying, BTC, not two. Every CPU cell above is taken at that
intensity.

## 5. The profile at 10x

**Not measured, and this run cannot measure it.** 10x means ten times the event rate, which
needs a load the venue does not supply and `tools/loadgen.py` does not synthesise onto the
bus. R6's section 5 stands as `derived`, untouched. The only thing this run says about it is
that its 1x inputs are now `measured`, so the 10x arithmetic rests on firmer ground than it
did.

## Start-to-healthy per container

**Three methods, and the one that answers the criterion is C.** The I13 collection's own
`healthy_at` values are **not usable**: all six carry the identical stamp
`2026-09-12T05:50:07.311037Z`, which is the collector's first sample, taken 35 s after the
stack was already up. Six containers, six start times, one healthy time.

From the restart at `measured` 2026-09-12T15:52:27Z, images rebuilt with #103's fix:

| Container | A: `up` to healthy | B: start to healthy | C: start to ready | ready to healthy |
|---|---|---|---|---|
| `dxp-redis` | 10.99 | 9.28 | **0.264** | 9.02 |
| `dxp-feed` | 11.19 | n/a | **1.376** | 4.41 |
| `dxp-store` | 11.38 | n/a | **1.632** | 5.23 |
| `dxp-api` | 11.56 | 6.98 | **1.560** | 5.42 |
| `dxp-web` | 11.78 | 10.08 | **0.820** | 9.26 |
| `dxp-discord-alerts` | 12.22 | 7.68 | **1.210** | 6.47 |
| `dxp-proxy` | 15.72 | 5.34 | **0.250** | 5.09 |

- **A** is `docker compose up` to `.State.Health.Status == healthy`, polled at 1 s. **Publish
  it only with this caveat**: six of seven land inside 1.3 s of each other because Compose
  serialises the start on `depends_on` and the healthcheck quantises the flip. It is an
  **upper bound** and it measures the stack, not the service.
- **B** is A minus the container's own `.State.StartedAt`. It removes the serialisation and
  is `derived`. `feed` and `store` have no B: both were restarted in place after 15:52Z, so
  their original `StartedAt` is overwritten.
- **C** is `.State.StartedAt` to the process's own ready line, both on the daemon's clock -
  `Ready to accept connections tcp`, `Ready in 332ms`, `Application startup complete`,
  `start worker processes`. No poller sits in this path. **`feed` and `store` are measured on
  their later restarts**, 16:17:24Z and 15:56:57Z.

**Every service in this stack is ready in under 1.7 s.** What start-to-healthy measures is
the healthcheck: `interval` 3 s and `start_period` 10 s on six of seven, 2 s and 0 s on
Redis, which adds 4.4-9.3 s of detection lag on top. **Deployment time here is a healthcheck
setting, not a service property**, and that is the figure R7 should size a rolling deploy on.

## Cells that moved more than 25%

**Thirteen cells moved.** The full ledger with every reference, percentage and attribution is
[0007b-container-measurement-numbers.md](0007b-container-measurement-numbers.md) section 1.
The four that carry a decision:

- **`feed` CPU is `measured` 0.3592 against R6's `derived` 0.52-0.71, -30.9% on the lower
  bound and -49.4% on the 0.71 the sizing actually used.** R6 doubted `feed` as its runner-up
  and resolved the doubt upward. The measurement says it should have resolved downward.
- **`api` CPU is `measured` 0.3045 and sits inside R6's 0.25-0.49.** The band was right.
- **Redis memory is `measured` 761.8 MiB against R6's 1,056.4, -27.9%** - one underlying, not
  two, and the first ten minutes ran under a 1 GiB `maxmemory` before it was raised. R6 tags
  that reference `measured` in its decision table and `derived` in its research; see R6's own
  appended note. This file does not re-tag it.
- **`api`'s per-viewer network is `measured` 10.4 KB/s for the first viewer and 8.6 KB/s for
  each of the next two, against R6's `derived` 38.9 KB/s a viewer: -73% and -78%.**

**The 1x total is `measured` 1.0888 cores over six containers**, or `derived` 1.1351 with
`discord-alerts`. That is R7's threshold and it is crossed - see
[../decisions/0008-topology.md](../decisions/0008-topology.md).

## The service R6 least believed, which is `api` and not `feed`

R6 section 9 is titled *The share I least believe: `api`*, and names `feed` its **runner-up**.
The answer to the ticket's "what to notice" is therefore `api`, and **R6 was right to doubt it
but wrong about which half**. `api`'s base is `measured` 0.3045 core - 0.2701 at zero viewers
- and sits inside the 0.25-0.49 band, so the thing section 9 feared, a base measured with
nothing watched, was sound. Its **marginal** term was not: `+0.05-0.14 a watched expiry` is
`measured` **0.0466** for the first viewer and **0.0168** for each of the next two, 66% below
the band, because the ladder is solved once and fanned out. **The runner-up is the larger
error**: R6 recorded that its `derived` 0.52 for `feed` disagreed with I2's `measured` 0.71,
called it unexplained and sized on 0.71; `measured` 0.3592 is below both. Full working in
[0007b](0007b-container-measurement-numbers.md) section 6.

## Method

`tools/measure_containers.py collect` discovers containers by
`label=com.docker.compose.project`, so the list cannot drift from the Compose file, and
appends each sample as it is taken; `summarize` reads the files back. Filling the tables above
is transcription of its output plus the two source-measured cells, not a design decision.

## The instrument, and what its error does to the figures above

These tables are transcribed from `tools/measure_containers.py`. **#100 found three defects in
its collect loop, all present when the I13 collection was taken**, and #79 found a fourth. Read
this before reading a number above as if the instrument were neutral. The A/B runs, the
standard-error table and the skew arithmetic are
[0007c](0007c-container-startup-and-instrument.md) section 3.

| Defect | Before | After |
|---|---|---|
| `--interval` was a gap added after the sweep, not a period | cadence `measured` 16.84 s against a configured 5 s, a 3.37x overrun | `measured` 5.00 s, jitter under 5 ms (#100) |
| one `sampled_at` stamped across every container in a sweep | six reads spanning `measured` 11.53 s shared one stamp, and the file disclosed nothing | each row carries its own stamp, `read_seconds` and a sweep id (#100) |
| `--samples` counted sweeps at a rate never achieved | 17,280 sweeps `derived` 78.7 h, not the 24 h intended | `--samples` is sweeps at the real cadence, `--duration` bounds wall clock (#100) |
| BlockIO does not observe a bind mount | `store`'s disk under-reported 59x: 25.4 B/s against 1,491.4 | `summarize` names every counter that never advanced (#79) |

**The figures above survive the first three.** The cadence overrun costs samples, not accuracy:
a mean over evenly spaced samples is unbiased however few of them there are. Every published
cell's standard error is under 4.4% of the cell and under 9% at two standard errors, so **the
instrument cannot manufacture a move past the 25% threshold R6's rule turns on.** Halving each
container's series moves every mean by 0.11 to 0.71 points, inside two standard errors in every
case. The viewer slice survives too: shifting `dxp-api`'s stamps across the whole plausible
within-sweep offset, 0 to 14 s, moves the three window means by at most 0.07, 0.14 and 0.12
points and reassigns 2 of 659 samples.

**What the skew does invalidate is any cross-container, time-aligned claim at sweep
resolution.** Within a sweep the read order was `proxy`, `api`, `store`, `feed`, `redis`,
`web`, up to `derived` 9.57 s apart, and the old format does not record which. **Lead and lag
between services is unavailable from this collection at any resolution finer than an hour.**

**Throughput rates are unaffected, and this is not obvious.** `summarize` divides a counter
delta by the gap between two consecutive stamps *of the same container*, and those advance at
the real cadence. **Nothing in the file was ever divided by 5 s.**
