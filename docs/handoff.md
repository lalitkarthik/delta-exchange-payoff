# Handoff — 2026-09-17

Written for whoever continues the **execution-half design** (strategy worker, OMS, risk checks,
paper broker) on another machine. Nothing in this design is built; the work is documents and diagrams.

## Start here

```
git fetch origin && git checkout docs/execution-design && git pull
```

Then open `docs/design/execution/00-overview.md` and read the eight pages in its reading order
(about forty minutes). Every diagram is a mermaid block; GitHub renders them, VS Code's Markdown
preview renders them with the Mermaid extension.

## In flight

- **PR #127** (`docs/execution-design`, open): the eight design pages, `CONTEXT.md` §8, the spec at
  `docs/superpowers/specs/2026-09-16-execution-half-design.md`, and two research reports under
  `docs/design/research/execution-*.md`. **Waiting on the senior's review.** The spec is deliberately
  *not* published as a GitHub issue; that happens after his answers, then `to-tickets`.
- **PR #126** (`feat/logging-per-component`, open, not this work): per-component log naming. Not
  touched here; check whether it changes `logging_setup.py` before designing the three tracing ids into it.
- Nothing uncommitted. `gh issue list` failed on a network error while writing this; the open tickets
  as of 2026-09-16 were #121, #122, #63, #70, #71, #80, #116, #57, and the parked IV/RV set.

## What changed this session

- `6400367` — eight pages under `docs/design/execution/`, `CONTEXT.md` §8 with the new terms.
- `b7640ce` — the spec, local only.
- `304dc95`, `30dace1` — `AGENTS.md` exempts the spec from the 200-line bound and is itself back to 199 lines.
- The two research reports are added with this handoff.
- The private vault (`lalitkarthik/convex-hedge-vault`, not required to continue) gained eight decision
  rows, the glossary collisions, and the rendered pictures `assets/oms-books.png` and
  `assets/execution-design-diagrams.png`. Ask Lalit for them if you want the PNGs; the mermaid sources
  in the pages are the truth.

## Decisions made

Each is one line; the reasoning lives in the page named.

- A strategy publishes a whole desired position, never an order — `strategy-worker.md`.
- Orders are diffed **per strategy**, not pooled as the senior's diagram draws it — `books.md`. **To confirm with him.**
- The engine's mark is `client_order_id`; a fill without it is discretionary; Book 6 is derived — `books.md`, evidence in `research/execution-delta-order-api.md`.
- Reconcile every 10 s and on every fill; a mismatched contract is frozen — `books.md`.
- One `oms` process with risk and execution as modules; sandbox is a second instance on venue `PAPER` — `oms.md`.
- Postgres for all state that outlives a process; Redis stays a pipe — `oms.md`.
- `correlation_id`, `causation_id`, `actor` on every envelope; no OpenTelemetry, no log server — `events-and-tracing.md`, evidence in `research/execution-tracing-and-log-search.md`.
- Risk checks reject and never resize; a loss-limit breach blocks opening orders — `rms.md`.
- Paper broker first, fills at the touch; testnet next; never real money — `paper-broker.md`.
- Execution v1: limit at the touch, cancel and replace; buy legs first, wait, then sells — `oms.md`.
- Vocabulary: **strategy set**, not *portfolio*; **book** numbered 1–6 — `00-overview.md`, `CONTEXT.md` §8.

## Open questions

For the senior, collected in the spec's *Further Notes*: per-strategy versus pooled diffing; whether
a one-sided stop re-entry rebuilds that side or re-enters a fresh condor; premium profit marked at
mid or at the closing price; the margin model for Delta (no pre-trade estimate exists); whether
`actor` carries a process instance id; whether the live `oms` sits dormant in compose before a key exists.

Assumed, not verified: the sample strategy's semantics in `strategy-worker.md` were chosen by the
author (close the side, not the leg; counters reset daily). The senior wrote the strategy; ask him.

## Traps found

- **`AGENTS.md` inventories every doc at or over 200 lines and `test_doc_bounds.py` enforces it**,
  including `AGENTS.md` itself. Adding one inventory line pushed it to exactly 200 and failed the test.
- **`test_nomenclature.py` pins the heading `## 7. Words this repository refuses` by number.** Insert
  new `CONTEXT.md` sections after it, never before.
- **Python's `write_text` on Windows writes CRLF.** Three files came out CRLF and had to be normalised;
  write bytes with `\n`, or use the editor.
- **The terminal does not render mermaid.** To show a diagram in chat, render it: a page with a local
  `mermaid.min.js`, served over `python -m http.server`, screenshotted with a browser tool. The gstack
  `diagram` skill produces `.excalidraw` from the same mermaid but needs gstack installed under
  `~/.claude/skills/gstack`; excalidraw.com's *Mermaid to Excalidraw* import is the no-install route.
- **Write in the `docs/final-design/` register.** The senior wants a newcomer to follow it; a page of
  ticket numbers and symbols was rejected once already. "What this page contains / How to read it" opener,
  terms explained on first use, tables, a "Where to go next" footer, under 200 lines.
- **The senior's instruction is "build first, fix later."** Settle contracts, list intricate behaviour
  as open questions, do not audit. Design and diagrams first, spec second, tickets third.

## Read these

1. `docs/design/execution/00-overview.md` and its reading order — the design.
2. `docs/superpowers/specs/2026-09-16-execution-half-design.md` — the same decisions as a contract, with the test seams.
3. `CONTEXT.md` §8 — the words; §7 the words this repository refuses.
4. `docs/design/research/execution-delta-order-api.md` — what Delta's API can and cannot tell us.
5. `docs/design/research/execution-tracing-and-log-search.md` — why three ids and no OpenTelemetry.
6. `docs/final-design/architecture.md` and `message-bus.md` — the half that exists, and the bus the new half rides.
7. `docs/design/events.md` — the envelope the three ids are added to.
8. `AGENTS.md` — the repository's rules, including the doc bound and the no-attribution rule for commits.

Verified this session: `test_doc_bounds.py`, `test_nomenclature.py` and `test_doc_render.py` pass
except one nomenclature test that needs `fakeredis`, which fails on `main` too. The full suite was
not run; nothing under `engine/src/` changed.
