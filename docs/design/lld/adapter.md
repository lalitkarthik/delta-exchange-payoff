# Low-level design: the broker adapter

**What is built inside `engine/src/deltapayoff/adapters/`.** The parts are
[../hld.md](../hld.md); what crosses out of one is [../events.md](../events.md), the
authority on the names. Numbers carry the run that produced them.

**Landed by #36 as the expand half of an expand–contract.** The adapter emits canonical
events; a shim rebuilds today's `feed.Quote` until #37 moves the two remaining consumers and
deletes it, and #38 lifts reconnect into a controller.

## 1. The files

| File | Holds |
|---|---|
| `base.py` | The `Adapter` protocol and the `Publish` callable it is handed |
| `delta.py` | `DeltaAdapter`, `instrument_from_symbol`, `VENUE` |
| `shim.py` | `LegacyQuoteBridge` — **temporary, #37 deletes it** |
| `tests/fakes/scripted_adapter.py` | The scripted double. **The one new test seam** |

Three modules outside the package are owned by it and reached through nothing else:
`feed.py` (the socket), `wire.py` (the offsets, now including `decode_ob_l2_top`) and
`delta_client.py` (the REST reads). **`feed.py` stayed separate**: #38 lifts the socket
owner under a controller, and a decoder folded into it now would have to be pulled out
again. Since #36 it decodes nothing — it publishes a `VenueMessage`, the frame verbatim
with its channel and arrival stamp, which makes "the wire layout lives behind the adapter"
true of the code and not only of the diagram.

## 2. The protocol, and what it deliberately omits

Seven members in three groups: **describe yourself** (`venue`, `underlyings`), **feed**
(`instruments`, `subscribe`, `on_connection`, `stream`, `stop`), **read** (`expiries`,
`chain_snapshot`). `on_connection` is #38's — [controller.md](controller.md) §5.
The two REST reads are on the adapter because the venue client *is* part of knowing a
venue, and putting them beside it means a second module learns a second venue.

`stream(publish)` is **push, not an async generator**, because the socket owner already
publishes and returns — the rule `fanout.py` exists to keep — and a generator would need a
bridging queue to invert it.

**Reconnect is not on the interface.** Backoff, the lifetime budget, subscription replay
and the reason a connection ended stay inside `feed.DeltaFeed`, correct and tested. #38
put the **state machine** around this protocol — which is why `on_connection` exists — and
#39 lifts the rest up beside it. Moving them early meant writing the machine twice.

**Conformance is structural**, for the reason `events/bus.py` records: an explicit subclass
would inherit `...`-bodied stubs and pass. A test pins that the check can still fail.

## 3. The three mappings

    ob_l2  frame  ->  md.option_quote
    ticker frame  ->  md.option_reference
    ticker frame  ->  md.index_quote      once per underlying, when spot moves

Over the committed captures: 136 book frames yield 136 quotes, and 136 ticker frames yield
136 references and exactly **one** index quote (§10). **Why one and not 136:** all 136
frames carry an identical `sp` of 77651.9, so spot is a property of BTC and not of the
contract whose frame carried it. An event is emitted when the value *changes*, the first
observation always counting. **The cost is named rather than discovered** — an unchanged
spot re-observed later is not re-emitted, so this stream alone cannot say how long a price
held. #37's spot bars need that, and the frame's own stamp is on every reference beside
it.

**The instrument.** `C-BTC-77600-040926` becomes `DELTA-BTC-20260904-77600-C`, the venue's
string kept in `venue_symbol`. A symbol that is not a contract returns `None` and is counted,
not raised: `underlying` becomes a partition directory name, so a wrong guess files quotes
under an asset they did not happen in.

**Both stamps travel.** `ts_venue` is Delta's `ts` — microseconds since the epoch — as
Delta gave it; `ts_received` is our wall clock at the socket read, taken once in
`VenueMessage`. Neither is corrected against the other: the arrival lag is the data.

## 4. `null` is not `0` — the boundary, and why it is here

Delta spells an absent quote three ways: `"0"`, `""` and `null`. All three become `None`
on a price, a size or an implied volatility, because rendering one as `0.0` claims somebody
bid zero; a real zero in open interest or a greek stays `0.0`. `convert`'s `to_quote_number`
and `to_number` are that split, applied field by field in `wire.py`.

**The events cannot enforce it** — a string `"0"` handed to a pydantic `float` field is
coerced to `0.0` — which is why `lld/events.md` §4 assigns the rule here. It is pinned by
`tests/test_delta_adapter.py` from `tests/fixtures/tickers-absent-quotes.json`, whose
spellings are lifted verbatim onto the wire in both channels' layouts. Red-green verified:
`to_number` for `to_quote_number` in `decode_ob_l2_top` turns a `"0"` bid into `0.0`.

**A non-finite number is absent, counted, and takes its size with it.** `Event` refuses
`NaN` outright — pydantic would serialise it to JSON `null`, indistinguishable from a quote
that was never there — and that refusal must not reach the socket reader, so the adapter
converts to `None` first and increments `non_finite`. A price that goes absent drops its
size, or the size would describe an order at no price. Reachable, not defensive:
`json.loads` accepts the bare tokens `NaN` and `Infinity`; standard JSON does not.

## 5. What the adapter learned about the venue

All `measured` from the 2026-09-03 captures, this session's decode pass; tags in §10.

- **The ticker frame carries no tick size.** `md.option_reference.tick_size` is `None` on
  all 136; REST carries one and the websocket does not. Absent rather than invented.
- **11 of 136 contracts carry an open interest of exactly zero**, so the real-zero half of
  §4's rule is not hypothetical.
- **`d` holds exactly one contract in every frame**, which `wire.py` assumed and nothing
  had checked.
- **Every book frame carries `lts`, and every level is exactly `[price, size]`** — no empty
  book on either side. Sizes had never been decoded; the offsets went into
  `wire.decode_ob_l2_top`, beside every other Delta offset.
- **16 of 136 contracts have never traded** — `ohlc` all-null — so `last_price` is `None`.
  Absent stays absent: a zero would read as "it last traded at zero".
- **Two ticker body fields reach no event**: `pb`, the price band, and `m24hc` — seen and
  skipped, not overlooked. **The decode is not the hot path**: `derived` 1.3% of a core.

## 6. The shim, and the four fields it exists for

`LegacyQuoteBridge` rebuilds `feed.Quote` from the venue frame the adapter just decoded.
**#36 specified it as a bus subscriber turning events back into quote records; it is not,
and the reason is #37's next problem:** four things the consumers read live only in the
frame and have no field in the catalogue.

| Not in any event | Read today by |
|---|---|
| `lts` | the quote bars' `last_lts` column |
| `to[0]`, turnover | the reference bars' `turnover` column |
| `i`, `product_id` | `Leg.product_id`, which reaches the browser |
| the ticker frame's own `q` bid and ask | the quote bars' fallback for a silent book, and its `from_book` provenance flag |

Rebuilding a frame from events would drop all four **silently**: three columns of nulls and
a fourth that quietly stops producing rows, every number still plausible. So the frame
travels verbatim, and the catalogue is not extended in passing by a ticket that does not own
those types. **#37 must answer those four rows** — optional fields added with
`schema_version` left at 1, which `events.md` calls a compatible change, or a written
decision that a column is not worth carrying — or deleting `shim.py` loses data.

**A fifth thing for #37, found in review.** `md.index_quote` reads `sp` with `to_number`,
so a spot spelled `"0"` becomes `0.0` rather than `null` — not a plausible index price.
Left alone deliberately: `wire.decode_ticker_extras` reads `sp` the same way for the spot
bars, and changing one path would make the event and the stored row disagree. #37 owns
both and should change them together; `last_price`, off `ohlc[3]`, has the same shape.

The decode happens **once, before either consumer sees anything**, so a frame that makes
no sense is dropped whole — as `feed.py` dropped it before the decode moved. Letting it
through would leave the chain cache raising on it every recompute pass, for as long as it
stayed the newest frame for that contract. The first such frame is logged; the counter
carries the rest, because logging every one would flood at 1,323 msg/s.

## 7. The scripted fake, and its script language

`tests/fakes/scripted_adapter.ScriptedAdapter` implements the same protocol and does what
it is told. Four verbs, walked in order by `stream`:

- **`Frames(channel, frames)`** — these arrived; their events are published.
- **`Close(reason)`** — dropped and came back, **replaying every subscription**.
- **`Silence(seconds)`** — nothing arrives for this long; nothing is published.
- **`Resume()`** — the feed returns with the book it left with: the last `Frames` again.

So `[Frames(...), Close(), Silence(20.0), Resume()]` produces the events, then nothing,
then the events again — the ticket's sentence, run in `tests/test_scripted_adapter.py`.
A scripted reconnect starts the decoder afresh, so a **ticker** replay produces its
`md.index_quote` again instead of deduplicating it into silence.

**`Silence` does not wait.** The clock is injected, so twenty seconds are free and still
assertable — which lets #38 test a 15 s staleness bound (`assumed`, `hld.md` §5) cheaply.

**It emits no `feed.connection` events.** Connection state is the controller's to decide
and #38 owns it; a double that pre-empted the machine would make #38's tests assert against
the double. `Close` is observable through `closes`, `connections` and `replays`, where an
empty snapshot is a reconnect that replayed nothing — the healthy-connection-carrying-zero-
messages failure, made visible. **#38 will need a connection signal on the protocol
itself**; there is none today, by design. It decodes with the real Delta decoder unless
`decode=` says otherwise, so the committed fixtures give genuine events.

## 8. Failure modes

| What goes wrong | What happens |
|---|---|
| Frame is not JSON | `feed.malformed` grows; the read loop continues |
| Frame is JSON and makes no sense | `adapter.undecodable` grows; **nothing reaches the shim or the bus** |
| `sy` is not a Delta option symbol | `unparseable_symbols` grows; no events, but the old path still gets the frame |
| A price is `NaN` or `Infinity` | Carried as `None`; `non_finite` grows |
| The venue gives no `ts` | `ts_venue` is `null`; our clock is never substituted |
| `sp` spelled `"0"` | Emitted as a spot of `0.0`. **A known gap** — see below |
| Control traffic (`subscriptions`, `error`) | Dropped in `feed._to_message`; never on a bus |
| `DELTA_LIVE_UNDERLYINGS` names an asset Delta does not list | Dropped, logged at error, BTC recorded |
| The venue is unreachable at start-up | `DeltaUnavailable`; REST serves, socket says `waiting` |

## 9. The seam the tests drive

`events_from_frame(channel, frame, received_at)` — three plain values, no socket, no bus,
no clock. Every captured frame in `tests/fixtures/ws-*.json` runs through it, so the
boundary is asserted against 136 real contracts on both channels. `tests/test_feed.py`
drives the whole loop, a scripted connection at one end and `md.option_quote` at the other;
`tests/test_composition.py` is the tracer bullet through the shim, red-green verified.

## 10. Numbers

| Number | Tag | Run |
|---|---|---|
| 136 book frames → 136 quotes; 136 ticker frames → 136 references + 1 index quote | `measured` | `tests/fixtures/ws-*.json`, decoded this session, 2026-09-07 |
| One distinct `sp` across all 136 ticker frames; `tick_size` absent on 136/136 | `measured` | same capture |
| 11 of 136 contracts at exactly zero open interest; 16 of 136 never traded | `measured` | same capture |
| Decode 30.6 µs per book frame, 43.6 µs per ticker frame | `measured` | 20 passes over the captures, this session |
| ≈1.3% of one core to decode the live BTC feed — 4.9% against the contested higher rate in `hld.md` §5. The wire decode inside it already ran in the socket reader before #36; only the event construction is new | `derived` | the two above against `docs/ingestion.md`'s 267.7 and 117.6 msg/s |
| Live: `/health` ok, `/ws/chain` a 21-row ladder after 5 `waiting` messages (spot 79634.8, forward 79642.12, 42 legs with our IV), 4 Parquet files at 412,100 bytes, 7,385 rows written | `measured` | engine run 2026-09-07T13:24:41Z, 410 s, BTC only |
