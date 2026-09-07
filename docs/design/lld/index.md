# Low-level designs — index

**A low-level design says how one component is built inside.** It is written when the component
lands, by the ticket that lands it, so that this folder records what was built rather than what
was planned. The parts and the traffic between them are in [../hld.md](../hld.md); what crosses
between them is [../events.md](../events.md).

**Every slot below is empty on purpose.** A ticket fills its own row: add `<name>.md` beside this
file, and change the row's status to a link. Do not write one ahead of the code.

| Component | Design | Landed by |
|---|---|---|
| Events — the envelope, the registry, the instrument | not yet written | #35, #37 |
| The broker adapter — the protocol, the Delta implementation, the fake | not yet written | #36 |
| The chain cache — invalidation, the recompute passes, watched pairs | not yet written | #37, #44 |
| The store — the four tables, sealing, flushing, ETH | not yet written | #37, #43 |
| The connection controller — the state machine, backoff, replay, commands | not yet written | #38, #41 |
| The supervisor — aggregate state, lifespan, the health report | not yet written | #39 |
| Logging — the JSON-lines formatter, the sinks, what is logged and at what level | not yet written | #42 |
| The historical read path — the ladder at a stored minute, and the day's minutes | [historical-read-path.md](historical-read-path.md) | #45 |
| The bars read path — one contract's minute bars for a date | not yet written | #46 |

## What a low-level design should contain

The things a reader would otherwise have to reconstruct from the code and could get wrong:
the types and their invariants; the states and what may change them; the failure modes and what
each one does; the seam the tests drive; and every number with its tag — `measured`, `assumed` or
`derived` — and the run that produced it. Not a restatement of the code.

Keep each one under 200 lines. A design that outgrows the bound is two components.
