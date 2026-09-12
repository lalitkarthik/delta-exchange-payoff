r"""The 200-line documentation bound, and `AGENTS.md`'s inventory of what breaks it.

`AGENTS.md` holds every note under `docs/`, plus itself and `CLAUDE.md`, to **200 lines**,
and lists the files that are over with their measured counts. That list is the thing this
module pins.

**Why a test and not a convention.** The list was wrong when #88 was filed: `AGENTS.md`
named exactly one over-bound document while fifteen were at or over the bound, and nine
of those were named nowhere at all. A list nobody checks decays into a claim, and this
repository's recurring failure is a claim that reads true and is not. So the list is
parsed out of the document and compared against `wc -l`, and a file that grows past the
bound without being written down fails the suite on the commit that grew it.

The inventory is **paths and counts**, not prose: the reasons beside each entry are for a
reader, and rewording one must not fail a test. Only the two fenced blocks are parsed.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENTS = REPO / "AGENTS.md"

#: The bound itself. A file at exactly 200 lines is over it, which is why
#: `0009-compaction-cadence.md` appears in the inventory at 200.
BOUND = 200

def documents() -> list[Path]:
    """Every file the line bound applies to, as absolute paths.

    Package `README.md` files are deliberately outside the rule — `AGENTS.md` says so in
    the same bullet, and their accuracy is tracked in `CLAUDE.md`'s "Known drift" rather
    than their length.
    """
    found = sorted(REPO.joinpath("docs").rglob("*.md"))
    return [AGENTS, REPO / "CLAUDE.md", *found]


def line_count(path: Path) -> int:
    """`wc -l`, counting the same way the shell does.

    Read as bytes and split on `\n` so a CRLF working tree and an LF one give the same
    answer; a trailing newline does not add a line.
    """
    text = path.read_bytes().decode("utf-8")
    return len(text.splitlines())


#: A row of the inventory: a count, whitespace, then a repository-relative path. Anything
#: after the path is commentary for a reader and is not parsed.
_ROW = re.compile(r"^\s*(\d+)\s+(\S+\.md)\b")


def inventory() -> dict[str, int]:
    """`AGENTS.md`'s own list of over-bound files, as path to claimed count."""
    rows: dict[str, int] = {}
    inside = False
    for line in AGENTS.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if not inside:
            continue
        match = _ROW.match(line)
        if match is not None:
            rows[match.group(2)] = int(match.group(1))
    return rows


def over_the_bound() -> dict[str, int]:
    """Every document at or over the bound right now, as path to measured count."""
    return {
        path.relative_to(REPO).as_posix(): line_count(path)
        for path in documents()
        if line_count(path) >= BOUND
    }


def test_agents_md_lists_every_document_that_is_over_the_bound() -> None:
    """A file that grows past 200 lines fails here until it is written down.

    The failure names the file, so the reader is told what to do next: split it, trim it,
    or add it to the inventory with the reason it is exempt.
    """
    measured = over_the_bound()
    claimed = inventory()
    missing = sorted(set(measured) - set(claimed))
    assert not missing, (
        "these documents are at or over the "
        f"{BOUND}-line bound and AGENTS.md does not list them: "
        + ", ".join(f"{path} ({measured[path]})" for path in missing)
        + " — split it, move its evidence to a sibling, or add it to the inventory "
        "with the reason it is exempt"
    )


def test_the_inventory_names_nothing_that_has_come_back_under_the_bound() -> None:
    """The other direction: a file that was split stops being listed.

    Without this the inventory only ever grows, and a reader cannot tell which entries
    still describe the tree. `logging.md` is the worked example — #63 split it from 213
    into 92 plus a sibling, and it is named in the prose as no longer over, not in the
    list.
    """
    measured = over_the_bound()
    stale = sorted(set(inventory()) - set(measured))
    assert not stale, (
        "AGENTS.md lists these as over the bound and they are not: "
        + ", ".join(stale)
        + " — remove them from the inventory"
    )


def test_every_claimed_count_matches_wc_l() -> None:
    """The numbers are `measured`, so they are measured here.

    A count that drifts is worse than no count: it is a number a reader would quote.
    """
    measured = over_the_bound()
    wrong = {
        path: (claimed, measured[path])
        for path, claimed in inventory().items()
        if path in measured and claimed != measured[path]
    }
    assert not wrong, (
        "AGENTS.md's line counts have drifted from `wc -l`: "
        + ", ".join(
            f"{path} says {claimed}, is {actual}"
            for path, (claimed, actual) in sorted(wrong.items())
        )
    )
