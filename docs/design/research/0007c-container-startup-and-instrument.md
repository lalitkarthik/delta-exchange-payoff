# Container start-up and the instrument, for I13 (#79)

The second evidence sibling of
[0007a-container-measurement.md](0007a-container-measurement.md), split from
[0007b-container-measurement-numbers.md](0007b-container-measurement-numbers.md) by #79 when
that file reached the 200-line bound. The R6 ledger, the totals and the per-viewer slice are in
0007b; **start-to-healthy and the instrument are here.** The runs named below are the ones
0007b's table at the top defines. **Quote a number from this file, never from a sentence
elsewhere.**

---

## 1. Start-to-healthy, by three methods

Method A is `docker compose up` to `.State.Health.Status == healthy`, polled at 1 s, from the
**restart** run. Method B is A minus `.State.StartedAt`, `derived`. Method C is
`.State.StartedAt` to the process's own ready line, both stamps from the daemon's clock.

| Container | StartedAt | Ready line | A | B | C |
|---|---|---|---|---|---|
| `dxp-redis` | 15:52:28.717 | 15:52:28.981 `Ready to accept connections tcp` | 10.99 | 9.28 | **0.264** |
| `dxp-web` | 15:52:28.710 | 15:52:29.530 `Ready in 332ms` | 11.78 | 10.08 | **0.820** |
| `dxp-discord-alerts` | 15:52:31.546 | 15:52:32.757 `Application startup complete` | 12.22 | 7.68 | **1.210** |
| `dxp-api` | 15:52:31.592 | 15:52:33.152 `Application startup complete` | 11.56 | 6.98 | **1.560** |
| `dxp-proxy` | 15:52:37.395 | 15:52:37.645 `start worker processes` | 15.72 | 5.34 | **0.250** |
| `dxp-feed` | 16:17:24.373 | 16:17:25.749 `Application startup complete` | 11.19 | n/a | **1.376** |
| `dxp-store` | 15:56:57.538 | 15:56:59.170 `Application startup complete` | 11.38 | n/a | **1.632** |

**A is an upper bound and measures the stack.** Six of seven land inside 1.3 s of each other
because Compose serialises on `depends_on` - Redis healthy before `store`, `feed`, `api` and
`discord-alerts` start; `api` and `web` healthy before `proxy` starts - and the healthcheck
quantises the flip. Publish it only with that sentence attached.

**`feed` and `store` have no B.** Both were restarted in place after 15:52Z, which overwrote
`.State.StartedAt`; their original ready lines survive in the container log and their C is
`measured` on the later restart instead.

**Healthcheck configuration, `measured` by `docker inspect`**: `interval` 3 s,
`start_period` 10 s, `retries` 20, `timeout` 3 s on six of seven; `interval` 2 s,
`start_period` 0 s, `retries` 15 on Redis. The residual 4.4-9.3 s between C and A is the
healthcheck's detection lag under a seven-container concurrent start. **It is not
attributable to any service**, and it is the figure a rolling deploy actually waits on.

## 2. The I13 collection's own health records are not usable

All six carry `healthy_at` = `2026-09-12T05:50:07.311037Z`, the collector's first sample, taken
35 s after the stack was up. Since #100 that stamp is further known to be the sweep's
**pre-read** stamp, so it is not even when the container was checked. `seconds` there is
"container start to collector first looked", and the six values - 26.16 to 34.76 s - are that
and nothing else.

## 3. The instrument

**[../instruments.md](../instruments.md) is the standing record of this tool's defects** -
#100's three, with the A/B runs and the per-sweep daemon costs behind them. What belongs here
is only what the I13 figures rest on.

**Standard error of each published mean**, over the 1,130 usable samples of I13-clean:

| Container | cpu% mean | standard error | share of the cell | at 2 SE |
|---|---|---|---|---|
| `dxp-feed` | 35.92 | 0.34 | 0.95% | 1.9% |
| `dxp-store` | 30.48 | 0.34 | 1.12% | 2.2% |
| `dxp-api` | 30.45 | 0.33 | 1.08% | 2.2% |
| `dxp-redis` | 7.06 | 0.11 | 1.56% | 3.1% |
| `dxp-web` | 3.88 | 0.17 | 4.38% | 8.8% |
| `dxp-proxy` | 1.09 | 0.03 | 2.75% | 5.5% |

**R6's rule calls out every cell moving more than 25%, and the instrument's own contribution is
under 9% of any cell even at two standard errors. It cannot manufacture a move past that
threshold.** Halving each container's series - simulating a cadence twice as coarse again -
moves every mean by 0.11 to 0.71 points, inside two standard errors in every case. The viewer
slice survives the whole plausible skew: shifting `dxp-api`'s stamps across 0 to 14 s, far past
its true ~1.9 s, moves the three window means by at most 0.07, 0.14 and 0.12 points and
reassigns **2 of 659** samples.

**A fourth defect, found by #79 and not yet in `instruments.md`.** `docker stats` BlockIO does
not observe a bind mount on this host, so the disk column under-reported `store` by **59x**:
25.4 B/s against 1,491.4 measured at the source. `summarize` now names every counter whose
first and last readings are identical, so a flat counter cannot be quoted as a measured zero;
`engine/tests/test_measure_containers.py` pins it. **It belongs in `../instruments.md` beside
the other three**, which #79 did not have the scope to edit.

**Two facts about the file itself, visible only since #100.** Two of 1,500 sweeps are short:
06:11:08Z is missing `dxp-redis` - which is why that container has 1,129 samples against
everyone else's 1,130 - and the final sweep at 13:31:09Z was truncated when the collector was
stopped. **The old format disclosed neither.**
