# The api's minute-boundary stall — the evidence behind bus-reader.md §5a

**Sibling of [bus-reader.md](bus-reader.md).** That note designs the reader and its pending-list
repair; this one holds the measurement of *why the reader's reads fail in the first place*, split
out rather than grown in place for the same reason [hld-evidence.md](../hld-evidence.md),
[logging-catalogue.md](logging-catalogue.md) and [store-numbers.md](store-numbers.md) were.

**The repair in §5a stops the loss. It does not stop the stall.** These are two defects and only
the first is closed. A reader whose read times out will now recover its batch on the next pass;
it will still time out, twice a minute, until something here changes.

## 1. The claim #111 made, and what was missing

#111 observed all four api readers raising `TimeoutError: Timeout reading from redis:6379`
**within 15 ms of each other** at each minute boundary, against a 2.0 s socket timeout, and
inferred an event-loop stall in the api rather than a fault in Redis. **It did not instrument
it**, and named three candidates: `recompute_every_minute`, `publish_computed_forever` and
`ChainStream`'s solve.

## 2. The stall, measured

`measured` 2026-09-12T17:20–17:24Z, stack up 2 h, `main` at `9f0084f` plus this branch. One
`GET /api/health` at a time through the proxy, back to back, **3,993 samples over 260 s**, read
only, nothing restarted. The handler is trivial, so a request's wall time is dominated by how
long the event loop took to get round to it.

**Median 29.8 ms.** Every request over 500 ms, in order:

| Sent | Second of minute | Elapsed |
|---|---|---|
| 17:20:02Z | 02 | 3,506.6 ms |
| 17:20:08Z | 08 | 4,065.2 ms |
| 17:21:02Z | 02 | 5,285.9 ms |
| 17:21:08Z | 08 | 4,893.0 ms |
| 17:22:02Z | 02 | 4,970.9 ms |
| 17:22:08Z | 08 | 4,316.5 ms |
| 17:23:02Z | 02 | 4,911.2 ms |
| 17:23:08Z | 08 | 5,227.9 ms |
| 17:24:02Z | 02 | 6,820.9 ms |

**Two stalls a minute, at fixed offsets `:02` and `:08`, of 3.5–6.8 s each** — `measured`, and
every one of them longer than the 2.0 s socket timeout `BusConfig.client_kwargs` sets. That is
the number #111 asked for. Seconds `:59`, `:00`, `:01` and `:07` all carried samples with a
maximum under 285 ms, so the loop is responsive either side of each stall and between the two.

## 3. Which coroutine — narrowed to one task, not to one line

**The three candidates #111 named are excluded by their own cadences**, none of which is `:02`
or `:08`:

| Candidate | Cadence | Why it is not this |
|---|---|---|
| `recompute_every_minute` | `60 - (now % 60) - 0.5`, so `:59.5` | the loop answers inside 285 ms through `:59`, `:00`, `:01` |
| `publish_computed_forever` | the minute edge and a 10 s timer (`COMPUTED_SAMPLE_SECONDS`) | a 10 s timer would stall `:18`, `:28`, `:38` too; none of them does |
| `recompute_forever` (`ChainStream`'s solve) | every 0.1 s | a continuous tick raises the median, it does not cut twice a minute |

**What does fall on `:02` and `:08` is the store's bar publication.** `XREVRANGE
md.option_bar:DELTA:BTC + - COUNT 8000`, `measured` 2026-09-12T17:25Z, covering 300.0 s — the
second-of-minute of every one of the 8,000 entry ids:

| Second of minute | Entries per minute |
|---|---|
| `:02` | 517 |
| `:08` | 1,035 |
| anything else | 0 |

**Exactly periodic, and exactly the two seconds the loop stops answering.** 1,552
`md.option_bar` entries a minute arrive in two bursts, and `hld.md` §4 routes `md.option_bar` to
`BarBuffer` in the api process only. `bar-buffer` is also the api's only **lossless**
subscription, which is why it is the one that loses data rather than merely arriving late: the
task whose own work stalls the loop is the task whose read then times out.

**What is not established.** Which call inside that path holds the loop, and for how much of the
4–6 s — `BarBuffer`'s fold, the `/bars` pending-source recompute `main.py:1282` makes of it, or
something else downstream. Separating them needs a timer inside the api process, and instrumenting
the api means restarting it, which this branch is not permitted to do. **The correlation is
`measured` and the attribution to a coroutine is not.** The next step is one `perf_counter` pair
around `BarBuffer`'s drain, logged at `bus.reader`, and a restart at a flush boundary.

## 4. What the repair would be

Not settled here, and the choice depends on §3's missing number.

- **A longer socket timeout** is the smallest change and the weakest: it hides a 6 s stall
  rather than removing it, and `stale_after_seconds` is derived from that timeout, so raising it
  also raises how long a genuinely dead reader reads as alive. `bus-reader.md` §4 argues that
  bound is deliberately tight.
- **A thread hop** for the fold takes the CPU work off the loop and is the real fix if §3's
  number lands on `BarBuffer`. It needs the buffer's own locking looked at first.
- **Both** is the likely answer, and neither is this ticket.
