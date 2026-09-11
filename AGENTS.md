# AGENTS.md

Option chain and payoff analysis for Delta Exchange India crypto options. A FastAPI engine
holds one websocket to the venue and pushes a solved strike ladder to a Next.js page; a
Parquet store folds the same stream into one-minute bars.

**Architecture, the thesis, and the reasoning: `CLAUDE.md`.** Read it before changing
anything in `engine/src/deltapayoff/`. It is not repeated here.

**State lives in the GitHub issues, not in files. If an issue and a file disagree, the
issue wins.**

## Verification — run these verbatim

This is a Windows machine. `CLAUDE.md` quotes POSIX paths (`.venv/bin/`); **they do not
exist here**. Use these.

Engine, from `engine/`:

```sh
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check .
```

**Inside the Codex sandbox, add `--basetemp=.pytest_cache/basetemp` to the pytest command.**
The sandbox runs as a different Windows user, which cannot write the real user's
`%TEMP%\pytest-of-Acer`. Without the flag every test that uses `tmp_path` errors at setup
with `PermissionError: [WinError 5]` — 205 of them, `measured` 2026-09-12. `.pytest_cache/`
is already git-ignored, so the temp tree never reaches a diff.

Web, from `web/`:

```sh
node node_modules/typescript/bin/tsc --noEmit
node node_modules/next/dist/bin/next build
```

`next build` rewrites `web/next-env.d.ts`. Leave it dirty and say so in your report; the
orchestrator restores it. Do not run git yourself.

The suite is 1,084 tests with Docker available, 1,071 plus 13 skipped without. A run that
collects far fewer than that has failed to collect, whatever it printed.

## Hard rules

- **Never run a git command that changes state** — no commit, add, checkout, stash, reset,
  push, clean. The orchestrator commits. Leave your work in the working tree.
- **Test-driven.** Write the failing test first, see it fail for the reason the ticket
  describes, then make it pass. Report that you saw it fail; a test that passed on first
  run proves nothing.
- **No test may touch the network.** `tests/conftest.py` sets `DELTA_LIVE_FEED=0` and
  replaces the async client factory with one that raises. Do not work around it.
- **No wall clock in tests.** Expiry dates and windows are fixtures, never `now()`. Tests
  that pass today and fail in November have been written here before.
- **Documentation notes stay under 200 lines.** Split rather than overflow. `docs/design/hld.md`
  is currently 219 lines and over the bound — if your ticket touches it, split it.
- **Stay inside the ticket's allowed scope.** Note anything else you spot in
  `out_of_scope_noticed`; do not fix it.
- Numbers are tagged `measured`, `assumed` or `derived`, with the run that produced them.
  Never quote a figure from a doc as if you observed it.

## Off limits

- **`data/` is read-only.** It holds the live Parquet store. Read it, never write it,
  never delete from it.
- `docs/design/cloud/epic.md` mirrors issue #57 — the issue wins. Do not restructure it;
  agents have collided here before.
- `web/next-env.d.ts` — generated. Do not hand-edit.

## Must stay running

- **The engine on port 8000.** It is a live feed against the venue and its store has holes
  if it stops. Do not kill it, do not start a second one on that port. If your change
  alters what a running engine does, say so in your report; the orchestrator restarts it
  at a flush boundary.
- Web dev server on port 3000. **CORS allows only port 3000** — serving from another port
  fails in a way that looks like the engine being down.
- `lss-portal-pg` in Docker is not this project's. Leave it.
- Remove any container you start. Redis containers named `i2-*` have been left behind
  before.

## Local traps

- **`bun` resolves to `bun.ps1`**, a PowerShell shim. It cannot be spawned and the
  execution policy blocks it. `CLAUDE.md` says "bun, not npm" — that was true elsewhere.
  Use `node` on the `next` and `tsc` bins directly, as above.
- **Heredocs mangle non-ASCII** in this shell — em dashes, `\n` inside f-strings. Write
  such content to a file, then run the file with `PYTHONIOENCODING=utf-8`.
- `--app-dir src` is what puts the package on the path; there is no install step.
- A one-line shell health check has misread `/expiries` and reported an empty currency
  when the payload was correct. Check a payload with `curl | python -c` before believing a
  one-liner.
- Synthetic browser events do not reach `<input type=range>`; a collapsed browser pane
  reports `innerWidth: 0`.

## Reporting

Report against the ticket's acceptance criteria one by one, each with the specific
evidence — a test name, a command's exit code — not a restatement of the criterion.

Say plainly what you did not finish. A partial result reported honestly is worth more than
a complete-looking one that fails the gate.
