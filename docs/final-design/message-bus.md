# Message bus

**What this page contains.** What the message bus is, its two implementations, the two delivery
guarantees it offers and why there are two, and the rules for naming, acknowledging, discarding and
persisting messages.

**How to read it.** The first three sections are concepts and are worth reading in order; the rest
are operational rules to come back to when changing the bus or diagnosing a problem. It assumes you
know what an event is -- if not, read [Events](events.md) first.

## What the bus is

The bus is the thing that carries messages from the part that produced them to the parts that want
them. It offers exactly two operations, and the entire design exists to keep those two honest.

| Operation | What it does |
|---|---|
| **Publish** | Put one event onto the bus. Returns immediately; the publisher never learns who read it |
| **Subscribe** | Ask for a copy of every event on a named stream, choosing a delivery policy |

Because that is the whole interface, producers and consumers are written without either knowing how
delivery happens -- in memory, or across a network. **Publishing must never wait.** The code reading the venue's connection publishes and returns to
reading immediately. If publishing could block, the venue's messages would pile up in the operating
system's buffer, the buffer would fill, and the venue would close the connection on us. That single
requirement is why a bus exists at all, instead of the reading loop simply calling the other parts.

## Two implementations, one interface

The same interface has two implementations, and a single setting chooses between them.

| | In-process fan-out | Redis Streams |
|---|---|---|
| Chosen by | leaving `DELTA_BUS` unset -- the default | setting `DELTA_BUS=redis` |
| How it delivers | queues inside one running program | through a Redis server, between programs |
| Used by | the single-process arrangement | the multi-process arrangement, including production |

Neither choice requires a producer or consumer to be written differently. In the multi-process
arrangement **Redis is required**: if it cannot be reached the feed refuses to start, because a feed
that looks healthy while silently discarding everything it receives is the worst failure available.

## The two delivery policies

Every subscriber picks one of two policies when it subscribes, and the choice does not change later.
The table below explains both.

| Policy | Behaviour when a reader falls behind | Who uses it |
|---|---|---|
| **Drop-oldest** | The oldest waiting messages are discarded, and the number discarded is counted | The live screen, and the small caches that track the feed's and the store's condition |
| **Lossless** | Nothing is discarded, however far behind the reader falls | The file writer, and the alert forwarder |

The reasoning differs in each case. For the live screen a price from four seconds ago is useless --
it keeps only the newest price per contract anyway, so discarding an older one loses nothing. For
the file writer a discarded message is a hole in the record that no later run can fill, and dropping
under load would systematically remove the busiest moments, which are exactly the ones a price
summary exists to capture.

**Every discard is counted, because a silent discard is a lie.** That extends to Redis, which
discards old entries quietly on its own: our reader notices how many it skipped and reports that
too. For a lossless subscription the configured size is a *warning level* rather than a hard limit --
exceeding it is counted and reported, but the message is still delivered, and an actual lossless
drop is logged as an error because it should be impossible.

## Why Redis, running as a container

Redis is not a message broker in the traditional sense; it is a fast in-memory data store that
happens to offer a stream data type, which is what we use. It runs as an ordinary container beside
the other programs, so publishing never leaves the machine. **It is treated as a pipe, not as
storage.** Losing it entirely costs a restart, not history: the
recorder resumes from its saved position and the screen refills within about half a second from the
prices that arrive next. The permanent record is the Parquet files. Moving to a managed Redis
service later would be a change of one connection string, with the configuration described under
*Persistence* below.

## How streams are named

Messages are separated into named **streams**, so a reader only receives what it asked for. Names
follow one pattern:

```
{kind of event}:{VENUE}[:{UNDERLYING}]

md.option_quote:DELTA:BTC
```

The colon separates sections, which is Redis's own convention, and dots stay inside a section. The
table below shows how finely each kind of stream is divided, and why.

| Level of detail | Which streams | Why that level |
|---|---|---|
| One per venue and underlying | the market-data and computed streams | A reader interested in Bitcoin must not have to receive and discard Ethereum |
| One per venue | the store's state, connection changes, heartbeats, operator commands | There is one connection and one recorder per venue, so no finer split is meaningful |
| One overall | alerts | Nobody wants a subset of the alerts |

**Stream names carry no environment name.** There is one Redis on a laptop and a different one in
production, never shared, so writing "production" into every key would record a fact already obvious
from which server you are talking to.

**A reader is told which streams to read; it never goes looking.** There is no wildcard and no
scanning. That sounds restrictive and is deliberate: a stream that appeared after a reader started
would silently not be read, and the damage would stay invisible until somebody examined the
historical record weeks later.

## How an event is laid out on the wire

Each event becomes one entry in a stream: the envelope fields written out individually, and
everything specific to that kind of event in one block of JSON called `payload`. The table below
shows which fields are always present and which are left out when they do not apply.

| Field | When it is present |
|---|---|
| `type`, `event_id`, `schema_version`, `source`, `ts_received`, `payload` | Always |
| `ts_venue` | Only when the venue supplied a time |
| `instrument` | Only when the event concerns one specific contract |
| `venue_symbol` | Only when we know the venue's own name for that contract |

Two rules govern this layout. **Absence is expressed by leaving the field out entirely** -- never by
writing an empty string, the word "null" or a zero -- and **inside the JSON payload a missing number
is written as JSON's own `null`**, which really does mean "no value" as distinct from zero.

Together these mean the whole event can be reassembled and validated by exactly the same code that
validates a locally produced one, so nothing extra had to be written to keep the guarantees working
across a network. Finally, **if an event's declared type disagrees with the stream it arrived on,
that is an error**: the stream's name is a claim about its contents, and a reader that trusts that
claim must be able to.

## Keeping your place in a stream

Redis lets several readers read one stream independently, each tracking its own position, by giving
each a **consumer group** name. The rules are short.

| Thing | Rule |
|---|---|
| Group name | The name of the service, lower case, one word: `store`, `api` |
| Reader name within the group | The service name and an instance number, such as `store-1` |
| Where a reader starts by default | At the end -- new messages only, nothing historical |
| Where the recorder starts | At its saved position; on its very first run, at the end. **Never from the beginning** |

Starting the recorder from the beginning would re-record everything Redis still happens to be
holding, duplicating data already written. **There is one group per service, never one per running
copy** -- that is what makes the screen and the recorder independent readers of the same messages
rather than competitors splitting them between themselves.

## Acknowledging, discarding and persisting

These three rules together decide what a restart loses, so read all three before changing one.

### Acknowledging

A reader tells Redis it has taken a batch **immediately on receipt, before doing the work**. This is
the opposite of the usual advice, and the reason is specific: acknowledging one message tells us
nothing about whether it reached a file, so it cannot be the safety net. The recorder's real safety
net is the position it saves after each successful file write, and it resumes from there.

A second benefit falls out of it. Because acknowledgement happens on receipt, anything still listed
as unacknowledged means precisely "handed over but not processed", so after a crash those entries
can be re-read and processed exactly once. Acknowledging after the work would mix processed and
unprocessed entries in that list and make it useless. The other rules that go with it follow.

| Rule | What it says |
|---|---|
| The durability boundary is the **file write**, not the acknowledgement | Up to five minutes of sealed data can be waiting in memory |
| The API never replays anything | It starts at the end and refills from what arrives next |
| A discarded position is reported, not fatal | The recorder replays what is left, reports exactly how many entries it lost, alerts, and carries on |
| That check runs continuously | Checking only at start-up would miss a reader that dies while running |
| While catching up, "now" means the time inside the messages | Otherwise a catch-up judges every replayed minute late and discards what it just recovered |

### Discarding old messages

Redis holds messages until told to discard them. We discard by **age**, keeping a thirty-minute
window, and the instruction rides along with every batch of new messages rather than running on its
own timer -- one fewer thing that can stop.

Age is used rather than a count because a count is really a guess about how busy the market is: it
would hold hours in a quiet market and four minutes in a busy one. Thirty minutes is the promise the
bus makes to a recorder that has to restart.

### Persistence

Redis is started with all of the following, and each matters.

| Setting | Effect | Why |
|---|---|---|
| `appendonly no` | No write-ahead log on disk | The Parquet files are the archive |
| `save ""` | No periodic snapshots | The default settings would make Redis copy itself to disk roughly every minute at our message rate |
| no attached disk | Nothing survives the container | Deliberate: there is nothing here worth surviving |
| `maxmemory 2gb` | A ceiling on memory use | Roughly twice what thirty minutes of both underlyings needs |
| `maxmemory-policy noeviction` | At the ceiling, **refuse new writes with an error** | See below |

That last one is a decision about losing data, not about tuning. Redis's usual behaviour at its
memory ceiling is to quietly discard whole keys to make room -- and one of our keys is one entire
stream, so a whole category of market data could vanish with nothing reported. A loud error at the
publisher is far better. Managed Redis services default to the quiet behaviour and must be
reconfigured.

## Where to go next

[Events](events.md) describes the messages themselves; [Configuration](configuration.md) lists every
setting named here.
