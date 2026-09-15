# API reference

**What this page contains.** Every web address the back end answers on, what each expects to be
given, and what it sends back. It also explains the conventions those answers follow, which matter
if you are writing something that reads them.

**How to read it.** Read the conventions first -- they apply to every address and explain several
things that would otherwise look like mistakes. After that the sections are independent; go to the
one you need. The addresses are grouped by what they are for: live data, historical data,
volatility, and operations.

## Conventions

These hold everywhere, without exception.

**Numbers are always numbers.** Every decimal value is sent as a JSON number or as `null`, never as
text. The front end never converts text to numbers, and it raises an error rather than displaying a
value that arrives in the wrong shape.

**`null` means "there is no value", and it is not zero.** An absent price means nobody is offering
one. A zero would mean somebody offered nothing. Do not treat them alike.

**Volatility is a fraction, not a percentage.** A value of `0.62` means 62 percent. The conversion
happens only when the number is displayed.

**Asking for data that does not exist is not an error.** The addresses that read the stored history
answer normally with an empty result if nothing was recorded. A day nobody has lived through yet is
"nothing yet", not a mistake, and a `404` would suggest otherwise. Only the addresses that ask the
venue itself can fail because the venue failed.

The table below shows how each kind of parameter is written, since the formats differ and it is a
common source of confusion.

| Parameter | Format | Example |
|---|---|---|
| `underlying` | Capitals | `BTC` |
| `expiry` | Day-month-year, as the venue writes it | `27-06-2026` |
| `date` | Year-month-day, as the stored files are named | `2026-09-08` |
| `minute` | Full timestamp in UTC, to the second | `2026-09-04T09:00:00Z` |
| `instrument` | Our own contract name | `DELTA-BTC-20260627-60000-C-USD` |

Interactive documentation generated from the code itself is available at `/docs` when the back end
is running. It is not exposed through the production setup.

## Live data

Two ordinary addresses and one long-lived connection.

| Address | Give it | Get back |
|---|---|---|
| `GET /expiries` | `underlying` | Every expiry the venue currently lists, earliest first. This is what fills the dropdown |
| `GET /chain` | `underlying`, `expiry` | The full option chain for that expiry |

In the multi-program arrangement both of these are answered from the API's own in-memory picture
rather than by asking the venue, so displaying a screen never causes a request to Delta.

### The streaming connection

`WS /ws/chain`, given `underlying` and `expiry`, and optionally how often to send (one second by
default).

What it sends is **the same chain object that `GET /chain` returns**, wrapped in a small envelope so
that the four things the connection can say stay distinguishable: here is a chain, I am still
waiting for data, the venue connection changed state, or something went wrong. Because the chain
itself is identical, one piece of display code handles both sources.

It sends the current state of the venue connection **once, immediately on connecting**, so a browser
that joins halfway through is never left guessing, and then sends every subsequent change.

## Historical data

All four of these read the stored files and never contact the venue.

| Address | Give it | Get back |
|---|---|---|
| `GET /chain/minutes` | `underlying`, `expiry`, `date` | Which minutes of that day have data. This is what the time slider uses, and the minutes it does *not* list are the gaps |
| `GET /chain/at` | `underlying`, `expiry`, `minute` | The whole chain as it stood at that minute, **in the same envelope the streaming connection uses** |
| `GET /bars` | `instrument`, `date` | One contract's minute-by-minute record for that day |
| `GET /smile` | `underlying`, `expiry` | Every stored minute of implied volatility for that expiry |

A complete example:

```
GET /bars?instrument=DELTA-BTC-20260627-60000-C-USD&date=2026-09-08
```

## Volatility

| Address | Give it | Get back |
|---|---|---|
| `GET /volatility/bounds` | `underlying`, optionally `interval` | What range of lookback periods the stored data can actually support |
| `GET /volatility` | `underlying`, `lookback_days`, and several optional settings | Implied and realised volatility over that period |

The first of these exists for a specific reason. When the system has only just started recording,
there is not yet enough history to compute anything meaningful. Rather than failing, it answers
normally and says that no usable range exists yet. "Not enough data yet" is a true answer that a
screen can display, and displaying it is the difference between an instrument that is honest about
its limits and one that merely looks broken for its first month.

Note that `lookback_days` is deliberately a single value covering both series. Allowing two would
allow somebody to compare implied volatility over one period against realised volatility over a
different one and not notice.

## Operations

| Address | Give it | Effect |
|---|---|---|
| `GET /health` | -- | Whether this program is alive, and what it knows about the venue connection |
| `GET /recording` | -- | Whether data is currently being recorded |
| `POST /recording` | `{"recording": true}` or `false` | Start or stop recording |
| `POST /feed/{adapter}/{command}` | `pause`, `resume` or `reconnect` in the address | Control the venue connection |

### Health

The API's health address is the authoritative answer about the venue connection -- but note that it
knows this from the connection's own messages on the bus, not by asking it. The badge on the web
page is derived from exactly the same information, so the page and the health check can never
disagree.

The other two programs answer their own health addresses, and they mean different things.
[Data flow](data-flow.md) sets out what each one actually reports.

### Starting and stopping recording

This is the only address that changes stored data, and there is one important design decision behind
it: **whether recording is on lives in the back end and nowhere else.** Not in the browser, and not
in a browser's local storage. Two open tabs must not be able to disagree about it, and somebody
opening a fresh page must be told the truth rather than shown a default.

It answers with the state *after* the change, so no second request is needed and a caller cannot
display a state that was never true. Calling it twice with the same value is harmless.

**Switching recording off writes out whatever is buffered before stopping.** Up to five minutes of
finished data can be waiting in memory, and discarding it would throw away data the system already
had.

### Controlling the venue connection

This address is deliberately thin: it checks the two names, builds one instruction message, and puts
it on the bus. Everything the instruction actually *does* happens in the part that owns the
connection. No program calls another program directly, even for this.

| Command | What it does |
|---|---|
| `pause` | Disconnect and stay disconnected. **Uses none of the reconnection allowance**, because a pause is a decision rather than a failure |
| `resume` | Start connecting again, with the allowance restored in full |
| `reconnect` | Drop the current connection and let the normal reconnection logic take over |

It answers with the connection's state **after** the instruction has taken effect, so the answer is
never merely a statement of intent.

## How the browser reaches all of this

In every environment the browser talks to exactly one address, and something in front forwards
anything beginning with `/api` to the back end. So the browser asks for `/api/chain` and the back
end receives `/chain`. The streaming connection uses the same prefix.

The table below shows what performs that forwarding in each environment.

| Environment | What forwards the requests |
|---|---|
| Docker setup | A small nginx container, on the single published port |
| Production | Amplify, which forwards `/api` to the back end machine |
| `bun run dev` | Nothing -- the browser calls port 8000 directly, which is specifically permitted |

## Where to go next

[Data store](data-store.md) explains where the historical answers come from.
[Events](events.md) explains the messages behind the operational addresses.
