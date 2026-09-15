# Data flow

**What this page contains.** A trace of one price from the moment the venue sends it to the moment a
person sees it on screen and it lands in a file, followed by an explanation of what the system does
when the connection to the venue misbehaves.

**How to read it.** The first section is a numbered walk-through; read it alongside the diagram in
[Architecture](architecture.md), which names the same parts. The later sections can be read on their
own when you need them.

## The life of one price

Suppose somebody on Delta Exchange changes their offer to buy one Bitcoin call option. Here is
everything that then happens, in order.

**1. The message arrives.** The venue sends a small packet of data over the open connection. The
feed's reading loop takes it, notes the time it arrived, and hands it straight onward without
examining it. This handover is deliberately the fastest thing the feed does: as explained in
[Architecture](architecture.md), a reading loop that stops to think loses the connection.

**2. The message is translated.** The packet is in the venue's own format -- short field names,
prices as text, its own way of spelling the contract. The **adapter** turns it into one of our own
messages: a *top-of-book* event, carrying the bid, the ask, the sizes offered, and a description of
the contract written in our vocabulary rather than the venue's.

Two things are decided here and can never be recovered later if they are decided wrongly. The first
is absence: if the venue reports no bid, that becomes "there is no value here" rather than a bid of
zero. The second is the two timestamps -- when the venue says it sent the message, and when we
actually received it -- both of which are carried forward unaltered. The gap between them is real
information about the network, not an error to be smoothed away.

**3. The message goes onto the bus** and is delivered to everyone who asked for that kind. In
practice that is two readers, with opposite requirements: the API takes it under a drop-oldest
policy, and the store takes it losslessly.

**4. The API updates its picture.** It replaces whatever it previously knew about that contract with
this newer information. It keeps only the newest state per contract, never a history -- that is the
store's job.

**5. The mathematics runs.** On its own cycle, the API takes the current state of a whole expiry and
computes, in this order:

  - the **forward** -- what the market currently expects the underlying to be worth at expiry;
  - for each strike, the **implied volatility**, recovered by inverting the price of whichever of
    the two contracts at that strike is out of the money;
  - from that volatility, the five **Greeks** for both contracts at that strike.

If the volatility cannot be recovered for a strike, the process stops there for that strike and no
Greeks are produced for it. The result is published back onto the bus as its own message, so the
store can record what we concluded alongside what we observed.

**6. The browser is told.** Anyone with the page open has a connection held open to the API, and
about once a second it sends them the rebuilt chain. The object it sends is byte-for-byte the same
shape as the one the ordinary "give me the chain" web address returns, which is why one piece of
display code can render either.

**7. The store folds it into a minute.** The store puts the observation into the bucket for the
minute the *venue* says it happened in -- never the minute we received it in. When that minute is
over, plus a short grace period, the bucket is sealed.

**8. The minute is written.** Sealed minutes pile up in memory and are written out to a Parquet file
every five minutes. The writing happens on a separate thread, so a large file write never pauses the
part of the program reading the connection.

## Why the two timestamps both travel

Every message carries the venue's own timestamp and ours. Neither is corrected against the other,
even when they disagree, because the disagreement is the interesting part: it says how long the
message spent in transit.

The measured difference is very different on the two streams -- a couple of hundred milliseconds on
the order book, around three seconds on the ticker -- and that is exactly why the store waits longer
before sealing a minute of ticker data than a minute of order book data. A single grace period would
either seal the ticker too early and lose data, or seal the book too late and delay everything.

One caution for anyone writing timing code here: our timestamp comes from the ordinary wall clock,
which can jump backwards when the machine corrects itself against a time server. It is fine for
recording *when* something happened and useless for measuring *how long* something took. Elapsed
time is always measured with a separate clock that only ever counts forward.

## What happens when the connection misbehaves

A connection to a venue does not simply work or fail. It can be silent without being closed, it can
close and reopen, and it can fail to come back at all. The system therefore models the connection as
being in exactly one of five named conditions at any moment.

The table below lists them and says what each one means in practice.

| Condition | What it means |
|---|---|
| `connecting` | We are dialling, or we have dialled and are re-registering our interest in every contract |
| `connected` | Working normally: the connection is open and messages are arriving |
| `degraded` | The connection is still open, but nothing has arrived for longer than expected. Possibly a quiet market, possibly a problem |
| `reconnecting` | The connection has gone. We are waiting a short while before trying again, and the wait grows with each failed attempt |
| `stopped` | Not connected and not trying. Either an operator paused it, or we ran out of reconnection attempts and gave up |

Moving between these conditions is the only way the connection's state ever changes, and every move
produces exactly two things: one message on the bus, and one line in the log. That pairing is what
makes it possible to reconstruct afterwards what a connection was doing at any moment.

The table below lists every move that can happen and what causes it.

| From | To | What causes it |
|---|---|---|
| (start) | `connecting` | The feed starting, or an operator resuming a paused feed |
| `connecting` | `connected` | The connection opened and every subscription was re-registered |
| `connected` | `degraded` | Nothing has arrived for longer than the staleness limit |
| `degraded` | `connected` | Something arrived |
| `degraded` | `reconnecting` | The silence went on past a second, longer limit |
| anything | `reconnecting` | The connection closed, or an operator asked for a reconnect |
| `reconnecting` | `connecting` | The waiting period elapsed and another attempt is being made |
| `reconnecting` | `stopped` | The allowance of attempts ran out. This also raises an alert and logs an error |
| anything | `stopped` | An operator paused it. No allowance is consumed |

Two of these deliberately do **not** raise an alert. Entering `degraded` does not, because fifteen
quiet seconds is already visible as a badge on the screen, and because a quiet market is normal.
A pause does not, because somebody just did it on purpose. An alerting system that cries out about
ordinary things is an alerting system people learn to ignore.

## What "healthy" means for each program

Each program answers a web address that says whether it is working, and the three mean different
things, which is worth knowing before you rely on one.

| Program | What its health check actually reports |
|---|---|
| Feed | Whether it is connected and reading. It reports unhealthy when stopped, paused, out of reconnection attempts, silent for over two minutes, or when one of its internal loops has died |
| Store | Whether it is *keeping up*, not merely alive. It reports unhealthy if it has stopped reading, or fallen so far behind that messages it still needs are being discarded |
| API | Whether it is alive, plus what it knows about the feed -- which it knows from the feed's own messages, not by asking the feed |

The badge on the web page comes from the same information the API's health check uses, so the page
and the health check can never disagree with each other.

## Threads, and the one place they are used

Almost all of this happens in a single-threaded loop that switches between jobs whenever one is
waiting: reading the socket, reading the bus, running the mathematics, answering web requests.

There is exactly **one** exception. Writing a Parquet file can take a noticeable amount of time and
cannot be paused halfway, so it is handed to a separate worker thread. Nothing else is threaded, and
nothing is shared across that boundary except the batch of finished data being written.

## Where to go next

[Events](events.md) describes each message named above in full.
[Data store](data-store.md) explains what happens to a minute after it is written.
