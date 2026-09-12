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

**If you pass `--basetemp`, create its parent directory first.** pytest makes the basetemp
directory with `parents=False`, so `--basetemp=X/basetemp` fails outright when `X` does not
exist — every test that uses `tmp_path` errors at setup with
`FileNotFoundError: [WinError 3] The system cannot find the path specified`, **315 of them**
(`measured` 2026-09-12). They are reported as errors, not failures, the run finishes faster
because those tests never ran, and an immediate re-run is clean because `-o cache_dir=X/cache`
creates `X` on the way past. Four separate workers read that as a transient glitch on a loaded
machine before it was diagnosed. `mkdir -p .sandbox-tmp` first, and a run that reports ~315
errors at setup is this, not your change.

**Inside the Codex sandbox, add `--basetemp=.sandbox-tmp/basetemp -o cache_dir=.sandbox-tmp/cache`
to the pytest command, and delete `.sandbox-tmp/` as your last command, with exactly
`rm -rf .sandbox-tmp`.** Use that literal command and not an equivalent: this machine's Codex
sandbox policy **rejects** `Remove-Item -Recurse -Force`, and a #81 worker that reached for the
PowerShell form lost its cleanup to `blocked by policy` (`measured` 2026-09-12). It was the
only failed command in a 212-command run. The sandbox runs
as a different Windows user, which cannot write the real user's `%TEMP%\pytest-of-Acer`:
without the flag every test that uses `tmp_path` errors at setup with `PermissionError:
[WinError 5]` — 205 of them, `measured` 2026-09-12. Nor can it create anything inside
`engine/.pytest_cache/`, which the real user owns (`measured` 2026-09-12, #62: 209 errors).
A directory the sandbox creates itself works; `.sandbox-tmp/` is git-ignored. The real
user runs pytest without either flag.

Web, from `web/`:

```sh
node node_modules/typescript/bin/tsc --noEmit
node node_modules/next/dist/bin/next build
```

`next build` rewrites `web/next-env.d.ts`. Leave it dirty and say so in your report; the
orchestrator restores it. Do not run git yourself.

The suite is **1,401** tests with Docker available (`measured` 2026-09-12, after the audit
fixes #82 to #91). Without Docker the Redis-backed parametrisations skip rather than run: the
collected count is the same, the passed count is lower. A run that collects far fewer than
1,401 has failed to collect, whatever it printed — and see the `--basetemp` note above before
you conclude your change broke something.

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
- **A test waits on a condition, never on a duration.** `time.sleep(0.6)` betting that two
  passes of a background loop fit inside it is a bet the OS scheduler settles, not the
  test. Use `tests/wait_helpers.py` — `wait_until` where you can `await`, `wait_until_sync`
  where you cannot — and wait on the thing you actually care about, with a message naming
  it. A sleep that *drives* a fake cadence is fine and is not this. #83, #93.
- **Documentation notes stay under 200 lines.** Split rather than overflow, unless named
  exempt below with a reason. Scope: every file under `docs/`, plus this file and
  `CLAUDE.md`. Package `README.md` files (root, `engine/`, `web/`) are outside this rule —
  their accuracy is tracked in `CLAUDE.md`'s "Known drift" instead, not their length.
  `wc -l` on this worktree, `measured` 2026-09-12 (`HEAD` `4a22022`):

  Grown or created since `d163bbc`, the point before #62-#81 landed:

  ```
  200  docs/design/research/0009-compaction-cadence.md   created  #76 (f7df53c)
  236  docs/design/events.md                              grown    #81 (f2fd905)
  248  docs/design/cloud/epic.md                          grown    #74 (ceb9911)
  295  docs/iv-vs-rv.md                                   grown    #54 (4209ec2)
  640  docs/architecture.md                               grown    #77 (01710dd)
  944  docs/storage.md                                    grown    #77 (01710dd)
  ```

  Legacy — already over the bound before the epic touched them, and named here for the
  first time; none of the nine below has been triaged yet:

  ```
  244  docs/superpowers/specs/2026-09-04-iv-vs-rv-design.md
  249  docs/chain-contract.md
  263  docs/maths-start-here.md
  264  docs/implied-vol.md
  309  docs/index-history.md
  372  docs/greeks.md
  377  docs/delta-api-scope.md
  420  docs/superpowers/specs/2026-09-07-engine-feed-management-design.md
  493  docs/superpowers/specs/2026-09-03-crypto-iv-research-design.md
  ```

  `events.md`: do **not** split — `tests/test_events.py` parses it and the parser does not
  survive a split. `epic.md`: mirrors issue #57, see Off limits — do not restructure.
  `architecture.md` and `storage.md`: large legacy documents; splitting either is its own
  ticket with its own risk, not attempted here. `0009-compaction-cadence.md` sits exactly
  at the bound and is a plausible near-term split candidate.

  `docs/design/lld/logging.md` is no longer over: #63 split it, 213 becomes **92** plus a
  75-line `logging-catalogue.md`, and generalised the parser from one file to a tuple.
  `docs/design/hld.md` is **193**; it was 219 before #62 moved its evidence into
  `hld-evidence.md`. `docs/design/lld/store.md` is **191**; it was 204 before #81 moved its
  evidence into `store-numbers.md`, then lost a paragraph #87 found stale. Three times now,
  a design note filling up has been fixed the same way — the design stays and the evidence
  moves to a sibling — as `hld-evidence.md` (#62), `logging-catalogue.md` (#63) and
  `store-numbers.md` (#81) each did.
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
