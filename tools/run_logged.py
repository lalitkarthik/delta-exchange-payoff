"""Run a Python file and watch its log in the terminal as it happens.

The engine already writes every record twice — JSON to `logs/<today>.log` and a line to
standard error (`logging_setup.configure_logging`, #103). What it does *not* do is make
that stderr line readable when the process is not a tty: pipe it, run it under a
supervisor, and you get raw JSON. This wrapper runs the script unbuffered, reads its
output line by line as it arrives, and pretty-prints anything that parses as one of our
JSON records, passing everything else through untouched.

    python tools/run_logged.py engine/src/deltapayoff/store_main.py
    python tools/run_logged.py -m uvicorn --app-dir engine/src deltapayoff.main:app

Exits with the child's exit code, and forwards Ctrl-C to it.
"""

from __future__ import annotations

import json
import subprocess
import sys

# One renderer, two callers: `logs.py` follows the day's file, this follows one child's
# stdout, and both want the same line out the other end.
from logs import render


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    # `-u` so the child does not sit on a full buffer; stderr folded into stdout so the
    # log lines and any print() keep their real order.
    child = subprocess.Popen(
        [sys.executable, "-u", *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        for line in child.stdout:
            print(render(line.rstrip("\n")), flush=True)
    except KeyboardInterrupt:
        child.terminate()
    return child.wait()


def _self_check() -> None:
    record = json.dumps({"ts": "2026-09-15T05:20:00.123+00:00", "level": "INFO",
                         "logger": "deltapayoff.store", "event": "store.flush",
                         "msg": "wrote 40 rows", "instrument": "BTC"})
    out = render(record)
    assert "05:20:00.123" in out and "store.flush" in out and "instrument=BTC" in out, out
    assert "wrote 40 rows" in out, out
    assert render("plain text") == "plain text"
    assert render('{"not": "a record"}') == '{"not": "a record"}'
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-check"]:
        _self_check()
    else:
        raise SystemExit(main(sys.argv[1:]))
