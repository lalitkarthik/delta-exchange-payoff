# Adapters

**What this page contains.** An explanation of what an adapter is and why the system has one, a
description of the eight things every adapter must be able to do, and a walk through how the Delta
Exchange adapter is built, ending with what it does when a message cannot be used.

**How to read it.** Read the sections in order; each builds on the one before. When you want to
connect a different exchange, [Adding an adapter](adding-an-adapter.md) is the practical guide, and
it only makes sense after this page. Both assume you have read [Events](events.md), because an
adapter's whole job is to produce events.

## What an adapter is

Every exchange describes the world slightly differently. It has its own names for contracts, its own
field names, its own way of saying "there is no price", its own idea of which updates belong on
which connection. If that vocabulary were allowed to spread through the codebase, adding a second
exchange later would mean editing every file.

An **adapter** is the one piece of code that knows a particular exchange. Everything it says outward
is in our own vocabulary -- the contracts and events described in [Events](events.md) -- and
everything it hears inward is in the exchange's. **Nothing downstream of an adapter ever sees the
exchange's own data format**, or its names, or its channels.

In the multi-process arrangement, the feed program owns the adapter and is the only program that has
one. The API has no connection to any exchange, and the store has never had one.

## What an adapter must be able to do

An adapter has to provide eight things, and no more. They fall into three groups, which the table
below lists along with what each one promises.

| Group | What it provides | What it promises |
|---|---|---|
| Describe itself | `venue` | The exchange's short name in capitals, such as `DELTA` |
| Describe itself | `underlyings` | Which underlyings to record. This is configuration, not something fixed in code |
| Feed | `instruments` | Every contract the exchange currently lists for one underlying, described in our vocabulary |
| Feed | `subscribe` | Register interest in contracts. Must work even before any connection exists |
| Feed | `on_connection` / `off_connection` | Let something else be told when the connection opens and closes, and stop being told |
| Feed | `stream` | **Connect once, deliver messages until that connection ends, then return** |
| Feed | `stop` | Ask `stream` to return |
| Read | `expiries`, `chain_snapshot` | Answer two ordinary questions the screens need, by asking the exchange directly |

A few of these carry important subtleties.

**`stream` handles exactly one connection.** It dials, re-registers every subscription, delivers
messages until that connection ends, and returns. It does not retry. Deciding whether to try again,
how long to wait first, and when to give up altogether belongs to the layer above it -- because the
layer that has to announce "we have given up" is the layer that should be deciding it.

**`stream` pushes messages rather than being asked for them.** It is handed a function to call for
each message. That function must never pause, because the same loop is reading the connection, and a
pause there loses the connection.

**An adapter reports facts about its connection, never conclusions.** It can say "the connection
opened" and "the connection closed", and nothing else. Whether that means the system is healthy,
degraded, or reconnecting is decided elsewhere, by the state machine described in
[Data flow](data-flow.md). An adapter that reported states would be a second state machine quietly
disagreeing with the first.

**"Opened" means more than "connected".** An adapter must only report that its connection is open
once it has also re-registered interest in every contract. A freshly opened connection with no
subscriptions on it delivers nothing at all, and looks perfectly healthy while doing so. That is
precisely the failure this whole layer exists to prevent.

## How the Delta adapter is built

The table below lists the files involved and what each one holds.

| File | What it holds |
|---|---|
| `adapters/base.py` | The list of eight things an adapter must provide |
| `adapters/delta.py` | The Delta adapter itself, and the conversion from Delta's contract names to ours |
| `adapters/delta_socket.py` | The connection to Delta, and the only two places the exchange's channel names appear |
| `wire.py`, `delta_client.py` | Where each field sits in Delta's messages, and the ordinary web requests |

There is an automated test whose only job is to search the whole codebase for Delta's two channel
names and fail if they appear anywhere outside this folder.

The connection file itself performs no interpretation. It reads a message, attaches the channel it
came from and the time it arrived, and passes it on unchanged. The interpretation happens one layer
above, which keeps the part that must never pause as simple as possible.

### Turning exchange messages into ours

Delta's two streams produce three kinds of our events, as the table below shows.

| Delta sends | We produce |
|---|---|
| An order book update | One top-of-book event for that contract |
| A ticker update | One reference event for that contract |
| A ticker update | One spot-price event for the underlying |

The last row deserves a note, because it looks wasteful. Every ticker message carries the
underlying's price, and that price is usually identical to the one in the previous message. We
publish it every time anyway. The reason is the permanent record rather than the screen: the stored
data counts how many observations a minute contained, and that count is what later tells "the price
did not move" apart from "nothing was running at all". A deduplicated stream cannot answer that.

### Turning exchange names into ours

Delta calls a contract `C-BTC-77600-040926`. We call the same contract
`DELTA-BTC-20260904-77600-C-USD`, and we keep Delta's own name alongside so we can always ask Delta
about it again without a lookup table.

If a name arrives that does not describe an option at all, the adapter returns nothing and counts
it, rather than raising an error or guessing. Guessing would be actively dangerous here: the
underlying becomes a folder name in the permanent record, and a wrong guess would file Bitcoin
quotes under Ethereum.

### The rule about absent prices

This is the single most important thing the adapter does, and it cannot be fixed anywhere else.

Delta expresses "nobody is offering a price" in three different ways: the text `"0"`, an empty
string, and a proper null. All three become *absent* in our events -- for prices, sizes and
volatility figures alike -- because displaying any of them as zero would claim somebody offered to
buy at nothing. A genuine zero in a quantity like open interest stays zero, because there it is a
real measurement.

The reason this must happen in the adapter is that nothing downstream can undo it. Our event format
cannot enforce the rule on its own: the text `"0"` handed to a numeric field is silently converted
to the number zero, and by then the distinction is gone forever.

A related rule covers nonsense numbers. JSON technically permits the values `NaN` and `Infinity`,
and our event format refuses them outright. So the adapter converts them to absent first and counts
how often it happened. When a price is absent, the size that went with it is dropped too -- a size
without a price would describe an order at no price at all.

### Timestamps

Delta's own timestamp is converted carefully enough that the last digit survives, because the
permanent record groups observations by it. Our own arrival time is recorded separately. **Neither
is ever corrected against the other**: the gap between them is real information about the network.

## When things go wrong

The table below lists every way a message can be unusable and what the adapter does about it. The
pattern throughout is that a bad message is counted and skipped, never allowed to stop the feed.

| What goes wrong | What happens |
|---|---|
| The message is not valid JSON | A counter increases; reading continues |
| The message is valid JSON but makes no sense | A counter increases and **nothing is published**. The first such message is logged; the rest are only counted, because logging each one would flood the log at over a thousand messages a second |
| The name is not an option contract | A counter increases; no events are produced |
| A price is `NaN` or `Infinity` | Treated as absent, and counted |
| The exchange gives no timestamp | The field is left empty. Our clock is never substituted for it |
| The exchange sends its own housekeeping messages | Discarded at the connection layer; they never reach the bus |
| Configuration names an underlying the exchange does not list | It is dropped and logged as an error; the others still record |
| The exchange is unreachable at startup | The ordinary web routes still answer; the connection reports that it is waiting |

## Where to go next

[Adding an adapter](adding-an-adapter.md) is the step-by-step guide to connecting another exchange.
[Events](events.md) describes what an adapter must produce.
