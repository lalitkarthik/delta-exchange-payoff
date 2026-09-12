"""`tools/backfill_index_bars.py`, TDD against a fake HTTP seam. R2-R6 pinned here.

No test opens a socket: `urllib.request.urlopen` is replaced on the tool's own module,
constructed candle pages and raised errors standing in for the venue, exactly the pattern
`test_measure_window.py` uses for `tools/_window.py`. `time.sleep` is replaced too, so the
suite does not actually wait out a 429's reset or the inter-page delay.

Nothing here reads or writes the repository's real `data/`; every store is a `tmp_path`.
"""

from __future__ import annotations

import io
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import backfill_index_bars as bib  # noqa: E402

from deltapayoff.store import INDEX_DATASET, INDEX_SCHEMA, BarStore  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 8, 1, tzinfo=UTC)


# ---------------------------------------------------------------- fakes


class FakeHTTPResponse:
    """Stands in for `http.client.HTTPResponse` on the `with ... as resp:` protocol."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def ok(payload: dict) -> FakeHTTPResponse:
    import json

    return FakeHTTPResponse(200, json.dumps(payload).encode())


def http_error(
    code: int, *, headers: dict[str, str] | None = None, body: bytes = b"{}"
) -> urllib.error.HTTPError:
    hdrs = Message()
    for name, value in (headers or {}).items():
        hdrs[name] = value
    return urllib.error.HTTPError(
        "https://api.india.delta.exchange/v2/history/candles", code, "err", hdrs,
        io.BytesIO(body),
    )


def install(monkeypatch: pytest.MonkeyPatch, actions: list) -> list[float]:
    """`actions` is consumed one per `urlopen` call: a `FakeHTTPResponse` to return, or
    an exception instance to raise. `time.sleep` is captured rather than honoured, and
    its recorded durations are returned so a test can assert on the pacing."""
    queue = list(actions)
    sleeps: list[float] = []

    def fake_urlopen(req: object, timeout: float | None = None):
        action = queue.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    monkeypatch.setattr(bib.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bib.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def candle(minute: datetime, *, o=80_000.0, h=80_010.0, low=79_990.0, c=80_005.0) -> dict:
    return {"time": int(minute.timestamp()), "open": o, "high": h, "low": low, "close": c}


def index_store(root: Path) -> BarStore:
    return BarStore(root, dataset=INDEX_DATASET, schema=INDEX_SCHEMA)


# ---------------------------------------------------------------- page planning


def test_a_ten_thousand_minute_span_plans_widths_4000_4000_2000() -> None:
    end = START + timedelta(minutes=10_000)
    pages = bib.plan_pages(START, end)

    widths = [
        int((page_end - page_start).total_seconds() // 60)
        for page_start, page_end in pages
    ]
    assert widths == [4_000, 4_000, 2_000]
    # Newest-first: the first page is nearest `end`.
    assert pages[0][1] == end
    assert pages[-1][0] == START
    # Non-overlapping and exactly covering the span.
    assert pages[0][0] == pages[1][1]
    assert pages[1][0] == pages[2][1]


def test_an_empty_or_inverted_span_plans_no_request() -> None:
    assert bib.plan_pages(START, START) == []
    assert bib.plan_pages(START, START - timedelta(minutes=5)) == []


# ---------------------------------------------------------------- --days validation


def test_days_zero_or_negative_is_rejected_with_exit_code_two(capsys) -> None:
    for value in ("0", "-5"):
        with pytest.raises(SystemExit) as excinfo:
            bib.build_parser().parse_args(["--days", value])
        assert excinfo.value.code == 2
    capsys.readouterr()


# ---------------------------------------------------------------- candle conversion, R4


def test_a_bucket_the_venue_did_not_return_produces_no_row() -> None:
    """R4: a minute absent from `result` is never forward-filled or interpolated."""
    candles = [candle(START), candle(START + timedelta(minutes=2))]  # minute 1 missing

    bars = bib.bars_from_candles(candles, symbol=".DEXBTUSD", underlying="BTC")

    minutes = sorted(bar.minute for bar in bars)
    assert minutes == [START, START + timedelta(minutes=2)]


def test_an_empty_result_is_zero_rows_not_a_flat_interval() -> None:
    assert bib.bars_from_candles([], symbol=".DEXBTUSD", underlying="BTC") == []


def test_timestamps_and_ohlc_are_preserved_unchanged() -> None:
    row = candle(START, o=1.0, h=2.0, low=0.5, c=1.5)
    [bar] = bib.bars_from_candles([row], symbol=".DEXBTUSD", underlying="BTC")

    assert bar.minute == START
    assert (bar.index_open, bar.index_high, bar.index_low, bar.index_close) == (
        1.0, 2.0, 0.5, 1.5,
    )


# ---------------------------------------------------------------- the HTTP seam, R3


def test_a_clean_200_returns_its_candles(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [ok({"success": True, "result": [candle(START)]})])
    status, candles = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
    assert status == 200
    assert candles == [candle(START)]


def test_a_200_with_an_empty_result_is_zero_candles_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, [ok({"success": True, "result": []})])
    status, candles = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
    assert status == 200
    assert candles == []


def test_a_429_sleeps_the_reset_header_and_retries_within_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = install(
        monkeypatch,
        [
            http_error(429, headers={"X-RATE-LIMIT-RESET": "2000"}),
            ok({"success": True, "result": [candle(START)]}),
        ],
    )
    status, candles = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
    assert status == 200
    assert candles == [candle(START)]
    assert sleeps == [2.0 + 1]  # 2000ms / 1000 + 1


def test_a_missing_or_invalid_reset_header_defaults_to_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for headers in ({}, {"X-RATE-LIMIT-RESET": "not-a-number"}):
        sleeps = install(
            monkeypatch,
            [http_error(429, headers=headers), ok({"success": True, "result": []})],
        )
        status, _ = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
        assert status == 200
        assert sleeps == [60.0 + 1]


def test_a_non_429_http_error_returns_its_status_and_decoded_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, [http_error(500, body=b'{"error": "boom"}')])
    status, body = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
    assert status == 500
    assert body == {"error": "boom"}


def test_a_non_json_error_body_is_truncated_to_two_hundred_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, [http_error(503, body=b"x" * 500)])
    status, body = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
    assert status == 503
    assert body == "x" * 200


def test_a_transport_failure_retries_after_one_second_and_then_reports_status_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = install(monkeypatch, [OSError("reset"), OSError("reset"), OSError("reset")])
    status, body = bib.fetch_page(".DEXBTUSD", START, START + timedelta(minutes=1))
    assert status == 0
    assert "reset" in str(body)
    assert sleeps == [1.0, 1.0]


def test_status_zero_is_never_treated_as_an_empty_venue_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    install(monkeypatch, [OSError("down")] * 3)
    exit_code = bib.run_backfill(
        symbol=".DEXBTUSD", underlying="BTC", root=tmp_path,
        pages=[(START, START + timedelta(minutes=1))],
    )
    assert exit_code == 1
    assert not list(tmp_path.rglob("*.parquet"))


# ---------------------------------------------------------------- run_backfill, R2/R5


def test_a_page_that_does_not_finish_with_200_aborts_and_writes_no_rows_from_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Earlier completed pages remain valid so a rerun resumes safely."""
    pages = [
        (START + timedelta(minutes=1), START + timedelta(minutes=2)),  # fetched second
        (START, START + timedelta(minutes=1)),  # fetched first (newest-first order)
    ]
    install(
        monkeypatch,
        [
            ok({"success": True, "result": [candle(START)]}),
            http_error(500),
        ],
    )

    exit_code = bib.run_backfill(
        symbol=".DEXBTUSD", underlying="BTC", root=tmp_path, pages=pages,
    )

    assert exit_code == 1
    store = index_store(tmp_path)
    rows = store.scan().collect()
    assert rows.height == 1, "the first page's row must survive the second page's failure"
    assert rows.row(0, named=True)["minute"] == START


def test_a_rerun_over_a_fully_stored_range_writes_no_new_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    page = (START, START + timedelta(minutes=1))
    install(monkeypatch, [ok({"success": True, "result": [candle(START)]})])
    first = bib.run_backfill(
        symbol=".DEXBTUSD", underlying="BTC", root=tmp_path, pages=[page],
    )
    assert first == 0
    files_before = sorted(tmp_path.rglob("*.parquet"))
    assert len(files_before) == 1
    bytes_before = files_before[0].read_bytes()

    install(monkeypatch, [ok({"success": True, "result": [candle(START)]})])
    second = bib.run_backfill(
        symbol=".DEXBTUSD", underlying="BTC", root=tmp_path, pages=[page],
    )
    assert second == 0

    files_after = sorted(tmp_path.rglob("*.parquet"))
    assert len(files_after) == 1, "a fully-stored rerun must create no new file"
    assert files_after[0].read_bytes() == bytes_before, "existing bytes must be untouched"


def test_the_same_minute_under_a_different_symbol_is_written_as_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """R5: `(minute, symbol)` is the idempotence key, not `minute` alone."""
    page = (START, START + timedelta(minutes=1))
    install(monkeypatch, [ok({"success": True, "result": [candle(START)]})])
    bib.run_backfill(symbol=".DEXBTUSD", underlying="BTC", root=tmp_path, pages=[page])

    install(monkeypatch, [ok({"success": True, "result": [candle(START)]})])
    bib.run_backfill(symbol=".DEXBTUSDT", underlying="BTC", root=tmp_path, pages=[page])

    rows = index_store(tmp_path).scan().collect()
    assert rows.height == 2
    assert set(rows["symbol"].to_list()) == {".DEXBTUSD", ".DEXBTUSDT"}


def test_rows_land_under_the_hand_built_underlying_then_date_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    page = (START, START + timedelta(minutes=1))
    install(monkeypatch, [ok({"success": True, "result": [candle(START)]})])
    bib.run_backfill(symbol=".DEXBTUSD", underlying="BTC", root=tmp_path, pages=[page])

    store = index_store(tmp_path)
    directories = {
        path.relative_to(store.path).parent.as_posix()
        for path in store.path.rglob("*.parquet")
    }
    assert directories == {f"underlying=BTC/date={START.strftime('%Y-%m-%d')}"}


# ---------------------------------------------------------------- CLI / --dry-run


def test_dry_run_reports_the_plan_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("--dry-run must perform no HTTP request")

    monkeypatch.setattr(bib.urllib.request, "urlopen", refuse)

    exit_code = bib.main(
        [
            "--dry-run", "--root", str(tmp_path), "--days", "5",
            "--symbol", ".DEXBTUSD", "--underlying", "BTC",
        ],
        now=lambda: START,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "page count" in out
    assert "page size" in out
    assert "newest-first" in out
    assert str(tmp_path) in out
    assert ".DEXBTUSD" in out
    assert "BTC" in out
    assert not list(tmp_path.rglob("*"))


def test_the_tool_runs_as_a_subprocess_with_dry_run(tmp_path: Path) -> None:
    """R6: a tool with no entry point passed its whole suite last ticket while doing
    nothing. This proves the script actually runs standalone and exits cleanly."""
    result = subprocess.run(
        [
            sys.executable, str(TOOLS / "backfill_index_bars.py"),
            "--dry-run", "--root", str(tmp_path), "--days", "3",
        ],
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "page count" in result.stdout
    assert "newest-first" in result.stdout
    assert not list(tmp_path.rglob("*"))
