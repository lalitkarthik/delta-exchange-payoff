# 0007a - Container measurement run for I13 (#79)

Status pending. Depends on docs/design/decisions/0007-load-profile.md. Tool
docs/design/decisions/0007-load-profile.md (fill after I13-04/05 land:
tools/measure_containers.py collect / summarize). Compose project dxp (default,
ORCHESTRATOR confirms the real name post-I6).

#65 (I6) has not landed as of this writing, so no measurement has been taken. This
file is the skeleton the day-long run will fill. The source file uses the actual
multiplication-sign and en-dash glyphs; these tables render them as ASCII x and -.

## 4. The profile at 1x

| | CPU, cores | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | pending | pending | pending | pending | pending |
| `store` | pending | pending | pending | pending | pending |
| `api` | pending | pending | pending | pending | pending |
| `web` | pending | pending | pending | pending | pending |
| Redis | pending | pending | pending | pending | pending |

## 5. The profile at 10x

| | CPU, cores | Memory | Disk I/O | Network | Bound by |
|---|---|---|---|---|---|
| `feed` | pending | pending | pending | pending | pending |
| `store` | pending | pending | pending | pending | pending |
| `api` | pending | pending | pending | pending | pending |
| `web` | pending | pending | pending | pending | pending |
| Redis | pending | pending | pending | pending | pending |

## Start-to-healthy per container

One row per container the Compose project reports, filled in when the run completes.

| Container | started_at | healthy_at (or first-seen-running if the container defines no HEALTHCHECK) | seconds |
|---|---|---|---|
| pending | pending | pending | pending |

## Cells that moved more than 25%

pending - no measurement has been taken yet.

## Method

The tool is tools/measure_containers.py and its two entry points are collect and
summarize. The sampling interval is SAMPLE_INTERVAL_SECONDS, proposed 5 seconds and
argued in the plan's unsettled_decisions. Container names come from querying the
Docker daemon for the Compose project rather than a fixed list.

Filling this table is a transcription of tools/measure_containers.py summarize's
output, not a design decision, per R1-R7.

## The instrument, and what its error does to the figures above

This file's tables are transcribed from tools/measure_containers.py. #100 found three
defects in its collect loop, all present when the I13 collection was taken. Read this
section before reading a number above as if the instrument were neutral.

| Defect | Before #100 | After #100 |
|---|---|---|
| --interval was a gap added after the sweep, not a period | cadence measured 16.84 s against a configured 5 s, a 3.37x overrun | cadence measured 5.00 s, jitter under 5 ms |
| one sampled_at stamped across every container in a sweep | six reads spanning measured 11.53 s shared one stamp, and the file disclosed nothing | each row carries its own read's stamp, plus read_seconds and a sweep id |
| --samples counted sweeps at a rate never achieved | 17280 sweeps derived 78.7 h, not the 24 h intended | --samples is sweeps at the real cadence; --duration bounds wall clock |

Runs: A/B/A/B against the live dxp stack, 2026-09-12, quiet host, 8 sweeps per arm.
Before 16.833 and 16.841 s; after 5.001 and 5.000 s. The docker-daemon share of each
period was measured 70.0% before and 46-47% after; it is smaller but not negligible.

### Why the I13 figures survive it anyway

The cadence overrun costs samples, not accuracy: a mean over evenly spaced samples is
unbiased however few of them there are. Halving each container's series, which
simulates a cadence twice as coarse again, moves every per-container mean by 0.11 to
0.71 points, inside two standard errors in every case.

The standard error of each published mean, over the 1,130 usable samples:

| Container | cpu% mean | standard error | as a share of the cell |
|---|---|---|---|
| dxp-feed | 35.92 | 0.34 | 0.95% |
| dxp-store | 30.48 | 0.34 | 1.12% |
| dxp-api | 30.45 | 0.33 | 1.08% |
| dxp-redis | 7.06 | 0.11 | 1.56% |
| dxp-web | 3.88 | 0.17 | 4.38% |
| dxp-proxy | 1.09 | 0.03 | 2.75% |

R6's rule is that every cell moving more than 25% is called out and attributed to the
system. The instrument's own contribution to each cell is under 9% even at two
standard errors, so it cannot manufacture a move past that threshold.

The skew does not reach the time slices either. Shifting dxp-api's stamps across the
whole plausible within-sweep offset, 0 to 14 s, moves the three viewer-window means by
at most 0.07, 0.14 and 0.12 points and reassigns at most 2 of 659 samples.

What the skew does invalidate is any claim that aligns one container against another
at sweep resolution. Within a sweep the containers were read in the order dxp-proxy,
dxp-api, dxp-store, dxp-feed, dxp-redis, dxp-web, up to derived 9.57 s apart, and the
file does not record which. Lead and lag between services is not available from this
collection at any resolution finer than an hour.

Throughput rates are unaffected. summarize divides a counter delta by the gap between
two consecutive stamps of the same container, and those stamps advance at the real
cadence, measured 16.59 s, not at the configured 5 s.
