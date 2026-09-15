# Events

**What this page contains.** An explanation of what an event is and why the system is built out of
them, a description of the fields every event carries, and then a catalogue of all ten kinds of
event with what is inside each one.

**How to read it.** Read the first two sections properly -- they explain the idea and the common
fields, and everything after depends on them. The catalogue that follows is a reference: skim it
once, then come back to individual entries when you need them. How events physically travel between
programs is a separate subject, covered in [Message bus](message-bus.md).

## What an event is, and why we use them

An **event** is a single self-contained message describing something that happened: a price changed,
a minute of data was finished, a connection dropped. One part of the system produces it and puts it
on the bus; other parts read it. **The producer does not know who the readers are, and the readers
do not know about each other.**

The value of arranging things this way is that it lets the parts be built, tested and replaced
separately. If the part producing prices and the part recording them agree on what a price event
looks like, then either can be rewritten without opening the other, and a test can feed one of them
handmade events without any venue or network being involved. The events, in other words, are the
contract between parts that never speak to each other directly.

There are exactly ten kinds. Nine of them travel outward from the part that produced them. One --
an operator's instruction -- travels inward.

## What every event carries

Regardless of kind, every event carries the same seven fields, called the envelope. The table below
lists them and explains what each is for.

| Field | What it is for |
|---|---|
| `type` | Which of the ten kinds this is. If a program receives a type it does not recognise, it raises an error rather than ignoring the message |
| `event_id` | A unique identifier for this one event. It lets a reader spot a duplicate, and lets a log line be matched up with the event that caused it |
| `schema_version` | Which version of this event's shape is being used. See *Changing an event* below |
| `source` | Who produced it -- the name of the venue it came from, or of the component that raised it |
| `ts_venue` | The time the venue itself says the thing happened. Empty if the venue gives no time |
| `ts_received` | The time we received it, by our own clock |
| `instrument` | Which contract this is about, in our own vocabulary. Empty for events that are not about a single contract |

Three rules are worth stating plainly, because a surprising amount of the design follows from them.

**The two timestamps are never reconciled.** The venue's time is recorded exactly as the venue gave
it, even when it looks wrong. The difference between the two is real information about the network.

**Events cannot be modified once created.** A reader holding an event must not be able to change
what another reader sees.

**An unexpected field is an error, not something to ignore.** If an event arrives carrying a field
nobody declared, that is either a bug in the producer or a newer version this reader does not
understand. Both should stop loudly rather than continue quietly.

## How a contract is named

Every event about a specific contract carries an `instrument`: our own description of it, made up of
the venue, the underlying, the expiry date, the strike, whether it is a call or a put, and the
currency it is priced in. Written out as a single string it looks like this:

```
DELTA-BTC-20260627-60000-C-USD
```

That reads as: on Delta, a Bitcoin option, expiring on 27 June 2026, at a strike of 60,000, a call,
priced in US dollars. The venue's own name for the same contract is carried alongside, unchanged, so
that we can always ask the venue about it again.

A few details about this string matter in practice:

- **It is always recalculated from the underlying values, never stored as the truth.** It exists so
  that a log line, a cache key, a web address and a message field all describe a contract the same
  way.
- **The currency at the end is the currency the contract is *priced* in.** The currency it settles in
  is recorded separately and does not appear in the string, because a string naming two currencies
  with no way to tell which is which would be worse than not naming one at all.
- **The strike never has trailing zeros or scientific notation.** `60000`, never `60000.0` and never
  `6E+4`, because those would produce different-looking keys for the same contract.
- **Neither the venue name nor the underlying may contain a dash**, since dashes separate the parts.

## The ten events

### Prices and market data

**`md.option_quote` -- the best bid and offer for one contract.** Produced by the adapter from the
venue's order book stream, roughly every half second per contract. It carries the bid, the ask, the
sizes offered at each, and the time of the venue's last trade in that contract. The screen and the
recorder both read it. An absent price is recorded as absent, never as zero.

**`md.option_reference` -- the venue's own view of one contract.** Produced from the venue's slower
stream, roughly every five seconds per contract. It carries the venue's mark price, its last traded
price, open interest, turnover, and its own implied volatility and Greeks. **Everything in here is
recorded for comparison and never used in a calculation** -- there is a test whose only job is to
fail if that ever changes. It also carries a bid and an ask of its own, which act as a fallback when
the order book stream has been silent for a whole minute.

**`md.index_quote` -- what the underlying itself is worth.** Produced from the same slower stream,
once per message rather than only when the value changes. Repeats are kept deliberately: the stored
data counts observations, and that count is how we later tell "the price did not move" apart from
"nothing was running". It names the underlying rather than a contract, because a spot price belongs
to Bitcoin, not to any particular option on Bitcoin.

**`md.option_bar` -- one finished minute.** Produced by the store when a minute is sealed. It says
which collection it belongs to, which underlying, which minute, and carries that minute's figures.
A minute in which nothing arrived produces no event, because it produces no record.

**`computed.chain` -- what we concluded about one expiry.** Produced by the API after each round of
mathematics. It carries the forward, the discount factor, how the forward was computed, the time
remaining to expiry, and then per strike: the implied volatility, which of the two contracts it was
recovered from, why it failed if it failed, and the five Greeks. It is stamped with the version of
the model and the name of the solver that produced it, so a stored row can always be traced to the
code that made it. A volatility of zero is not a thing, and the system refuses to record one.

### Health and control

**`store.state` -- how the recorder is doing.** Published every ten seconds and whenever something
changes: whether it is recording, how much is waiting in memory, how much has been written, and
whether it has fallen behind.

**`feed.connection` -- the connection changed state.** Published once for every move between the
five conditions described in [Data flow](data-flow.md). It says which connection, what it moved
from, what it moved to, and why. The API forwards it to every open browser so the badge on the page
is never stale.

**`heartbeat` -- the feed is still alive.** Published on a timer whatever else is happening. It is
**not** a message from the venue; it is the feed saying "I am running", and it carries how long it
has been since a real message arrived. That is what lets a quiet market be told apart from a dead
connection.

**`alert` -- something a person should look at.** Published by whichever part noticed. It carries a
severity, a short stable code, and a description. The table below lists the codes raised today.

| Code | Raised when |
|---|---|
| `connection_silent` | Nothing has arrived for long enough that we are about to reconnect |
| `poll_failing` | The watchdog that checks the connection is itself failing |
| `store.flush_failed` | Writing a file failed |
| `store.replay_gap` | The recorder needed messages that the bus had already discarded |
| `store.consumer_lag` | The recorder has fallen too far behind |
| `store.empty_generation` | A whole recording period produced no data at all, while believing it was recording |
| `bus.reader_stopped` | A reader gave up after repeated failures |

**`control.command` -- the one instruction that travels inward.** Raised when an operator uses the
web address for pausing, resuming or reconnecting a feed. It names the component it is addressed to,
and any component it is not addressed to ignores it. Pausing costs none of the reconnection
allowance, because a pause is a decision rather than a failure; resuming restores the allowance in
full.

## Why no event mentions a channel

The venue organises its updates into named "channels". None of those names appear in any event,
because a channel name is the venue's vocabulary and the whole purpose of the adapter is that no
other part of the system learns it.

This left one genuine question. The stored data records whether a minute's prices came from the
order book or from the slower fallback stream, and simply counting messages cannot answer it. The
answer chosen was that **the type of the event is itself the answer**: a figure derived from a
`md.option_quote` came from the book, and one derived from `md.option_reference` came from the
fallback. Nothing needed to be added to any message.

## Changing an event

Each kind of event carries a version number, and the rule for when to increase it is narrower than
people expect.

**Adding a new optional field does not change the version.** An older reader ignores the new field,
a newer reader uses it, and nothing that was true stops being true.

**Changing what an existing field means does change the version**: renaming it, changing its units,
or changing the circumstances in which it is empty. Those are the changes that would let an old
reader quietly misinterpret a message, and a version bump is how that is prevented.

A reader that receives a version it does not know **must fail loudly rather than guess**, naming both
the version it received and the version it understands. Versions are tracked per kind of event, so
changing one leaves the other nine untouched.

Three things are fixed and are not a versioning matter at all, because changing any of them would
simply be a bug: volatility travels as a decimal fraction and is turned into a percentage only when
displayed; every decimal number travels as a number and never as text; and open interest counted in
contracts is a different quantity from open interest valued in dollars.

## Where to go next

[Message bus](message-bus.md) explains how these messages actually get from one program to another.
[Adapters](adapters.md) explains who produces the market-data ones.
