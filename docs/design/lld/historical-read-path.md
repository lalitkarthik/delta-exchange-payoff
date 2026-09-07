# The historical read path

Landed by [#45](https://github.com/lalitkarthik/delta-exchange-payoff/issues/45). Parts
and traffic between them: [../hld.md](../hld.md) §2.6. Contract:
[../../historical-chain-contract.md](../../historical-chain-contract.md).

## What it is

Two routes and one module, `historical.py`, sibling to `smile.py`. `/chain/minutes`
lists the stored minutes for one underlying, expiry and day; `/chain/at` rebuilds the
ladder at one of them. Neither solves anything or calls Delta — both read what
`quote-bars`, `reference-bars`, `computed-bars` and `spot-bars` already hold.

`read_ladder_at` joins the three per-contract tables on `symbol` at one minute, and adds
`spot-bars` for two chain-level fields — see *The fourth table* below. `list_minutes`
reads `quote-bars` alone, because that is the table `read_ladder_at` gates on: the two
routes have to agree on what "stored" means, or the slider could stand on a position
its own ladder route refuses.

## The no-forward-fill guarantee

`read_ladder_at` returns `None` — not an empty ladder, not the row either side — when
`quote-bars` holds no row for the exact minute asked for. `main.chain_at` turns `None`
into `{"type": "waiting", ...}`, the same envelope `/ws/chain` sends for "nothing has
arrived yet". There is no code path in `historical.py` that reads a minute other than
the one it was given; the guarantee is the absence of a fallback, not a check.

`engine/tests/test_historical.py`'s first test is three minutes written with the middle
one empty, asserting the minutes route lists two and the ladder route answers
`"waiting"` for the third. Red-green verified during development: with the early-return
removed, the same test failed with `'chain' == 'waiting'` — an empty ladder rather than
the correct absence, because the two are the same JSON shape and only one of them means
something.

## The fourth table

The ticket's concept section names three tables. `spot-bars` is a fourth, read for
`spot` and `atm_strike` alone. `spot` is honest when read — it is never derived from
`forward`, which `docs/chain-contract.md` is explicit is a different number — and it is
`None` when `spot-bars` itself has nothing for the minute.

**That `None` used to be an open item; [#47](https://github.com/lalitkarthik/delta-exchange-payoff/issues/47)
closed it.** `docs/chain-contract.md` fixed `spot` and `atm_strike` as plain numbers
while this route could already answer `null`, and `web/components/ChainLadder.tsx`'s
`inTheMoney` compared `strike < spot` with no null guard — JavaScript's `<` coerces
`null` to `0`, so every put washed in-the-money and every call did not, silently. #47
typed both fields `number | null` on the contract, guarded the comparison so a null spot
highlights neither side, and pinned it on both seams:
`engine/tests/test_historical.py::test_spot_is_null_when_table_d_has_nothing_for_this_minute`
(already in place from this ticket) and the new `web/tests/moneyness.test.ts`.

## Where each field comes from

See `docs/historical-chain-contract.md`'s table. In short: quote bars give `bid`/`ask`
as the minute's OHLC close; reference bars give `mark`, the venue's IV and Greeks, and
open interest; computed bars give ours and the four chain-level fields; spot bars give
`spot`. `product_id`, `tick_size` and `oi_value_usd` are never stored on any bar and are
always `null` here.

## Cost

**`measured` 15.3 ms minimum (16.8 ms median) for one minute, 68 strikes / 136 legs —
the figure this codebase already uses for a realistic Delta chain — against `/smile`'s
`measured` 6.8 ms for a whole expiry's stored day, 540 minutes / 18,676 rows.**

Run: `python tools/measure_historical_chain.py --strikes 68 --runs 20`, three
invocations, minimum of the 20-run minimums, on the machine this ticket was built on,
2026-09-07. The script builds a throwaway store under a temporary directory — no
`data/` is read — writes one minute across all four tables, flushes, and times
`historical.read_ladder_at` directly, bypassing FastAPI and HTTP.

**The two numbers are not the same read scaled down, and the comparison is the point of
asking for it.** `/smile` opens one table's Parquet files once and returns thousands of
rows; `/chain/at` opens **four** tables' files and does the join and the two dict
look-ups in Python rather than in Polars — `_leg` and `_computed_leg` build a
`pydantic` model per leg rather than staying inside a lazy frame. For 136 legs that is
136 `Leg` constructions and up to 136 `ComputedLeg` ones, which is almost certainly
where the extra ~9 ms against `/smile`'s fixed cost goes, though this was not profiled
line by line — a claim `derived` from the shape of the code, not `measured` on its own,
and the distinction matters enough to state rather than blur.

**This route serves one minute at a time by design** — `docs/historical-chain-contract.md`
explains why the domain and the ladder are two separate requests rather than one
whole-day response the way `/smile` is: a ladder carries roughly a hundred legs against
a smile's one number per strike, so returning every stored minute of a day in one
response would be tens of megabytes rather than the few hundred kilobytes `/smile`
sends. Fetching per minute costs a round trip per drag instead; at ~16 ms server-side
plus loopback latency, that is comfortably inside what a drag can wait on without the
scrubber's polish work (throttling, a pending indicator) becoming necessary yet.

## What is not measured

**The live gap rate between `quote-bars` and `reference-bars`/`spot-bars`.**
`docs/storage.md` records `quote-bars` vs `computed-bars`: 217 of 904 minutes for one
expiry on one day carried quotes and no computed bar. Whether `reference-bars` and
`spot-bars` show the same gap, a different one, or none — they are folded from ticks
rather than sampled, so the mechanism `tools/measure_computed_gaps.py` diagnoses does
not obviously apply to them — is unmeasured. The ticket's own *What to notice* section
asks for this to be recorded "after both have landed", naming #44; #44 has not landed in
this worktree, and the number needs the live engine's `data/`, not a temporary test
store. Owed, not answered here.

## Seam

The FastAPI app under `TestClient`, `DELTA_LIVE_FEED=0`, exactly as `smile.py`'s tests
use it — `engine/tests/test_historical.py`. Four `BarStore`s on a `tmp_path`, injected
through `get_historical_source`'s override, the same pattern `get_computed_store` uses
for `/smile`. One test per file checks the seam itself with nothing overridden, reading
whatever a `BarWriter` built on the same `tmp_path` buffered.
