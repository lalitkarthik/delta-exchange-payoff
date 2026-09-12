# Handoff — 2026-09-12

## Start here

```
pwsh -File "C:\Users\Acer\AppData\Local\Temp\claude\D--second-brain-convex-hedge-vault\0d0309cd-6f7e-4201-adff-bbf0e1a2ac41\scratchpad\live63.ps1"
```

Runs #63's live steps 1–4: a full split stack against a scratch root beside the untouched
live monolith, three flush intervals (~20 min), then `tools/compare_store_runs.py` over the
window. It is first because #63's criteria 4 and 6 need **24 h of wall clock that only starts
at the I3→I4 cutover**, and the cutover is the step after this one. Everything else can
proceed in parallel.

## In flight

- **`p81-right-edge` (worktree `D:\Convex Hedge\dxp-81`)** — #81, stopped on the owner's
  instruction, not by failure. 2 of 15 micro-tickets done: P81-01 (`OptionBar` gained
  `underlying` plus a validator) and P81-02 (`translate_bar_columns` in `store.py`).
  **Verified coherent:** 246 passed on the three touched test files, ruff clean. Nothing
  committed. **Resume the same Codex session rather than restarting** — it is the same
  attempt, not a new rung:
  `codex exec resume 01a09417-93d4-7163-81c0-a5fbeffd90b0 -m gpt-5.6-luna -c model_reasoning_effort=max -c 'sandbox_mode="workspace-write"'`
  (from `dxp-81`; `resume` takes no `-s` and no `-C`). Brief: `scratchpad/brief81code.txt`.
- **`i6-compose` (worktree `dxp-65`)** — fully landed and merged into main; branch and
  worktree are disposable. **`dxp-65/web/node_modules` and `dxp-65/engine/.venv` are both
  junctions into the main checkout: `cmd /c rmdir` each one BEFORE `git worktree remove`**, or
  the removal follows them and deletes the real ones. `dxp-81/engine/.venv` is a junction too.
- **`dxp-plan`** (detached at `ccb0064`) — a pristine snapshot used for planning. Disposable.
- **`rv-backfill` / `dxp-54`** — #54, stopped by the owner earlier. Leave exactly as is.
- **#63, #65, #79 are landed but their issues are still OPEN, uncommented, and absent from
  `runs.tsv`.** That is the biggest gap: `AGENTS.md` says the issues are the state of record,
  and right now they disagree with the code. Comment drafts: `scratchpad/c63.md` (placeholders
  `@@COMMIT@@`, `@@SUITE@@`, `@@WINDOW@@`, `@@COMPARE@@`, `@@COVERAGE@@`, `@@BEHIND@@`,
  `@@LOGGING_LINES@@`, `@@CATALOGUE_LINES@@`; fill with `scratchpad/fill.py`),
  `scratchpad/v65-gate.txt` and `scratchpad/v79-gate.txt` hold #65's and #79's numbers.

## What changed this session

| Commit | Ticket |
|---|---|
| `83120d6` | #63 I4 — the store as its own process, replaying from its last flush |
| `142f200` | #65 I6 — five services behind one proxy on one port |
| `5cbe672` | #79 I13 — a committed tool to measure each service on its own container |

**Verified after landing:** 1,312 passed / 0 failed with Docker up, ruff clean, `tsc --noEmit`
clean, `next build` exit 0, main clean. No PRs — this repo commits straight to `main`.

## Decisions made

- #63's live step 2 as written was impossible; the comparison is two independent recorders on
  one venue stream — `scratchpad/rulings63-live.md`.
- #65's api moved under a `/api/` prefix instead of a per-route proxy table, amending R7/R8 —
  reasoning in `plan65.json`'s `unsettled_decisions`, and in `proxy/nginx.conf`'s own comments.
- The proxy answers its own `/healthz` so its healthcheck does not depend on `web` —
  `engine/tests/test_stack_proxy.py` pins it.
- #81's R5 was corrected: the target is the newest **sealed** minute, not the open one —
  `scratchpad/brief81code.head.txt`.
- #66 must **not** touch `redis_bus.py`: #63 already landed `group_start` — see
  `redis_bus.py:443` and the correction inside `plan66.json`'s I7-03.
- Every Compose service must declare a `healthcheck`, or #79 cannot measure start-to-healthy —
  `docs/design/cloud/local-stack.md`.
- `docs/design/events.md` stays one file at 239 lines, over the bound, because
  `test_events.py` parses it. Follow-up written: `scratchpad/followup-events-parser.md`.

## Open questions

- **Assumed, not verified:** that #79's day-long collection should run with the real venue
  feed rather than the smoke override. The plan says "live feed, one full trading day", but it
  means a second venue subscriber for 24 h. Worth one sentence from the owner.
- Two follow-up tickets are drafted and **not filed**: `scratchpad/followup-events-parser.md`
  and `scratchpad/followup-live-feed-flag.md`.
- `docs/design/cloud/redis-hosting.md` states a 2 GB Redis standard; the local stack uses 1 GB.
  Not a contradiction — different things — but the local-stack doc should say why.
- The `store.state` flushed-through position would retire #81's `BUFFER_HORIZON_SECONDS`
  entirely. File it after #81 lands, not before.

## Traps found

- **Windows Python writes CRLF.** A `mapfile` fed from a Python one-liner gives every array
  entry a trailing `\r`, so `[ -e "$f" ]` is false for all of them and a land script reports
  "nothing to stage" against a plainly dirty tree. Pipe through `tr -d '\r'`. The first attempt
  at this very fix was itself corrupted the same way.
- **A green suite does not prove a script runs, twice over.** `tools/smoke_stack.py` had
  `main(argv)` and then `del argv`, so `--help` built six containers while 1,214 tests passed.
  Probe every new tool with `--help` and assert it exits 0 **with `PATH` emptied**.
- **`fakeredis` hides real bugs.** #63's `behind` flag never cleared on an empty blocking
  `XREADGROUP`; only the Docker-Redis parametrisation showed it. Workers' sandboxes skip those
  — re-run them yourself.
- **`compare_store_runs.py` refuses a timestamp without a UTC offset.** Minute-only strings
  fail *after* the recording window. Send `yyyy-MM-ddTHH:mm:ssZ`.
- **`next build` cannot run in a worktree** whose `web/node_modules` is a junction: Turbopack
  refuses to cross it. `tsc --noEmit` and `next build --webpack` both work; run the real build
  in the main checkout.
- **Codex sandbox process creation is the first thing to fail under memory pressure**
  (`STATUS_DLL_INIT_FAILED`). It is bursts, not levels: the pagefile grows to a 32 GB maximum
  but not instantly. Two workers maximum, and stagger their starts by ~60 s.
- **PowerShell `>` writes UTF-16 with a BOM**, which Compose's `env_file` cannot parse.

## Read these

1. `C:\Users\Acer\AppData\Local\Temp\handoff-delta-exchange-payoff-LIVE.md` — the running log,
   newest first. Everything below is detail on it.
2. `gh issue view 63 / 65 / 79 / 81 / 66` — the state of record.
3. `scratchpad/` at the path in **Start here** — every brief, gate, land script and comment
   draft. Naming is `plan<N>.json` → `brief<N>*.txt` → `gate<N>.sh` → `land<N>*.sh` → `c<N>.md`.
4. `docs/design/decisions/0010-store-replay.md` — #63's rulings and the owner's five answers.
5. `~/.claude/skills/codex-route/SKILL.md` §"Verified 2026-09-12" — the measured traps.
