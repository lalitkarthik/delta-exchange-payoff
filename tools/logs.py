"""Watch the engine's log live, all components at once or one on its own.

Every process in this engine already appends one JSON object per line to
`<repo>/logs/<YYYY-MM-DD>.log` (`deltapayoff.logging_setup`, #42/#103), and since the
`component` field landed each line also says which *process* wrote it. So the master log
already exists as a file; what was missing was something to read it with while the site is
running, and a way to narrow it to one component without losing the combined view.

    python tools/logs.py                  # master: every component, live
    python tools/logs.py store            # one component
    python tools/logs.py feed store       # two
    python tools/logs.py --level WARNING  # level floor
    python tools/logs.py --event store.flush
    python tools/logs.py -n 50            # 50 lines of backlog first, then follow
    python tools/logs.py --dir .stack-logs   # the Docker stack's per-service dirs, merged

The web app has no logger of its own, so it joins by being piped in:

    cd web && bun run dev 2>&1 | python ../tools/logs.py --ingest web

`--ingest` wraps each line Next.js prints as a record with `component: web`, appends it to
the same day's file, and echoes it rendered — so the terminal running the dev server stays
readable and the master view gains the web app without one line of web source changing.

`--self-check` runs the assertions at the bottom and prints `ok`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

#: How long to wait when every followed file is at EOF. A quarter second is under what
#: anybody notices and keeps a 24-hour tail from spinning a core.
POLL_SECONDS = 0.25

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_COLOURS = {"DEBUG": "\033[2m", "INFO": "\033[36m", "WARNING": "\033[33m",
            "ERROR": "\033[31m", "CRITICAL": "\033[41m"}
_RESET = "\033[0m"

#: One stable colour per component, so a master view is scannable without reading the
#: name on every line. Chosen by hashing rather than by a table: a deployment can call a
#: process anything, and a name with no entry would otherwise be the one that is hard to
#: follow.
_COMPONENT_COLOURS = ("\033[35m", "\033[32m", "\033[34m", "\033[36m", "\033[95m", "\033[92m")

#: Fields rendered in their own position; everything else on the record becomes a `k=v`.
_SKIP = {"ts", "level", "logger", "event", "msg", "exc_info", "component"}

#: Next.js writes colour to its stdout whether or not anybody is looking. Those codes are
#: for a terminal, not for a file that gets grepped a week later, so the ingest path
#: strips them before the line is stored.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_logs_directory() -> Path:
    return repo_root() / "logs"


def today() -> date:
    return datetime.now(UTC).date()


def day_file(directory: Path, day: date) -> Path:
    return directory / f"{day.isoformat()}.log"


def component_colour(name: str) -> str:
    return _COMPONENT_COLOURS[sum(name.encode()) % len(_COMPONENT_COLOURS)]


def parse(line: str) -> dict[str, Any] | None:
    """The record this line carries, or `None` if it is not one of ours."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or "level" not in record or "ts" not in record:
        return None
    return record


def render(line: str, *, colour: bool = True) -> str:
    """A record as one readable line; anything that is not a record, unchanged.

    Passing a foreign line through untouched matters more than it looks: a traceback that
    uvicorn printed before logging was configured, or a bun error, is exactly the thing
    somebody is tailing for.
    """
    record = parse(line)
    if record is None:
        return line
    level = str(record.get("level", ""))
    stamp = str(record.get("ts", ""))[11:23]
    level_colour = _COLOURS.get(level, "") if colour else ""
    reset = _RESET if colour else ""
    bits = [f"{stamp} {level_colour}{level:<8}{reset}"]
    name = record.get("component")
    if name:
        tint = component_colour(str(name)) if colour else ""
        bits.append(f"{tint}[{name}]{reset}")
    bits.append(f"{record.get('logger', '')} {record.get('event', '')}")
    extras = " ".join(f"{k}={v}" for k, v in record.items() if k not in _SKIP)
    out = " ".join(bits) + (f" {extras}" if extras else "") + f": {record.get('msg', '')}"
    return out + ("\n" + record["exc_info"] if record.get("exc_info") else "")


def make_filter(
    components: list[str],
    *,
    events: list[str] | None = None,
    level: str | None = None,
    grep: str | None = None,
) -> Callable[[str], bool]:
    """Should this line be shown?

    **A line that is not one of our records always passes**, whatever the filters say.
    Dropping a stray traceback because it has no `component` field would hide the one
    thing a filtered view is least able to afford to lose.
    """
    floor = _LEVELS[level.upper()] if level else 0
    pattern = re.compile(grep) if grep else None

    def keep(line: str) -> bool:
        if pattern is not None and not pattern.search(line):
            return False
        record = parse(line)
        if record is None:
            return True
        if components and str(record.get("component", "")) not in components:
            return False
        if events and str(record.get("event", "")) not in events:
            return False
        return _LEVELS.get(str(record.get("level", "")), 0) >= floor

    return keep


def paths_today(directory: Path, *, per_service: bool) -> list[Path]:
    """Today's file, or today's file in every per-service subdirectory.

    The stack mounts `./.stack-logs/<service>` over each container's `/app/logs`, because
    four containers appending to one host file is a collision nobody wants to debug. So
    for a stack the master view is a merge of several files rather than one; that is the
    only difference `--dir` makes.
    """
    if not per_service:
        return [day_file(directory, today())]
    stamp = f"{today().isoformat()}.log"
    return sorted(p / stamp for p in directory.iterdir() if p.is_dir())


def follow(
    paths: Callable[[], list[Path]],
    *,
    backlog: int = 0,
    poll: float = POLL_SECONDS,
    forever: bool = True,
) -> Iterator[str]:
    """Yield lines from every named file as they arrive, reopening as the day rolls.

    The set of files is recomputed on each pass rather than resolved once, which is what
    makes midnight work: tomorrow's file simply appears in the list and is opened, on the
    same check-the-date-when-you-read rule `DailyFileHandler` uses to write it. A handle
    for a file that has left the set is closed once it has been drained.

    A writer flushes each record as one `write`, but a reader can still catch a line
    mid-write; a partial read is held back and completed on the next pass rather than
    yielded as a truncated line that would not parse.
    """
    handles: dict[Path, Any] = {}
    partial: dict[Path, str] = {}
    while True:
        wanted = paths()
        for path in wanted:
            if path in handles or not path.exists():
                continue
            handle = path.open(encoding="utf-8", errors="replace")
            if backlog:
                head = handle.read()
                if head and not head.endswith("\n"):
                    head, _, partial[path] = head.rpartition("\n")
                yield from head.splitlines()[-backlog:]
            else:
                handle.seek(0, 2)
            handles[path] = handle
        moved = False
        for path, handle in list(handles.items()):
            while True:
                chunk = handle.readline()
                if not chunk:
                    break
                if not chunk.endswith("\n"):
                    partial[path] = partial.get(path, "") + chunk
                    break
                moved = True
                yield partial.pop(path, "") + chunk.rstrip("\n")
            if path not in wanted and not partial.get(path):
                handle.close()
                del handles[path]
        if not forever:
            return
        if not moved:
            time.sleep(poll)


def wrap(line: str, component: str, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> str:
    """One line of somebody else's output, as a record of ours.

    `event` is `"log"` — the same fallback `JsonFormatter` uses for a library's own
    logger — because an ingested line has no registered event name and inventing one per
    source would be a catalogue that nothing enforces.
    """
    return json.dumps({
        "ts": now().isoformat(timespec="milliseconds"),
        "level": "INFO",
        "logger": component,
        "event": "log",
        "component": component,
        "msg": _ANSI.sub("", line),
    })


def ingest(component: str, directory: Path, stream: Iterator[str] | None = None) -> int:
    """Read stdin, append each line to today's file as a record, echo it rendered."""
    directory.mkdir(parents=True, exist_ok=True)
    colour = sys.stdout.isatty()
    source = sys.stdin if stream is None else stream
    for raw in source:
        line = raw.rstrip("\n")
        if not line.strip():
            print(flush=True)
            continue
        record = line if parse(line) is not None else wrap(line, component)
        with day_file(directory, today()).open("a", encoding="utf-8") as sink:
            sink.write(record + "\n")
        print(render(record, colour=colour), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("components", nargs="*", help="show only these components")
    parser.add_argument("--dir", default=None, help="log directory (default <repo>/logs)")
    parser.add_argument("--per-service", action="store_true",
                        help="the directory holds one subdirectory per service (implied by --dir .stack-logs)")
    parser.add_argument("--event", action="append", default=[], help="show only these events")
    parser.add_argument("--level", default=None, choices=sorted(_LEVELS), help="level floor")
    parser.add_argument("--grep", default=None, help="regex over the raw line")
    parser.add_argument("-n", "--backlog", type=int, default=0, help="lines of history first")
    parser.add_argument("--ingest", metavar="COMPONENT", default=None,
                        help="read stdin and file each line under this component name")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)

    if args.self_check:
        _self_check()
        print("ok")
        return 0

    directory = Path(args.dir) if args.dir else default_logs_directory()
    if args.ingest:
        return ingest(args.ingest, directory)

    per_service = args.per_service or directory.name == ".stack-logs"
    if not directory.exists():
        print(f"no log directory at {directory}", file=sys.stderr)
        return 1
    keep = make_filter(args.components, events=args.event, level=args.level, grep=args.grep)
    colour = sys.stdout.isatty()
    try:
        for line in follow(lambda: paths_today(directory, per_service=per_service),
                           backlog=args.backlog):
            if keep(line):
                print(render(line, colour=colour), flush=True)
    except KeyboardInterrupt:
        return 130
    return 0


def _self_check() -> None:
    record = json.dumps({
        "ts": "2026-09-15T08:00:18.804+00:00", "level": "INFO",
        "logger": "deltapayoff.store", "event": "store.flush", "component": "store",
        "msg": "wrote", "rows": 12,
    })
    out = render(record, colour=False)
    assert out.startswith("08:00:18.804 INFO"), out
    assert "[store]" in out and "rows=12" in out and out.endswith(": wrote"), out
    assert render("not json at all", colour=False) == "not json at all"

    keep = make_filter(["store"], level="INFO")
    assert keep(record)
    assert not keep(json.dumps({"ts": "x", "level": "INFO", "component": "api"}))
    # a foreign line survives every filter, which is the whole point of the guard
    assert keep("Traceback (most recent call last):")
    assert not make_filter([], level="ERROR")(record)
    assert make_filter([], events=["store.flush"])(record)
    assert not make_filter([], events=["alert"])(record)
    assert make_filter([], grep="store")(record)
    assert not make_filter([], grep="nowhere")(record)

    wrapped = json.loads(wrap("\033[32m ready in 1.2s\033[0m", "web"))
    assert wrapped["component"] == "web" and wrapped["event"] == "log"
    assert wrapped["msg"] == " ready in 1.2s", wrapped["msg"]

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "one.log"
        path.write_text("a\nb\nc\n")
        lines = list(follow(lambda: [path], backlog=2, forever=False))
        assert lines == ["b", "c"], lines
        # a line still being written is held back, not yielded truncated
        path.write_text("a\nb\npartial")
        assert list(follow(lambda: [path], backlog=3, forever=False)) == ["a", "b"]


if __name__ == "__main__":
    raise SystemExit(main())
