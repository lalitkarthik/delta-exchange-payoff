"""Structured logging, on the standard library alone. `docs/design/lld/logging.md` is
the design; `log_events.py` is the catalogue of names this module enforces.

**The motivating fact.** A three-day hole in the Parquet store — 2026-09-04 09:38Z to
2026-09-07 09:45Z — went unnoticed because there was nothing to notice it with. One
logger existed and it said almost nothing. This module makes every record the engine
writes a JSON object on one line, with the instrument and the connection state on it
when either is known, so "what happened to this contract" and "what happened while
degraded" are each one filter over one day's file.

**Why the standard library and not a dependency.** `logging.Formatter`, `logging.Handler`
and the `extra` mechanism already do everything asked for: a custom formatter to shape
the line, a handler to place it, and a dict of fields attached to a record without
inventing a second logging API calls have to learn. Adding `structlog` or `python-json-
logger` would buy nothing this does not already have and would be one more version to
track.

**One handler per day, not `TimedRotatingFileHandler`.** That class names the *current*
file with a fixed base name and only stamps a date onto files it has already rotated
away from — today's file would not carry today's date until tomorrow rotated it. An
operator filtering by day wants today's file named for today, so `DailyFileHandler`
below computes the date itself and opens a new file the moment it changes, checked on
every `emit()` rather than on a timer, which is what makes it correct under a clock a
test can move without waiting for a fake alarm to fire.

**Why a call is refused rather than merely logged wrong.** `log_event` raises if `event`
is not in `log_events.ALL` — a typo in an event name is a document that has quietly
stopped being true, and the whole value of the catalogue is that it cannot drift from
what the code actually emits. Every call site in this engine goes through this
function; nothing here calls `logger.info` directly.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import log_events

#: Extra fields with a fixed, documented meaning. Anything else passed as `extra` lands
#: beside them under its own name — see `JsonFormatter.format`.
FIXED_EXTRA_FIELDS = ("component", "venue", "instrument", "conn_state", "event_id")

#: Which **subsystem** a record came from, keyed by the module that logged it.
#:
#: **Not the process.** Naming the process was the first design, and in the split
#: deployment it reads well — but in the ordinary `uvicorn main:app` monolith there is
#: exactly one process, so every line said `api` and `tools/logs.py feed` printed nothing
#: while the socket was busily reading Delta *inside that same process*. The subsystem is
#: the thing an operator actually wants to filter on, and it is right in both
#: deployments: in the split stack the feed process only runs feed modules anyway, so the
#: two answers coincide there and only the monolith gains.
#:
#: A module with no entry falls back to the process name below, so a new module is
#: unlabelled rather than mislabelled, and `deltapayoff.adapters.delta_socket` matches on
#: `adapters.delta_socket` — the longest suffix wins, so a package entry can cover a
#: subtree without naming every module in it.
COMPONENT_BY_MODULE: dict[str, str] = {
    "adapters": "feed",
    "adapters.delta": "feed",
    "adapters.delta_socket": "feed",
    "controller": "feed",
    "delta_client": "feed",
    "feed_main": "feed",
    "feed_runtime": "feed",
    "supervisor": "feed",
    "fanout": "bus",
    "redis_bus": "bus",
    "bar_buffer": "store",
    "bars": "store",
    "contract_bars": "store",
    "historical": "store",
    "store": "store",
    "store_home": "store",
    "store_main": "store",
    "chain": "chain",
    "compute": "chain",
    "iv_index": "chain",
    "smile": "chain",
    "stream": "chain",
    "volatility": "chain",
    "alert_consumer": "alerts",
    "alert_main": "alerts",
    "discord_alerts": "alerts",
    "main": "api",
}

#: What to call records from a module `COMPONENT_BY_MODULE` does not name — uvicorn's own
#: loggers, a library's, a module nobody has classified yet. Each entrypoint sets it, and
#: `DELTA_COMPONENT` overrides the default for a deployment.
#:
#: A module global read at **emit** time rather than an argument to `configure_logging`,
#: because `main.py` configures at import and `feed_main.py` imports `main` — so whichever
#: entrypoint you launch, main's call is the one that wins and `_configured_loggers` makes
#: every later call a no-op. A parameter would stamp `api` on the feed process. This way
#: the ordering cannot matter.
_component = os.environ.get("DELTA_COMPONENT", "api")


def set_component(name: str) -> None:
    """Name the process, for records no module mapping claims. Called by an entrypoint."""
    global _component
    _component = name


def current_component() -> str:
    return _component


def component_for(logger_name: str) -> str:
    """The subsystem a logger belongs to; the process name when nothing claims it."""
    parts = logger_name.removeprefix("deltapayoff.").split(".")
    for start in range(len(parts)):
        found = COMPONENT_BY_MODULE.get(".".join(parts[start:]))
        if found is not None:
            return found
    return _component


class _ComponentFilter(logging.Filter):
    """Stamp the subsystem on every record that does not carry one already."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "component", None) is None:
            record.component = component_for(record.name)
        return True


#: Attributes a library puts on its own records that are noise in ours. Uvicorn attaches
#: `color_message` — the same sentence again with ANSI escapes in it — to every line it
#: logs, and a log file that gets grepped a week later does not want a second, escaped
#: copy of every message.
_NOISE_ATTRS = frozenset({"color_message"})

#: A record's own attributes before any `extra` is applied, computed once from a real
#: `LogRecord` rather than hand-copied from the documentation — so a future Python that
#: adds one (`taskName` arrived in 3.12) is picked up automatically instead of leaking
#: into the JSON line as a spurious "other" field.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord(
        name="x",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
) | {"message"}

#: The repository root, three parents up from `engine/src/deltapayoff/`. The same
#: convention `store.default_root()` uses for `data/`, so the two gitignored, generated
#: directories this engine writes sit side by side.
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_logs_directory() -> Path:
    return _repo_root() / "logs"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: `ts`, `level`, `logger`, `event`, `msg`, then extras.

    `event` falls back to `"log"` rather than raising, because a record that reached
    this formatter without going through `log_event` — a library's own logger, say —
    still has to produce a parseable line; `log_event` is what refuses the call before
    it gets here for anything this engine emits itself.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or "log",
            "msg": record.getMessage(),
        }
        for field in FIXED_EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key in FIXED_EXTRA_FIELDS or key == "event":
                continue
            if key in _NOISE_ATTRS:  # a library's escaped copy of its own message
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


#: ANSI codes, applied only when a `ColorFormatter` is actually attached — which
#: `configure_logging` does only when standard error is a terminal. No other code path
#: reaches these, so redirecting stderr to a file means these never print, by
#: construction rather than by a check on every line.
_LEVEL_COLOURS = {
    logging.DEBUG: "\033[2m",  # dim
    logging.INFO: "\033[36m",  # cyan
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[41m",  # red background
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """Readable, coloured, one line — for a human at a terminal, never for the file."""

    def format(self, record: logging.LogRecord) -> str:
        colour = _LEVEL_COLOURS.get(record.levelno, "")
        event = getattr(record, "event", None) or "log"
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        bits = [f"{stamp} {colour}{record.levelname:<7}{_RESET} {record.name} {event}"]
        for field in FIXED_EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                bits.append(f"{field}={value}")
        line = " ".join(bits) + f": {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class DailyFileHandler(logging.Handler):
    """One file per UTC day, named `<dir>/<YYYY-MM-DD>.log`. Checked on every `emit`.

    Not a timer: a background thread that wakes at midnight is one more thing that can
    silently stop running, and the whole premise of this ticket is that this engine
    already has too many of those. Comparing today's date to the file that is open costs
    nothing on the write path that matters, and a test can move `today` by hand instead
    of waiting for a real midnight.
    """

    def __init__(
        self,
        directory: Path | str,
        *,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        super().__init__()
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._today = today
        self._open_date: date | None = None
        self._stream: Any = None

    @property
    def current_path(self) -> Path | None:
        return None if self._open_date is None else self._path_for(self._open_date)

    def _path_for(self, day: date) -> Path:
        return self._directory / f"{day.isoformat()}.log"

    def _ensure_open(self) -> None:
        today = self._today()
        if today == self._open_date:
            return
        if self._stream is not None:
            self._stream.close()
        self._open_date = today
        self._stream = self._path_for(today).open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_open()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


_configured_loggers: set[str] = set()


def configure_logging(
    logger_name: str = "deltapayoff",
    *,
    directory: Path | str | None = None,
    level: int = logging.DEBUG,
    is_terminal: Callable[[], bool] = lambda: sys.stderr.isatty(),
) -> logging.Logger:
    """Attach the JSON file handler, and a console handler beside it — always.

    **The console handler is unconditional; `is_terminal` picks its formatter** (#103).
    Gating the handler itself on a tty is what made a containerised process silent.

    **Idempotent.** Importing `main` more than once in a process — which every test
    file that imports it does — must not multiply the handlers, or one call to
    `logger.info` would write the same line to the file twice and every volume number
    this ticket measures would be wrong by whatever factor `main` was imported.

    Attached to the named logger, not the root: `logging.getLogger(__name__)` in every
    module of this package resolves to a child of `"deltapayoff"`, and children
    propagate to their parent's handlers by default without this having to touch
    `propagate` — which stays `True` on every logger, because `pytest`'s `caplog`
    fixture captures through the root logger's own handler and relies on that
    propagation reaching it.
    """
    target = logging.getLogger(logger_name)
    if logger_name in _configured_loggers:
        return target

    target.setLevel(level)
    directory = default_logs_directory() if directory is None else Path(directory)
    file_handler = DailyFileHandler(directory)
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(_ComponentFilter())
    target.addHandler(file_handler)

    # **A tty chooses the formatter, never whether the handler exists** (#103). It was
    # the gate, and in a container `sys.stderr` is a pipe, so the only stream handler
    # was never attached: seven hours of records — 252 `store.flush` among them — went
    # to `/app/logs`, which no volume was mounted on, and `docker logs` showed nothing
    # while the store had stopped consuming. A container is the normal case and not the
    # exception. Colour is for a person; JSON is for `docker logs`, for the shipper #71
    # wants, and for anything that parses a line rather than looks at it.
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(ColorFormatter() if is_terminal() else JsonFormatter())
    console_handler.addFilter(_ComponentFilter())
    target.addHandler(console_handler)

    # **Uvicorn's own loggers, onto the same two handlers.** They are not children of
    # `deltapayoff`, so until now they propagated to root with the default format and
    # never reached the day's file — which is why `docker logs` in #103 showed four
    # lines of uvicorn and nothing else. A server's "application startup complete" and
    # its access lines belong in the same stream as everything else the api process
    # says. `propagate`
    # goes false so root does not print a second, unformatted copy beside ours. They
    # arrive without an `event`, which `JsonFormatter` already renders as `"log"`.
    if logger_name == "deltapayoff":
        uvicorn_logger = logging.getLogger("uvicorn")
        uvicorn_logger.handlers = [file_handler, console_handler]
        uvicorn_logger.propagate = False

    _configured_loggers.add(logger_name)
    return target


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    msg: str,
    *args: Any,
    exc_info: Any = None,
    **extra: Any,
) -> None:
    """The one way this engine writes a log record. Refuses an unregistered `event`.

    `extra` becomes attributes on the `LogRecord`, which `JsonFormatter` and
    `ColorFormatter` both read back off it — `venue`, `instrument`, `conn_state` and
    `event_id` by their fixed names when the caller passes them, anything else beside
    them. A `None` value is dropped rather than passed through: `null` on a record that
    never had the field is indistinguishable from a field that was asked for and came
    back empty, and the ticket's own rule — an absent value is `null`, not fabricated —
    reads better as the field being absent altogether.

    Raising on an unregistered `event` rather than logging one anyway is deliberate: a
    silently-accepted new name is a name `docs/design/lld/logging.md` does not have to
    agree with, which is exactly the drift this whole mechanism exists to refuse.
    """
    if event not in log_events.ALL:
        raise ValueError(
            f"{event!r} is not a registered log event; add it to log_events.py and "
            "docs/design/lld/logging.md"
        )
    clean_extra = {"event": event, **{k: v for k, v in extra.items() if v is not None}}
    logger.log(level, msg, *args, exc_info=exc_info, extra=clean_extra)
