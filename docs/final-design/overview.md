# Overview

**What this page contains.** A description of what the system does, written for somebody who has
never seen the project and does not need to know any finance to follow it. It starts with the
problem the project exists to solve, explains the small amount of options vocabulary needed to
understand that problem, and then walks through each feature in turn.

**How to read it.** Read it from the top; each section assumes the one before it. If a word is
unfamiliar and not explained here, it will be in the [Glossary](glossary.md). When you have
finished, [Architecture](architecture.md) shows how the software is arranged to do all of this.

## The short version

Delta Exchange India is an online marketplace where people trade **options** on Bitcoin and
Ethereum. This project connects to that marketplace, continuously receives the prices being quoted
there, performs its own mathematics on those prices, displays the result on a web page that refreshes
about once a second, and writes a permanent minute-by-minute record of everything it saw so that any
past day can be examined again later.

It is a tool for studying a market, not for trading in one. It places no orders, holds no money, and
needs no account: everything it reads is public.

## A little vocabulary

An **option** is a contract about the future. It gives its owner the right -- but not the obligation
-- to buy or sell something at a price agreed today, on a date agreed today. The "something" is
called the **underlying**, and here it is Bitcoin or Ethereum. The agreed price is the **strike**,
and the agreed date is the **expiry**. An option to buy is a **call**; an option to sell is a
**put**.

For one underlying and one expiry, the venue lists many contracts: a call and a put at each of
several dozen strikes. Displayed together as a table, one row per strike with calls on the left and
puts on the right, that is an **option chain**, and it is the main screen of this application.

The interesting question about an option is not really its price. It is what that price implies
about how much people expect the underlying to move around before the expiry date. That expected
amount of movement is called **volatility**, and the figure recovered backwards out of a market
price is called **implied volatility**. Computing it well, and honestly, is what this project is
mostly about.

## The problem the project exists to solve

The venue sends us two separate streams of information, and they do not arrive at the same speed.

The table below shows the two streams, what each one carries, and how often each contract's
information is refreshed on it. The refresh figures were measured against the live venue.

| Stream | What it carries | How often it refreshes |
|---|---|---|
| The order book stream | The best price anyone is currently offering to buy at, and to sell at | `measured` every **508 milliseconds** per contract |
| The ticker stream | The underlying's price, how many contracts are outstanding, and the venue's own implied volatility and Greeks | `measured` every **5,001 milliseconds** per contract |

The second stream is roughly **ten times slower** than the first. That matters, because the venue's
own implied volatility figures travel on the slow stream. They were calculated from prices that, by
the time we see them, may be several seconds out of date, while the actual buying and selling prices
have moved on.

So this project takes a deliberate position: **the venue's implied volatility and Greeks are
recorded but never used in any calculation.** They are kept as a column to compare against, nothing
more. Everything the project shows as its own number is worked out from the fast stream, from the
prices people are actually quoting. There is an automated test whose entire job is to fail if any
venue-supplied volatility figure is ever fed into a calculation.

## What the system does, feature by feature

### The live option chain

The main screen shows one underlying and one expiry at a time: every strike, calls on the left,
puts on the right, updating about once a second over a connection that stays open. Above the table
sits a small badge that appears whenever the connection to the venue is not healthy, and disappears
by itself when it recovers, so nobody is left reading stale numbers and believing they are live.

### Implied volatility, computed here

Working out implied volatility means running a standard pricing formula backwards: searching for the
volatility figure that would produce the price the market is charging. The project does that with
two deliberate choices.

The first is that **volatility is treated as a property of the strike, not of the individual
contract**. For a given strike the call and the put describe the same expectation, so the project
computes one figure and writes it to both, recording which of the two it came from.

The second is *which* of the two it uses. It always inverts the **out-of-the-money** contract -- the
call if the strike is above the expected future price, the put if below. That contract's price is
made up almost entirely of expectation rather than of present value, which makes the answer far more
stable.

There is also a rule about failure. **If no volatility can be recovered for a strike, that strike
shows no Greeks at all.** Filling them in using some default volatility would put five plausible
numbers on the screen that describe nothing, and a reader has no way to tell them from real ones.

### Greeks

Once volatility is known, the project computes the five **Greeks** -- the numbers describing how an
option's price responds to a change in the underlying's price, in volatility, in time, and in
interest rates. Because Delta's options settle in ordinary US dollars, the standard textbook
mathematics applies directly, with none of the corrections that some other crypto venues require.

### The volatility smile

If you plot implied volatility against strike for one expiry, the result is usually a curve rather
than a flat line -- traditionally called a **smile**. One screen draws that curve for any expiry from
the stored record, together with the individual points it was fitted from.

### Implied against realised volatility

Implied volatility is an expectation about the future. **Realised volatility** is a measurement of
what actually happened. Putting the two side by side over a chosen window is one of the more
interesting things you can do with this data, and one screen does exactly that.

### Structures

Traders rarely buy a single option; they buy combinations. Two of the most common are the
**straddle** and the **strangle**. One screen prices every straddle and strangle available on an
expiry and lays them out on a single grid.

### History, with nothing invented

Everything that arrives is folded into one-minute summaries and written to permanent files, so any
past minute can be reconstructed. Four ways of reading it are provided: the whole chain as it stood
at a chosen past minute, the list of minutes that exist at all, one contract's day as a candle
chart, and the volatility series. The live chain screen has a slider whose right-hand end is "now"
and whose left-hand end is the start of the stored day.

There is one rule here that shapes everything else. **A minute during which nothing arrived produces
no record at all** -- not a row of blanks, and certainly not a copy of the previous minute's prices.
This is not a small point of tidiness. The venue's own historical price service does copy the last
known price forward and does not say so: asking it for the daily history of one expired contract
returns 801 days of prices, of which 797 are manufactured by repetition. A system that quietly
invents data is worse than one that admits a gap, and this project always admits the gap.

### The permanent record

The stored data is split into five separate collections, so that what we observed, what the venue
claimed, and what we concluded are never mixed up with one another. The table below names each one.

| Collection | What it holds |
|---|---|
| `quote-bars` | What the order book did: bids, asks, and the sizes offered |
| `reference-bars` | What the venue said: its mark price, open interest, and its own volatility figures |
| `spot-bars` | What the underlying itself was worth |
| `computed-bars` | What this project concluded: our forward, our implied volatility, our Greeks |
| `index-bars` | Longer-range index price history, filled in separately |

### Alerts

Some conditions need a person to notice them: the connection has given up reconnecting, a file could
not be written, the recording has been producing empty results while it believed it was recording.
These raise an **alert**, and a small dedicated program forwards alerts to a Discord channel. It
does nothing else, so it cannot interfere with the parts doing the real work.

## What this project is not

It is worth being explicit about the boundaries, because several of them look like omissions.

It is **not a trading system.** There is no order placement, no position tracking, no account, no
secret key anywhere in the repository. The internal message format was designed so that an order
system *could* later share it, and that is as far as it goes.

It is **not a backtesting engine**, although the stored files are deliberately written in a format
that a backtester in any language can read directly, with no export step.

It is **not a copy of the team's other project** for Indian index options. The overall shape of the
software carries over; almost none of the mathematics does, because the two markets work differently.

## How the code is arranged

Four folders, each with one job. The table below is the map most people need on their first day.

| Folder | What lives there |
|---|---|
| `engine/` | The Python back end: the venue connection, the mathematics, the file writer, the web API |
| `web/` | The Next.js front end. It displays things and performs no arithmetic of its own |
| `tools/` | Small standalone scripts for measuring the venue and inspecting the stored files |
| `docs/` | The contracts that define the interfaces, and the design record |

One principle governs where new code goes. Most of the engine is **pure**: it takes values in and
returns values out, without touching the network, the clock or the disk. Only one file talks to the
venue and only one file writes to disk. Keeping it that way is what allows the project to have
sixteen hundred automated tests that run in seconds.

## Where to go next

[Architecture](architecture.md) explains how these features are divided between running programs.
[Getting started](getting-started.md) gets it running on your own machine.
