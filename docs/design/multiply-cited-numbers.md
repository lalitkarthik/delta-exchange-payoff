# Multiply-cited numbers: the sweep, and what it found

**#105's real deliverable, per its own acceptance criterion 5**: not the one number the ticket
named, but the check that finds every number shaped like it — cited with a tag in two or more
documents, where the tags disagree.

The check itself lives in
[`engine/tests/test_cloud_numbers.py`](../../engine/tests/test_cloud_numbers.py) as
`_multiply_cited_tag_disagreements`, and runs on every test collection —
`test_the_multiply_cited_figure_sweep_finds_no_new_disagreement`. This file is that sweep's
output, `measured` 2026-09-12 at this ticket's `HEAD`, kept as the readable record instead of
only the report. **Quote a number's disagreement from here or from the test's docstrings; the
full per-number reasoning lives in the test's `_KNOWN_MULTIPLY_CITED` and is not restated.**

## 1. Method, in short

A number is a candidate if it is specific enough not to collide by coincidence — four or more
significant digits, or two or more fractional digits (`1,849.8` qualifies; `30 minutes` and
`50 ms` do not) — and it is cited with a `measured`/`derived`/`assumed`/`unmeasured` tag in the
same table cell or the same sentence in **two or more files** under `docs/`. Its tag is whichever
one sits nearest it, so a table row citing two different tagged figures side by side does not
cross-attribute one's tag to the other.

**This is an approximation, the same way #95's `CONDITION_WINDOW` pin is.** A first pass that
attributed *any* tag on the same line, rather than the nearest one, flagged over 70 numbers.
Reading every one with three or more citing files by hand found exactly one real, previously
unnoticed disagreement — §2 below — and two more the sweep surfaces correctly but this ticket
does not resolve — §3. Everything else was the heuristic attaching a neighbour's tag to the
wrong number; §4 lists those so the next reader does not re-derive it.

## 2. Fixed by this ticket

| Figure | Sites | Tags found | Now |
|---|---|---|---|
| 1,849.8 events/s | `message-bus-numbers.md`, `redis-hosting.md` (×2), `decisions/0010-store-replay.md`, `lld/redis-bus.md`, `lld/store-replay.md`, `research/0001`, `research/0005`, `research/0005b`, `research/0007-load-profile.md` (×2) | `derived` at 9 sites, `measured` at 3 | **`derived` everywhere** — three sites corrected, listed below |

The three: `redis-hosting.md:111`, `decisions/0010-store-replay.md:97` and
`lld/store-replay.md:191`. The last of those also named the wrong run for it — the figure is
#58's arithmetic, and that line had credited it to I2's batch-interval work instead; both the tag
and the attribution are corrected there now.

Full evidence: the issue itself, and
`engine/tests/test_cloud_numbers.py::test_1849_8_events_per_second_is_derived_everywhere`.

## 3. Found, not fixed here — `out_of_scope_noticed`

Both are real candidates the sweep surfaces correctly. Neither is 1,849.8's shape of defect (a
plain tagging slip); each needs someone who knows the underlying run to say which reading is
right, which is outside this ticket's territory and its evidence.

| Figure | Sites | Disagreement |
|---|---|---|
| 5,511 ms | `docs/storage.md:135`, `docs/design/research/0010-store-replay.md:119` | `storage.md` computes it from two other tagged figures, calling the total `derived`; `research/0010` calls that same total a `measured` max arrival lag. Whether it is a sum or an observation determines which document is describing the underlying run correctly. |
| 240.8 MiB | `docs/design/cloud/compute.md:189`, `docs/design/research/0007-load-profile.md:74` | `compute.md` tags it `measured`, sharing a cell with the Redis working set; `research/0007-load-profile.md` tags the same `web`-container figure `derived` (its own row, M3). |

Both are allowlisted in `_KNOWN_MULTIPLY_CITED` with this same note, so the tripwire does not
fail on them while they wait for an owner.

## 4. Candidates that are not real disagreements

Every one of these is a number the sweep found tagged two ways across files. In each case the
"second" tag belongs, on inspection, to a different number sharing the same table cell or
sentence — not to the number in the left column. Full one-line reasoning per figure is in
`_KNOWN_MULTIPLY_CITED` in `engine/tests/test_cloud_numbers.py`; not restated here.

| Figure | What the nearby tag actually belongs to |
|---|---|
| 1,000 | a different, unrelated "1,000" — round-number coincidence between a cost model and a throughput figure |
| 0.05 core | the 2.00-core figure it is scaled into |
| 843.4 KB/s | the monthly-GB figure computed from it |
| 30.89% | the per-service point values computed from it |
| 0.71 core | none — documented and intentional (§9 of `research/0007-load-profile.md`) |
| 1,152 objects/day | "the day" (the flush-file count basis), a different quantity |
| 1.45 (grace multiplier) | the adjacent measured transit figure |
| 40.77% | the adjacent 29.96-point figure in the same cell |
| $78.07 | the oms/strategy addition layered on top of it |
| 156.2 ticker frames | the adjacent 1,693.6 frames/s figure in the same source cell |
| 7.25 ms | the 58 ms product computed from it |

## 5. Why #95's tripwire did not catch 1,849.8

`test_cloud_numbers.py` before this ticket held two pins, both written by hand after #95 found a
specific defect: a decimal unit written wrong, and two measurements of one publisher at
different batch intervals. Both tests check for the **exact shape of defect #95 found** — neither
asks the general question, "does every citation of this number agree on its tag." A pin written
for one bug catches that bug recurring; it does not catch a structurally identical bug in a
number the original author never looked at. §1's sweep is the general form, and it now runs
permanently instead of waiting for the next person to notice by reading three documents at once.
