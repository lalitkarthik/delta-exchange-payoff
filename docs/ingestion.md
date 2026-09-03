# One socket, many consumers

**Verdict: both baselines reproduce, and the in-process fan-out is free.** A live
136-symbol chain on `ob_l2` delivers **267.7 msg/s at 130.0 KB/s**, refreshing each
contract every **508 ms**, against #3's stated 270 msg/s, 131.7 KB/s and 504 ms. The
fan-out costs **2.5 microseconds** per record across three consumers — against a 508 ms
publish interval, that is five ten-thousandths of one percent of the budget.

**And OpenAlgo's frame limit is stale.** Their source says `ob_l2` accepts one symbol per
subscribe message, verified 2026-08-12. Measured today: **300 accepted in a single
message on both channels**, acknowledged and all sending data. 300 is where the probe
stopped asking, not the ceiling.

Implemented in `engine/src/deltapayoff/{wire,fanout,feed}.py`. Measured by
`tools/probe_ws.py` and `tools/measure_feed.py`.

## How to read this

**Measured** names the run. Every number here came from a live connection on 2026-09-03;
nothing is quoted from documentation, which does not state a websocket rate limit at all.

---

## 1. Why a websocket, and why one

The order book moves every 508 ms. Seeing that over REST means polling twice a second
forever, against a budget of 150 connections per IP per 5 minutes — banned in 75 seconds.
REST cannot do this at all; it is not a matter of being slower.

One connection, not three. Three consumers each opening their own would burn the
connection budget and give three inconsistent views of one market. So the socket-owner /
consumer split is **mandatory**, and everything below follows from it.

## 2. What each channel is for

**Measured** on 2026-09-03, `tools/measure_feed.py`, 20-second windows:

| channel | symbols | msg/s | KB/s | per symbol |
|---|---|---|---|---|
| `ticker` | 588 (every live BTC option) | 117.6 | 51.9 | **5001 ms** |
| `ob_l2` | 136 (the 04-09-2026 chain) | 267.7 | 130.0 | **508 ms** |

`ticker` at 588 symbols over a 5 s cycle predicts 117.6 msg/s and delivers 117.6. #3's
187 msg/s was measured when 967 options were listed; 967/5 = 193. Both are the same fact.

**The calculation needs four things and `ob_l2` carries all of them:**

```
best bid    ob_l2  b[0][0]
best ask    ob_l2  a[0][0]
strike      the symbol -- C-BTC-77600-040926
expiry      the symbol, settling 12:00 UTC
```

Verified rather than assumed: stripping a chain to bid, ask and strike only — no spot, no
Greeks, no mark, no implied vol — reproduces the forward to 77590.394261 and all 63
implied volatilities identically. Put-call parity uses only call prices, put prices and
strikes; spot never enters, and it was needed only by F3 and F4, both rejected in
`docs/implied-vol.md`.

**So `ticker` drives nothing.** It carries spot and open interest for the screen, and
Delta's own Greeks and implied vols as reference columns that
`tests/test_no_delta_inputs.py` asserts are never consumed. A `ticker` message being five
seconds stale costs the calculation nothing.

### The freshness edge, which is the point of the ticket

```
Delta republishes ITS implied vol   every 5001 ms   (ticker)
the prices underneath it move       every  508 ms   (ob_l2)
                                    ─────────────
                                          9.8x
```

**Delta computes an implied volatility from these prices and republishes it ten times
more slowly than the prices move.** We take the fast channel and invert it ourselves in
1.095 ms. That is the whole opportunity, and it comes from *which channel we subscribe
to*, not from faster arithmetic.

### A correction worth recording

An earlier run of `tools/probe_ws.py` reported 940 ms per symbol at 136 symbols and this
document briefly claimed a 6x edge. **That was an artefact of symbol selection.** The
probe takes the first N of the all-expiries list, which is mostly far-dated contracts,
and Delta publishes on change rather than on a metronome — a contract nobody is trading
is silent. On a real single-expiry chain of the same size the interval is 508 ms.

*Lesson: a rate measured over the wrong population is not a slower rate, it is a
different question. The probe now says so in its own docstring.*

## 3. The fan-out, and why not ZeroMQ

Three consumers want the stream: the storage writer (#5), the IV engine (#4), the screen.
The naive design has the handler call all three, and it breaks three ways — the handler
ends up knowing about everyone, a slow consumer blocks the socket until the receive
buffer fills and Delta disconnects us, and a consumer that raises takes ingestion with it.

So the handler publishes and returns. Each consumer reads its own bounded queue in its
own task.

**Overflow drops the oldest, and counts it.** A quote from four seconds ago is not
slightly worse than the current one, it is worthless. An unbounded queue is a memory leak
with good manners; blocking the producer reinvents the failure above. And a silent drop
is a lie — this project has twice been damaged by numbers that looked plausible and were
not.

### What the seam costs

**Measured**, 20,000 runs per row, publishing one record and draining every queue:

| consumers | fan-out | direct call | overhead |
|---|---|---|---|
| 1 | 1.800 µs | 0.300 µs | **1.500 µs** |
| 3 | 2.800 µs | 0.300 µs | **2.500 µs** |
| 10 | 8.200 µs | 0.500 µs | **7.700 µs** |

Against the budget:

```
Delta publishes            508      ms
our full chain recompute     1.095  ms
the fan-out hop              0.0025 ms      ← 0.0005% of the publish interval
```

**So the indirection is free, and ZeroMQ is not justified at this scale.** OpenAlgo needs
it for 36 brokers and many users across separate processes; we have one venue, one user
and 130 KB/s. A broker would add a serialise-deserialise round trip and a deployment step
to save nothing. The seam sits exactly where OpenAlgo puts the bus, so promoting later
replaces what is behind it rather than rewriting the producer.

That is the argument made rather than assumed, which is what #3 asked for.

## 4. The socket owner

Four jobs. The last two are where the silent failures live.

**Subscribe both channels** — one message each, since 300 symbols fit in one.

**Heartbeat every 30 s.** A quiet connection and a dead one are indistinguishable over
TCP. Delta's documented 60 s idle disconnect did not reproduce in a 75 s test on this
project, so it is tagged unverified and pings are sent regardless. The pong future is
deliberately not awaited — waiting for it would delay the next ping by a round trip, and
the client library runs its own keepalive for dead-peer detection.

**Reconnect with a budget that resets.** Taken from OpenAlgo, including the bug they
record: a cumulative retry counter looks correct and dies after a month, because a feed
that reconnects once a day silently exhausts a lifetime allowance. A connection that came
up and delivered data has proved the endpoint works, so the counter is restored.

**Resubscribe everything.** The one that produces no error at all. A reconnected socket
is a fresh, empty socket and Delta has forgotten every subscription — skip the replay and
you get a healthy connection, zero messages, and a screen that quietly stops updating. So
the registry is **never cleared** and is replayed in full on every open, keyed per symbol
rather than per message for OpenAlgo's reason: a message-keyed registry replays a whole
batch when one symbol in it is rejected.

Tested against a fake connection that drops on demand, so "pull the cable" is an
assertion rather than a manual exercise.

## 5. What we took from OpenAlgo, and what we left

`broker/deltaexchange/streaming/delta_websocket.py`, 443 lines, read from source.

**Taken.** The persistent per-symbol subscription registry and its reconnect replay. The
retry budget that resets after a healthy connection. The 30 s heartbeat interval. The
channel names after their 31 July 2026 migration — `ticker`, `ob_l1`, `ob_l2`, with
`v2/ticker` and `l2_orderbook` now rejected as invalid, which `DeltaFeed.subscribe`
refuses up front rather than letting a silently empty stream happen.

**Left.** ZeroMQ, for the reason measured in §3. Their threading model, since the engine
is already async. And `MAX_SYMBOLS_PER_FRAME`, which was true when they measured it and
is not true now.

*Lesson, and it is the second time this project has hit it: reference implementations go
stale. Trust your own measurements over their constants, including the ones in the ticket.*

## 6. Still open

- **No documented websocket rate limit exists.** Delta's docs state a REST quota — and
  the two pages disagree, 10,000 against 20,000 per 5 minutes — but say nothing about
  websocket messages, subscription counts or message sizes. The real limit is on our
  side: fail to drain the receive buffer and Delta closes the connection.
- **The 150-connections-per-5-minutes figure is ours, not Delta's.** It is not in the
  documentation and has not been re-verified.
- **The frame ceiling is above 300 and unmeasured.** 300 is where the probe stopped.
- **The coalescing tick is not built.** At 267.7 msg/s, recomputing per message would be
  29.6% of a core and 135x oversampled against a 508 ms data rate; a 20 Hz tick is 2.2%
  and still ten times faster than the data. One message cannot be answered alone anyway —
  the forward is fitted across every paired strike, so one quote moving changes every
  strike's implied volatility. This belongs in #6 with the live read path.
- **A lossless subscription policy is not built.** #5's storage writer wants the opposite
  of drop-oldest, because a gap there is a hole in the historical record rather than a
  stale price nobody wanted.
