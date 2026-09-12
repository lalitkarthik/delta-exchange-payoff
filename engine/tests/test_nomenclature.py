r"""`docs/design/cloud/nomenclature.md`: the store start id, and its stream list.

#94: `nomenclature.md`'s "Start id, `store`" row said `0` — take everything Redis still
holds — while `redis-hosting.md` said the opposite, never `0`, for the same consumer
group. [0010](../decisions/0010-store-replay.md) settles it: `store` starts from its
checkpoint, or the head taken once as a concrete id on first start, and never `0`.
Starting a group at `0` re-reads the whole retained window and folds it into bars that
already exist — #84's defect at the scale of the retention window rather than a seam.

This module pins six things so no document, and no change to the code, can drift back:

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
5. `CONTEXT.md` §7's refusals hold over the engine source, not only over `docs/`. #114:
   five of the six were pinned by nothing, and the one violated at nineteen sites was
   violated twice in the code that a document-only scan cannot see.
6. **The timer does not merely reach the replay-gap counter — it moves it.** #117: the
   check at 3 above is a call graph, and a call graph cannot tell a counter that is
   reachable from one that is reached. The mutation table above that test says exactly
   which check catches which change.

Every check here but the last reads text: documents, and source files read as text and
never imported. **The last one runs the store** over `fakeredis`, because the question
it asks cannot be answered by reading. It belongs beside its siblings in
`test_store_health.py` and is here only because #114 and #117 were one worktree; move it
when nothing else holds that file.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path

import pytest

from deltapayoff import store_main

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
    """#103's fifth change, pinned against the source rather than against a document.

    **This asserts wiring and nothing else, and #117 corrected its failure message for
    claiming otherwise.** It said "a store that never restarts never evaluates it" --
    a statement about running code that a call graph cannot make. Whether the counter
    *moves* is `test_the_timer_itself_moves_the_replay_gap_counter` below, and the
    mutation table there says which of the two catches what.
    """
    counters = periodic_replay_gap_counters(STORE_MAIN.read_text(encoding="utf-8"))
    assert counters, (
        f"nothing reachable from {PERIODIC_ENTRY_POINT!r} in {STORE_MAIN} writes "
        f"{REPLAY_GAP_COUNTER!r} -- the replay-gap check is **wired** only where "
        "start-up can reach it. This says nothing about whether it is evaluated; it "
        "says the call graph no longer joins the timer to the counter at all, which is "
        "the state the module was in while /health reported 0 for two hours through a "
        "97-minute hole on 2026-09-12"
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
    # #114 extended this from `docs/` to the engine source. #106 pinned the word over
    # documents alone, and #114 found the neighbouring refusal -- "lifetime budget" --
    # violated twice in the source and not once by a document that a `docs/`-only scan
    # could see. Green on the day it was extended: no source line carries both words.
    sources = [path for path in refusal_corpus() if path.suffix == ".py"]
    for doc in provenance_documents() + sources:
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


# --------------------------------------------------------------------------------------
# #114 --- `CONTEXT.md` section 7's refusals, pinned over the engine source as well as the
# documents.
#
# Section 7 refuses six phrasings. Before this ticket exactly one of the six -- item 6,
# "supported" as a tag -- was pinned at all, and only over `docs/`. **A rule written in
# the dictionary and pinned by nothing decays at the rate the repository is edited**:
# record 0003 said of item 3 that "the word is corrected wherever it appears" on
# 2026-09-09, and on 2026-09-12 it was still written at nineteen sites -- four lines above
# the note recording the correction, in the very LLD 0003 names as changed.
#
# Where each of the six now stands. **Three are held by a test and three are held by a
# reader**, and saying which is the point: a scan that claims to hold a rule it cannot is
# worse than no scan.
#
# **1. "save", "write" or "persist" for a store flush -- a reader.** `persist` is the
# right word 33 times over, for Redis's own persistence policy, and writing is the
# store's actual job. The refusal is about one referent and no pattern separates the
# referents.
#
# **2. "healthy" for a connection -- a test, over the source only.** `healthy_literals`
# below: no engine string **is** the word, which is what `lld/supervisor.md:122` and
# `cloud/controller-policies.md:193` already promise. The 15 prose uses in the source
# and 64 in `docs/` are the named failure ("the healthy-connection-zero-messages
# failure") and Docker's own `Up (healthy)`, so the prose half stays a reader's.
#
# **3. "lifetime budget" -- a test, over documents, source and tests.** `lifetime`
# beside `budget`, with six exemptions named by sentence. This ticket's own subject.
#
# **4. "forward-fill" near a bar -- a reader.** Every one of the 40-odd uses *is* the
# refusal, in a dozen grammars: "never forward-fill", "aggregation is not
# forward-filling", "a forward-fill invents events that did not". A scan needs a
# hand-kept allowlist of all 40, which decays faster than the rule it guards.
#
# **5. `0` for absent -- a reader.** It refuses a **value**, not a word.
# `replay_gap_entries: 0` was a correct integer; what was wrong was that nothing had
# evaluated it. No text scan reaches that.
#
# **6. "supported" as a tag -- a test, over `docs/` and the engine source.** #106 built
# it over `docs/`; #114 extended the corpus, and it was green on extension.
#
# Item 5 is the one to notice: **it is the refusal #103 was about, and it is the one no
# scan can hold.** That is the same boundary #117 draws around the AST check -- a static
# reader sees words and shapes, never what a running program evaluated.
# --------------------------------------------------------------------------------------

#: `docs/design/research/` holds dated evidence of what was believed on a date and
#: `docs/superpowers/specs/` holds the specs written before the work. Neither is corrected
#: when the vocabulary moves and neither tells a reader what to do today, so neither is in
#: scope -- the same exclusion `test_cloud_numbers.py` makes of `research/`.
NON_NORMATIVE_DOC_DIRS = ("design/research", "superpowers")

ENGINE_SOURCE = REPO / "engine" / "src" / "deltapayoff"
ENGINE_TESTS = REPO / "engine" / "tests"


def normative_documents() -> list[Path]:
    """Every document that says what to do today."""
    return [
        path
        for path in sorted((REPO / "docs").rglob("*.md"))
        if not any(
            path.relative_to(REPO / "docs").as_posix().startswith(directory)
            for directory in NON_NORMATIVE_DOC_DIRS
        )
    ]


def refusal_corpus() -> list[Path]:
    """Documents **and** code.

    #114's whole point: two of the thirteen sites were in the engine source, including the
    docstring on the constant itself, and a check that reads only `docs/` cannot see
    either. `test_composition.py:427` already walks the source line by line for the
    venue's vocabulary escaping the adapter; this is the same shape.
    """
    return [
        path
        for path in (
            normative_documents()
            + sorted(ENGINE_SOURCE.rglob("*.py"))
            + sorted(ENGINE_TESTS.rglob("*.py"))
        )
        # This module is left out for the reason `provenance_documents` leaves out
        # `CONTEXT.md`: the check that refuses a phrase has to be able to write it.
        if path != Path(__file__).resolve()
    ]


#: Section 7 item 3. The budget counts **consecutive** failures: `message_arrived` sets
#: `_budget_spent` back to zero, so the word is wrong wherever it describes this
#: repository's own reconnect budget.
REFUSED_LIFETIME = re.compile(r"(?<!\w)lifetime(?!\w)", flags=re.IGNORECASE)
BUDGET_WORD = re.compile(r"(?<!\w)budget(?!\w)", flags=re.IGNORECASE)

#: The uses that are **correct and must stay**, each named by the sentence that makes it
#: correct rather than by a line number, so the exemption moves with the text and dies
#: with it. A kept use is either somebody else's bug, the word quoted as the one that was
#: corrected, or a different quantity that really is a lifetime count.
KEPT_LIFETIME_USES: tuple[tuple[str, str, str], ...] = (
    (
        "docs/architecture.md",
        "silently exhausts a lifetime budget and never returns",
        "OpenAlgo's bug, which is the argument for not having one",
    ),
    (
        "docs/design/decisions/0003-controller-policy.md",
        "never been a **lifetime** budget",
        "the correction itself; it has to be able to write the word it corrects",
    ),
    (
        "docs/design/lld/reconnect.md",
        "Called *lifetime* until #59",
        "the parameter table naming the word that was corrected",
    ),
    (
        "docs/design/lld/reconnect.md",
        "lifetime budget and never coming back",
        "OpenAlgo again, in the LLD",
    ),
    (
        "engine/src/deltapayoff/adapters/delta_socket.py",
        "silently exhausts a lifetime budget and never comes back",
        "OpenAlgo again, in the code",
    ),
    (
        "engine/src/deltapayoff/redis_bus.py",
        "The lifetime count is what remembers",
        "a bus reader's `retries_total`, which really is a lifetime count",
    ),
)


def _lifetime_budget_offenders() -> tuple[list[str], set[tuple[str, str]]]:
    """Every line writing `lifetime` of a budget, and which exemptions were used."""
    offenders: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in refusal_corpus():
        relative = path.relative_to(REPO).as_posix()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not (REFUSED_LIFETIME.search(line) and BUDGET_WORD.search(line)):
                continue
            exemption = next(
                (
                    (owner, marker)
                    for owner, marker, _ in KEPT_LIFETIME_USES
                    if owner == relative and marker in line
                ),
                None,
            )
            if exemption is not None:
                used.add(exemption)
                continue
            offenders.append(f"{relative}:{number}: {line.strip()}")
    return offenders, used


def test_the_reconnect_budget_is_never_called_a_lifetime_budget() -> None:
    """**Section 7 item 3, over the source as well as the documents.**

    `CONTEXT.md:163` and record 0003 both say the budget counts consecutive failures, and
    the code agrees: `_budget_spent` rises by one per drop in `_spend_reconnect` and is
    set back to zero by `message_arrived` and by `resume`. Every sentence calling it a
    lifetime budget states the opposite of what the constant does -- `controller.py`'s own
    docstring on `RECONNECT_BUDGET` said both halves inside one sentence.
    """
    offenders, _ = _lifetime_budget_offenders()
    assert not offenders, (
        f"the reconnect budget is called a lifetime budget at {len(offenders)} sites; "
        "it counts **consecutive** failures, because `message_arrived` sets the spend "
        "back to zero:\n" + "\n".join(offenders)
    )


def test_every_kept_lifetime_use_still_exists() -> None:
    """An exemption nobody needs is a hole nobody is watching.

    Each one names a sentence; when that sentence goes, the exemption goes with it rather
    than quietly widening the check that depends on it.
    """
    _, used = _lifetime_budget_offenders()
    stale = [
        f"{owner}: {marker!r} ({why})"
        for owner, marker, why in KEPT_LIFETIME_USES
        if (owner, marker) not in used
    ]
    assert not stale, (
        "these exemptions matched nothing, so they widen "
        "`test_the_reconnect_budget_is_never_called_a_lifetime_budget` for no reason:\n"
        + "\n".join(stale)
    )


#: Section 7 item 2, in the half a machine can hold: a **field** that says `healthy`.
#: `lld/supervisor.md:122` and `cloud/controller-policies.md:193` both promise "no field
#: says `healthy`", and nothing was holding them to it.
REFUSED_HEALTHY_VALUE = "healthy"


def healthy_literals(source: str) -> list[int]:
    """Line numbers where a string constant **is** the word `healthy`.

    A string that merely contains it is prose -- every one of the fifteen in the engine
    source names "the healthy-connection-zero-messages failure" or quotes Docker's
    `Up (healthy)`, and both are correct. A string that *equals* it is a value on a wire:
    a status, a field name, a badge. Parsed rather than grepped for exactly that reason,
    and taking the source as a string so a red run can be shown without editing a file.
    """
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().lower() == REFUSED_HEALTHY_VALUE
    )


def test_no_engine_field_or_value_says_healthy() -> None:
    """**Section 7 item 2.**

    `/health` reports a state; whether that state is acceptable is a judgement, and a
    field called `healthy` makes the engine assert the judgement on the reader's behalf.
    #108 is the live instance of what that costs: `"status":"ok"` in the same object as
    `"feed":"stopped"`, `"budget_remaining":0` and a 583-second-old last message, with
    Docker reading `Up (healthy)` throughout.
    """
    offenders = [
        f"{path.relative_to(REPO).as_posix()}:{number}"
        for path in sorted(ENGINE_SOURCE.rglob("*.py"))
        for number in healthy_literals(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "the engine emits the string `healthy`, which `lld/supervisor.md:122` and "
        "`cloud/controller-policies.md:193` both promise it never does -- a state is "
        "reported, and whether it is acceptable is the reader's judgement:\n"
        + "\n".join(offenders)
    )


#: Each refused phrasing, as section 7 numbers them, with the text that names it in its
#: own item. The dictionary is the only place all six are held at once.
SECTION_7_REFUSALS: tuple[tuple[int, str, str], ...] = (
    (1, "save/write/persist, for a store flush", '"Save", "write" or "persist"'),
    (2, "healthy, of a connection", '"Healthy"'),
    (3, "lifetime budget", '"Lifetime budget"'),
    (4, "forward-fill, near a bar", '"Forward-fill"'),
    (5, "`0` for absent", "`0` for absent"),
    (6, "supported, as a tag", '"Supported" as a tag'),
)


def _section_7() -> str:
    text = CONTEXT_DOC.read_text(encoding="utf-8")
    refusals = re.search(
        r"^## 7\. Words this repository refuses\s*$(.*)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert refusals is not None, (
        f"no 'Words this repository refuses' section in {CONTEXT_DOC}"
    )
    return refusals.group(1)


def test_context_still_refuses_all_six_phrasings() -> None:
    """**The one pin all six share.**

    Three of the six are held by a scan and three only by a reader; a reader can only hold
    a rule that is still written down, and an item quietly dropped from section 7 would
    take its refusal with it and fail nothing at all.
    """
    section = _section_7()
    missing = [
        f"{number}. {name}"
        for number, name, marker in SECTION_7_REFUSALS
        if marker not in section
    ]
    assert not missing, (
        f"{CONTEXT_DOC.name} section 7 no longer names these refusals:\n"
        + "\n".join(missing)
        + "\nThree of the six are pinned by a test; all six are pinned by this list."
    )


# --------------------------------------------------------------------------------------
# #117 --- what the AST check above proves, and the one thing nothing else asserts.
#
# **The audit's question was "construct a change it would miss", and it constructed two.**
# Both were re-run here against every check that claims to hold R5, not only against
# `periodic_replay_gap_counters`, and the result is not the one the ticket expected:
#
# | Mutation of `store_main.py` | AST check | `test_store_health.py` | the test below |
# |---|---|---|---|
# | A counter behind a never-true condition | survives | **caught** | **caught** |
# | B R5's `alert` deleted | survives | **caught** | **caught** |
# | C `_note_replay_gaps` off `poll_bus` | **caught** | **caught** | **caught** |
# | D the timer loop no longer calls `poll_bus` | **caught** | survives | **caught** |
# | E R5's `store.replay_gap` log record deleted | survives | survives | **caught** |
# | F `_note_lag` not called from `poll_bus` | survives | **caught** | survives |
# | G `poll_bus` behind a never-true condition | survives | survives | **caught** |
#
# `measured` 2026-09-12 in worktree `dxp-114`, one mutation at a time against a restored
# tree; the runs are in #117's report.
#
# **So the ticket's premise is half wrong and the half it gets right is the important
# half.** A and B do *not* pass with the suite green -- #103 landed
# `test_a_watermark_trimmed_past_is_counted_and_alerted_without_a_restart`, which drives
# the real composition over `fakeredis` and asserts the counter moves and the alert is
# published, and it catches both. The behavioural test #117 asks for already exists, and
# it was written the day the AST check was.
#
# **Two things genuinely had nothing behind them: E and G.**
#
# E is R5's third obligation. `decisions/0010-store-replay.md:45-47` asks for three
# things -- an `alert`, an error-level `store.replay_gap` record with both bounds and an
# exact count, and a counter. The alert and the counter were held; **the record could be
# deleted outright with every check in this repository green**, including the behavioural
# one. It is asserted below, off the same pass, for the cost of a `caplog` block.
#
# **G is the one that matters**, and G is A's shape moved one frame out.
# The AST check says the timer *can* reach the counter. The existing behavioural test
# says the counter moves *when `poll_bus` is called*. **Nothing asserted that the timer
# calls it**, and a `poll_bus` guarded inside `monitor_bus_forever` by a condition that
# is never true satisfies both -- reachable, and never reached, which is #103's shape
# exactly, one level up. That is the one test worth adding, and it is the test below.
#
# **Why #106 reached for AST**, which the ticket asks. Not instead of behaviour -- #103
# had landed the behavioural test hours earlier, and #106 could see it. It reached for
# AST because the question it was asked was a different one: R5 the *record* said the
# check was continuous, and the ticket was whether the record and the code agreed. That
# is a claim about the shape of `store_main.py`, it is answerable without Redis, it runs
# on every collection in milliseconds, and `test_r5_and_the_code_agree_on_when_the_check
# _runs` fails if **either side** moves. No behavioural test expresses that: a test that
# drives the store cannot notice that a decision record now says something else.
#
# **The route chosen, of the three the ticket offers.** Not (1): strengthening the AST
# check to catch A is a promise a static reader cannot keep. `replay_gap_alerted is None`
# is never true because of a type declared thirty lines away; the next mutation would
# hide behind a constant, a flag read from the environment, or a comparison that is false
# only at run time. Every rule added buys one mutation and costs the reader their sense of
# what the check refuses -- and **a static check that claims to catch evaluation is worse
# than one that admits it cannot**, because the claim is what stops the next person
# writing the test that would.
#
# So: (2) for the gap, (3) for the check. The AST check stays exactly as it is -- it is
# the only thing that catches D, it costs nothing, and it needs no Redis. What changes is
# that `test_the_replay_gap_check_is_reachable_from_the_timer_in_the_code`'s failure
# message no longer claims evaluation: it said "a store that never restarts never
# evaluates it", which is a statement about running code that a call graph cannot make.
#
# **`_note_lag`, which #117 asks about.** The same question applies and the answer is
# better than the ticket expects: `grep -n "_note_lag" engine/tests/` returns no match,
# but F above is caught -- `test_store_health.py::
# test_a_lossless_consumer_past_the_threshold_raises_an_alert` asserts its **effect**,
# which is the only thing worth pinning. What `_note_lag` does *not* have is the G row:
# nothing drives it off the timer, and the AST check has no counterpart for it. It sits
# on the same `poll_bus` line as `_note_replay_gaps`, so the timer test below covers the
# call site it shares -- but not its own alert. That is a one-assertion ticket, not this
# one, and it is named in #117's report rather than fixed here.
#
# **What none of these catches, stated so nobody reads them as more than they are.**
# A counter that moves by the wrong amount, against the wrong stream, or at the wrong
# cadence -- the fakeredis scenario fixes all three, so all three are asserted against
# one arrangement and none is asserted in general. And everything about the real Redis:
# `consumer_lag` is two `XINFO` calls, `fakeredis` answers them from its own
# implementation, and a version of Redis that answered differently would be invisible
# to every row of that table.
# --------------------------------------------------------------------------------------


class _StopTheLoop(Exception):
    """Ends `monitor_bus_forever`'s infinite loop after exactly one pass.

    The loop takes its `sleep` as a parameter, so a fake cadence stops it without a
    duration and without cancelling a task -- the loop runs, the poll it is supposed to
    drive happens, and the first sleep ends the test. **`AGENTS.md`'s rule is honoured:**
    nothing here waits on a duration, and the one sleep that exists drives the cadence
    rather than betting on it.
    """


def test_the_timer_itself_moves_the_replay_gap_counter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """**The assertion nothing else in the suite makes.**

    `test_the_replay_gap_check_is_reachable_from_the_timer_in_the_code` says the timer
    *can* reach the counter. `test_store_health.py::
    test_a_watermark_trimmed_past_is_counted_and_alerted_without_a_restart` says the
    counter moves when `poll_bus` is called. Neither says the timer calls it, and
    `monitor_bus_forever` guarding its own `poll_bus` behind a condition that is never
    true passes both -- reachable, never reached, which is #103 one level up.

    So this drives `monitor_bus_forever` itself, on a fake cadence, over the incident's
    own arrangement: a reader that stopped, a publisher that did not, and retention
    passing the watermark.
    """
    from test_store_health import (
        BASE,
        QUOTE_STREAM,
        Clock,
        _alerts,
        _close,
        _make,
        quote,
        until,
    )

    async def scenario() -> tuple[int, tuple[str, ...], list[str]]:
        import fakeredis.aioredis

        clients: list = []
        clock = Clock(BASE.timestamp())
        process, server = await _make(tmp_path, clock, clients)
        try:
            for minute in range(3):
                process.bus.publish(quote(minute, bid=100.0 + minute))
            await process.bus.flush()
            await until(
                lambda: process.subscription.offered == 3,
                what="the store read the three entries",
            )

            # The reader stops, the publisher does not, retention passes the watermark.
            clients[0].failing = -1
            await until(
                lambda: not process.bus.readers()["store"]["alive"],
                what="the store's reader stopped",
            )
            for minute in range(3, 9):
                process.bus.publish(quote(minute, bid=100.0 + minute))
            await process.bus.flush()
            trimmer = fakeredis.aioredis.FakeRedis(
                server=server, decode_responses=False
            )
            try:
                await trimmer.xtrim(QUOTE_STREAM, maxlen=2, approximate=False)
            finally:
                await trimmer.aclose()

            # **Nothing calls `poll_bus` here. The loop does, or nothing does.**
            async def one_pass(_seconds: float) -> None:
                raise _StopTheLoop

            with pytest.raises(_StopTheLoop):
                await store_main.monitor_bus_forever(process, sleep=one_pass)

            return (
                process.writer.replay_gap_entries,
                process.bus_gaps,
                [alert.code for alert in _alerts(process)],
            )
        finally:
            await _close(process)

    with caplog.at_level(logging.ERROR, logger="deltapayoff.store_main"):
        counted, gaps, codes = asyncio.run(scenario())

    assert gaps == (QUOTE_STREAM,)
    # Nine entries published, two retained, three read: four were trimmed unread.
    assert counted == 4, (
        "one pass of `monitor_bus_forever` left the replay-gap counter at "
        f"{counted}; the timer is not reaching the counter, which is what "
        '"replay_gap_entries": 0 meant for two hours on 2026-09-12'
    )
    assert "store.replay_gap" in codes, (
        "the timer counted the gap and published no alert; R5 asks for three things "
        "and the counter is only one of them"
    )
    # **R5's third obligation, which nothing held until #114/#117.** Deleting the
    # `log_event` call from `_note_replay_gaps` left every other check in this
    # repository green, the existing behavioural test included.
    records = [
        record
        for record in caplog.records
        if record.name == "deltapayoff.store_main"
        and getattr(record, "event", None) == "store.replay_gap"
    ]
    assert len(records) == 1, (
        "R5 asks for an error-level `store.replay_gap` record with both bounds and an "
        f"exact count; the timer's pass produced {len(records)}"
    )
    record = records[0]
    assert record.levelno == logging.ERROR
    assert record.stream == QUOTE_STREAM
    assert record.lost == 4
    assert record.saved_id and record.first_retained_id, (
        "both bounds, which is what let #106's live restart be read at all"
    )
