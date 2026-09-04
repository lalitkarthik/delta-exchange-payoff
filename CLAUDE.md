# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Option chain and payoff analysis for **Delta Exchange India** crypto options (BTC, ETH). A
FastAPI engine holds one websocket to the venue, pivots tickers into a strike ladder, solves
implied volatility and Greeks from the order book, and pushes the result to a Next.js page once
a second. A Parquet store folds the same stream into one-minute bars.

**State lives in the GitHub issues, not in files. If an issue and a file disagree, the issue
wins.** Issue #1 is the whole study; #5–#8 are open.

## Commands

Engine (`engine/`, venv at `engine/.venv`; docs say Python 3.13, the venv here is 3.12):

```sh
.venv/bin/python -m uvicorn --app-dir src deltapayoff.main:app --port 8000 --reload
.venv/bin/python -m pytest                          # whole suite
.venv/bin/python -m pytest tests/test_bars.py -q    # one file
.venv/bin/python -m pytest "tests/test_solvers.py::test_every_solver_round_trips_or_declines[70000.0-0.05-S3]"
.venv/bin/python -m ruff check .
```

`--app-dir src` is what puts the package on the path; there is no install step for it. Paths
throughout the docs are Windows-flavoured (`.venv/Scripts/`) because that is where this was
built — use `.venv/bin/` here.

Web (`web/`) — **bun, not npm, not pnpm**:

```sh
bun run dev        # http://localhost:3000
bun run typecheck  # tsc --noEmit
bun run build
```

Both processes, engine first. Engine on 8000, web on 3000. **CORS allows only port 3000**
(`localhost` and `127.0.0.1`), so serving the web side from another port fails in a way that
looks like the engine being down. The `/ws/chain` route is not covered by CORS — a handshake
is not subject to it.

`DELTA_LIVE_FEED=0` serves the REST routes and the websocket without opening a socket to
Delta. `tests/conftest.py` sets it, and also replaces the async client factory with one that
raises — **no test may touch the network**, and one that tries fails rather than quietly
succeeding against live data.

`tools/` holds probes, not engine code: `measure_feed.py`, `measure_arrival_lag.py`,
`measure_store.py`, `compact_store.py` (the nightly compaction job), `capture_ws.py`,
`probe_api.py`, `probe_ws.py`. The numbers in the docs came from these; re-run them rather
than trusting a quoted figure.

## Architecture

**One socket, one cache, many browsers.** `DeltaFeed` (`feed.py`) owns the single connection to
Delta and subscribes every live contract on both channels — `LIVE_UNDERLYINGS` in `main.py` is
`("BTC",)`, so **ETH is served over REST but is not on the live feed or in the store**. It
publishes to `FanOut`
(`fanout.py`), an in-process bus. The socket handler never runs inside a consumer — if it did,
a slow flush would stop it reading, the receive buffer would fill, and Delta would close the
connection. Sockets are per browser; the connection to Delta is not. A second tab costs a
queue, not a connection.

**The two channels are not interchangeable.** `ob_l2` carries top-of-book and refreshes every
**508 ms**; `ticker` carries spot, open interest and Delta's own IV/Greeks and refreshes every
**5001 ms**. That 9.8x gap is the project's thesis: the venue's implied vol is fitted to prices
that have already moved. **Delta's IV and Greeks are reference columns only and are never
consumed as inputs** — `tests/test_no_delta_inputs.py` pins that.

**Two bus consumers, deliberately not one.** `ChainStream` (`stream.py`) keeps only the newest
frame per `(channel, symbol)` and rebuilds a ladder on demand — it drops on overflow, because a
four-second-old quote is worthless. `BarWriter` (`store.py`) subscribes **losslessly**, because
a dropped message there is a permanent hole in the record. Its disk write runs in a worker
thread. Sharing one structure would make them fight: one wants the latest state, the other
wants every state.

**The pure core.** `chain.py`, `wire.py`, `convert.py`, `compute.py`, `forward.py`,
`solvers.py`, `black76.py`, `black_scholes.py`, `greeks.py`, `bars.py` take data in and return
data out — no socket, no clock, no filesystem. Only `delta_client.py` talks to Delta and only
`store.py` touches a file. Keep new logic on the pure side; that is why the suite can be large
and fast.

**IV is a property of the strike, not the leg.** It is recovered by inverting the
**out-of-the-money** leg's bid/ask midpoint (calls above the forward, puts below), where the
whole price is time value and vega is largest, then written to both legs with `iv_leg` naming
the source. A leg with no volatility carries **no Greeks** — reporting them at a default sigma
would put five plausible numbers on screen that describe nothing.

**Four solvers, four forwards.** S1 Newton, S2 Brent, S3 Jaeckel-shaped, S4 vectorised;
F1–F4 for the forward. They exist to be compared (`agreement.py`, `docs/implied-vol.md`,
`docs/forward.md`), not because four were needed.

**The store: four tables, four dataset roots**, under a gitignored `<repo>/data/`.
`quote-bars` (what the book did), `reference-bars` (what the venue said), `spot-bars`, and
`computed-bars` (what we made of it). Hive-partitioned `date=/underlying=` — expiry, strike and
option type are **columns**, because expiry as a partition level explodes into thousands of
directories of a handful of rows. **Polars is not allowed to lay out the tree**:
`write_parquet(partition_by=...)` names its output `00000000.parquet` every call, so the 10:00
flush would silently overwrite the 09:00 one. Directories are built by hand, each flush writes
a uniquely named file, and a test pins it.

`docs/chain-contract.md` is the engine↔web interface and the authority; `web/lib/contract.ts`
mirrors it field for field. The websocket sends the identical object `/chain` returns, so
`ChainLadder.tsx` renders either unchanged.

## Invariants that span files

- **Never forward-fill.** A minute with no arrivals produces **no row** — not nulls, never the
  previous close. This is the project's moral as well as a rule: Delta's own
  `/v2/history/candles` pads with the last trade and does not say so — `C-BTC-60000-270624`
  returns 801 daily bars of which 797 are fabricated. Always set `end` to the contract's
  `settlement_time`, never to `now`.
- **`null` is not `0`.** An absent quote means nobody is bidding; Delta spells it `"0"` in some
  fields and that is still `null` here. In `oi` or a greek, `0` is a real zero and stays.
  That split is `to_quote_number` vs `to_number`, and on screen it is an empty cell vs `0`.
- **Every decimal is a JSON number or `null`, never a string.** The engine converts once, at
  the boundary; the web app never calls `parseFloat` and raises `ContractViolationError` if the
  engine breaches this.
- **IV is a decimal fraction on the wire, a percentage on screen.** The engine never multiplies
  by 100.
- **`spot` is Delta's top-level `spot_price`.** `greeks.spot` disagrees with it and is never
  exposed.
- **Tag every number `measured` or `assumed`,** naming the request or run that produced it.
  Every source consulted on this project has given a good design and a bad number.
- **Read source, not READMEs** — including this one. That rule cost a full spec revision to
  learn.
- Greek conventions are the sibling project's, not textbook: delta/gamma undiscounted, vega/rho
  discounted and per one percent, theta a one-calendar-day repricing (a 1/252 year overstates
  it by 1.456x here — crypto trades weekends).
- Delta India's options are **vanilla, linear, USD-settled**. Textbook Black-Scholes and
  put-call parity apply with no correction term. Anything claiming inverse settlement is stale.
- Public market data only. No API key, no `.env`. Every request needs a `User-Agent` or Delta's
  edge answers 403 with HTML.

## Working conventions

- **Commits carry the repository owner's name only.** No AI or assistant attribution in commit
  messages, trailers or PR descriptions.
- **This is a learning project, not a delivery project.** Tickets carry six sections — concept,
  why this way, learn first, task, how you will know, what to notice — and are meant to be
  honoured in that order. Worked one at a time, in the owner's order of interest.
- **Measure rather than assert.** Most questions here are better answered by running something
  for twenty seconds than by reasoning about it.
- **Do not dispatch parallel background agents on these tickets.** Asked for explicitly: the
  token cost is not repaid on work this sequential.
- Decisions already settled — do not silently reopen: all contracts on both channels (the
  `ob_l2` narrowing optimisation was built and deliberately reverted); in-process fan-out
  rather than ZeroMQ; screen-side overflow drops the oldest.
- `docs/handoff.md` §9 lists the skills that earned their keep here — TDD, verification before
  completion, code review before each substantive commit.

## Known drift — verify before trusting

The docs are the record, but four of them have fallen behind the code:

- **Root `README.md` says the options are inverse-settled.** They are not.
  `docs/settlement.md` measured it. The README was never updated behind it.
- **`web/README.md` says "no polling, no websocket, no auto-refresh"** and describes a Refresh
  button. The page has streamed over `/ws/chain` since commit `8280083`; there is no Refresh
  button. Its file list also omits `lib/live.ts` and `lib/direction.ts`, and its "IV is per
  side" note predates the shared computed IV.
- **`NEXT_PUBLIC_USE_FIXTURE=1` no longer gets you a ladder.** `FORCE_FIXTURE` is honoured in
  `lib/engine.ts`, so the expiry dropdown populates, but the ladder now comes from
  `subscribeChain` in `app/page.tsx`, which has no fixture branch. With no engine reachable,
  fixture mode renders the header and nothing under it.
- **`engine/README.md`'s layout section lists five modules.** There are twenty; `bars.py` and
  `store.py` alone are ~2,300 lines.
- **`docs/handoff.md` says 291 tests passing and names #5 as the ticket to start on.** #5's
  storage layer has since landed (four commits through `d164ba2`), `docs/storage.md` is its
  findings document and is not in handoff's reading list.

**State of `main` as measured 2026-09-04, after `a018fb3`:** `ruff` clean, **468 passed /
1 failed** under Python 3.12. The one failure is
`test_solvers.py::test_every_solver_round_trips_or_declines[70000.0-0.05-S3]` — S3 returns
0.625 for a price that underflowed to exactly 0.0, where the contract says it must decline.
It may be genuine or may be the 3.12/3.13 and NumPy/SciPy version gap; nobody has checked.
Diagnose before building on top of it.

The second failure recorded here earlier —
`test_wire.py::test_a_websocket_chain_solves_with_the_untouched_forward_and_solver_code`,
`ForwardResult.trusted` false on an implied rate of 30.1% — **passes as of `2725da7`**,
which regenerated the websocket fixtures alongside the open-interest fix.
