# How long the live feed actually goes quiet

**The measurement behind `degraded_after` and `reconnect_after`.** The bounds themselves,
and the machine that uses them, are [design/lld/controller.md](lld/controller.md) §4.
Taken with `tools/measure_quiet_gap.py`, which subscribes every listed BTC option on both
channels — exactly what the engine subscribes — and records the wall-clock gap between
consecutive frames off the socket, which is precisely what the staleness timer measures.

**This lives here and not in `tools/out/`, which is gitignored.** A completed run there is
one re-run away from being unrecoverable; two of the three below very nearly were.

## The three runs

All on 2026-09-07, same BTC chain, both channels, one process. All `measured`.

| Run | Window | Symbols | Messages | Connections | **Max gap** | p99 | p95 | median |
|---|---|---|---|---|---|---|---|---|
| `20260907T135906Z` | 35 s | 492 | 25,033 | 1 | 0.321 s | 0.011 | 0.002 | 0.0 |
| `20260907T144659Z` | 550 s | 502 | 594,024 | 1 | 3.355 s | 0.011 | 0.002 | 0.0 |
| `20260907T135951Z` | **3610 s** | 492 | 3,808,870 | 3 | **44.785 s** | 0.011 | 0.002 | 0.0 |

0 malformed frames in all three. The longest twenty gaps are on `ob_l2` in every run but
one — the book is what goes quiet when anything does.

## The finding: only the tail moves

**The bulk of the distribution is identical in all three runs** — p99, p95, median and mean
do not move at all between 35 seconds and an hour. **The maximum moves by two orders of
magnitude.** The quiet gap is heavy-tailed, so the only thing a short window measures is
the part of the distribution that was never in question.

**A short window would have answered confidently and wrongly.** Thirty-five seconds says
15 s is 47x the worst gap seen (`derived`, 15 / 0.321) and the default has enormous room.
The hour says the worst gap is nearly three times the degraded bound. The ticket's
insistence on an hour was right, and the 35-second run should not be quoted on its own.

## What it says about the two defaults

- **`degraded_after` = 15 s fires, and roughly twice an hour.** Two gaps crossed it:
  **44.785 s** at t=1919 and **15.496 s** at t=2580. Both are long enough to be an
  interruption rather than a quiet market, which is exactly what the badge is for. The
  default is **not too tight** on this evidence, and the ticket's worry — that it would
  fire on a quiet Sunday minute — did not happen in an hour of ordinary trading.
- **`reconnect_after` = 45 s held by 0.215 s.** The largest gap in the hour sat just
  inside it. It was not crossed, but there is no margin here worth the name, and a second
  hour should be taken before anyone treats 45 s as comfortable.
- **The hour was not clean.** Three connections means two reconnects, and `last_error`
  records a local network abort (`WinError 1236`) — at least one interruption was this
  machine's rather than Delta's. **The tool does not tag a gap with the connection it
  spanned**, so which gaps were the reconnects is *not proven*; the 6.903 s and 44.785 s
  gaps cluster at t≈1873–1919 and are consistent with a single outage there.
- **The flap fixed in this review came within 0.215 s of happening for real.** The old code
  measured a reopened socket's staleness from a message that predated the drop and demoted
  it the moment that age passed `reconnect_after`. That age reached 44.785 s in this hour.
  A gap a quarter of a second longer would have flapped the badge during the outage.

## What is still not measured

- **A quiet market.** All three runs are an active BTC session. The case the bound is most
  likely to misfire on — a weekend, an overnight lull, a thin ETH chain — is untested.
- **ETH.** The tool subscribes BTC only.
- **Whether a gap spanned a reconnect.** Worth adding to the recorder before the next hour:
  it is the difference between "the venue went quiet" and "our socket died", and the
  controller treats those very differently.

## Reproducing

    python tools/measure_quiet_gap.py            # one hour
    python tools/measure_quiet_gap.py 300        # five minutes

Output is written to `tools/out/measure_quiet_gap-<start>-<seconds>s.json`, named by the
run so a later one cannot overwrite an earlier one, plus a `-latest.json` convenience copy.
