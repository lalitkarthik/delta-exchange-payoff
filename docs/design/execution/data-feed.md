# The data feed: adapters, market hours and instruments

**What this page contains.** The two kinds of adapter and why they are always separate, what a data
adapter knows about each market (its hours, its instruments, what to subscribe to), how the connection
controller uses market hours, and the folder of venue files that holds all of it.

**How to read it.** The first section is the split every other section relies on. Market hours and the
connection controller go together. The venue folder at the end is where every setting on this page
lives.

## Two kinds of adapter

An **adapter** is the one piece of code that speaks an outside company's own API, so nothing else in
the engine learns that company's vocabulary. There are two kinds, and they are always separate
programs, even when one company does both jobs.

| Kind | Job | Lives in | Examples |
|---|---|---|---|
| **Data adapter** | Brings market data in: quotes, option chains, trades. Owns the market hours, the instrument list and the subscription list for its source. | the feed | Delta, Databento |
| **Broker adapter** | Sends orders and reads back fills, positions and the wallet. | the OMS ([oms.md](oms.md)) | the paper broker, Delta |

Databento is a **data vendor**: it sells market data and places no orders, so it only ever has a data
adapter. Delta does both jobs, so it has one of each; the two share the venue's settings and its
credentials, and nothing else. Nothing on the order path depends on where a price came from, so a
market can take its data from one company and send its orders through another.

## Market hours

Every data adapter knows when each of its instruments trades. The source is the
**market calendar** library `pandas_market_calendars`, which holds the weekly sessions, the holidays
and the early closes for each exchange.

| Instrument | Exchange | Calendar |
|---|---|---|
| NIFTY options | NSE | `NSE` |
| Sensex options | BSE | `BSE` |
| SPX options | Cboe | `CBOE_Index_Options` |
| SPXW options | Cboe | `CBOE_Index_Options` |
| BTC and ETH options | Delta | none: open every hour of every day |

An instrument whose hours differ from its exchange's session, such as a series that stops trading
earlier on its expiry day, narrows its session in the venue file.

**Two checks keep the calendar honest.**

1. **Against the broker, every morning.** When the broker used for that market publishes its own
   calendar, the adapter compares today's session with it before the open and raises an alert if they
   differ. This catches what a library misses: a special session, or a closure announced at short
   notice.
2. **Against the end of its data.** When the library holds no sessions for the next 30 days, the
   adapter raises an alert: the library needs updating.

## The connection controller follows market hours

The **connection controller** is the part of a data adapter that keeps its connection to the source
alive. It treats silence differently depending on the hour.

| When | 45 seconds with no message means | It does |
|---|---|---|
| inside the instrument's session | a dead feed | reconnects and raises an alert |
| outside every session of the adapter | a closed market | nothing: no reconnect, no alert |
| 5 minutes before a session opens | — | connects, so the first message of the day is not missed |

A market that is closed is therefore never mistaken for a feed that has died, and the controller does
not spend the night reconnecting to an exchange that is shut.

## Instruments: lot size and multiplier

Two numbers belong to each instrument and are never set once for the whole engine:

| Number | Means | Example |
|---|---|---|
| **Lot size** | How many units of the underlying one lot holds | set by the exchange for each index option, and revised by it from time to time |
| **Multiplier** | How much money one point of price is worth | 100 US dollars per point on an SPX option |

Every profit-and-loss figure, margin estimate and position limit is built from them, so a wrong value
is wrong everywhere at once. The data adapter reads both from its source's **instrument list** (Delta's
product list, Databento's instrument definitions, the Indian source's instrument file) when it starts
and again once a day. The numbers travel on the bus with the instrument, so no other program keeps its
own copy. When a number differs from the day before, the adapter raises an alert and uses the new one.

## The subscription list

A data adapter asks its source only for what the strategies use. Its **subscription list**, in the
venue file, names **root symbols** and an **expiry window**:

```yaml
subscriptions:
  - root: SPXW
    expiries_within_days: 2
```

That line takes SPXW options expiring within two days from Databento, and nothing else from the whole
options market. The adapter re-applies the list each day as expiries roll.

## The venue folder

Everything above that describes a venue lives in a `venues/` folder next to the code, not inside it,
with one file per venue: `delta.yaml`, `nse.yaml`, `bse.yaml`, `cboe.yaml`.

| Setting | What it says |
|---|---|
| calendar | the market calendar name, or none |
| data adapter | which data adapter serves this venue |
| broker adapter | which broker adapter sends its orders, if any |
| subscriptions | root symbols and expiry windows |
| session overrides | instruments whose hours differ from the exchange's |
| retention | how long the bus keeps each of this venue's feeds ([message-bus.md](message-bus.md)) |
| credentials | the **names** of the environment variables that hold keys; never the keys |

Adding a venue is adding a file. No code changes.

## Open questions

- What "outside venue/exchange folder" meant in the 20 September review. Written: the `venues/` folder
  above. **To confirm with Bilal.**
- The source of NSE and BSE market data. Nothing is chosen; it decides which Indian data adapter is
  built.
- The exact closing times of SPX and SPXW, on ordinary days and on expiry days. To verify against
  Cboe's published hours before the first Cboe feed runs.

## Where to go next

[operations.md](operations.md) places this work in the build order.
