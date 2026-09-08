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
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import log_events

#: Extra fields with a fixed, documented meaning. Anything else passed as `extra` lands
#: beside them under its own name — see `JsonFormatter.format`.
FIXED_EXTRA_FIELDS = ("venue", "instrument", "conn_state", "event_id")

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
    """Attach the JSON file handler, and a coloured one when a terminal is attached.

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
    target.addHandler(file_handler)

    if is_terminal():
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(ColorFormatter())
        target.addHandler(console_handler)

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
