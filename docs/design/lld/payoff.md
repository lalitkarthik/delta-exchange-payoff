# The payoff feature — low-level design

Landed by #9, #3, #4, #5, #6 and #7; documented by #8. The interface is
[../../payoff-contract.md](../../payoff-contract.md) and it is the authority — this file says how
the thing behind it is built, what it costs and where it fails. Parts: [../hld.md](../hld.md) §2.6.

**Nothing here is stored.** A payoff is a view over data already recorded: no new table, no event,
no bus subscriber. `POST /analyse` reads the chain cache or the local store, composes, and answers.
Below the route nothing touches a socket, a clock or a filesystem, which is why all 25 tests in
`test_payoff.py` run in `measured` 0.19 s.

## 1. The modules, and what each owns

| Module | Owns | Knows nothing about |
|---|---|---|
| `payoff_models.py` (347 L) | The contract as Pydantic types: `LegRequest`, `AnalyseRequest`, `PayoffPoint`, `Window`, `Curve`, `Metrics`, `Greeks`, `AnalysedLeg`, `AnalyseResponse`. `extra="forbid"` on every one; strictly-ascending guards on the three ordered sequences | FastAPI, the venue, the ladder. Imports Pydantic and `typing` only, so a pure module can build these directly |
| `payoff.py` (463 L) | The arithmetic: `pnl_at_expiry`, `corner_prices`, `payoff_curve`, `end_slopes`, `breakevens`, `strategy_metrics`, `suggested_window`, `payoff_table`, `scale_greeks`, `position_greeks`. Input is `PayoffLeg` — strike, right, direction, quantity, entry price, optional per-unit Greeks — **not** an `AnalysedLeg`: taking the parsed pieces is what keeps the core free of the instrument vocabulary | Canonical strings, instruments, chains. Imports `math`, `dataclasses`, `collections.abc` and `.payoff_models`, pinned by an AST test |
| `analyse.py` (310 L) | The composition layer: `strategy_series`, `analysed`, `AnalyseRefusal`. Resolves a canonical string to a ladder row, crosses the spread, picks the window's anchor and ATM volatility off a `ChainResponse`, weights and signs the Greeks once | HTTP, and which read path handed it a ladder |
| `main.py`'s `POST /analyse` | **The ladder, and nothing else.** Live cache or stored minute; the two kinds of nothing; mapping `AnalyseRefusal` to `HTTPException` | Everything above the ladder |
| `web/lib/payoff.ts` | The mirror, plus `assertPayoff` — it **walks** the response rather than listing paths, because the same field names recur four levels deep | — |
| `web/lib/payoffChart.ts` | Geometry: `pnlAt`, `visiblePoints`, `zoomAbout`, `priceAtPointer`, `polyline`, the plot constants | React |
| `web/lib/legs.ts`, `legs-url.ts`, `leg-edit.ts`, `analyse-live.ts` | Picking, the URL codec, editing, the poll | Each other, deliberately — the ladder's B/S buttons know nothing about half-typed text |

## 2. The seam the design missed

**The spec claimed this feature is "pure arithmetic over legs". It is not** — that holds for
`payoff.py` and nothing else. About **60 of the 259 lines `analyse.py` landed with** (310 today)
are genuinely new logic, and four decisions had no home anywhere in the design:

1. **Which ladder, and what "nothing" means** — the choice between the two read paths and the two
   dispositions for their two kinds of absence (§5). It is also the only reason the route is
   `async def`: the live cache is mutated on the event loop, the Parquet read would block it, hence
   `await asyncio.to_thread(read_ladder_at, ...)`.
2. **Leg resolution** — canonical string → strike → ladder row → side → a 404 when absent. Exists
   nowhere else: `payoff.py` refuses the vocabulary and `chain.py` pivots rather than looks up.
3. **Crossing the spread** — ask when bought, bid when sold, a supplied price winning, an empty side
   refusing the whole analysis. Eleven lines, and the feature's central pricing decision; nothing
   upstream had a concept of *a side of the book being taken*.
4. **The window's inputs** — something must pick an anchor, a volatility and a time off a
   `ChainResponse`: the fitted forward first, spot otherwise, neither if non-positive; and the
   volatility of the strike nearest the **anchor**, not `chain.atm_strike`, which is nearest spot.

**There is a layer between "a ladder" and "weighted legs", and the spec assumed there was not.** It
is `analyse.py`; nothing in it could move into the pure core without handing `payoff.py` a
`ChainResponse`, which review confirmed independently, and it is the file a second venue changes.

## 3. From click to curve

1. **Pick.** `ChainLadder`'s B/S cell calls `onPick`; `lib/legs.ts` appends a one-lot leg with **no**
   `entry_price` — the engine crosses the spread at analysis time — or `dropFirst` removes one lot.
2. **Open.** *Analyse* is an `<a target="_blank">` carrying `analyseHref(legs, minute)`, so a tab
   opened from a pinned minute stays there. `app/analyse/page.tsx` reads `legs=` **raw and
   undecoded** — a malformed link is reported, not treated as absent.
3. **Ask.** `subscribeAnalysis` asks once, then polls at `POLL_MS = 1000` (live) or registers **no
   timer at all** (a named minute). An in-flight guard skips a tick whose predecessor has not
   answered; `legs: []` asks nothing.
4. **Compose.** `strategy_series` agrees the underlying, then the expiry, **before** any ladder is
   read — the expiry decides which ladder; the route picks it. `analysed` resolves each leg, crosses
   the spread, calls `scale_greeks` **once** on unweighted per-unit Greeks, then `payoff_curve`,
   `strategy_metrics`, `suggested_window`, `payoff_table`.
5. **Draw.** Inline SVG: segments between corners, the two rays extended to the window edges —
   rendering, not arithmetic, so the web app's rule that it computes nothing holds.

## 4. Three things that are not obvious

**The window is log space.** `anchor · e^(±3σ√t)`, no drift term, widened to hold every strike. The
additive reading `anchor · (1 ± 3σ√t)` drives the low edge below zero once `σ√t` passes a third —
**70% volatility a quarter out** (`0.70·√0.25 = 0.35`), or **296 days at 37%**. An earlier draft of
the contract illustrated this with "37% at 82 days", wrong by 3.6× (`0.37·√(82/365) = 0.175`) and
caught in review. Recorded as an instance of the rule, not merely fixed.

**"One line, and it does not move" is only half true.** It holds for a leg with a **typed** entry
price; a leg still priced off the live book re-prices from the ask or the bid every poll, so its
corners genuinely move — correctly, since a trade not yet entered has no fixed cost and freezing
the first quote seen would invent a fill that never happened.

**Zoom out is unlimited, and the wall at price 0 is not a cap.** The underlying cannot finish below
zero and the engine's own `_extremes` already treats price 0 as a vertex — which is why only the
right tail can be unbounded — so `zoomAbout` **slides** the window rather than truncating it: the
reader keeps the width they asked for and zooming out at the wall keeps widening rightward.
Useful for about a decade of width: `derived` from the `measured` polylines P5's tests pin, the
butterfly's kinked region falls from 33% of the frame at ±3σ to 2% at 16× and ~0.5% at 64×, thinner
than the stroke, past which every strategy is two straight lines. **A soft cap at that ~16× was
proposed and rejected** — a reader stopped at a cap cannot get past it, the sibling's failure.

## 5. Failure modes, and what the reader sees

| What goes wrong | Code | What the reader sees |
|---|---|---|
| A leg's `instrument` is not canonical | 400 | The string, and which of its six parts was wrong |
| A leg is not listed on the chain being analysed | 404 | The instrument string |
| An `as_of` minute the store does not hold | 404 | Underlying, expiry and minute — the minute genuinely does not exist under never-forward-fill |
| The live cache has not warmed | 503 | Underlying and expiry. The answer exists; it does not exist *yet*. **No 200-with-`waiting`** unlike `/chain/at`: a one-shot POST whose only product is an analysis, so a waiting envelope would make every consumer branch on a union to represent nothing to draw |
| `legs` is empty | 422 | FastAPI's **validation envelope — a list**, not a string. The rule lives on `AnalyseRequest`, so the pure core cannot build an empty strategy either |
| Legs span two expiries / two underlyings | 422 | Both expiries, or both underlyings |
| A leg with no quote on its side and no `entry_price` | 422 | The instrument and which side was empty — **asked about, not disabled** |
| A response that breaches the contract | 500 | Our bug, answered as our bug. Inventing a refusal meaning "our own code is wrong" would let a real defect leave disguised as a market condition |
| A leg with an `iv` and fewer than five Greeks | 200 | Published **unfitted** — `iv` and `greeks` both `null`, no `total_greeks`. Dropping a number is not fabricating one, and it is a condition in the data rather than a defect in us |
| A live poll starts failing | — | The chart clears. Stale numbers under an error message was judged the worse lie |

## 6. Numbers

**Run `t7-analyse`** — `measured` 2026-09-10 against the committed capture
`ws-ticker-04-09-2026.json` + `ws-ob-l2-04-09-2026.json` (136 symbols, 69 strikes, both channels,
through the real decoder into a `ChainStream`), `POST /analyse` over `TestClient`,
`DELTA_LIVE_FEED=0`, 50 requests a case. `fetched_at` frozen at the capture's own instant so the
chain **fits**: re-dated to today it expires unfitted at `measured` **1,680 B**, `derived`
**31% smaller**, understating every byte below.

| Strategy | bytes | gzip | corners | table rows | median | p95 | one minute, one tab |
|---|---|---|---|---|---|---|---|
| 1 leg, long call | 1,548 B | 593 B | 3 | 18 | 3.33 ms | 4.16 ms | 90.7 KiB `derived` |
| 2 legs, call spread | 1,832 B | 728 B | 4 | 18 | 3.42 ms | 4.22 ms | 107.3 KiB `derived` |
| 4 legs, iron condor | 2,448 B | 901 B | 6 | 18 | 3.60 ms | 4.20 ms | 143.4 KiB `derived` |
| **6 legs**, condor + call spread | 3,051 B | 1,079 B | 8 | 18 | 3.56 ms | 4.34 ms | 178.8 KiB `derived` |

Every cell `measured` except the last column, `derived` as bytes × 60 ÷ 1024. **Solve time is flat
in the leg count** — 3.33 → 3.56 ms from one leg to six — because the ladder is already solved and
the rest is arithmetic over a handful of corners. `derived`: 3.6 ms once a second is **0.36% of one
core per open tab**, free until a hundred simultaneous tabs. These reproduce P6's independent run
(2,447 B / 3.3 ms, its own four legs) to within a byte and 0.3 ms, and confirm its **41.3%**
never-changing share — `curve` + `table` + `metrics`, `measured` here 1,037 B of 2,448, **42.4%**.

**Run `t7-spread`** — `measured` 2026-09-10 against the committed REST snapshot
`engine/tests/fixtures/tickers-btc-04-09-2026.json`. All **128** contracts are two-sided. Spread as
a share of mid: median **1.81%**, p90 **46.15%**, max **116.67%** (`P-BTC-58000-040926`, 0.5 / 1.9),
narrowest **0.43%** (`C-BTC-78000-040926`, 700.0 / 703.0); at the money `C-BTC-77000-040926` quotes
1208.0 / 1231.0 — 23.0 wide, 1.89%. In dollars — per underlying `measured`, per contract `derived`
as `× contract_value` 0.001:

| Structure | crossed, per underlying | per contract |
|---|---|---|
| ATM straddle, 77,000 C + P | 37.00 | **$0.0370** |
| ATM call spread, 77,000 / 78,000 (23.00 + 3.00) | 26.00 | **$0.0260** |
| Iron condor, 74/75 P and 79/80 C | 33.00 | **$0.0330** |
| A generic two-leg — twice the median leg (16.00) | 32.00 `derived` | $0.0320 |

**A typical two-leg structure crosses about three cents a contract**, and the p90 says where that
stops being true: a wing quotes 46% of mid, so the *share* it gives up is twenty-five times the
ATM one even though the dollars are fewer. Crossed silently — **#1**. **The two runs above do not
share a board and their quotes will not reconcile**: `C-BTC-77000-040926` is 1208 / 1231 in this
REST snapshot and 877 / 887 in `t7-analyse`'s websocket capture, two committed fixtures taken at
different instants. Each run names its own; neither is wrong.

**Run `t7-curve`** — corner points against a 400-point sampled curve. The corner side is `measured`
(each response's `curve.corners`, serialised compactly); the sampled side is **`derived`** — the cost
of 400 `{price, pnl}` pairs at 2 decimals, because this feature has no sampling path and building one
to weigh it would be building the thing the measurement exists to reject. At full float precision the
sample is 16.8–18.2 KB `derived`; the kinder reading still loses.

| Strategy | corners | 400 points @2dp | whole response | with a 400-point curve |
|---|---|---|---|---|
| 2 legs | **143 B** `measured` | 12,585 B `derived` | 1,832 B `measured` | 14,273 B `derived`, gzip 2,955 B |
| 4 legs | **205 B** `measured` | 12,605 B `derived` | 2,448 B `measured` | 14,845 B `derived`, gzip 3,367 B |
| 6 legs | **269 B** `measured` | 12,788 B `derived` | 3,051 B `measured` | 15,567 B `derived`, gzip 3,789 B |

**`assumed` constants**, all in `payoff.py` with the reasoning beside them:
`FALLBACK_LOG_HALF_WIDTH = 0.05` (a chart still needs a frame when there is no model),
`TABLE_TARGET_ROWS = 25`, the `(1, 2, 2.5, 5)` step ladder, `PRICE_TOLERANCE = 1e-9`,
`PNL_TOLERANCE = 1e-6`. **`TABLE_TARGET_ROWS` is a ceiling, not a target** — the ladder rounds the
step *up*, so real counts run ~13–25: the contract's 7,000-wide frame yields 15, `t7-analyse` 18.
**Per-contract gamma still reaches scientific notation below `1e-7` per underlying** — `measured`,
run `t7-gamma`, `bun -e` against `formatScaled`: the value reaching it is `g × 0.001 × 10⁴ = g × 10`
and `toPrecision(3)` goes exponential under 1e-6, so the threshold is `g < 1e-7`, deep-wing
territory. P5's report said `~1e-6`; review corrected it by 10× and this run confirms that.

### Which of the spec's claims did not survive

**"Corner points are smaller on the wire" survives, by a factor nobody expected** — **47–88×**
smaller than a 400-point sample of the same line, and swapping them in would grow the *whole*
response `derived` **5.1–7.8×** (7.8 at two legs, 5.1 at six). Not merely true; understated.

**"The fat response is worth the round trips it saves" survives, but not for its stated reason.**
P6 found 41.3% of a four-leg response never changes between ticks — an argument for a leaner
refresh, until you find that **the responses are not compressed at all** today (no
`content-encoding`). gzip takes the four-leg body from 2,448 B to **901 B**, a **63%** saving,
larger than the 41% a leaner shape could reach, for one middleware. If the poll must cost less that
is the first move; neither is built here. **What did not survive is the spec's account of its own
architecture** — §2 — found by writing code, not by measuring anything.

## 7. Costs accepted on the record

- **The chain tab goes stale behind the analyse tab** and nothing reconciles them.
- **The spread is crossed silently** — #1. §6 says what it costs.
- **One expiry per strategy** — #2. With two, no date exists on which every leg has finished.
- **`AnalyseScreen`'s composition has no automated test** — `jsdom`, `happy-dom` and
  `@testing-library` are all absent from `web/node_modules`, so the decisions were pushed into pure
  functions (`syncedAnalyseHref`, the three `commit*`, `priceAtPointer`); only the wiring is unverified.
- **No browser exists here.** Layout, gestures, focus and blur are unverified on both screens; every
  rendering claim in the tests is about markup.
- Open beside this: **#10** (a test flaking on a `measured` 247 ms margin), **#11** (21 columns).

## 8. The seams the tests drive

`payoff.py` directly (`test_payoff.py`, 25 tests, every expectation hand-worked or lifted from the
contract's worked example); the route under `TestClient` with a hand-fed `ChainStream` and four
`BarStore`s on `tmp_path` (`test_analyse.py`, 23); the models (`test_payoff_contract.py`, 21); and,
on the web side, the URL codec, the geometry, the edit modules and `renderToStaticMarkup`
fingerprints. Nothing touches the network, `data/` or the wall clock.
