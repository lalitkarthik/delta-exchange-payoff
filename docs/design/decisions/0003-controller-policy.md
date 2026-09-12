# 0003 — The connection controller's policy

**Status** decided, #59, 2026-09-09. **Supersedes** nothing. **Fills**
[../cloud/controller-policies.md](../cloud/controller-policies.md). **Evidence**
[../research/0003-controller-against-nautilus.md](../research/0003-controller-against-nautilus.md).
**Changes** [../lld/controller.md](../lld/controller.md) §8 and
[../lld/reconnect.md](../lld/reconnect.md) §4.

## The question

Where does our connection controller's policy — five states, thirteen moves, a budget of
ten restored by a delivered message, backoff from 1 s doubling to 60 s, `degraded_after`
15 s, `reconnect_after` 45 s — fall short of what Nautilus Trader's live data clients do,
and which of those gaps are worth closing?

## The options

| Dimension | Considered |
|---|---|
| Silence past `reconnect_after` | **cut the socket and redial**; leave it to the venue or an operator, as before; a shorter bound instead |
| Backoff | 1 s ×2 to 60 s, no jitter (**ours**); add `reconnect_jitter_ms`; add an attempt-rate throttle |
| Budget | 10, reset by a delivered message (**ours**); reset by 10 s of uptime; unlimited; a larger number |
| Staleness clocks | one, data-only (**ours**); a second, transport-liveness clock refreshed by any frame |
| Heartbeat | a 30 s Ping control frame (**ours**); Delta's `enable_heartbeat` with a 35 s inbound window |
| Resubscribe | replay on open, announce when sent (**ours**); track server acknowledgements |

## The decision

**One rule is adopted and everything else is kept.**

> **Silence past `reconnect_after` now ends the socket itself.** The staleness watchdog
> cuts the stream and reports the drop, exactly as an operator's `reconnect` command does,
> and the ordinary dial loop backs off and redials. It costs one of the budget, as any
> drop does, and the first frame off the replacement restores it in full.

Landed in `ConnectionController._check_staleness`, four lines, red-green as
`test_a_silence_the_venue_never_closed_is_cut_and_redialled`. Two tests it supersedes were
removed and one had its arithmetic corrected; all three are named in the new test and in
`reconnect.md` §4.

Every other dimension is **kept**, and one correction of vocabulary is made: the budget has
never been a **lifetime** budget. `message_arrived` sets the spend back to zero, so it has
always counted **consecutive** failures. The word is corrected wherever it appears.

`reconnect_after` **stays at 45 s** and stays `assumed`. The six-hour tagged run this
ticket asks for is running, not finished; the ten-minute tagged run that verifies the tool
is `measured` and is in the findings.

## Why, in the criteria's order

**1. Invariants — no silent data loss.** Decisive, and decisive alone.

- *For the cut.* A socket that is open, subscribed and silent produced a red badge, an
  alert, and then nothing. The connection stayed in `reconnecting` until the venue finally
  ended it or a person sent `POST /feed/{adapter}/reconnect`. Every minute of that is bars
  the store does not write. **Detecting a failure and not acting on it loses exactly the
  same data as not detecting it**, and it is worse to read afterwards, because the log says
  we knew. Nautilus's read task breaks its own connection on the idle timeout and its
  controller redials (`crates/network/src/websocket/client.rs`, `idle_timeout_exceeded`
  then the controller's `Reconnect` branch); Delta's own documentation says the client
  "should exit the existing connection and try to reconnect"; `reconnect.md` §4 recorded
  the gap in our own words and left it open. #41 built the lever and nothing pulled it.
- *The rule the cut does not break.* `reconnect.md` §4 refuses a redial decided by
  `state is RECONNECTING`, because the watchdog reaches that state over a socket that is
  still open and dialling a second one over it is the failure. This does not redial on
  state. It **ends** the socket first and reports the drop, so by the time the dial loop
  reads the adapter's last word the old socket really is gone. The loop is untouched.
- *Against restoring the budget on uptime.* Nautilus resets on ten seconds of an active
  connection. For us an open proves less than it does for them: `delta_socket` fires
  `OPENED` only after the subscribe is on the wire, but Delta does not have to honour it,
  and a socket that stays up for ten seconds having silently dropped our subscriptions is
  precisely the healthy-connection-zero-messages failure this whole module exists against.
  A delivered frame proves the endpoint, the subscription and the decode. It stays.
- *Against a second, transport-liveness clock.* It would be refreshed by Delta's own
  heartbeat and by pongs — frames that prove the pipe and say nothing about the data. Our
  bars come from data. Nautilus's own configuration doc says the same thing about the two
  and warns that a venue answering a keepalive with a text payload "refreshes this timer
  exactly like real data does". Adding one would give us a green light that a quiet feed
  could satisfy.

**2. Operations burden for two or three people.** Confirms it, and settles two rejections.

- The cut removes the only recovery path that needed a human at a terminal. There is no
  new knob, no new state, no new `reason` and no new event: the events an operator sees
  are the ones a real drop already produces.
- *Against jitter.* Jitter de-synchronises a crowd — `backoff.rs` says so: "Random jitter
  reduces synchronized reconnect storms." We run one feed process with one connection per
  venue. A configuration field that cannot change an outcome is cost with no benefit.
- *Against an attempt-rate throttle.* Nautilus's `ReconnectThrottle` guarantees at least
  1 s between attempts once three fall inside two minutes. Our first wait is 1 s and we
  have no immediate-first mode, so **we already satisfy its guarantee by construction**.

**3. Cost at our rate and at ten times it.** Not moved. The change adds no message to the
bus and no byte to the store; a silence-driven reconnect publishes the same
`feed.connection` events a venue-driven one does. What it costs is dials: `derived` 11
dials across 303 s of backoff to exhaust the budget, **7.3% of Delta's documented 150
connections per 5 minutes per IP**. At ten times the rate the message volume changes and
this number does not — it is bounded by the budget, not by traffic.

**4. Path to the right half — OMS, execution, NSE, more consumers.** Supports it. An
execution client cannot be told "the feed noticed it was dead and waited for a person". The
rule generalises to every adapter with no per-venue special case, because the cut is
`asyncio` cancellation unwinding `stream`'s own `async with` — `commands.md` §4's finding
that no protocol member was needed is what makes this free for NSE.

**5. Latency to the venue.** Not moved. The cut happens on a connection that has delivered
nothing for 45 s; there is no latency to lose.

## Rejected, and why

| Rejected | Why |
|---|---|
| Jitter on the backoff | Criterion 2. Jitter is for crowds; we are one connection. It would be an untestable knob |
| A reconnect-rate throttle | Criterion 2. Our 1 s first wait already meets Nautilus's 1 s floor, with no immediate-first to defeat it |
| Restoring the budget on 10 s of uptime | Criterion 1. An open does not prove a subscription took; a frame proves all three things |
| An unlimited budget, Nautilus's default | Criterion 1. Ours is bounded because Delta's connection allowance is, and because a feed that has failed eleven times without delivering will not deliver on the twelfth. Kept — but see below |
| A second, transport-liveness clock | Criterion 1. It would be refreshed by keepalives, and a keepalive is not a bar |
| Enabling Delta's `enable_heartbeat` now | Criterion 1, and honesty about scope. It is the venue's own recommendation and it is worth doing — but it is a live-socket protocol change in the adapter, it needs a measured run against the venue, and it carries a trap: a `heartbeat` frame routed to `sink()` would reset the staleness clock and silently retire this whole design. It gets its own ticket, not a line in this one |
| Tracking subscription acknowledgements | Criterion 2 against criterion 1, and criterion 1 is already answered. Delta does reply with a `subscriptions` message and we do drop it, which is a real gap — but an unacknowledged subscribe delivers nothing and is therefore **caught by `reconnect_after` within 45 s**. The failure is bounded, not silent, so the tracker is an improvement rather than a fix |
| Changing `reconnect_after` | Nothing measured says to. It sits under Delta's documented 60 s and above Delta's own 35 s heartbeat window. The six-hour run may say otherwise |

## What would change this decision

- ~~**The six-hour tagged run.**~~ **Landed 2026-09-09T19:03Z**: the longest gap on an
  unbroken connection was `measured` **6.792 s**, 6.6x inside the bound, so the adopted rule
  cannot cut a healthy socket on this evidence. What the run did show is thirteen
  connections in six hours, partly this laptop's tunnel; the venue's own drop rate is still
  to be measured from a host without one.
- **A second feed process, or a fleet.** Jitter stops being a knob with no effect the day
  more than one client of ours redials at the same instant.
- **A Delta outage longer than five minutes.** `derived` 303 s of backoff exhausts the
  budget, after which recovery is a process restart — `resume` refuses a spent budget
  (`commands.md` §5). Delta's own advice after a 429 is to wait "5 to 10 minutes", which is
  longer than our whole ladder. One observed outage that outlasts it turns Nautilus's
  unlimited default from a rejected option into the right one.
- **A venue that is legitimately silent.** Every measurement so far is an active BTC
  session. A thin ETH chain, a weekend, or NSE outside market hours could make 45 s an
  ordinary gap, and then the adopted rule reconnects a working feed on a schedule.
- **Delta's heartbeat, once enabled.** It would give a liveness signal independent of
  market data, and the second clock rejected above becomes buildable and worth building.

## Appended 2026-09-12 (#106) — the tag is settled at `assumed`

`reconnect_after` **stays `assumed`**, and that is now the only tag it carries anywhere:
this record, [../lld/controller.md](../lld/controller.md) §4,
[../cloud/controller-policies.md](../cloud/controller-policies.md) C3 and
[../../../CONTEXT.md](../../../CONTEXT.md) §5 agree. Run `20260909T130306Z` **bounds** the value at
6.6x the worst gap on an unbroken connection; it did not pick it, and a tag says where a
number came from, not whether a later run agreed with it. The phrasing that briefly made
this a fourth tag is refused in [../../../CONTEXT.md](../../../CONTEXT.md) §7.

## Appended 2026-09-12 (#114) — "corrected wherever it appears" was not true until now

This record said above, on 2026-09-09, that *the word is corrected wherever it appears*.
**It was not.** Three days later the refused word was still written of this budget at
nineteen sites, including the state table's own `reconnecting -> stopped` row
([../lld/controller.md](../lld/controller.md) §3), the docstring on `RECONNECT_BUDGET`
itself — which asserted the refusal and its refutation in one sentence — and
[../lld/reconnect.md](../lld/reconnect.md) line 4, twenty-eight lines above that file's
own note recording the correction. **A record claiming a completed cleanup that did not
happen is the same class of defect as the records #106 found**: canon that a later
document is assembled from, saying something a reader will get wrong.

All nineteen are corrected, and the claim is now held rather than asserted:
`engine/tests/test_nomenclature.py::test_the_reconnect_budget_is_never_called_a_lifetime_budget`
scans the normative documents, `engine/src/deltapayoff/` and `engine/tests/`, with six
uses exempted by naming the sentence that makes each correct — OpenAlgo's bug, the word
quoted as the one corrected, and a bus reader's `retries_total`, which really is a
lifetime count.

**The substance was checked before the wording was.** The budget counts consecutive
failures, as this record says: `_budget_spent` rises by one per drop in
`_spend_reconnect` and is set back to zero by `message_arrived` and by `resume`, so
eighteen drops with one delivered frame between them leave a budget of ten intact
(`measured` 2026-09-12 against `controller.py` in worktree `dxp-114`; the shipped
assertion is `test_controller.py::test_a_message_restores_the_whole_budget`). **What is
spent is a drop, not an outage** — the same run stops the connection after eleven
consecutive drops with no frame between, which is #108's 348 s of name resolution
failing, exactly. Whether a drop is the right unit is #108's third criterion and is
**not** decided here; the word is.
