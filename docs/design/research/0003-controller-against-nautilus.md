# 0003 — Our connection controller against Nautilus Trader's reconnect policy

**#59, 2026-09-09.** Decision: [../decisions/0003-controller-policy.md](../decisions/0003-controller-policy.md).
Fills [../cloud/controller-policies.md](../cloud/controller-policies.md). Ours is
[../lld/controller.md](../lld/controller.md), [../lld/reconnect.md](../lld/reconnect.md),
[../lld/connection-signal.md](../lld/connection-signal.md),
[../lld/commands.md](../lld/commands.md).

**Sources, primary only.** Nautilus Trader, `nautechsystems/nautilus_trader`, branch
`develop`, read 2026-09-09: `crates/network/src/backoff.rs`,
`crates/network/src/heartbeat.rs`, `crates/network/src/websocket/config.rs`,
`crates/network/src/websocket/client.rs`, `crates/network/src/websocket/subscription.rs`;
and <https://nautilustrader.io/docs/latest/concepts/live/>. Delta Exchange's own websocket
documentation, <https://docs.delta.exchange/>, sections *Websocket Feed* and *Detecting
Connection Drops*. Line numbers are as fetched on that date and are given beside the
symbol name, which is the stable half.

**Nautilus's Python package no longer holds these knobs.** The ticket asked for
`LiveDataClientConfig` / `LiveExecClientConfig` reconnect fields. On `develop` the Python
tree has moved to `python/nautilus_trader/` and `python/nautilus_trader/live/__init__.pyi`
exposes `DataClientConfig` with `instrument_provider` and `routing` and **no reconnect
field at all**. The whole reconnect policy is in the Rust `WebSocketConfig`, per adapter.
That is itself a finding: they made it transport configuration, not client configuration.

## The gap table

| Dimension | Ours | Nautilus | Verdict | Citation |
|---|---|---|---|---|
| **Staleness detection** | **One clock, data only.** Refreshed by canonical events through `sink()`; Delta's control frames (`subscriptions`, `heartbeat`, pong) are dropped by `delta_socket._to_message` and never reach it. Measured from the later of last message, `_opened_at`, `_attempt_at`. Two thresholds: `degraded_after` 15 s (badge, no alert), `reconnect_after` 45 s | **Two clocks.** `heartbeat_timeout_secs` — *any* inbound frame refreshes, Ping and Pong included; detects a dead peer. `idle_timeout_ms` — only Text and Binary refresh; detects "a feed that has stopped flowing even while the transport is provably alive". Heartbeat timeout defaults to 3 × the heartbeat interval; idle detection is off unless set | **keep** ours; **measure** a second clock | `websocket/config.rs` L183, L204; `heartbeat.rs` L22 `DEFAULT_HEARTBEAT_TIMEOUT_INTERVALS = 3`; `client.rs` L2144 `heartbeat_timeout_exceeded`, L2162 `idle_timeout_exceeded` |
| **Two-stage warning** | `degraded` at 15 s is a state and a badge and **not** an alert; `reconnecting` at 45 s is the action | **No equivalent.** The idle timeout has one threshold and it tears the connection down | **keep** ours | `client.rs` L1580–L1631: every branch on timeout does `break` |
| **Reconnect trigger** | A `CLOSED` from the adapter — a venue close or a failed dial. **The staleness watchdog could not trigger one**: it marked the state, alerted, and waited for the venue or a person | The read task **breaks itself** on either timeout, `state_notify.notify_one()` wakes the controller, `inner.is_alive()` is false, and the controller moves to `Reconnect` and redials | **ADOPT** | `client.rs` L1624–L1631 then L3505–L3520; docs: "Any transport configured with a heartbeat therefore reconnects when no inbound frame of any kind arrives within three heartbeat intervals" |
| **Backoff shape** | 1 s, ×2, ceiling 60 s, restored in full by a message | `reconnect_delay_initial_ms`, `reconnect_delay_max_ms`, `reconnect_backoff_factor` (validated into `[1.0, 100.0]`), reset by the stability rule below. Optional `immediate_first` returns a zero first delay | **keep** | `websocket/config.rs` L142–L152; `backoff.rs` L159 `next_duration` |
| **Jitter** | **None** | `reconnect_jitter_ms`, uniform `0..=jitter_ms` added to each delay, with the base lowered near the cap so the spread survives saturation. Purpose stated: "Random jitter reduces synchronized reconnect storms" | **keep** (reject) | `backoff.rs` L16–L21, L167 |
| **Attempt-rate floor** | 1 s, by construction: `retry_delay` is 1 s and there is no immediate-first | `ReconnectThrottle`: once 3 attempts fall inside a 2-minute window, every further attempt waits at least 1 s. Reason given: "Binance permits 300 connections per 5 minutes per IP; OKX permits 3 per second" | **keep** | `backoff.rs` L33–L77 |
| **Budget shape** | **10, consecutive in effect** — spent one per drop, reset to zero by a delivered message. Checked before spending | **Unlimited by default** (`reconnect_max_attempts: None`, "recommended for production"). With `Some(n)`: `n` **consecutive** attempts that failed *or* produced a connection alive under 10 s | **keep** the shape; **measure** the size | `websocket/config.rs` L158–L166; `client.rs` L3558–L3568 |
| **What restores the budget** | **A delivered message**, in full. An open restores nothing — Delta can accept a handshake and close at once, `measured` at 21 attempts in 0.3 s with a budget of 3 | **Neither.** `RECONNECT_STABILITY_THRESHOLD = 10 s` of *uptime*: a replacement connection active ≥ 10 s resets both the attempt count and the backoff | **keep** | `backoff.rs` L33; `client.rs` L3543–L3555 |
| **Heartbeat, outbound** | A WebSocket **Ping control frame** every 30 s, pong not awaited | Configurable interval, and an optional **text payload**: "A venue that counts only an application-level keepalive needs the text form; **the two are not interchangeable**" | **measure** | `websocket/config.rs` L115–L127 |
| **Heartbeat, inbound** | **Nothing.** Delta's `enable_heartbeat` is never sent, and a `heartbeat` frame would be dropped as control traffic if it arrived | The liveness window, refreshed by any inbound frame, and validated to exceed the send cadence — a timeout at or below it "tears every connection down before its first reply is due" | **measure** | `websocket/config.rs` L272–L288 |
| **Resubscribe on reopen** | Registry **never cleared**, replayed in full on every open; `OPENED` fires only after the subscribe payload is on the wire; an empty registry is not announced at all | `SubscriptionState` keeps **desired**, **pending_subscribe**, **confirmed** (server-acknowledged) and **pending_unsubscribe** apart; `all_topics()` returns confirmed plus pending intent for recovery. A `RECONNECTED` message is handed to the adapter so it can replay | **measure** | `websocket/subscription.rs` L16–L26, L74–L90; `client.rs` L3628–L3640 |
| **What "open" means** | Socket up **and every subscription replayed** | `Active` after the transport handshake, plus an optional auth handshake bounded at 10 s. The stability clock starts here, not at first data | **keep** ours | `websocket/consts.rs` `AUTHENTICATION_TIMEOUT_SECS = 10` |
| **Giving up, and how loud** | `-> stopped`, one `alert` at error on the bus, one error log record, `adapter.stop()`, and `/health` carries the state and the reason. Recovery is a process restart: `resume` refuses a spent budget | `Closed`, one `log::error!("Max reconnection attempts (n) exceeded, transitioning to CLOSED")`, registered auth waiters failed. Nothing on a bus | **keep** ours | `client.rs` L3558–L3568 |
| **Commanded reconnect** | `POST /feed/{adapter}/reconnect`; the route answers **after** the command, with the state it put the connection in | `reconnect_socket(client_id, endpoint)`; the docs call it "fire-and-observe" — a successful return means the command "passed local validation and was queued" | **keep** ours | docs, *Live* concepts page |

## Delta's own bounds, which are the ceiling on all of this

Four numbers, all from <https://docs.delta.exchange/>, all the venue's own words.

1. **"There is a limit of 150 connections every 5 minutes per IP address."** A connection
   past it is refused with HTTP 429, and the advice is to "wait for 5 to 10 minutes before
   making new connection requests."
2. **"You will be disconnected, if there is no activity within 60 seconds after making
   connection."** Read the last four words: this is worded as a deadline *after the
   connection is made*, not as a rolling idle timer. That reading is consistent with the
   75-second test recorded in `adapters/delta_socket.py`, which did not reproduce an idle
   disconnect. It is the ceiling on `reconnect_after` under either reading.
3. **The venue's own drop detection**, under *Detecting Connection Drops*: send
   `{"type": "enable_heartbeat"}` after every successful connection; the server then sends
   `{"type": "heartbeat"}` every **30 seconds**; set a **35-second** timer, reset it on
   every heartbeat, and if it fires "the client should exit the existing connection and
   try to reconnect."
4. **Ping/pong**: send a ping about every 30 s; if no pong arrives within **5 seconds**,
   "the client should exit the existing connection and try to reconnect."

Points 3 and 4 are the venue telling us, in its own documentation, the rule this ticket
adopts from Nautilus: **when the connection has gone quiet, end it yourself and redial.**
Three independent authorities — Nautilus's read task, Delta's documentation, and our own
`reconnect.md` §4, which recorded the gap and left it open — agree.

## Numbers

| Number | Value | Tag | Run or source |
|---|---|---|---|
| Longest quiet gap, tagged, 610 s | **0.724 s** | `measured` | `tools/measure_quiet_gap.py`, run `20260909T125124Z`, 610.0 s, 502 BTC symbols, both channels, 657,163 messages, **1 connection**, 0 malformed, 0 empty opens |
| Gaps spanning a connection event, same run | **0** | `measured` | Same run. `spanning_gap_seconds.count == 0`; the one connection event is the open at t=1.3 s |
| p99 / p95 / median, same run | 0.011 / 0.002 / 0.0 s | `measured` | Same run. **Identical to all three runs of 2026-09-07** at 35 s, 550 s and 3610 s |
| Longest quiet gap, six hours, tagged, **unbroken connection** | **6.792 s** | `measured` | `tools/measure_quiet_gap.py`, run `20260909T130306Z`, 21,610 s, 23,713,768 messages, 13 connections. Longest spanning gap 22.83 s. `reconnect_after` 45 s is 6.6x above it. Full row in `../quiet-gap.md` |
| Longest gap, one hour, **untagged** | 44.785 s | `measured` | Run `20260907T135951Z`. Three connections; which gaps spanned a drop is **not proven**. `../quiet-gap.md` |
| `reconnect_after` | 45 s, **unchanged** | `assumed`, bounded | Below Delta's 60 s (bound 2). Above Delta's own 35 s heartbeat window (bound 3) |
| Backoff to a spent budget | **303 s** of waiting across 11 dials | `derived` | 1+2+4+8+16+32+60+60+60+60, from `retry_delay` 1 s, factor 2, ceiling 60 s, budget 10 |
| Dials used against Delta's allowance | 11 in ~5 min, **7.3%** of 150 per 5 min | `derived` | From the row above and Delta's bound 1 |
| Nautilus stability threshold | 10 s of uptime | source | `backoff.rs` L33 |
| Nautilus heartbeat timeout default | 3 × the interval | source | `heartbeat.rs` L22 |
| Delta heartbeat cadence / client window | 30 s / 35 s | source | Delta docs, bound 3 |

**The six-hour slot is empty on purpose.** The ticket asks for at least six hours and six
hours cannot be waited for inside one session, so the ten-minute run above proves the
tool's connection tagging works and the long run was started detached:

```
Start-Process -FilePath "...\engine\.venv\Scripts\python.exe" `
  -ArgumentList "tools\measure_quiet_gap.py","21600" `
  -WorkingDirectory "D:\Convex Hedge\delta-exchange-payoff" `
  -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "docs\design\research\0003-quiet-gap-6h.log" `
  -RedirectStandardError  "docs\design\research\0003-quiet-gap-6h.err.log"
```

PID **28432** (the venv launcher) with the interpreter as its child, PID **31740**;
stopping 28432 stops both. Started **2026-09-09T13:03:04Z**, due about
**2026-09-09T19:03Z**. The JSON lands in `tools/out/measure_quiet_gap-20260909T1303*.json`
and must be copied into `../quiet-gap.md` the way the other runs were: `tools/out/` is
gitignored, and so is the console log beside this file — `.gitignore` line 32 is `*.log`,
so `0003-quiet-gap-6h.log` exists on the machine and will never be committed.
**Until the row is copied across, no six-hour number may be quoted and `reconnect_after`
stays `assumed`.**

## What I learned

Every term, before it is used.

| Term | What it means here |
|---|---|
| Frame | One message off the websocket. |
| Data frame | A frame carrying market data. Ours are `ticker` and `ob_l2`. |
| Control frame | A frame the protocol sends to manage itself: ping, pong, close. Carries no data. |
| Keepalive | A frame sent only to prove the connection is alive. |
| Staleness | How long it has been since something arrived. |
| Backoff | The wait between one failed connection attempt and the next. |
| Jitter | A small random amount added to a backoff so many clients do not all redial together. |
| Budget | How many reconnects are allowed before the feed gives up. |
| Idle disconnect | The venue closing a connection because nothing has happened on it. |
| Resubscribe | Asking again, on a new connection, for everything you were getting on the old one. |

**A quiet feed and a dead feed look the same, so you need a clock.** We have one. Nautilus
has two, and the second one answers a different question: ours asks "is data flowing", theirs
also asks "is this connection alive at all". Ours is the one that matters, because the
whole point is bars that do not get written.

**Noticing is not the same as acting.** Our controller noticed silence, turned the badge
red and raised an alert — and then did nothing. The connection sat there. Nautilus's
reader kills its own connection when it goes quiet, and Delta's documentation tells clients
to do exactly that. This is the one thing we took, and it is now in the code.

**A connection that opens proves nothing.** Delta can accept a connection and drop it
straight away. We measured 21 attempts in 0.3 seconds when the budget reset on connecting.
So our budget resets only when real data arrives. Nautilus solves the same problem a third
way: a connection that stays up for ten seconds counts as a success. Both work. Ours is
stricter, because ours also proves the subscriptions took.

**Jitter is for crowds.** It stops a thousand clients redialling in the same millisecond.
We are one process with one connection. Adding it would be a knob that changes nothing.

**The venue is the real ceiling.** Delta disconnects an inactive connection at 60 seconds,
allows 150 connections every 5 minutes, and offers its own heartbeat with a 35-second
window. Our 45-second bound sits under Delta's 60 and above Delta's 35. That is the whole
argument for the number, and it is an argument from documents, not from a measurement — so
the six-hour run still matters.

**We throw away an acknowledgement we already receive.** Delta answers a subscribe with a
`subscriptions` message and we drop it as control traffic. Nautilus tracks which
subscriptions the server has actually confirmed. We call a socket "open" when we have
*sent* the subscribe. The difference is a real gap, and the 45-second bound is what stops
it being silent — a subscription that never took delivers nothing and we reconnect.
