# The historical chain contract

The interface between the engine and the option-chain page's time slider. Sibling of
[chain-contract.md](chain-contract.md), which is the authority for `/expiries`, `/chain`
and `/ws/chain` — all three serve Delta **now** — and of
[smile-contract.md](smile-contract.md), which reads the same stored tables for a
different screen. Split out for the reason `smile-contract.md` was: `chain-contract.md`
was already past this project's 200-line bound, and a second read path is a different
subject, not an appendix to the first.

`/chain/at` answers with the **same shape** `/chain` and `/ws/chain` do, so
`web/components/ChainLadder.tsx` renders a historical response exactly as it renders a
live one. This file states only what differs: two routes, four tables instead of a
Delta call, and the one rule that makes a hole in the store visible rather than
papered over.

## `GET /chain/minutes?underlying=BTC&expiry=04-09-2026&date=2026-09-04`

Every minute `quote-bars` holds for one underlying, one expiry, on one day.

```json
{
  "underlying": "BTC",
  "expiry": "04-09-2026",
  "date": "2026-09-04",
  "minutes": ["2026-09-04T09:00:00Z", "2026-09-04T09:02:00Z"]
}
```

`minutes` ascends, ISO 8601 UTC, second precision, `Z`-suffixed — `smile.MINUTE_FORMAT`'s
own spelling, so a stamp taken from here reaches `/chain/at` with no reformatting.

**`date` is `YYYY-MM-DD`, the store's own partition spelling — not `expiry`'s
`DD-MM-YYYY`.** The two are deliberately different formats for a deliberate reason: an
expiry's stored history can span more days than the one the slider is looking at, so
"which day" is a question this route has to ask that `/smile` never does — `/smile`
answers for an expiry's entire recorded history in one response, by design.

**The domain is quote arrivals.** A minute lists here exactly when `/chain/at` would
answer `"chain"` rather than `"waiting"` for it — the two routes are built to agree on
what "stored" means, because a slider that could stand on a position its own ladder
route refuses would be a control lying about its own track.

Absence is `{"minutes": []}`, not an error — an underlying nobody has collected and a
day nobody has lived through are both "nothing yet", the same discipline `/smile` uses.

## `GET /chain/at?underlying=BTC&expiry=04-09-2026&minute=2026-09-04T09:00:00Z`

The ladder as it stood at one stored minute, in one of two shapes:

```json
{"type": "chain", "data": { "...": "the `/chain` shape, plus minute" }}
```

```json
{"type": "waiting", "detail": "no stored quotes for BTC expiring 04-09-2026 at 2026-09-04T09:01:00Z"}
```

**The same envelope `/ws/chain` sends**, deliberately: a client that already reads
`chain`/`waiting` off the socket needs no third vocabulary to read this over REST.
`web/lib/live.ts`'s `LiveMessage` type is exactly this shape, plus `minute` on `data`.

### `data`

Everything `/chain-contract.md#a-leg` and its parent document describe, unchanged,
plus:

```json
{ "minute": "2026-09-04T09:00:00Z" }
```

`minute` is the exact stamp asked for. `fetched_at` carries the **same** stamp — there is
no "when we asked Delta" for a historical read, and the minute this ladder describes is
the only honest answer to what would otherwise be a clock naming nothing.

### Where each field comes from

| Table | Gives |
|---|---|
| `quote-bars` | `bid`, `ask` — the minute's OHLC **close**, not its open, high or low |
| `reference-bars` | `mark`, `bid_iv`/`ask_iv`/`mark_iv`, the five venue Greeks, `oi`, `oi_change_usd_6h` |
| `computed-bars` | everything under `computed`, and the chain-level `forward`/`discount`/`years_to_expiry`/`forward_method` |
| `spot-bars` | `spot`, and `atm_strike` derived from it exactly as `chain.nearest_strike` derives it live |

**Never stored, and therefore always `null` here: `product_id`, `tick_size`,
`oi_value_usd`.** The first two are not on any bar's schema at all. `oi_value_usd` only
ever arrives over Delta's REST snapshot, and this route reads bars — absent, not
derived, the same rule `docs/chain-contract.md` states for the live path.

**A table with nothing for this minute makes its fields `null`, never a default.** A
leg with quote bars and no reference bars carries `bid`/`ask` and nothing else; a leg
with no computed bars carries `computed: null` rather than Greeks at some invented
volatility. `docs/storage.md` already records that computed-bars can be missing while
quote-bars is not, for the same minute — see the LLD for what this means for the two
tables read here for the first time, `reference-bars` and `spot-bars`.

**`spot-bars` is a fourth table, and the ticket that specified this route named three.**
`chain-contract.md` fixes `spot` and `atm_strike` as plain numbers, and
`web/components/ChainLadder.tsx` reads `chain.spot` without a null check — a `null`
there is not rendered as an absence, it is `strike < null`, which JavaScript coerces to
`strike < 0` and silently washes every leg on the wrong side. Reading the table that
already records the real spot for that minute is the honest fourth field; inventing one
from the forward would conflate two figures the parent contract is explicit are never
the same number. When `spot-bars` itself holds nothing for the minute, `spot` and
`atm_strike` are `null` and that pre-existing gap in the component is inherited rather
than closed — see the LLD's open item.

## No forward-fill

**A minute nobody quoted answers `"waiting"`, never the ladder either side of it.** The
store writes no row for an empty minute by design; this is the one read path in the
engine where filling that hole with a neighbour would turn an honest gap into a
fabricated quote, on the one screen built specifically to show the gap. `engine/tests/test_historical.py`'s
first test is exactly this: three minutes written, the middle one empty, asserted `"waiting"`.

## Cost

`docs/design/lld/historical-read-path.md` carries the measured number, beside `/smile`'s
6.8 ms, and the run that produced it.

## Errors

Same table `/smile` uses, plus a 400 for a malformed `date` or `minute`:

| Status | When |
|---|---|
| 400 | `underlying`/`expiry` malformed (see `docs/chain-contract.md`), `date` not `YYYY-MM-DD`, or `minute` not `YYYY-MM-DDTHH:MM:SSZ` |
| 422 | a parameter is absent altogether — FastAPI's own validation |

No 404 and no 502, for the reason `/smile` gives: both routes read the local store and
never call Delta.
