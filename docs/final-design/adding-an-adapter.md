# Adding an adapter

**What this page contains.** A seven-step guide to connecting a new exchange to this system, and the
full set of naming conventions a new adapter has to follow.

**How to read it.** Read [Adapters](adapters.md) first -- it explains what an adapter is and shows
how the existing one is built, and every step here assumes that. Then work through the steps in
order; they are arranged so that each one is testable before the next begins.

## The steps

### 1. Fill the eight members, and change nothing else

The eight members are the real test of the design. A different exchange with a genuinely different
shape -- a stock index rather than a coin, priced in rupees, sold in lots, on a different holiday
calendar -- should be able to fill them without the list needing to change. If you find yourself
needing to add a ninth member for one exchange, something is wrong elsewhere and adding it will hide
the problem rather than fix it.

### 2. Keep the exchange's vocabulary inside your folder

Channel names, web addresses, field positions and contract spellings belong in your adapter's files
and nowhere else. Write the test that searches the codebase for your channel names and fails if they
have escaped; it is the cheapest guarantee available here.

### 3. Produce the existing events. Do not invent a new one

The ten events are the agreement between parts. If your exchange offers something none of them can
carry, that is a change to the shared catalogue -- discussed, versioned, and documented in
[Events](events.md) -- and not something to smuggle through as an extra field.

### 4. Hold the boundary rules where the boundary is

Absent is not zero. Every decimal becomes a number, never text. Nonsense numbers become absent and
are counted. Both timestamps travel uncorrected. Nothing downstream can recover a distinction your
adapter collapsed.

### 5. Follow the naming conventions

Names in this system are chosen once and then used identically everywhere. The table below is the
full set of conventions an adapter has to follow, with an example of each.

| Thing being named | The convention | Example |
|---|---|---|
| The exchange | Capitals, one word, no dashes | `DELTA`, `NSE` |
| The underlying | Capitals, no dashes | `BTC`, `NIFTY` |
| The adapter's files | `adapters/<name in lower case>.py`, connection in `<name>_socket.py` | `adapters/nse.py` |
| A contract | Exchange, underlying, expiry, strike, call or put, currency, joined by dashes | `NSE-NIFTY-20260908-25500-C-INR` |
| Call and put | The single letters `C` and `P`, with no translation table anywhere | |
| A stream on the bus | The kind of event, then the exchange, then the underlying | `md.option_quote:NSE:NIFTY` |
| A consumer group | The **service** name in lower case -- never the exchange name | `store`, `api` |
| One reader in that group | The service name and an instance number | `store-1` |
| A log entry's name | An area and a noun, joined by a dot, registered in a central list | `feed.transition` |
| An alert code | An area and a condition, short and stable | `store.flush_failed` |
| A settings variable | A prefix naming the exchange or the service | `DELTA_LIVE_UNDERLYINGS` |
| A folder of stored data | A noun and the word `bars` | `quote-bars` |

The exchange's name appears in exactly two places: the stream name and the contract name. It does
not go into consumer group names, because the stream already says which exchange it is and two
places for one fact eventually disagree.

### 6. Write the scripted stand-in before the real connection

The project has a fake adapter that implements the same eight members and simply does what a script
tells it. It is how almost every test of the surrounding machinery is written, without any network
involved. Its script has four instructions, listed below.

| Instruction | What it does |
|---|---|
| `Frames` | These messages arrived; publish the events they produce |
| `Close` | The connection dropped and came back, re-registering every subscription |
| `Silence` | Nothing arrived for this long |
| `Resume` | The connection returned, carrying the same prices it left with |

`Silence` does not actually wait. The clock is supplied from outside, so twenty seconds of silence
cost nothing to test while still being fully checkable -- which is what makes it affordable to test
a fifteen-second staleness rule in a suite that runs in seconds.

The fake deliberately produces no connection-state events. Deciding what a dropped connection means
belongs to the state machine, and a stand-in that pre-empted that decision would make the state
machine's own tests meaningless.

### 7. Make the conversion a plain function

Expose the conversion from an exchange message to our events as an ordinary function taking three
plain values: the channel, the message, and the time it arrived. No connection, no bus, no clock.
Every captured real message in the test fixtures is then run through it, and every test of every
downstream part uses the same function, so no test can quietly drift away from what the real
adapter does.

## Where to go next

[Events](events.md) describes exactly what your adapter must produce, and
[Data flow](data-flow.md) shows where its output goes once it is produced.
