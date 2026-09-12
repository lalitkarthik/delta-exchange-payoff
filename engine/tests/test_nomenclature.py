r"""`docs/design/cloud/nomenclature.md`: the store start id, and its stream list.

#94: `nomenclature.md`'s "Start id, `store`" row said `0` — take everything Redis still
holds — while `redis-hosting.md` said the opposite, never `0`, for the same consumer
group. [0010](../decisions/0010-store-replay.md) settles it: `store` starts from its
checkpoint, or the head taken once as a concrete id on first start, and never `0`.
Starting a group at `0` re-reads the whole retained window and folds it into bars that
already exist — #84's defect at the scale of the retention window rather than a seam.

This module pins four things so no document can drift back:

1. The documented `store` start id is checkpoint-or-`$`, never `0`, and names the record
   that settled it.
2. `nomenclature.md` §1's stream list agrees with `events.md`, which its own header says
   wins on names — keyed off headings and table rows rather than a line number, the same
   convention `test_events.py` and `test_logging.py` use.

3. Record 0010 R5 says **when** the replay-gap check runs, and `store_main.py`
   actually runs it there. #106: this is the one check here that reads the code. In
   #103 R5 and `message-bus.md` A5 agreed with each other and both were wrong about
   the code, and no document-against-document check can catch that.
4. The provenance vocabulary stays closed at the three words `CONTEXT.md` defines.
   #106: a fourth had appeared in three phrasings and `reconnect_after` ended up
   carrying `assumed` and its opposite two table rows apart.

Nothing here touches a socket, a clock, or any disk beyond these documents and one
source file, which is read as text and never imported.
"""

from __future__ import annotations

import ast
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


# --------------------------------------------------------------------------------------
# #106 --- checks a consistency check between two documents structurally cannot make.
#
# In #103 `message-bus.md` A5 and record 0010 R5 agreed with each other and **both were
# wrong about the code**: the replay-gap check ran once, inside `_prepare_process`, and a
# store that never restarts never ran it. Every test above pins one document against
# another, and not one of them could have caught that. The three below read
# `store_main.py` itself, and the last asks the question that actually failed: does the
# record claim what the code does?
# --------------------------------------------------------------------------------------

STORE_MAIN = REPO / "engine" / "src" / "deltapayoff" / "store_main.py"
DECISION_0010 = REPO / "docs" / "design" / "decisions" / "0010-store-replay.md"
CONTEXT_DOC = REPO / "CONTEXT.md"

#: The loop that gives the check its cadence. Reachability is measured from here and not
#: from `poll_bus`, because a `poll_bus` nothing calls on a timer is start-up again.
PERIODIC_ENTRY_POINT = "monitor_bus_forever"

#: The counter #103 found reporting `0` through the exact condition it was built to
#: report. An assignment to it is the check having an opinion.
REPLAY_GAP_COUNTER = "replay_gap_entries"


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every `def` in the module by name, nested ones included."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _called_names(node: ast.AST) -> set[str]:
    """Names this function calls, whether `f()` or `x.f()`.

    Deliberately coarse: it over-approximates the call graph, so this check can only ever
    be too generous. One that under-approximated would fail on a rename rather than on
    the defect, and nobody keeps a test like that.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _writes_the_replay_gap_counter(node: ast.AST) -> bool:
    """Does this function assign to something's `replay_gap_entries`?"""
    for child in ast.walk(node):
        if isinstance(child, ast.AugAssign):
            targets: list[ast.expr] = [child.target]
        elif isinstance(child, ast.Assign):
            targets = list(child.targets)
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == REPLAY_GAP_COUNTER:
                return True
    return False


def periodic_replay_gap_counters(source: str) -> set[str]:
    """The functions that count a replay gap **and are reachable from the timer**.

    Empty means the count happens only where start-up can reach it -- the state
    `store_main.py` was in until #103, and the state R5's silence permitted. Takes the
    source as a string rather than reading the file, so the red run can be shown against
    the tree as it stood before #103 landed.
    """
    tree = ast.parse(source)
    functions = _functions(tree)
    if PERIODIC_ENTRY_POINT not in functions:
        return set()
    reached: set[str] = set()
    pending = [PERIODIC_ENTRY_POINT]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        node = functions.get(name)
        if node is not None:
            pending.extend(_called_names(node))
    return {
        name
        for name in reached
        if name in functions and _writes_the_replay_gap_counter(functions[name])
    }


def _r5_text() -> str:
    """R5's own paragraph, and not R5a's -- the criterion is that **R5** says it."""
    text = DECISION_0010.read_text(encoding="utf-8")
    match = re.search(
        r"^\*\*R5 [—-] .*?(?=\n[ \t]*\n)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, (
        f"no R5 paragraph found in {DECISION_0010}; the '**R5 -- ' opening this test "
        "keys off has changed"
    )
    return match.group(0)


CONTINUOUS = re.compile(r"every poll|continuous(ly)?", flags=re.IGNORECASE)


def test_the_replay_gap_check_is_reachable_from_the_timer_in_the_code() -> None:
    """#103's fifth change, pinned against the source rather than against a document."""
    counters = periodic_replay_gap_counters(STORE_MAIN.read_text(encoding="utf-8"))
    assert counters, (
        f"nothing reachable from {PERIODIC_ENTRY_POINT!r} in {STORE_MAIN} writes "
        f"{REPLAY_GAP_COUNTER!r} -- the replay-gap check has gone back to running only "
        "where start-up can reach it. A store that never restarts never evaluates it, "
        "and /health reported 0 for two hours through a 97-minute hole on 2026-09-12"
    )


def test_r5_says_when_the_check_runs() -> None:
    """R5's defect was silence, not error. It never said when the check ran, and
    `message-bus.md` A5 inherited 'at start-up only' from that silence."""
    text = _r5_text()
    assert CONTINUOUS.search(text), (
        f"R5 does not say when the check runs:\n{text}\n"
        "A reader who cannot tell from R5 will write 'at start-up only' into the next "
        "document, which is what happened"
    )


def test_r5_and_the_code_agree_on_when_the_check_runs() -> None:
    """**The check this ticket exists for.** Two documents that agree with each other
    prove nothing about the code; this one fails if either side moves without the
    other."""
    record_says_continuous = bool(CONTINUOUS.search(_r5_text()))
    code_is_continuous = bool(
        periodic_replay_gap_counters(STORE_MAIN.read_text(encoding="utf-8"))
    )
    assert record_says_continuous == code_is_continuous, (
        f"record 0010 R5 says the check is continuous: {record_says_continuous}; "
        f"{STORE_MAIN.name} counts a replay gap off the timer: {code_is_continuous}. "
        "One side moved without the other, which is #103 exactly"
    )


# --------------------------------------------------------------------------------------
# #106 --- the provenance vocabulary is closed at three words.
# --------------------------------------------------------------------------------------

#: The fourth word, standing alone. AWS's `supported-extension` list is its own term of
#: art and is not this, so a neighbouring hyphen is not a match.
FOURTH_WORD = re.compile(r"(?<![\w-])supported(?![\w-])", flags=re.IGNORECASE)

#: A line is about provenance when it carries one of the three words CONTEXT.md defines,
#: or the negation of one.
PROVENANCE_WORD = re.compile(
    r"(?<!\w)(un)?(measured|assumed|derived)(?!\w)", flags=re.IGNORECASE
)


def provenance_documents() -> list[Path]:
    """Every document a number can be tagged in.

    `CONTEXT.md` is left out on purpose: it is the dictionary, and the entry that refuses
    a word has to be allowed to write it. `test_context_refuses_the_fourth_word` pins
    that entry instead.
    """
    return sorted((REPO / "docs").rglob("*.md"))


def test_the_fourth_provenance_word_is_not_used_as_a_tag() -> None:
    """`reconnect_after` carried two opposite tags two rows apart because a fourth word
    was available. A tag says **where a number came from**; a later run that did not
    contradict an `assumed` value did not change where it came from. Corroboration goes
    in the evidence and names the run."""
    offenders: list[str] = []
    for doc in provenance_documents():
        for number, line in enumerate(
            doc.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if FOURTH_WORD.search(line) and PROVENANCE_WORD.search(line):
                offenders.append(f"{doc.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "a fourth provenance word is being used as a tag, which is how "
        "`reconnect_after` came to carry `assumed` and its opposite two rows apart:\n"
        + "\n".join(offenders)
        + "\nWrite the tag, then say which run bounds it."
    )


def test_context_refuses_the_fourth_word() -> None:
    """The ban is only real while the dictionary carries it, with the reason. Forbidding
    a word in a test and not in `CONTEXT.md` is the half-defined state #106 ends."""
    text = CONTEXT_DOC.read_text(encoding="utf-8")
    refusals = re.search(
        r"^## 7\. Words this repository refuses\s*$(.*)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert refusals is not None, (
        f"no 'Words this repository refuses' section in {CONTEXT_DOC}"
    )
    assert FOURTH_WORD.search(refusals.group(1)), (
        f"{CONTEXT_DOC}'s refusal list does not name the fourth provenance word"
    )
