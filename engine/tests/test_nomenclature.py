r"""`docs/design/cloud/nomenclature.md`: the store start id, and its stream list.

#94: `nomenclature.md`'s "Start id, `store`" row said `0` — take everything Redis still
holds — while `redis-hosting.md` said the opposite, never `0`, for the same consumer
group. [0010](../decisions/0010-store-replay.md) settles it: `store` starts from its
checkpoint, or the head taken once as a concrete id on first start, and never `0`.
Starting a group at `0` re-reads the whole retained window and folds it into bars that
already exist — #84's defect at the scale of the retention window rather than a seam.

This module pins two things so neither document can drift back:

1. The documented `store` start id is checkpoint-or-`$`, never `0`, and names the record
   that settled it.
2. `nomenclature.md` §1's stream list agrees with `events.md`, which its own header says
   wins on names — keyed off headings and table rows rather than a line number, the same
   convention `test_events.py` and `test_logging.py` use.

Nothing here touches a socket, a disk beyond these two documents, or a clock.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NOMENCLATURE_DOC = REPO / "docs" / "design" / "cloud" / "nomenclature.md"
EVENTS_DOC = REPO / "docs" / "design" / "events.md"


def _store_start_id_row() -> str:
    """The "Start id, `store`" table row's rule cell, as written in the document."""
    text = NOMENCLATURE_DOC.read_text(encoding="utf-8")
    match = re.search(
        r"^\|\s*Start id, `store`\s*\|\s*(.*?)\s*\|\s*.*\|\s*$",
        text,
        flags=re.MULTILINE,
    )
    assert match is not None, (
        f"no 'Start id, `store`' row found in {NOMENCLATURE_DOC}; "
        "the table shape this test keys off has changed"
    )
    return match.group(1)


def documented_stream_events() -> set[str]:
    """The event names named in §1's stream table, read out of its own rows.

    Restricted to the section between the "### The ten" heading and the next `###` or
    `---`, so a code span quoted anywhere else in the document (an example, a cross
    reference) is not mistaken for a catalogued stream.
    """
    text = NOMENCLATURE_DOC.read_text(encoding="utf-8")
    section = re.search(
        r"^###\s+The ten\s*$(.*?)(?=^###\s|^---)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section is not None, (
        f"no '### The ten' section found in {NOMENCLATURE_DOC}; "
        "the heading this test keys off has changed"
    )
    return set(
        re.findall(
            r"^\|\s*`([a-z][a-z0-9_.]*)`\s*\|",
            section.group(1),
            flags=re.MULTILINE,
        )
    )


def documented_event_types() -> set[str]:
    """The catalogue's own section headings — `test_events.py`'s own helper, duplicated
    rather than imported, so this module stays a pure document check with no dependency
    on the package under test."""
    text = EVENTS_DOC.read_text(encoding="utf-8")
    return set(re.findall(r"^###\s+`([^`]+)`", text, flags=re.MULTILINE))


def test_store_start_id_is_checkpoint_or_head_and_never_zero() -> None:
    """The rule cell must not be, or start with, the bare `0` id #94 found."""
    rule = _store_start_id_row()
    assert not re.match(r"^`0`", rule), (
        f"nomenclature.md's store start id reads {rule!r} — this is #94's defect: "
        "starting the store consumer group at `0` replays everything Redis still "
        "holds, refolding a retention window's worth of events into bars that "
        "already exist"
    )
    assert "never `0`" in rule or "never 0" in rule.lower()
    assert "checkpoint" in rule


def test_store_start_id_names_the_record_that_settled_it() -> None:
    """0010 decided this, and supersedes 0002 on this point — both must be named so a
    reader who lands on this row is not left to take it on faith."""
    rule = _store_start_id_row()
    assert "0010-store-replay.md" in rule
    assert "supersedes" in rule.lower()
    assert "0002-redis-hosting.md" in rule


def test_nomenclature_streams_match_the_events_catalogue() -> None:
    """`nomenclature.md`'s own header says `events.md` wins on names — #94 found
    `nomenclature.md` one stream short (`store.state` missing) of the catalogue it
    defers to. This must hold for any future addition too."""
    documented = documented_event_types()
    assert len(documented) == 10, (
        f"parsed {len(documented)} type headings out of {EVENTS_DOC}; "
        "the heading shape this test keys off has changed"
    )
    streams = documented_stream_events()
    assert streams == documented, (
        f"nomenclature.md §1 names {sorted(streams)} but events.md names "
        f"{sorted(documented)} — nomenclature.md must list the same streams as the "
        "catalogue it defers to, or say why one is excluded"
    )
