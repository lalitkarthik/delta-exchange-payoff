# Handoff — picking this project up from GitHub

**For a collaborator joining with repository access only.** Everything you need is in this
repo or in the issues. Nothing here depends on a machine you cannot reach.

This file deliberately does **not** restate the issues, the commits or the four findings
documents. Those are the record. This is the context they cannot carry: what is decided,
what is deliberately unbuilt, and what the repo currently gets wrong.

---

## 1. Read these first, in this order

| Read | For |
|---|---|
| [issue #1](https://github.com/lalitkarthik/delta-exchange-payoff/issues/1) | The whole study, and why each ticket exists |
| [`docs/delta-api-scope.md`](./delta-api-scope.md) | What Delta's API actually gives you, and the padding trap |
| [`docs/settlement.md`](./settlement.md) | **Delta is vanilla, USD-settled — not inverse.** The project's highest-risk unknown, now measured |
| [`docs/forward.md`](./forward.md) | Four ways to get a forward, and why the choice matters |
| [`docs/implied-vol.md`](./implied-vol.md) | Two models, four solvers, the agreement matrix |
| [`docs/ingestion.md`](./ingestion.md) | One socket, the fan-out, what it costs |
| [`docs/chain-contract.md`](./chain-contract.md) | The engine ↔ web interface |
| [`docs/smile-contract.md`](./smile-contract.md) | `/smile` — a day of stored IV per expiry, disk **and** buffer |

Then read `git log`. Twenty-one commits, each message written to explain a decision rather
than to label a diff. They are worth more than a summary of them.

**If an issue and a file disagree, the issue wins.** That rule is in the root README and
it still holds.

## 2. Running it from a clean clone

Install steps live in [`engine/README.md`](../engine/README.md) — Python 3.13, a venv,
`requirements-dev.txt`. **No API key and no `.env`:** every endpoint and channel this
project touches is public market data.

The web side is Next.js 16 with Bun; `cd web && bun install`.

Two processes, engine first:

```sh
cd engine && .venv/Scripts/python.exe -m uvicorn --app-dir src deltapayoff.main:app --port 8000
```

```sh
cd web && bun run dev
```

Engine on 8000, web on 3000. **CORS allows only port 3000** (`localhost` and `127.0.0.1`),
so serving the web side from another port fails in a way that looks like the engine being
down. Note that CORS does not cover the `/ws/chain` websocket route — a handshake is not
subject to it — which is fine while the server binds to loopback and carries only public
market data, and stops being fine the moment either changes.

Paths throughout the docs are Windows-flavoured (`.venv/Scripts/`) because that is where
this was built. On macOS or Linux use `.venv/bin/` and the equivalents; nothing in the
code is platform-specific.

## 3. What is built, and what is deliberately not

Verified on `main` at the time of writing: **291 tests passing, ruff clean, frontend
typecheck clean.**

The page streams. One Delta websocket connection for the whole process, all 588 live BTC
options subscribed on both the `ticker` and `ob_l2` channels, pushed to the browser as a
complete `ChainResponse` once a second. There is no Refresh button.

Three things are **not** built, and each is deliberate rather than forgotten:

**Nothing is stored.** No disk, no Parquet, no database. `ChainStream` is two dictionaries
of ~588 entries that overwrite each other. Restart the engine and every quote ever
received is gone. That is [issue #5](https://github.com/lalitkarthik/delta-exchange-payoff/issues/5)'s
job and it is the natural next ticket.

**Nothing is computed on the live path.** The implied vols and Greeks on screen are
Delta's own values, passed straight through. Everything built for #2 and #4 — Black-76,
Black-Scholes, four solvers, the forward recovery — exists, is tested, and is not wired
in. Connecting them is the point of the remaining tickets, not an oversight.

**The fan-out has exactly one subscriber.** `ChainStream`. `FanOut` is a seam built for
#5 and #4 to plug into, not a fan-out doing real work yet. Be straight about that if it
comes up.

## 4. Two things the repo currently gets wrong

Both are worth fixing early, and both are the kind of drift that costs a day if you
believe them.

**The root README contradicts `docs/settlement.md`.** [`README.md`](../README.md) still
says crypto options here are *"inverse-settled — quoted in USD, margined and settled in
the underlying"*. That was the assumption the project started from. It was measured and
found **false**: Delta India's options are vanilla, linear, USD-settled, and textbook
Black-Scholes and put-call parity apply with no correction term. `docs/settlement.md` is
correct; the README was never updated behind it.

**Issue #5's storage estimate is roughly 7x too small.** The ticket asks you to check the
footprint against "~16M rows/day". That figure is unsourced in the spec, and the
arithmetic that reproduces it is `ticker` alone at 187 msg/s × 86,400 s = 16.2M — a
single-channel estimate from when 967 options were listed.

What the socket actually delivers today, **measured** on a live connection 2026-09-03 by
`tools/measure_feed.py`: **1,322.9 msg/s at 636.5 KB/s** across both channels. Which is:

| | |
|---|---|
| Rows per day | 1,322.9 × 86,400 = **~114M** (derived) |
| Raw JSON per day | 636.5 KB/s × 86,400 = **~52 GB** (derived) |

It does not break the design. It decides how hard the compression and the buffer have to
work, and whether a day reads back in Polars in seconds or in minutes. Measure it rather
than trusting either number.

## 5. Decisions already made — do not silently reopen

**All 588 contracts on both channels.** An `ob_l2` narrowing optimisation was built —
subscribe only the watched expiry, reference-count the watchers, 637 → ~183 KB/s — and
then deliberately reverted: *"just all contract connected and receiving properly rather
than optimising for per page"*. It is about twenty minutes of work if it is ever wanted
again. **Do not rebuild it unprompted.**

**In-process fan-out, not ZeroMQ.** OpenAlgo fans out over ZeroMQ because it serves 36
brokers across separate processes. This is one venue, one user, 82 KB/s per consumer. The
reasoning, and the conditions that would reverse it, are in the `fanout.py` module
docstring.

**Overflow drops the oldest — for the screen.** A four-second-old quote is worthless, not
slightly worse. Storage will need the opposite policy, because a dropped message there is
a permanent hole in the historical record. That per-subscription choice is #5's work and
is flagged in the same docstring.

## 6. Open questions carried forward

The full lists are in `docs/implied-vol.md` §6 and `docs/ingestion.md` §6. The headline
ones, largest first:

- **Mid versus mark is unmeasured.** Every implied vol in the project inverts a bid/ask
  midpoint. `mark_price` is Delta's own model output, so fitting *that* recovers Delta's
  surface rather than the market's. Nobody has measured how far apart they are.
- **Our Greeks have never been compared** against Delta's reference columns. #4 asked for
  it and it was not done.
- **`greeks.rho` does not reconcile** with a textbook vanilla rho.
- **`R2` versus `R1` was never run** as a direct rate-versus-rate table — the one #4
  acceptance criterion left unticked, and noted as such on the issue.
- **The `under a day` expiry band is empty** in the agreement matrix.
- **The 1 Hz push discards intermediate states.** Thirty seconds on the at-the-money call
  produced 40 distinct quotes and showed 30. Harmless for a screen; #5's writer must
  therefore subscribe to the bus directly rather than to the pushed chain.

## 7. Conventions — these are not negotiable

**Tag every number `measured` or `assumed`,** naming the request or run that produced it.
This convention has paid for itself at least four times: every source consulted on this
project has given a good design and a bad number. Do not relax it.

**Read source, not READMEs.** This rule cost a full revision of the spec to learn.
OpenAlgo's `delta_websocket.py` was eventually read in full and it surfaced three failure
modes that would otherwise have shipped, plus a stale constant that would otherwise have
been copied — their `MAX_SYMBOLS_PER_FRAME[ob_l2] = 1`, against 300 symbols measured
accepted in a single subscribe message.

**Never forward-fill.** A gap in the data is a gap in the data. This is both a technical
rule and the project's moral: forward-filling is precisely the defect caught in Delta's
own `/v2/history/candles`, where `C-BTC-60000-270624` returns 801 daily bars of which 797
are fabricated. Do not build the same thing into our store.

**Commits carry the repository owner's name only.** No AI or assistant attribution in
commit messages, trailers or pull request descriptions. All 21 commits follow this.

## 8. How this project is meant to be worked

This is a **learning project, not a delivery project.** The owner said so explicitly and
reshaped the spec around it. Every ticket carries six sections — concept, why this way,
learn first, task, how you will know, what to notice — and they are meant to be honoured
in that order: understand and explain the concept first, state what was rejected and why,
then build, then point at what to notice in the result.

Tickets are worked **one at a time**, and the order follows the owner's interest rather
than what parallelises best.

**Measure rather than assert.** Almost every question on this project has been better
answered by running something for twenty seconds than by reasoning about it. A table of
real numbers beats a paragraph of explanation, every time.

**Still open:** [#5](https://github.com/lalitkarthik/delta-exchange-payoff/issues/5)
(storage), [#6](https://github.com/lalitkarthik/delta-exchange-payoff/issues/6)
(parallelisation), [#7](https://github.com/lalitkarthik/delta-exchange-payoff/issues/7)
(caching and the live read path),
[#8](https://github.com/lalitkarthik/delta-exchange-payoff/issues/8) (findings).
#5 is the one to start on.

## 9. Suggested skills

If you are working this repo with an AI agent, these earned their keep here:

- **`superpowers:test-driven-development`** — every module in `engine/src/deltapayoff/`
  was built test-first and it repeatedly caught errors in the *expectation* rather than
  the code. #5's storage layer is pure functions over frames: ideal territory.
- **`superpowers:verification-before-completion`** — before claiming any acceptance
  criterion is met. Four claims on this project have outrun their evidence.
- **`code-review`** — run it before every commit of substance. It found a retry-budget bug
  and an unbounded push interval, both of which had already been written and read twice.
- **`superpowers:systematic-debugging`** — for anything in the Parquet or partitioning
  layer that misbehaves.
- **`mattpocock-skills:research`** — if #5 needs Polars or hive-partitioning specifics.
  Primary docs and source, not blog posts.
- **`superpowers:brainstorming`** — for anything outside the seven tickets. The existing
  work is already specced; do not re-brainstorm it.

Avoid dispatching parallel background agents on these tickets. It has been asked for
explicitly: the token cost is not repaid on work this sequential.
