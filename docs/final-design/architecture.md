# Architecture

**What this page contains.** A picture of the whole system, followed by a description of every part
in it: what the part is, what it is responsible for, and what it deliberately does not do. It also
explains the two different ways the same code can be run, and finishes with a short summary of the
machines it runs on in production.

**How to read it.** Start with the big picture, then read the descriptions of the parts in order --
they are arranged in the direction the data flows, from the venue at one end to the browser at the
other. If you want to follow a single price all the way through, read [Data flow](data-flow.md)
afterwards. Words in **bold** on first use are defined in the [Glossary](glossary.md).

## The big picture

In one sentence: the system holds **exactly one** connection to the venue, converts everything
arriving on it into messages in our own vocabulary, and lets several independent parts read those
messages at their own speed without any of them talking to each other directly.

The diagram below shows those parts and the direction information travels between them. Delta
Exchange is at the top, a person's browser is at the bottom, and permanent storage is on the right.

```
                         DELTA EXCHANGE
                     (the venue, on the internet)
                               |
                               | one WebSocket connection, plus
                               | occasional ordinary web requests
                               v
             +-----------------------------------+
             |  FEED                             |
             |  Talks to the venue. Converts     |
             |  its messages into our own.       |
             +-----------------------------------+
                               |
                               | events, in our vocabulary
                               v
             +-----------------------------------+
             |  MESSAGE BUS                      |
             |  Delivers each event to everyone  |
             |  who asked for that kind.         |
             +-----------------------------------+
                    |                  |
      newest-price  |                  |  every single message,
      only          |                  |  nothing dropped
                    v                  v
     +---------------------+   +---------------------------+
     |  API                |   |  STORE                    |
     |  Keeps the latest   |   |  Folds messages into       |
     |  prices, does the   |   |  one-minute summaries and  |
     |  mathematics, and   |   |  writes them to files.     |
     |  serves web pages.  |   +---------------------------+
     +---------------------+                |
                    |                       v
                    |               +-----------------+
                    |               |  PARQUET FILES  |
                    |<------------- |  (the permanent |
                    |   reading     |   record)       |
                    |   history     +-----------------+
                    v
             +---------------+
             |  WEB          |
             |  The browser  |
             |  page         |
             +---------------+

       ALERTS -- a small separate listener that watches for problems
       only, and posts them to a chat channel.
```

## The parts, one at a time

### Feed -- the only part that knows the venue exists

The feed opens one WebSocket connection to Delta Exchange and subscribes to every contract listed
for the underlyings it has been configured to watch. Everything that arrives is converted
immediately into our own message format and published onto the bus.

This is the only part of the entire system that understands the venue's vocabulary. Nothing
downstream ever sees the venue's own message format, its names for things, or its way of spelling a
contract. That containment is what makes it possible to add a second venue later by writing one new
piece of code rather than by editing everything.

**One connection, no matter how many people are watching.** Browsers connect to us, not to the
venue, so ten people looking at the chain is still one connection to Delta.

One rule inside the feed shapes its whole design: the code reading the socket must never stop to do
work. It reads a message, hands it to the bus, and goes straight back to reading. If it paused -- to
do arithmetic, or to wait for a slow file write -- messages would pile up in the operating system's
buffer, that buffer would fill, and the venue would close the connection on us.

### Message bus -- the postal service

The bus accepts messages from whoever produced them and delivers a copy to every part that asked for
that kind of message. The producer never knows who is listening, and the listeners never know about
each other.

Every subscriber chooses one of two delivery policies when it signs up, and the choice is permanent.
The table below explains the two policies and who uses which.

| Policy | What happens when messages pile up | Who uses it, and why |
|---|---|---|
| **Drop-oldest** | The oldest waiting messages are thrown away, and the number thrown away is counted | The live screen. A price from four seconds ago is of no use to a screen showing "now", so throwing it away is correct |
| **Lossless** | Nothing is thrown away, however far behind the reader falls | The file writer. A thrown-away message would be a permanent hole in the historical record that no later run could fill |

These two needs genuinely conflict, which is why the screen and the recorder are separate parts
reading the same messages rather than one part doing both jobs: one wants only the newest state, the
other wants every state. The full set of rules is in [Message bus](message-bus.md).

### API -- the calculations and the web server

The API keeps the newest known price for every contract in memory and rebuilds a full option chain
from it whenever one is asked for. On a repeating cycle it performs the mathematics: the expected
future price of the underlying, then the implied volatility of each strike, then the Greeks. It also
answers every web address the front end uses -- the ordinary request-and-answer ones, and the
long-lived connection that pushes a fresh chain to the browser about once a second -- and it reads
the stored files to answer questions about the past.

### Store -- the permanent record

The store subscribes losslessly and folds everything arriving into one-minute summaries. When a
minute is finished, and a short grace period has passed to catch stragglers, it is *sealed* and no
more observations can be added. Sealed minutes accumulate in memory and are written to Parquet files
every five minutes.

**The store is the only part allowed to write those files.** When the system is running in its
multi-process arrangement, this is enforced by giving every other part read-only access to the
folder, rather than by trusting them not to write.

### Web -- the browser page

A Next.js application that displays the chain, the charts and the volatility screens. It performs no
arithmetic of its own: every number it shows arrived from the API already computed, and the page
refuses to display a value that reaches it in an unexpected shape rather than guessing at it.

### Alerts -- the part that only watches for trouble

A small separate program that listens for one kind of message -- an alert -- and posts it to a
Discord channel. It subscribes to nothing else, so it costs almost nothing to run, cannot interfere
with anything, and never contacts the feed, the store or the API to do its job.

### The pure core -- where the mathematics lives

Beneath all of the above sits a set of files containing only calculations: the pricing formulas, the
volatility solvers, the Greeks, the code that folds observations into minute summaries. None of it
touches the network, the clock or the disk. This is a deliberate discipline: code that cannot touch
anything outside itself can be tested exhaustively and instantly, which is why the project runs
sixteen hundred tests in a few seconds, and why new logic is expected to go here whenever possible.

## The two ways to run it

The same code can be run as one program or as several. Which one you get is decided by a single
setting, `DELTA_BUS`.

The table below compares the two arrangements. Neither is a different version of the software; they
are the same files, composed differently.

| | **Monolith** (one process) | **Split mode** (several processes) |
|---|---|---|
| Chosen by | leaving `DELTA_BUS` unset -- this is the default | setting `DELTA_BUS=redis` |
| What runs | one program doing everything | four programs plus a Redis server |
| How messages travel | in memory, within the one program | through Redis, between the programs |
| Who talks to the venue | that one program | the feed, and nothing else |
| Who writes files | that one program | the store, and nothing else |
| Used for | developing and testing on a laptop | production, and the local Docker setup that mirrors it |

The monolith exists because it is the easiest way to work on the mathematics: one thing to start,
one place to put a breakpoint. Split mode exists because in production the parts have different
needs -- the feed must never be slowed down, the store must never lose a message, and the API can be
restarted for a deployment without interrupting either.

**In split mode, no program ever calls another program directly.** Everything travels as messages on
the bus. Even an operator's instruction to pause the feed, which arrives as an ordinary web request
to the API, is turned into a message and published rather than passed along as a second web request.

## Where it runs in production

The back end runs on **one rented Amazon computer** (an EC2 instance) in Mumbai, holding the feed,
Redis, the store, the API, the alert forwarder, and a small proxy that handles encryption for the
API. They are together on purpose: they form one pipeline, and keeping them on one machine means
messages pass between them without ever crossing a network.

The browser page is **not** on that machine. It is built and served by **AWS Amplify**, which
compiles the front end from the repository whenever the code changes and serves it worldwide. The
page holds no data and talks to no venue, so it gains nothing from sitting next to the feed and
gains a lot from being served close to the reader.

The permanent Parquet files are stored in **Amazon S3**. The table below summarises the production
setup; every figure, and what it costs, is in [Deployment](deployment.md).

| Piece | Where it runs | Size |
|---|---|---|
| Feed, Redis, store, API, alerts, proxy | One EC2 computer, Mumbai region | 4 processors, 8 GB of memory |
| The web page | AWS Amplify | Managed by Amazon; no size to choose |
| The stored files | Amazon S3 | Grows by roughly 170 MB a day |

## Where to go next

[Data flow](data-flow.md) follows one price through every part above, and [Events](events.md) lists
the messages the parts send each other.
