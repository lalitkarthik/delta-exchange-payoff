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

**#112: the sweep itself had two structural defects, both making the repository look
cleaner than it is.** First, `_KNOWN_MULTIPLY_CITED` could only grow: an entry's site
could stop existing (`1.45` cited `lld/store.md:46`; #107 deleted that line five hours
after #105 wrote the entry down) and nothing noticed, because the old sweep asked "is
every disagreement explained" and never "does every explanation still describe a
disagreement." `test_no_allowlist_entry_has_gone_stale` below asks the second question.
Second, `_nearest_tag` only ever looked inside a number's own table cell, so it could
not read this repository's other numbers-table shape — `| Number | Tag | Run |`, the
one #62, #63 and #81 built by moving evidence out of design notes into siblings —
where the tag sits in a column of its own. `_column_tag` reads that column. Widening
the sweep this way surfaced nine further candidates. Eight are the column heuristic
itself attaching a row's tag to a different, incidentally-cited number in the same
"Run"/"Basis" cell rather than to the row's own subject — triaged in
`_KNOWN_MULTIPLY_CITED` below, the same as #105's cell-level false positives. The
ninth, `1.0888`, is real and open — see its entry.

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

#: This repository's *other* numbers-table shape: `| Number | Tag | Run |` puts the tag
#: in a column of its own rather than beside the figure -- `hld-evidence.md`,
#: `store-numbers.md`, `message-bus-numbers.md`, `compute-numbers.md`,
#: `data-feed-engine-numbers.md` and more are built this way, exactly because #62, #63
#: and #81 moved evidence out of design notes into siblings in this shape. A cell in
#: that shape carries nothing but the tag -- matched whole, so a cell that merely
#: *mentions* a tag among other prose (a "Tag" cell holding two tags for two different
#: figures side by side, as compute-numbers.md:19 and :21 do) stays ambiguous and is
#: left alone rather than guessed at.
_PURE_TAG_RE = re.compile(r"^\s*`(measured|derived|assumed|unmeasured)`\s*$")

#: Multiply-cited figures the sweep flags that are not a real, unresolved tagging
#: defect, each checked by hand against its sites (full detail in the #105 and #112
#: reports). Every one resolves to either (a) the nearest-tag or column-tag heuristic
#: attaching a neighbour's tag to the wrong number in a dense cell, row or sentence, or
#: (b) a genuine open question outside this ticket's territory -- noted as such below
#: and in `out_of_scope_noticed` on the issue rather than corrected here.
#:
#: `test_no_allowlist_entry_has_gone_stale` keeps this list honest going forward: a key
#: here that the sweep stops flagging fails by name, so an entry cannot outlive the
#: disagreement it explains the way `1.45` did (#112) after #107 deleted its cited site.
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
    "1.45": "in-cell: lld/store-replay.md:187 and research/0010-store-replay.md:118's "
    "nearest tag belongs to the adjacent `measured` 1,156.8 ms transit figure, not to "
    "the 1.45 multiplier. column: research/0010-store-replay.md:118 and :119's "
    "adjacent `derived` belongs to the row's own subject (2.0 s / 8.0 s grace), not to "
    "the 1.45 restated in the Basis prose. No site tags 1.45 itself (#112 re-verified "
    "after #107 deleted the entry's original site, lld/store.md:46, which is why the "
    "entry went stale and is now re-cited against sites that still exist)",
    "40.77": "compute-numbers.md:19's `derived` tags the adjacent 29.96-point figure "
    "in the same cell, not 40.77 (`measured` everywhere it is the subject)",
    "78.07": "compute-topology.md:32's `assumed` describes the oms/strategy addition "
    "layered on top, not the $78.07 base (`derived` everywhere else)",
    "156.2": "message-bus-numbers.md:17's nearest `measured` tag belongs to the "
    "adjacent 1,693.6 frames/s figure in the same source cell, not to 156.2 "
    "(`derived` at its direct citation, research/0001:133)",
    "7.25": "docs/superpowers/specs/2026-09-07-engine-feed-management-design.md:297's "
    "`derived` tags the 58 ms product, not 7.25 itself",
    # column-tag false positives, all found widening the sweep for #112: in each, the
    # adjacent Tag-column cell describes the row's own headline subject, and the
    # flagged number is only cited incidentally in that row's "Run"/"Basis"/"Source"
    # cell for a different, unrelated fact -- the same shape as the cell-level false
    # positives above, one column over.
    "0.002": "decisions/0007-load-profile.md:78's nearest `derived` belongs to the "
    "6.9 Mbit/s figure later in the same cell, not to the 0.002% aside; "
    "research/0003-controller-against-nautilus.md:75's column `measured` correctly "
    "tags its own row (quiet-gap percentiles, `../quiet-gap.md`)",
    "0.056": "compute-numbers.md:22's column `derived` belongs to the row's own "
    "2,216.5 GB/month and $165.00/month, not to the $0.056/GB AWS rate cited in the "
    "Source cell (`measured` at compute.md:66 and research/0005-compute-and-region.md:71 "
    "-- AWS's own published NAT price)",
    "0.321": "controller.md:83's column `measured` belongs to the row's own 44.785 s "
    "headline, not to the '0.321 s over 35 s' aside in the Where-from cell; "
    "quiet-gap.md:33's `derived` belongs to the 47x ratio computed from it (15 / "
    "0.321), not to 0.321 itself",
    "1,693.6": "redis-hosting.md:133's column `derived` belongs to the row's own "
    "1,051.5 MiB working-set forecast, not to the 1,693.6 frames/s cited as an input "
    "in the Source cell (`measured` at twelve other sites, e.g. hld.md §5, "
    "compute-numbers.md:17)",
    "200,000": "message-bus-numbers.md:27's column `derived` and redis-bus.md:116's "
    "in-cell `derived` both belong to the '108 s of traffic' they compute, not to the "
    "200,000-entry outbox bound reused in that arithmetic (`assumed`, its own subject "
    "at message-bus-numbers.md:26)",
    "44.785": "hld-evidence.md:14's column `assumed` belongs to the row's own "
    "`degraded_after`/grace thresholds (15 s, 30 s), not to the 'longest quiet gap "
    "44.785 s' cited as supporting evidence (`measured` at controller.md:83 and "
    "research/0003-controller-against-nautilus.md:77)",
    "5,001": "message-bus-numbers.md:17's column `derived` belongs to the row's own "
    "1,849.8 events/s, not to the 5,001 ms ticker refresh interval cited as an input "
    "in the Run cell (`measured` at six other sites, e.g. events.md:70, "
    "hld-evidence.md:11)",
    "724.8": "research/0007b-container-measurement-numbers.md:69's column `derived` "
    "belongs to the row's own 0.86x run intensity, not to the 724.8 KB/s `feed` "
    "ingress figure cited in the Arithmetic cell (`measured` at "
    "research/0007a-container-measurement.md:67)",
    # Genuine open questions.
    "1.0888": "OPEN, found widening the sweep for #112, not in that ticket: "
    "research/0007b-container-measurement-numbers.md:65 tags it `derived` ('sum of "
    "the six measured means'), while research/0007a-container-measurement.md:132, "
    "compute.md:191, decisions/0005-compute-and-region.md:191 and "
    "decisions/0008-topology.md:156 all call the same total `measured`. A real "
    "disagreement -- fixing it touches two decision records outside this ticket's "
    "territory, so it is reported, not corrected here; needs its own ticket",
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


def _column_tag(cells: list[str], cell_index: int) -> str | None:
    """The tag for a figure whose own cell carries none, read from the column beside
    it -- this repository's `| Number | Tag | Run |` shape.

    Only the two immediate neighbours are candidates, matching every site this
    ticket's evidence names: the tag column sits directly beside the figure's own,
    never a column further off. A neighbour counts only if it is *exactly* one tag
    and nothing else -- `_PURE_TAG_RE` -- so a neighbour that itself holds two tags
    for two different figures (compute-numbers.md's "measured / derived" cells) is
    not attributed to either one. If the two neighbours disagree, the tag
    is genuinely ambiguous from this row alone and is left unassigned rather than
    guessed.
    """
    candidates: set[str] = set()
    for neighbour in (cell_index - 1, cell_index + 1):
        if 0 <= neighbour < len(cells):
            match = _PURE_TAG_RE.match(cells[neighbour])
            if match:
                candidates.add(match.group(1))
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _multiply_cited_tag_disagreements() -> dict[str, set[str]]:
    """Every number tagged in 2+ distinct files, mapped to the set of tags it carries.

    A table row is split on `|` and a tag is matched first within a number's own cell
    (or the whole line, for prose), so that a row citing several different tagged
    quantities side by side does not cross-attribute one's tag to another. Failing
    that, `_column_tag` checks whether the tag instead sits in a column of its own
    beside the figure's -- #112's fix for the shape #105's sweep could not read.
    """
    occurrences: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path in cloud_documents():
        rel = path.relative_to(REPO).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            cells = line.split("|") if "|" in line else [line]
            for cell_index, cell in enumerate(cells):
                spans = list(_NUM_STRONG_RE.finditer(cell)) + list(
                    _NUM_UNIT_RE.finditer(cell)
                )
                for match in spans:
                    token = match.group(0)
                    if not _specific(token):
                        continue
                    tag = _nearest_tag(cell, match.start(), match.end())
                    if tag is None and len(cells) > 1:
                        tag = _column_tag(cells, cell_index)
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


def test_multiply_cited_numbers_doc_names_every_allowlist_entry() -> None:
    """Criterion 3 of #112: `docs/design/multiply-cited-numbers.md` is typed by hand,
    not generated from `_KNOWN_MULTIPLY_CITED` -- but it is pinned rather than left
    free to drift, the same way `store.md` §1's evidence and `store-numbers.md`'s copy
    of it are two files that have to agree. This is the pin: every allowlist key must
    be named somewhere in the doc, so adding, removing or re-keying an entry here
    without touching the readable record fails here instead of leaving the two silently
    out of step, which is exactly how #105's own copy of the allowlist (this same file)
    went stale in the first place.
    """
    doc = (REPO / "docs" / "design" / "multiply-cited-numbers.md").read_text(
        encoding="utf-8"
    )
    missing = [token for token in _KNOWN_MULTIPLY_CITED if token not in doc]
    assert not missing, (
        "these _KNOWN_MULTIPLY_CITED entries are not named anywhere in "
        "multiply-cited-numbers.md, which is supposed to be its readable copy: "
        + ", ".join(missing)
    )


def test_no_allowlist_entry_has_gone_stale() -> None:
    """#112: an allowlist that can only grow is a list of claims nobody re-checks.

    `1.45` sat in `_KNOWN_MULTIPLY_CITED` citing `lld/store.md:46`, and #107 removed
    that line five hours after #105 wrote the entry down -- nothing noticed, because
    the old test only ever asked "is every disagreement explained," never "does every
    explanation still describe a disagreement." A key the sweep no longer flags is a
    claim about the documents that the documents no longer support, and it fails here
    by name instead of rotting silently.
    """
    disagreements = _multiply_cited_tag_disagreements()
    stale = sorted(set(_KNOWN_MULTIPLY_CITED) - set(disagreements))
    assert not stale, (
        "these _KNOWN_MULTIPLY_CITED entries no longer correspond to any disagreement "
        "the sweep finds -- the site(s) they cite have been edited since the entry was "
        "written, so the entry is now a claim about the documents that is not true. "
        "Re-verify by hand and either remove the entry or update it to cite where the "
        "disagreement actually lives now: " + ", ".join(stale)
    )
