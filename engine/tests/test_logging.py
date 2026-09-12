r"""Structured logging: the formatter, the daily file handler, and the catalogue.

**The catalogue is `docs/design/lld/logging-catalogue.md` and it is the authority**, the
same discipline `test_events.py` holds `events.md` to. One test here parses the documents'
`### \`event.name\`` headings and asserts the set equals `log_events.ALL`, so a name
added in code without a paragraph, or a paragraph without a name, fails the suite.

No network, no real terminal. `configure_logging`'s `is_terminal` is always injected.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from deltapayoff import log_events
from deltapayoff.logging_setup import (
    ColorFormatter,
    DailyFileHandler,
    JsonFormatter,
    configure_logging,
    log_event,
)

REPO = Path(__file__).resolve().parents[2]
LOGGING_DOC = REPO / "docs" / "design" / "lld" / "logging.md"
LOGGING_CATALOGUE = REPO / "docs" / "design" / "lld" / "logging-catalogue.md"
LOGGING_DOCS = (LOGGING_DOC, LOGGING_CATALOGUE)


def documented_event_names() -> set[str]:
    """The catalogue, read out of the document's own `###` headings.

    Keyed off the code span in the heading rather than a line number, so reordering the
    sections or rewriting the prose after them does not move the goalposts — the same
    convention `test_events.py.documented_event_types` uses for `events.md`.
    """
    return {
        name
        for document in LOGGING_DOCS
        for name in re.findall(
            r"^###\s+`([^`]+)`",
            document.read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    }


def make_record(
    level: int = logging.INFO, msg: str = "hello", **extra: object
) -> logging.LogRecord:
    record = logging.LogRecord("deltapayoff.test", level, "", 0, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


# --------------------------------------------------------------------------- catalogue


def test_every_documented_name_is_registered_and_vice_versa() -> None:
    """#42's acceptance line: a test asserts every `event` name used in code is in the
    document's list. `log_event` enforces the other direction at every call site — this
    is what keeps the document itself from drifting."""
    assert documented_event_names() == log_events.ALL


# ----------------------------------------------------------------------------- log_event


def test_log_event_refuses_an_unregistered_name() -> None:
    logger = logging.getLogger("deltapayoff.test.refuses")
    with pytest.raises(ValueError, match="not a registered log event"):
        log_event(logger, logging.INFO, "made.up.name", "hello")


def test_store_replay_log_names_are_registered() -> None:
    assert log_events.STORE_REPLAY_GAP in log_events.ALL
    assert log_events.STORE_CHECKPOINT in log_events.ALL


def test_log_event_accepts_every_registered_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Red-green's other half: every name in the catalogue is itself accepted, so the
    refusal above is a real filter and not a guard that rejects everything."""
    logger = logging.getLogger("deltapayoff.test.accepts")
    caplog.set_level(logging.DEBUG, logger="deltapayoff.test.accepts")
    for name in log_events.ALL:
        log_event(logger, logging.DEBUG, name, "ok")
    assert {r.event for r in caplog.records} == log_events.ALL
    assert len(caplog.records) == len(log_events.ALL)


def test_log_event_drops_none_valued_extras_rather_than_sending_null() -> None:
    logger = logging.getLogger("deltapayoff.test.none")
    logger.setLevel(logging.DEBUG)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(Capture())
    log_event(
        logger,
        logging.INFO,
        log_events.FEED_TRANSITION,
        "x",
        venue="DELTA",
        instrument=None,
    )

    (record,) = records
    assert record.venue == "DELTA"
    assert not hasattr(record, "instrument")


# ----------------------------------------------------------------------------- formatter


def test_json_formatter_carries_exactly_the_fixed_fields_plus_the_extras_given() -> None:
    """#42's acceptance line: a captured record parses as JSON and carries exactly the
    fixed fields plus the extras given."""
    record = make_record(
        level=logging.WARNING,
        msg="feed connection DELTA: connected -> degraded (stale)",
        event=log_events.FEED_STALE,
        venue="DELTA",
        conn_state="degraded",
        extra_field="beside them",
    )

    line = JsonFormatter().format(record)
    parsed = json.loads(line)

    assert parsed["level"] == "WARNING"
    assert parsed["logger"] == "deltapayoff.test"
    assert parsed["event"] == "feed.stale"
    assert parsed["msg"] == "feed connection DELTA: connected -> degraded (stale)"
    assert parsed["venue"] == "DELTA"
    assert parsed["conn_state"] == "degraded"
    assert parsed["extra_field"] == "beside them"
    # `instrument` and `event_id` were never given, so they are absent — not `null`.
    assert "instrument" not in parsed
    assert "event_id" not in parsed
    # ts is a real ISO timestamp, not a formatting accident.
    datetime.fromisoformat(parsed["ts"])
    assert set(parsed) == {
        "ts",
        "level",
        "logger",
        "event",
        "msg",
        "venue",
        "conn_state",
        "extra_field",
    }


def test_json_formatter_falls_back_to_log_for_an_unstructured_record() -> None:
    """A record that never went through `log_event` — a library's own logger — still
    formats to valid JSON, which is what keeps every line in the file parseable."""
    record = make_record(msg="a plain message")

    parsed = json.loads(JsonFormatter().format(record))

    assert parsed["event"] == "log"
    assert parsed["msg"] == "a plain message"


def test_json_formatter_carries_exception_info() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = make_record(
            level=logging.ERROR, msg="failed", event=log_events.ENGINE_ERROR
        )
        record.exc_info = sys.exc_info()

    parsed = json.loads(JsonFormatter().format(record))
    assert "RuntimeError" in parsed["exc_info"]
    assert "boom" in parsed["exc_info"]


# ------------------------------------------------------------------------ daily rotation


def test_daily_file_handler_rotates_at_the_date_it_is_told(tmp_path: Path) -> None:
    """No timer: the file that is open is decided by the injected clock, checked on
    every `emit()`. A test can move midnight by hand instead of waiting for one."""
    days = iter([date(2026, 9, 7), date(2026, 9, 7), date(2026, 9, 8)])
    handler = DailyFileHandler(tmp_path, today=lambda: next(days))
    handler.setFormatter(JsonFormatter())

    for i in range(3):
        handler.emit(make_record(msg=f"line {i}", event=log_events.ENGINE_ERROR))
    handler.close()

    assert sorted(p.name for p in tmp_path.glob("*.log")) == [
        "2026-09-07.log",
        "2026-09-08.log",
    ]
    assert (tmp_path / "2026-09-07.log").read_text(encoding="utf-8").count("\n") == 2
    assert (tmp_path / "2026-09-08.log").read_text(encoding="utf-8").count("\n") == 1


def test_daily_file_handler_names_the_file_for_today_not_a_generic_name(
    tmp_path: Path,
) -> None:
    """The reason this is not `TimedRotatingFileHandler`: that class only stamps a date
    onto a file it has already rotated away from, so *today's* file would not carry
    today's date until tomorrow rotated it."""
    handler = DailyFileHandler(tmp_path, today=lambda: date(2026, 9, 8))
    handler.setFormatter(JsonFormatter())

    handler.emit(make_record(event=log_events.ENGINE_ERROR))
    handler.close()

    assert handler.current_path == tmp_path / "2026-09-08.log"
    assert handler.current_path.exists()


# -------------------------------------------------------------------------- colour guard


def _console_handlers(logger: logging.Logger) -> list[logging.Handler]:
    """Every handler that writes to a stream rather than to the daily file."""
    return [
        handler
        for handler in logger.handlers
        if not isinstance(handler, DailyFileHandler)
    ]


def test_no_colour_is_attached_when_standard_error_is_not_a_terminal(
    tmp_path: Path,
) -> None:
    """#42's acceptance line: with standard error redirected to a file, no colour codes
    appear anywhere.

    **Still structural, and no longer an absent handler (#103).** The console handler is
    now always attached; what a tty decides is the *formatter* on it. So the guard is
    that no `ColorFormatter` is anywhere, which is what #42 actually asked for.
    """
    logger = configure_logging(
        "deltapayoff.test.no_colour",
        directory=tmp_path,
        is_terminal=lambda: False,
    )

    assert not any(isinstance(h.formatter, ColorFormatter) for h in logger.handlers)


def test_a_process_with_no_terminal_still_writes_its_records_to_standard_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#103's second defect, reproduced: seven hours of records and `docker logs` empty.

    `/proc/1/fd/2` in `dxp-store` is a pipe, not a tty, so `is_terminal()` was false and
    the only stream handler was never attached. Every record went to `/app/logs`, which
    is not a mounted volume, and died with the container -- including the 252
    `store.flush` records #42 added specifically so a silent store hole could not happen
    twice. A container is the normal case, not the exception, so the record reaches
    standard error whether or not anyone is watching a terminal.
    """
    logger = configure_logging(
        "deltapayoff.test.container_stderr",
        directory=tmp_path,
        is_terminal=lambda: False,
    )

    log_event(logger, logging.INFO, log_events.STORE_FLUSH, "store flush q: 7 rows")

    written = capsys.readouterr().err
    assert written, "nothing reached standard error, which is where docker logs reads"
    line = json.loads(written.splitlines()[-1])
    assert line["event"] == log_events.STORE_FLUSH
    assert line["msg"] == "store flush q: 7 rows"
    assert "[" not in written


def test_the_console_handler_is_json_without_a_terminal_and_colour_with_one(
    tmp_path: Path,
) -> None:
    """A tty chooses the formatter, never whether the handler exists (#103).

    Colour is for a person reading a terminal; JSON is for `docker logs`, for the
    CloudWatch shipper #71 wants, and for anything that has to parse a line rather than
    look at it. Both are the same records through the same handler.
    """
    plain = configure_logging(
        "deltapayoff.test.formatter_plain", directory=tmp_path, is_terminal=lambda: False
    )
    coloured = configure_logging(
        "deltapayoff.test.formatter_colour", directory=tmp_path, is_terminal=lambda: True
    )

    assert [type(h.formatter) for h in _console_handlers(plain)] == [JsonFormatter]
    assert [type(h.formatter) for h in _console_handlers(coloured)] == [ColorFormatter]
    assert [h.stream for h in _console_handlers(plain)] == [sys.stderr]


def test_a_terminal_gets_a_second_coloured_handler(tmp_path: Path) -> None:
    logger = configure_logging(
        "deltapayoff.test.with_colour",
        directory=tmp_path,
        is_terminal=lambda: True,
    )

    assert len(logger.handlers) == 2
    assert any(isinstance(h.formatter, ColorFormatter) for h in logger.handlers)


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """Calling it twice — every test file importing `main` does — must not double the
    handlers, or every volume number in the design doc would be wrong."""
    first = configure_logging(
        "deltapayoff.test.idempotent", directory=tmp_path, is_terminal=lambda: True
    )
    second = configure_logging(
        "deltapayoff.test.idempotent", directory=tmp_path, is_terminal=lambda: True
    )

    assert first is second
    assert len(first.handlers) == 2


def test_color_formatter_actually_emits_ansi_codes_when_used() -> None:
    """The other half of the guard above: `ColorFormatter` itself does produce colour
    codes, so the previous test proves an absence of the handler rather than a formatter
    that never coloured anything to begin with."""
    record = make_record(
        level=logging.WARNING, event=log_events.FEED_STALE, conn_state="degraded"
    )

    line = ColorFormatter().format(record)

    assert "\033[" in line
    assert "feed.stale" in line
    assert "conn_state=degraded" in line
