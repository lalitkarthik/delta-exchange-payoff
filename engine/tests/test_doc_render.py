r"""Whether a Markdown document under `docs/` still renders as its author meant.

`test_doc_bounds.py` asks "is this document too long." This module asks a different
question — "does this document still say what it says" — and #98 is why the two must
stay separate tests rather than merge into one. #95 corrected a units error in
`docs/design/research/0001-stream-naming-and-payload-format.md` and, in the same edit,
inserted its explanatory blockquote **between** two table rows. A blank line followed by
a block element ends a GFM table, so the row below the blockquote rendered as a bare line
of pipe characters, outside any table, while the note above it claimed to have checked
"all five rows" of a table that now displayed four. The full suite stayed green: the only
test that fired was the line-count bound, and only because the same edit happened to cross
200 lines — an unrelated coincidence. One more short sentence in the note and nothing here
would have caught it. A line-count bound is a policy number picked by a person; a render
check is a structural fact about the document. Merging them into one test would mean a
single failure covering "split this file" and "this file is broken" — two unrelated fixes
a reader would have to untangle from one line of pytest output. They stay apart so a
failure names exactly one thing to do.

**Scope: every `*.md` under `docs/`.** `AGENTS.md` and `CLAUDE.md` are covered by
`test_doc_bounds.py`'s line bound only; neither carries data tables, so a render check has
nothing to catch in them.

**What this checks**, both shapes taken from the #95 evidence directly:

1. A line that begins and ends with `|` and carries at least two more `|` beyond that
   pair — the orphaned-row shape #95 produced — sitting outside every table this module
   can find.
2. A table body row whose column count disagrees with its own header — a shape #95 did
   not produce here but would have, one blank line short of it: delete the blank line
   between the blockquote and row E and the row rejoins the table with the wrong arity
   instead of leaving it outside.

**A pipe is only a cell boundary where GFM says it is.** A `\|` and a `|` inside a
backtick code span are literal characters, not delimiters, and a first pass here that
counted every `|` character found three false positives on real, correctly-rendering
tables: `` `--set-sparse <true\|false>` `` and `` `\|theta\| <= 0.34` `` in escaped or
backticked form, and `` `σ* = √(2·|ln(F/K)|/T)` `` with a bare `|` inside a code span. All
three are two- or three-column tables that a naive pipe count widened to four-or-more
columns, which is exactly the class of check this repository cannot afford — one that
fires on documents that are not broken. `_real_pipes` below excludes both cases the way a
renderer does, and every check downstream is built on it rather than on `str.count("|")`.

Considered and rejected, because none has bitten this repository and a first pass at each
produces false positives rather than findings: unclosed fences (a repo-wide scan at #98
found none); reference links with no target (a naive scan flagged `docs/ingestion.md` and
`docs/storage.md`, both false — `b[0][0]` and `stats()["computed"]["late"]` are inline
code, not `[text][ref]` links, and telling the two apart needs the same code-span-aware
parsing this module just needed for pipes, with nothing yet observed to justify building
it twice); headings that skip a level (the one hit in a repo-wide scan, `logging-catalogue
.md` H1 to H3, is a title followed by its first named entry — deliberate structure, not a
defect). Table shape is the one class with a measured casualty; these three are not, and
are left for whoever has one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def documents() -> list[Path]:
    """Every Markdown file the render check applies to."""
    return sorted(REPO.joinpath("docs").rglob("*.md"))


def _fence_mask(lines: list[str]) -> list[bool]:
    """`True` for a line inside, or itself, a ``` fence."""
    mask = [False] * len(lines)
    inside = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            inside = not inside
            mask[i] = True
            continue
        mask[i] = inside
    return mask


def _real_pipes(stripped: str) -> list[int]:
    """Indices of the `|` characters in `stripped` that GFM treats as cell
    delimiters: not preceded by a backslash escape, and not inside a backtick
    code span (a run of one or more backticks, closed by a run of the same
    length — CommonMark's rule, not just "between two backticks").
    """
    positions: list[int] = []
    i, n = 0, len(stripped)
    while i < n:
        ch = stripped[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "`":
            run_end = i
            while run_end < n and stripped[run_end] == "`":
                run_end += 1
            run_len = run_end - i
            j, closed_at = run_end, None
            while j < n:
                if stripped[j] != "`":
                    j += 1
                    continue
                k = j
                while k < n and stripped[k] == "`":
                    k += 1
                if k - j == run_len:
                    closed_at = k
                    break
                j = k
            i = closed_at if closed_at is not None else run_end
            continue
        if ch == "|":
            positions.append(i)
        i += 1
    return positions


def _row_pipes(line: str) -> list[int] | None:
    """The real pipe positions in `line`, if it is bounded by `|` on both ends
    once whitespace is trimmed — `None` if it is not shaped like a row at all.
    """
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    pipes = _real_pipes(stripped)
    if len(pipes) < 2 or pipes[0] != 0 or pipes[-1] != len(stripped) - 1:
        return None
    return pipes


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    pipes = _real_pipes(stripped)
    return [stripped[a + 1 : b].strip() for a, b in zip(pipes, pipes[1:], strict=False)]


_SEPARATOR_CELL = re.compile(r"^:?-+:?$")


def _is_separator_row(line: str) -> bool:
    """The `|---|---|` rule row a header needs before a table body starts."""
    if _row_pipes(line) is None:
        return False
    cells = _cells(line)
    return bool(cells) and all(_SEPARATOR_CELL.match(c) for c in cells)


def _table_blocks(
    lines: list[str], fenced: list[bool]
) -> list[tuple[int, int, list[int]]]:
    """Every `(header, separator, body-rows)` block, as 0-based line indices.
    Any width counts here — a two-column table is a table — because the
    column-count check below needs every table, not only the wide ones.
    """
    blocks: list[tuple[int, int, list[int]]] = []
    i, last = 0, len(lines) - 1
    while i < last:
        header_ok = not fenced[i] and _row_pipes(lines[i]) is not None
        sep_ok = not fenced[i + 1] and _is_separator_row(lines[i + 1])
        if header_ok and sep_ok:
            body: list[int] = []
            j = i + 2
            while j < len(lines) and not fenced[j] and _row_pipes(lines[j]) is not None:
                body.append(j)
                j += 1
            blocks.append((i, i + 1, body))
            i = j
        else:
            i += 1
    return blocks


def test_no_table_row_is_ejected_from_its_table() -> None:
    """A line that begins and ends with `|` and carries at least two more —
    three or more columns — outside every table block found is #95's defect: a
    row a blank line and a block element pushed out from under its header,
    still displaying as text but no longer part of the table a reader believes
    it is reading.
    """
    offenders: list[str] = []
    for path in documents():
        rel = path.relative_to(REPO).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        fenced = _fence_mask(lines)
        covered: set[int] = set()
        for header, sep, body in _table_blocks(lines, fenced):
            covered.update((header, sep, *body))
        for idx, line in enumerate(lines):
            if fenced[idx] or idx in covered:
                continue
            pipes = _row_pipes(line)
            if pipes is not None and len(pipes) >= 4:
                offenders.append(f"{rel}:{idx + 1}: {line.strip()!r}")
    assert not offenders, (
        "these lines are shaped like table rows (three or more columns) but sit "
        "outside any table this module can find — a blank line followed by a "
        "blockquote, heading or other block element likely ended the table above "
        "them, the way #95 ejected a row from "
        "docs/design/research/0001-stream-naming-and-payload-format.md: "
        + "; ".join(offenders)
    )


def test_every_table_row_matches_its_header_s_column_count() -> None:
    """A row with the wrong number of cells renders shifted or truncated — the
    reader sees numbers under the wrong column headers, which is as misleading
    as a row that vanishes outright. Every detected table counts here,
    including two-column ones the orphan check above deliberately skips.
    """
    offenders: list[str] = []
    for path in documents():
        rel = path.relative_to(REPO).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        fenced = _fence_mask(lines)
        for header, _sep, body in _table_blocks(lines, fenced):
            header_count = len(_cells(lines[header]))
            for row in body:
                row_count = len(_cells(lines[row]))
                if row_count != header_count:
                    offenders.append(
                        f"{rel}:{row + 1}: header has {header_count} columns, "
                        f"this row has {row_count}"
                    )
    assert not offenders, (
        "these table rows disagree with their own header's column count: "
        + "; ".join(offenders)
    )
