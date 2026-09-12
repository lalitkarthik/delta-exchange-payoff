# The split, in numbers

Every figure here carries `measured`, `assumed` or `derived` **and the run behind it**. That is
this repository's rule everywhere and this file has no exemption.

Read [split-start-here.md](split-start-here.md) first. This file is evidence, not design.

## 1. What each container costs, `measured`

Collected over the split stack under live load. Window `measured` **2026-09-12T05:50:07Z to
11:02:38Z**, 5h12m, 1,130 samples per container. The window ends there because the system
stopped storing (#103), not because the day ended.

| Container | CPU, cores | Memory | Note |
|---|---|---|---|
| `feed` | **0.3592** | — | R6 sized this at `derived` 0.52–0.71. It is below both. |
| `store` | **0.3048** | — | |
| `api` | **0.3045** | — | Inside R6's 0.25–0.49 band at zero viewers. |
| Redis | 7.06% | **761.8 MiB** | R6 sized it 27.9% higher. That earlier figure's tag is disputed — see #118. |
| `web` | 3.88% | — | It served 1,810 bytes all window. This is an **idle** container. |
| proxy | 1.09% | — | |
| `discord-alerts` | **0.0463** | **46.9 MiB** | A separate 10-minute run. See §4. |

**Total, `derived` 1.1351 cores across seven services.** Two runs in one sum.

**Two qualifiers belong with these figures, not in a footnote.**

1. The p95s are the p95 of **~1.9 s-averaged CPU**, not of instantaneous CPU. Each sample is a
   window average and the window belongs to the number.
2. The per-viewer cost carries **±0.6–0.8**. Do not quote 4.66 and 1.68 as exact. The ordering
   claim is solid; the second decimal is not.

**Thirteen cells moved more than R6's 25% call-out threshold.** The full table and the
call-outs are in [design/research/0007a-container-measurement.md](design/research/0007a-container-measurement.md).

## 2. What a restart costs

`measured` 2026-09-12, from each container's own `StartedAt` against its own ready log line.

| Container | Ready, s |
|---|---|
| proxy | 0.250 |
| Redis | 0.264 |
| `web` | 0.820 |
| `discord-alerts` | 1.210 |
| `feed` | 1.376 |
| `api` | 1.560 |
| `store` | 1.632 |

**Every service is ready in under 1.7 s.** A `docker compose up` reports them healthy at
10.99–15.72 s instead. The difference is the healthcheck's own cadence — `interval: 3s`,
`start_period: 10s` — and Compose's `depends_on` ordering.

**So deployment time here is a healthcheck setting, not a service property.**

**What a `feed` restart costs the bus**, `measured` off the 16:17:24.373Z restart:

| | s |
|---|---|
| First control event | 1.380 |
| Socket open | 2.478 |
| **First market-data event** | **2.961** |

**2.961 s is the figure R4 needs.** It is a quarter of the reported start-to-healthy.

## 3. The rate the system carries

**1,849.8 events/s is `derived`, not `measured`.** It comes from #58, by this arithmetic:
1,537.4 book frames + 156.2 ticker frames at two events each = 1,849.8. The derivation is in
[design/research/0001-stream-naming-and-payload-format.md](design/research/0001-stream-naming-and-payload-format.md) §5.

It was tagged `measured` in three documents and `derived` in three others, and one file tagged
it both ways. That is settled (#105). **The tag matters because it tells a reader where to go**:
`measured` says look at the observation, `derived` says check the arithmetic and the inputs. A
figure believed to be `measured` will not be revisited when its inputs move, because a
measurement has no inputs.

It is load-bearing. Record 0010's Table C grace rests on it, the outbox's 108 seconds is derived
from it, and Redis is sized against it.

`measure_computed_gaps.py` over the monolith store, `measured` 2026-09-12, expiry 25-09-2026,
date 2026-09-04: **214 of 1,118 minutes held a quote bar and no computed bar — 19.1%**, over a
1,181-minute span, 82.0% coverage.

**That figure replaces "24%, 217 of 904".** The old one came from a **truncated probe run** taken
while the day was still recording, and its 24% divided by a span including 62 minutes the engine
was down. #104 found it and corrected it.

## 4. What is not in the main collection

**`discord-alerts` was missing from the whole I13 run.** It was named the sixth service in
`CONTEXT.md` and written into every cost table by #95, and it was not in the stack. It is
running now and §1's figure for it is a **separate 10-minute run**, 120 sweeps at a 5.000 s
cadence, and is labelled as such.

#95's `assumed` 0.05 core is confirmed within 7.4%. **Its 0.25 GiB is 5.5× too generous.**

## 5. What the instruments got wrong

**Four instrument defects were found on 2026-09-12.** Each was found by accident while doing
something else, and **each one made the system look better than it was.**

| | What it did |
|---|---|
| #100 | The container collector sampled at 16.4 s when told 5 s, and stamped six reads with one clock. |
| #102 | An arrival-lag harness replayed the retention window into its own numbers. |
| #104 | A gap probe gridded from the first to the last minute it saw, so a stall shortened the span instead of holing it. A stalled store scored **eight times better** than a healthy one. |
| #79 | `docker stats` BlockIO does not observe a bind mount. It reported `store` at 25.4 B/s while the store wrote **1,491.4 B/s**. Wrong by **59×**. |

**A defect that makes a number look worse gets investigated. One that makes it look better gets
quoted.** That is why these took a day to find and why the sweep in
[design/instruments.md](design/instruments.md) exists: every tool under `tools/` is now recorded
against one question — does its denominator come from the data, or from the request?

## 6. What each health route can actually detect

`measured` 2026-09-12, from inside each container. **Read the last column first.** A route that
returns 200 whatever happens is a literal, not an assessment, and this repository has now been
bitten by that three times — #103, #108 and #115.

| Service | Status on failure | What it detects | What it cannot |
|---|---|---|---|
| `feed` | **503** | stopped, paused, spent budget, silence past 135 s, a dead bus reader, a dead flusher | whether `store` is keeping up |
| `store` | **503** | reader liveness, gave-up, task exits, trimmed watermarks, lag, monitor staleness | **a dead producer** — lag stays 0 when nothing arrives |
| `discord-alerts` | **503** | the consumer is not running, the last post failed | — |
| `api` | **always 200** | the feed's state, mirrored off the bus | **a spent budget** — `reconnects` and `budget_remaining` are `null` by construction; its own bus reader; its bar buffer |
| proxy | never | that nginx is running | **every upstream being down** |
| `web` | on non-2xx | the page renders | whether data is flowing |
| Redis | on non-PONG | the server answers | memory, evictions, stream depth |

**Three of the seven still cannot fail.** `api`'s is the one that matters: it is the route a
person is most likely to check, and it is always 200.

**A dead producer and a healthy consumer are identical from the consumer's side.** `store`'s lag
stayed 0 on all four streams through #108, because there was nothing arriving to be behind on.
#103's `consumer_lag` field cannot see that, by construction.

## 7. The suite

**1,541 tests pass, 0 fail, `ruff check .` clean**, `measured` 2026-09-12 on `main`. It was
1,401 that morning.

**Ten tests that proved nothing were found and fixed this week.** The shape is a test that would
still pass with the implementation reverted. The two methods that find them are in
[../CLAUDE.md](../CLAUDE.md)'s neighbours and in the vault's learning record: **mutate rather
than read**, and **scan the whole suite with `ast` rather than fix one more by hand**.
