r"""The two cloud numbers that decided something, pinned so they cannot drift back.

#95 found five documents carrying figures a later decision had superseded. Two were
load-bearing.

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
