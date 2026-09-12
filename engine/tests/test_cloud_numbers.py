r"""The cloud numbers that decided something, pinned so they cannot drift back.

#95 found five documents carrying figures a later decision had superseded. Two were
load-bearing.

**#105: a number can be tagged `measured` in one document and `derived` in another, and
nothing here checked for it.** #95's tests are pins written after the fact for the
specific defect #95 found — a decimal unit, a superseded figure. Neither one asks "does
every other citation of this number agree with this one," so a *new* figure with the same
shape of defect, 1,849.8 events/s tagged `measured` in three documents and `derived` in
three others, passed every existing check.
`test_the_multiply_cited_figure_sweep_finds_no_new_disagreement` below is the general
form: it re-derives the list criterion 5 of #105 asked for on every run, rather than
trusting a table written once by hand to stay current.

**The Redis working set is MiB, not MB.** Every row of R1's measurement table
reconciles only as binary: 598.2 x 1024 x 1800 = 1,102,602,240 bytes, which is
1,051.5 MiB exactly and 1,102.6 in decimal. Record 0002 rejects `cache.t4g.small` on
a margin of 692,060 bytes, **0.0628%**, computed from this figure. Read as a decimal
unit the node fits by 4.9% and the rejection needs a different reason. A unit is not
a formatting preference here; it decides a purchase.

**`feed` is sized on the 50 ms publisher cost, not the 100 ms one.** 5.88% of a core
is #69's isolated loopback figure at a 100 ms batch. 29.96 points is the same
publisher at the 50 ms interval record 0008 chose. Both are honest numbers and
**both were tagged `measured`**, which is why the tag saved nobody: a tag says where
a number came from, not whether it still applies. `compute.md`'s reservation table
rested on the smaller one and would have under-provisioned `feed` five-fold.

These parse the documents rather than the code, the way `test_events.py`,
`test_logging.py` and `test_nomenclature.py` already do.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Everywhere a reader might meet the working-set figure. Research findings included:
#: the mislabelled table was the source every other document quoted, so pinning only
#: the downstream copies would leave the original free to reinfect them.
DOCUMENT_ROOTS = (REPO / "docs",)

#: For the *superseded-figure* check the scope is narrower — the documents that tell a
#: reader what to do today. `docs/design/research/` holds dated evidence of what was
#: true when a run happened, and a finding later superseded is still an accurate record
#: of that run; rewriting one to match a later decision destroys the evidence the
#: decision was made from. The unit check above does cover them, because a mislabelled
#: unit was never accurate at any date.
NORMATIVE_ROOTS = (
    REPO / "docs" / "design" / "cloud",
    REPO / "docs" / "design" / "decisions",
)

#: How far from the figure its conditions may sit. A table row carries them in another
#: column and a paragraph in the next sentence; demanding the same line would fail
#: prose that is explaining this exact trap correctly.
CONDITION_WINDOW = 3

#: The working set at thirty minutes, as a bare number.
WORKING_SET = "1,051.5"

#: Units that are wrong for a quantity derived from a byte count.
DECIMAL_UNITS = ("MB", "GB", "KB/s")


def cloud_documents() -> list[Path]:
    """Every markdown file that could carry these numbers."""
    found: list[Path] = []
    for root in DOCUMENT_ROOTS:
        found.extend(sorted(root.rglob("*.md")))
    return found


def test_the_redis_working_set_is_never_written_in_decimal_units() -> None:
    """A decimal unit here is 4.8% out, and 0002's margin is 0.06%.

    The failure names the file and the line, because the fix is a two-character edit
    and the reader should not have to go looking for it.
    """
    offenders: list[str] = []
    for path in cloud_documents():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if WORKING_SET not in line:
                continue
            for unit in DECIMAL_UNITS:
                if re.search(rf"{re.escape(WORKING_SET)}\s*{re.escape(unit)}\b", line):
                    offenders.append(
                        f"{path.relative_to(REPO).as_posix()}:{number} "
                        f"writes {WORKING_SET} {unit}"
                    )
    assert not offenders, (
        "the Redis working set is a binary quantity and belongs in MiB: "
        + "; ".join(offenders)
        + " — 598.2 KiB/s x 1800 s = 1,102,602,240 bytes = 1,051.5 MiB exactly, and "
        "record 0002 rejects an instance class on 0.0628% of it"
    )


def test_compute_sizes_the_feed_from_the_chosen_batch_interval() -> None:
    """`compute.md` must size `feed` on 29.96 points, and say so where it does it.

    Pinned because the two figures are five-fold apart, both are real measurements of
    the same publisher, and each is tagged `measured`. Nothing else separates them.
    """
    compute = (REPO / "docs" / "design" / "cloud" / "compute.md").read_text(
        encoding="utf-8"
    )
    assert "29.96" in compute, (
        "compute.md no longer names the 50 ms publisher cost (29.96 points of a "
        "core). If the sizing basis changed, change this test deliberately and say "
        "which record decided it"
    )
    on_the_old_figure = re.search(
        r"siz\w+\s+(?:the\s+)?`?feed`?[^.\n]{0,80}5\.88", compute
    )
    assert on_the_old_figure is None, (
        "compute.md sizes `feed` from 5.88%, which is #69's isolated publisher at a "
        "100 ms batch. Record 0008 chose 50 ms, where it costs 29.96 points"
    )


def test_the_five_88_figure_always_carries_the_interval_it_was_measured_at() -> None:
    """Wherever 5.88% survives in a normative document, its conditions travel with it.

    It is a correct measurement and deleting it would lose evidence. What made it
    dangerous was appearing without the batch interval that makes it true.

    Matched as `5.88%`, not a bare `5.88`: `redis-hosting.md` reports a p50 batch
    latency of `5.88 ms`, a different quantity that happens to share three digits. A
    test that cannot tell those apart teaches people to ignore it.
    """
    naked: list[str] = []
    for root in NORMATIVE_ROOTS:
        for path in sorted(root.rglob("*.md")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if "5.88%" not in line:
                    continue
                lo = max(0, index - CONDITION_WINDOW)
                hi = min(len(lines), index + CONDITION_WINDOW + 1)
                near = "\n".join(lines[lo:hi])
                if "100 ms" in near or "100ms" in near or "29.96" in near:
                    continue
                naked.append(f"{path.relative_to(REPO).as_posix()}:{index + 1}")
    assert not naked, (
        "5.88% of a core is the publisher at a 100 ms batch. In a document that tells "
        f"a reader what to do, the interval must appear within {CONDITION_WINDOW} "
        "lines of it, or the number invites the five-fold error #95 found: "
        + ", ".join(naked)
    )


def test_1849_8_events_per_second_is_derived_everywhere() -> None:
    """#105: one figure, `measured` in three documents and `derived` in three others.

    `research/0001-stream-naming-and-payload-format.md` §5 carries the arithmetic --
    1,537.4 book frames plus 156.2 ticker frames counted twice, from `measured`
    1,693.6 venue frames/s -- so the figure is `derived`, from #58, everywhere. It is
    never `measured`, and `lld/store-replay.md` may not attribute it to the #61 run:
    #61 is I2's batch-interval measurement (`research/0061-batch-interval.md`), whose
    own committed numbers are 1,835.1-1,854.1 events/s, phase by phase -- it never
    produced 1,849.8.
    """
    offenders: list[str] = []
    for path in cloud_documents():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "1,849.8" not in line:
                continue
            if re.search(r"`measured`[^\n]{0,40}1,849\.8", line) or re.search(
                r"1,849\.8[^\n]{0,10}\([^)]*measured[^)]*\)", line
            ):
                rel = path.relative_to(REPO).as_posix()
                offenders.append(f"{rel}:{index + 1}: {line.strip()}")
    assert not offenders, (
        "1,849.8 events/s is `derived` (from #58, see research/0001 §5), never "
        "`measured`: " + "; ".join(offenders)
    )
    attributed_to_61 = []
    for path in cloud_documents():
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "1,849.8" in line and "#61" in line:
                rel = path.relative_to(REPO).as_posix()
                attributed_to_61.append(f"{rel}:{index + 1}")
    assert not attributed_to_61, (
        "1,849.8 events/s comes from #58's arithmetic, not the #61 run (I2's "
        "batch-interval measurement, which measured 1,835.1-1,854.1 events/s and never "
        "produced 1,849.8): " + ", ".join(attributed_to_61)
    )


# --- The general sweep: criterion 5 of #105 ---------------------------------------
#
# #95's tests above are pins, written by hand after a specific defect was found. They
# do not generalise: nothing checked whether *any other* multiply-cited figure carried
# two tags until #105 went looking and found 1,849.8. This section is the mechanical
# version of that search, run on every test collection rather than by hand once.
#
# A number's tag sits either in the same table cell ("| 1,849.8 events/s | `derived` |
# #58 |") or a handful of characters away in prose ("the `measured` 1,849.8 events/s").
# Associating "nearest tag in the same cell" is precise enough to find real
# same-figure disagreements without drowning in prose that legitimately discusses two
# different tagged quantities in one sentence -- the naive "any tag on the line"
# version tried first flagged over 70 numbers, and manual review of every one with 3+
# citing files (documented in the #105 report) found exactly one real disagreement:
# 1,849.8, at the three sites the test above pins. Everything else was the tag for a
# *different* number that happened to share the line or cell.
_TAG_RE = re.compile(r"`(measured|derived|assumed|unmeasured)`")
_NUM_STRONG_RE = re.compile(
    r"(?<![\w#])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])|(?<![\w#.])\d+\.\d+(?![\w])"
)
_UNITS = (
    r"(?:ms|s|µs|us|MiB|KiB|GiB|MB|GB|KB|B/s|B|%|events/s|entries|contracts|"
    r"minutes|min)"
)
_NUM_UNIT_RE = re.compile(rf"(?<![\w#.,])\d{{2,}}(?![\w,.])(?=\s?{_UNITS}\b)")
_TAG_PROXIMITY = 20

#: Multiply-cited figures the sweep flags that are not #105's defect, each checked by
#: hand against its sites (full detail in the #105 report). Every one resolves to
#: either (a) the nearest-tag heuristic attaching a neighbour's tag to the wrong
#: number in a dense cell or sentence, or (b) a genuine open question this ticket does
#: not fix -- noted in `out_of_scope_noticed` on the issue rather than corrected here.
_KNOWN_MULTIPLY_CITED = {
    "1,000": "two different quantities collide on a round number: `assumed` ladder "
    "reads/day (durable-store cost model) vs `measured` 1,000/s engine throughput "
    "(research/0010-store-replay.md:117)",
    "0.05": "decisions/0008-topology.md:135's `derived` tags the 2.00 it computes "
    "from 0.05, not 0.05 itself (still `assumed` everywhere it is the subject)",
    "843.4": "compute.md:65 and research/0005-compute-and-region.md:70's `derived` "
    "tags the monthly-GB figure computed from 843.4, not 843.4 itself (`measured` "
    "everywhere it is the subject)",
    "30.89": "research/0007-load-profile.md:51's `derived` tags the per-service "
    "points computed from R4's 30.89%, not 30.89 itself (`measured` everywhere it is "
    "the subject)",
    "0.71": "documented and intentional: research/0007-load-profile.md §9 -- 0.52 is "
    "`derived`, I2's 0.71 is `measured`, and sizing deliberately uses the higher, "
    "measured figure inside the `derived` 0.52-0.71 range",
    "1,152": "research/0004a-prices-and-sources.md:164's `measured` tags the flush-file "
    "count basis ('the day'), not the 1,152-a-day multiplier (`derived` everywhere it "
    "is the subject)",
    "1.45": "the nearest tag in both lld/store-replay.md:187 and lld/store.md:46 "
    "belongs to the adjacent measured transit figure, not to the 1.45 multiplier",
    "40.77": "compute-numbers.md:19's `derived` tags the adjacent 29.96-point figure "
    "in the same cell, not 40.77 (`measured` everywhere it is the subject)",
    "78.07": "compute-topology.md:32's `assumed` describes the oms/strategy addition "
    "layered on top, not the $78.07 base (`derived` everywhere else)",
    "156.2": "message-bus-numbers.md:17's nearest `measured` tag belongs to the "
    "adjacent 1,693.6 frames/s figure in the same source cell, not to 156.2 "
    "(`derived` at its direct citation, research/0001:133)",
    "7.25": "docs/superpowers/specs/2026-09-07-engine-feed-management-design.md:297's "
    "`derived` tags the 58 ms product, not 7.25 itself",
    # Genuine open questions, out of scope for #105 -- see the issue comment.
    "5,511": "OPEN: storage.md:135 derives 5,511 ms as 5,001 + 510.3 (`derived`); "
    "research/0010-store-replay.md:119 calls the same 5,511 ms a `measured` max "
    "arrival lag. Not resolved here; noted out_of_scope_noticed on #105",
    "240.8": "OPEN: compute.md:189 tags 240.8 MiB `measured` sharing a cell with "
    "1,056.4 MiB; research/0007-load-profile.md:74 tags it `derived` (M3) directly. "
    "Not resolved here; noted out_of_scope_noticed on #105",
}


def _specific(token: str) -> bool:
    """Exclude round numbers that recur by coincidence (30 minutes, 50 ms, 0.5) --
    this repository's carefully measured or derived figures carry enough precision
    that two independent quantities landing on the same value is not the ordinary
    case. Four or more significant digits, or two or more fractional digits."""
    digits = token.replace(",", "").replace(".", "")
    if "." in token and len(token.split(".", 1)[1]) >= 2:
        return True
    return len(digits) >= 4


def _nearest_tag(cell: str, start: int, end: int) -> str | None:
    best_tag: str | None = None
    best_distance: int | None = None
    for match in _TAG_RE.finditer(cell):
        if match.end() <= start:
            distance = start - match.end()
        elif match.start() >= end:
            distance = match.start() - end
        else:
            distance = 0
        if best_distance is None or distance < best_distance:
            best_distance, best_tag = distance, match.group(1)
    if best_distance is not None and best_distance <= _TAG_PROXIMITY:
        return best_tag
    return None


def _multiply_cited_tag_disagreements() -> dict[str, set[str]]:
    """Every number tagged in 2+ distinct files, mapped to the set of tags it carries.

    A table row is split on `|` and a tag is matched only within a number's own cell
    (or the whole line, for prose), so that a row citing several different tagged
    quantities side by side does not cross-attribute one's tag to another.
    """
    occurrences: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path in cloud_documents():
        rel = path.relative_to(REPO).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            cells = line.split("|") if "|" in line else [line]
            for cell in cells:
                spans = list(_NUM_STRONG_RE.finditer(cell)) + list(
                    _NUM_UNIT_RE.finditer(cell)
                )
                for match in spans:
                    token = match.group(0)
                    if not _specific(token):
                        continue
                    tag = _nearest_tag(cell, match.start(), match.end())
                    if tag is not None:
                        occurrences[token][rel].add(tag)
    return {
        token: set().union(*by_file.values())
        for token, by_file in occurrences.items()
        if len(by_file) >= 2 and len(set().union(*by_file.values())) > 1
    }


def test_the_multiply_cited_figure_sweep_finds_no_new_disagreement() -> None:
    """Criterion 5 of #105: run the same check for every number, not just 1,849.8.

    Any figure cited with two different tags across 2+ documents that is not already
    triaged in `_KNOWN_MULTIPLY_CITED` fails here, naming itself so the next person
    does not have to re-run the sweep by hand.
    """
    disagreements = _multiply_cited_tag_disagreements()
    unexplained = sorted(set(disagreements) - set(_KNOWN_MULTIPLY_CITED))
    assert not unexplained, (
        "new multiply-cited figures disagree on their tag and are not triaged in "
        "_KNOWN_MULTIPLY_CITED: "
        + "; ".join(f"{tok} tags={sorted(disagreements[tok])}" for tok in unexplained)
    )
