# The `/recording` contract

The interface between the engine and the recording control on the option chain screen.
Sibling of [chain-contract.md](chain-contract.md), which is the authority for
`/expiries`, `/chain` and `/ws/chain`, and of [smile-contract.md](smile-contract.md),
which is the authority for `/smile`. As with those two, **this file changes first**,
before the response models and before `web/lib/contract.ts`.

`/chain` serves Delta **now**. `/smile` serves **what was stored**. This one is about
neither: it is the switch that decides whether anything is stored at all.

**A third file rather than a section in one of the other two.** `chain-contract.md` was
already past this project's 200-line bound when `/smile` was split out of it, and the
same argument applies again: two documents are only two authorities when they describe
the same thing. This describes one pair of routes and is the sole authority for them.

## Why the route exists

The store began writing when the process started and stopped when it died. Anyone who
wanted to stop accumulating market data — to work on the machine, to keep a day's
capture clean, to stop filling a disk — had to kill the engine, which is also the only
way to lose the writer's buffer. So stopping cost up to five minutes of the very data
stopping was meant to protect.

## `GET /recording`

```json
{
  "recording": true,
  "buffered_rows": 0,
  "rows_written": 41231
}
```

| field | meaning |
|---|---|
| `recording` | whether the writer is aggregating and writing **right now** |
| `buffered_rows` | sealed bars held in memory, not yet on disk, across all four tables |
| `rows_written` | rows this process has written to Parquet, across all four tables |

**The state lives in the engine and is read from it.** Not in the browser, not in
`localStorage`. Two tabs must not be able to disagree about whether the store is
writing, and a reader arriving on a fresh page is told the truth rather than a default.

**`recording` is `true` at start-up, always.** A process that starts without recording
silently captures nothing, and forgetting to switch it on is a worse failure than
forgetting to switch it off. It follows that **a pause does not survive a restart**, and
the control says so rather than letting the reader assume otherwise.

The two counters are the sum across the four tables, not a per-table breakdown. They
exist so a reader can see that recording is a fact and not a label — `rows_written`
climbing is the engine capturing, and `buffered_rows` returning to zero the moment
recording is switched off is the flush below having happened.

## `POST /recording`

```json
{ "recording": false }
```

Answers with the identical body `GET /recording` returns, reflecting the state **after**
the change — so a client needs no second request and cannot render a state that was
never true.

Idempotent. Posting `false` twice is not an error and the second one flushes an already
empty buffer.

### What switching off does, and what it does not

**Pausing stops the aggregating and the writing. It does not stop the draining.** The
writer holds a *lossless* subscription to the fan-out bus. A paused writer that stopped
taking messages off that queue would let it grow without bound and back up the socket
reader behind it — turning a pause of the *store* into a stall of the *feed*. So the
queue is drained exactly as before and each record is discarded rather than folded into
a bar. Everything the live screens do is upstream of the writer and is completely
unaffected: the ladder and the smile keep updating while recording is off.

**Switching off flushes what is buffered, then stops.** The buffer holds up to a
five-minute flush interval of sealed bars, and discarding them would throw away data the
engine already has — the exact loss the five-minute interval exists to reduce. So the
switch seals what is eligible, writes every table to disk, and only then stops. The
boundary in the store is where the reader put it.

**The open minute is not flushed.** Partial bars stay in the aggregators. A partial
minute written at the pause and a second row for the same minute written on resume would
be two rows for one `(symbol, minute)`, which is a duplicate every reader downstream
would have to know about. The open minute is instead sealed and written whenever
recording resumes, carrying its true tick counts — it is a real observation of a minute
that really was being recorded.

**Switching on resumes and invents nothing.** No row is produced for any minute that
elapsed while recording was off. The store's rule has not moved: a minute with no
arrivals produces no row, not nulls and never the previous close.

## A paused stretch is indistinguishable from an outage

**Decided, and accepted.** The volatility screen already distinguishes three kinds of
absence, and "recording was off" would be a fourth — but nothing in the store records
when the pause began or ended, so a paused stretch reads back exactly as a feed outage
or a dead engine does.

Recording pause boundaries durably is a larger change than this one and is out of scope.
What is **not** acceptable is leaving it unsaid: the control states in words, on screen,
that a paused stretch will look like an outage when the day is read back. A reader who
creates a hole is told they are creating one.

## Who may call it

**Anything that can reach the port.** This is the engine's first mutating route and the
answer is deliberate rather than overlooked.

The engine binds to loopback and serves public Delta market data with no API key and no
credentials, so the caller set is "processes on this machine". CORS is not access
control — it constrains browsers and nothing else — and the browser allowance had to
widen for this route to work at all: `allow_methods` was `["GET"]`, which would have
refused the preflight and surfaced in the browser as a network error indistinguishable
from the engine being down. It is now `["GET", "POST"]`, against the same two localhost
origins as before.

The worst a caller can do is stop the day's capture, which the screen shows and which
any reader can undo in one press. **Authentication is named here and not built**, as
[#23](https://github.com/lalitkarthik/delta-exchange-payoff/issues/23) scoped it. The
day this binds to a non-loopback interface, this route is the first thing that needs a
token — and that is a code change in `main.py`, not a configuration one.

## Errors

FastAPI default shape, `{"detail": "..."}`.

| Status | When |
|---|---|
| 422 | `recording` is absent from the POST body, or is not a **boolean** — FastAPI's own validation |
| 503 | the process has no bar writer, so there is no recording state to report or change |

**A strict boolean.** `"off"`, `"no"` and `"0"` are 422, not false. Pydantic's lax
mode would read all three as false, and guessing at a string is the wrong disposition for
the one route in this engine that changes anything — the same rule as `null` is not `0`.

**503 rather than a default.** A process without a writer is not a process that is
paused; it is one where the question has no answer. Reporting `false` there would tell a
reader that recording is off and can be switched on, when neither is true. The lifespan
builds the writer unconditionally — whether or not the live feed runs — so the only
process this can happen in is one where the lifespan never ran.
