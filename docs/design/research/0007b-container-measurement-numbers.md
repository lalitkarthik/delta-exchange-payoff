# The numbers behind the I13 container measurement

Evidence for [0007a-container-measurement.md](0007a-container-measurement.md), split out by
#79 to keep that file under the 200-line bound - the same move
[hld-evidence.md](../hld-evidence.md) (#62), `logging-catalogue.md` (#63) and
`store-numbers.md` (#81) each made. **Quote a number from this file, never from a sentence
elsewhere.** The references it compares against are
[0007-load-profile.md](0007-load-profile.md) sections 4 and 9 and the decision record
[../decisions/0007-load-profile.md](../decisions/0007-load-profile.md). **Start-to-healthy
and the instrument are the second sibling,
[0007c-container-startup-and-instrument.md](0007c-container-startup-and-instrument.md).**

**Runs behind every figure here.**

| Run | What | Window |
|---|---|---|
| **I13-clean** | `containers-2026-09-12-clean.jsonl`, 6,779 samples, six containers | `measured` 2026-09-12T05:50:07Z-11:02:38Z, 5h12m, 1,130 samples a container |
| **I13-disk** | 248 Parquet flush files in `.stack-data/`, 27,965,139 B | the same window, by file mtime |
| **short-seven** | 120 sweeps, cadence `measured` 5.000 s, seven containers | `measured` 2026-09-12T16:36:31Z-16:46:26Z, restarted stack |
| **restart** | `docker compose up` to healthy, polled 1 s; `docker inspect`; container logs | `measured` 2026-09-12T15:52:27Z |

---

## 1. Every cell, against R6

`measured` from I13-clean unless the row says otherwise. `moved` is
`(measured - reference) / reference`. **Thirteen cells cross 25%.**

| Cell | `measured` | R6 reference | moved | >25% | Attribution |
|---|---|---|---|---|---|
| `feed` CPU cores | 0.3592 | 0.52, C1 lower | -30.9% | **yes** | the system: `feed` is cheaper than either estimate |
| `feed` CPU cores | 0.3592 | 0.71, the value the sizing used | -49.4% | **yes** | the same |
| `store` CPU cores | 0.3048 | 0.28, C3 lower | +8.9% | no | inside the band |
| `api` CPU cores | 0.3045 | 0.25, C4 lower | +21.8% | no | inside the band |
| `api` CPU, 0 viewers | 0.2701 | 0.25, C4 lower | +8.0% | no | inside the band |
| `api` CPU, 3 viewers | 0.3503 | 0.49, C4 upper | -28.5% | **yes** | the estimate: the upper bound is not reached at three viewers |
| `feed` memory MiB | 97.1 | ~50, M1 | +94.2% | **yes** | the estimate: M1 sized the process, not the container |
| `store` memory MiB | 113.0 | ~115, M1 | -1.7% | no | - |
| `api` memory MiB | 80.2 | ~80, M1 | +0.3% | no | - |
| `web` memory MiB | 110.6 | 240.8, M3 | -54.1% | **yes** | conditions: M3 measured `next dev`, two host processes; this is one idle `next start` container |
| `web` CPU cores | 0.0388 | 0.0008, m6 | +4,750% | **yes** | conditions: the same. An idle container still costs 3.9% of a core |
| Redis memory MiB | 761.8 | 1,056.4, M4 | -27.9% | **yes** | conditions: one underlying, and a 1 GiB cap for the first ten minutes |
| Redis CPU cores | 0.0706 | 0.05, C6 `assumed` | +41.2% | **yes** | the estimate, and it was tagged `assumed` |
| `store` disk KB/s | 1.4914, I13-disk | 2.33, D1 (BTC+ETH) | -36.0% | **yes** | conditions: one underlying |
| `store` disk KB/s | 1.4914, I13-disk | 1.6551, D1's own BTC-alone 143 MB/day | -9.9% | no | **the like-for-like comparison. D1 is confirmed** |
| `store` MB/day | 128.86, I13-disk | 143, D1 BTC alone | -9.9% | no | the same figure, per day |
| `store` files a flush | 4 | 8, D1 | -50.0% | **yes** | conditions: 4 tables x 1 underlying, not x 2 |
| `feed` net in KB/s | 724.8 | 859.9, N1 | -15.7% | no | - |
| `feed` net out KB/s | 719.5 | 598.2, N1 | +20.3% | no | - |
| `store` net in KB/s | 671.6 | 598.2, N2 | +12.3% | no | - |
| `store` net out KB/s | 45.0 | 2.3, N2 | +1,856% | **yes** | the estimate: N2 counted acks and not the consumer-group protocol around them |
| `api` net in KB/s | 671.6 | 598.2, N3 | +12.3% | no | - |
| `api` net out, 1st viewer KB/s | 10.37 | 38.9, N3 | -73.3% | **yes** | conditions: a 29-row ladder here; see section 3 |
| `api` net out, 2nd-3rd KB/s | 8.56 | 38.9, N3 | -78.0% | **yes** | the estimate: N3 priced every viewer alike |
| Redis net in KB/s | 767.4 | 598.2, N4 | +28.3% | **yes** | the estimate, marginally |
| Redis net out KB/s | 1,380.2 | 1,196.4, N4 | +15.4% | no | - |
| `proxy`, every cell | - | **no R6 row** | - | - | R6 has five rows; #65 made six containers. No judgement possible |
| `discord-alerts` CPU cores | 0.0463, short-seven | 0.05 `assumed`, #95 | -7.4% | no | **#95's assumption is confirmed** |
| `discord-alerts` memory MiB | 46.9, short-seven | 256 (0.25 GiB) `assumed`, #95 | -81.7% | **yes** | the estimate: 5.5x too generous |

## 2. The totals, and the three thresholds

| | Value | Tag | Arithmetic |
|---|---|---|---|
| 1x total, six containers | **1.0888 cores** | `derived` | sum of the six `measured` means, I13-clean |
| 1x total, seven | **1.1351 cores** | `derived` | the above plus `discord-alerts` 0.0463 from short-seven. **Two runs; say so when quoting it** |
| against the monolith | **3.52x** | `derived` | 1.0888 / R4's `measured` 0.3089 |
| against R6's split estimate | **-1.0%** | `derived` | 1.0888 against the 1.10-1.75 band's lower bound. **R6's split arithmetic lands on its lower bound** |
| run intensity | **0.86x** | `derived` | `feed` ingress 724.8 KB/s against R6's 1x 843.4 KB/s. One underlying, and a different day |

**Read every CPU cell at 0.86x.** Nothing here scales it back up to 1x, because the
relationship between ingress bytes and CPU is exactly what R6 is estimating and this run does
not establish it.

## 3. The per-viewer cost, and the one thing it does not explain

`api` sliced by `viewer-windows.log`, which is the authority. All three windows are inside
I13-clean. Network is a counter delta over the window, not a mean of per-sample rates.

| Window | Viewers | Samples | CPU cores | Memory MiB | `api` net out B/s | `proxy` net out B/s |
|---|---|---|---|---|---|---|
| 06:01:32Z-07:01:32Z | 0 | 220 | 0.2701 | 79.3 | 21,195 | 34 |
| 07:01:32Z-08:01:32Z | 1 | 220 | 0.3167 | 79.7 | 31,562 | 8,177 |
| 08:01:32Z-09:01:33Z | 3 | 219 | 0.3503 | 80.8 | 48,684 | 24,314 |

`derived`: **the first viewer costs 0.0466 core and 10.4 KB/s; the next two cost 0.0168 core
and 8.6 KB/s each.** A viewer is cheap and the second is cheaper than the first, which is
what a shared ladder should do. Each window mean carries SE ~0.59 points, so **4.66 and 1.68
must not be quoted as exact** - the ordering is solid at 5.6 and 2.0 standard errors, the
second decimal is not.

**Against R6's per-viewer cell the move is -73% to -78%, and only part of it is explained.**
The frame cadence matches: 3,573 frames in 3,600 s is 0.99 a second, and m3 saw 62 pushes in
62.1 s. So the difference is per-push size - `derived` ~8,625 B here against m3's `measured`
38,316 B, a factor of 4.4. The ladder was **29 rows** in this run, `measured` on the issue
before the driver was launched. That is one candidate and it is not sufficient on its own:
per row it still leaves 297 B against m3's ~1,064 B. **The remaining factor is `assumed` to
be websocket compression** - m3 counted application bytes and these are wire bytes - and
**this run does not separate the two. Do not close that gap without measuring it.**

`proxy` out is 8,105 B/s a viewer at three viewers, which agrees with `api`'s marginal
8,561 B/s to within 5.3% and is the independent check on the slice.

## 4. `web` served nothing, and the row has to say so

`web` moved **1,810 bytes in 5h12m**: `net_rx` 2,500 -> 4,310 B. The viewer driver connected to
`/api/ws/chain` through the proxy, which `proxy/nginx.conf` routes to `api`; nothing ever
requested a page, which `nginx` would have routed to `web`. **So `web`'s 0.0388 core and
110.6 MiB are an idle `next start` container**, and both its moves against R6 compare that
with a `measured` host-side `next dev` pair. R6's `assumed` for the network cell - "page loads
only; the browser's socket is to `api`" - is confirmed exactly, by a counter that did not move.

## 5. Disk, measured at the source

`docker stats` reported `block_read` 0.0 B/s for all six containers across the whole window
and `block_write` 25.4 B/s for `store`. **Both are artefacts.** BlockIO does not observe a
bind mount on this host, and `.stack-data/` is one.

`measured` at the source, by file mtime inside the window:

| Table | Files | Bytes | B/s |
|---|---|---|---|
| `reference-bars` | 62 | 16,584,524 | 884.5 |
| `computed-bars` | 62 | 6,739,848 | 359.4 |
| `quote-bars` | 62 | 4,496,320 | 239.8 |
| `spot-bars` | 62 | 144,447 | 7.7 |
| **total** | **248** | **27,965,139** | **1,491.4** |

62 flushes over 18,751 s is `derived` **302.4 s a flush** against a configured 300 s, which
is the independent check that the window caught whole flush cycles. `summarize` now names any
counter that never advanced, so the 0.0 cannot be quoted as a measured zero;
`engine/tests/test_measure_containers.py` pins that.

## 6. The service R6 least believed

**R6 section 9 is titled *The share I least believe: `api`* and names `feed` its runner-up.**
The ticket's "what to notice" is therefore `api`. An earlier comment on #79 read the
runner-up as the answer; it is not.

**`api`: right to doubt, wrong about which half.** Section 9's grounds were that "what drives
it was zero when it was measured" - nothing watched, one sample on the smallest ladder, and
the thinnest py-spy slice of any service. Measured with 0, 1 and 3 viewers:

| R6 cell | `derived` | `measured` | Verdict |
|---|---|---|---|
| base CPU | 0.25-0.49 core | 0.3045 overall, 0.2701 at zero viewers | **inside the band.** The base was sound |
| per watched expiry | +0.05-0.14 core | +0.0466 the first, +0.0168 each of the next two | **the second and third are 66% below the lower bound** |
| per viewer, network | 38.9 KB/s | 10.4 KB/s the first, 8.6 KB/s each of the next two | **-73% and -78%** |

**The marginal term is the error, and the reason is structural**: R6 priced every viewer
alike, and the ladder is solved once and fanned out, so the second viewer cannot cost what the
first did. **The doubt was justified; the cell it should have fallen on was the increment, not
the base.**

**`feed`, the runner-up, is the larger error.** R6 recorded that its own `derived` 0.52
disagreed with I2's `measured` 0.71, called the disagreement unexplained, and **sized on
0.71**. `measured` **0.3592** is below both - -30.9% on 0.52 and **-49.4%** on 0.71 - so the
disagreement was resolved in the wrong direction and `feed` is over-provisioned by `derived`
**1.98x** against the figure the sizing used. **Of the two services R6 doubted, the one it
ranked second is the one that moved.**
