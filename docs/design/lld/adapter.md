# Low-level design: the broker adapter

**What is built inside `engine/src/deltapayoff/adapters/`.** The parts are [../hld.md](../hld.md);
what crosses out is [../events.md](../events.md). Measurements are in [HLD evidence](../hld-evidence.md#7-adapter-evidence).

**Process boundary.** In split mode, `deltapayoff.feed_main:app` exclusively owns `DeltaAdapter`,
`DeltaFeed` and the Delta REST client; the engine has no venue socket. With `DELTA_BUS` unset —
the default — the adapter and socket remain in the unchanged in-process FanOut monolith.

**Landed by #36, completed by #37.** #36 was the expand half of an expand–contract: the
adapter emitted canonical events while a shim rebuilt the old quote record for two
consumers that had not moved. #37 moved them, deleted the shim and the record, and added
the four fields it existed to carry. #38 and #39 lifted reconnect into a controller.

## 1. The files

| File | Holds |
|---|---|
| `base.py` | The `Adapter` protocol and the `Publish` callable it is handed |
| `delta.py` | `DeltaAdapter`, `instrument_from_symbol`, `VENUE` |
| `delta_socket.py` | `DeltaFeed`, `VenueMessage`, and **the two channel names** |
| `tests/fakes/scripted_adapter.py` | The scripted double. **The one new test seam** |

Two modules outside the package are owned by the feed-side adapter and reached through nothing
else: `wire.py` (the offsets) and `delta_client.py` (the REST reads).

**`delta_socket.py` moved in here in #37**, from `deltapayoff/feed.py`, because it
subscribes by channel name and #37's acceptance test is that those two strings appear
nowhere in `src/` outside this package. It stays a separate file for the reason it always
did: #38 lifts it under a controller. Since #36 it decodes nothing — it publishes a
`VenueMessage`, the frame verbatim with its channel and arrival stamp.

## 2. The protocol, and what it deliberately omits

Eight members in three groups: **describe yourself** (`venue`, `underlyings`), **feed**
(`instruments`, `subscribe`, `on_connection`, `off_connection`, `stream`, `stop`), **read**
(`expiries`, `chain_snapshot`). The connection pair is #38's — [connection-signal.md](connection-signal.md).
The two REST reads are on the adapter because the venue client *is* part of knowing a
venue, and putting them beside it means a second module learns a second venue.

`stream(publish)` is **push, not an async generator**, because the socket owner already
publishes and returns — the rule `fanout.py` exists to keep — and a generator would need a
bridging queue to invert it.

**Reconnect is not on the interface, and since #39 not below it either.** `stream` is
**one connection**: dial, replay, publish until the socket ends, return. Backoff, the
budget and the redial are [reconnect.md](reconnect.md)'s; the replay and the reason a
connection ended stay in `DeltaFeed`, because both need a socket.

**Conformance is structural**, for the reason `events/bus.py` records: an explicit subclass
would inherit `...`-bodied stubs and pass. A test pins that the check can still fail.

## 3. The three mappings

    ob_l2  frame  ->  md.option_quote
    ticker frame  ->  md.option_reference
    ticker frame  ->  md.index_quote      one per frame, since #37

Over the committed captures: 136 book frames yield 136 quotes, and 136 ticker frames yield
136 references and **136** index quotes (§10). All 136 carry an identical `sp` of 77651.9,
and #36 emitted only the first for that reason — spot belongs to BTC, not to the contract
whose frame carried it.

**#37 removed the suppression, and the reason is the store rather than the screen.** The
spot bars count observations: `spot_ticks` is `measured` ~7,056 a minute against a single
contract's 118, and it is the column that says whether the ingester was running at all. A
deduplicated stream cannot say how long a price held, so a suppressed re-observation is a row
the engine would have had to invent. `derived` ≈118 extra events a second against a
`measured` 1,322.9 msg/s feed; the spot columns are unaffected either way. `instrument`
stays `null`.

**The instrument.** `C-BTC-77600-040926` becomes `DELTA-BTC-20260904-77600-C-USD`, the
venue's string kept in `venue_symbol`. A symbol that is not a contract returns `None` and is
counted rather than raised: `underlying` is a partition directory name, and a wrong guess
files quotes under an asset they did not happen in.

**`quote_currency` and `settlement_currency`, #60 (I1).** `instrument_from_symbol` sets
both explicitly, to `chain.QUOTE_CURRENCY` and `chain.SETTLEMENT_CURRENCY` — `"USD"`,
`"USD"` — rather than leaving them to `Instrument`'s own class default, because the
acceptance criterion is that the **adapter** states them, not merely that the right value
falls out of a default. Neither is read off this frame or off Delta's `/v2/tickers` row:
the venue calls the pair `quoting_asset`/`settling_asset` on `/v2/products/{symbol}`,
which `docs/settlement.md` §3.1 measured once per symbol; the ticker row `instruments()`
and `_decode` actually see carries neither key. Adding a `/v2/products` call per
instrument to read a currency that is, today, always `"USD"` was rejected as a network
round trip this ticket does not need — see #60's ticket comment, "Rejected and why". The
constants live in `chain.py`, re-exported to this module, so both files read one fact
rather than two that could drift; `chain.py` already holds `UNDERLYINGS`, a Delta fact,
for the same reason.

**Both stamps travel.** `ts_venue` is Delta's `ts` — microseconds since the epoch, converted
by integer arithmetic so the last digit survives, because the store buckets on it since #37;
`ts_received` is our wall clock at the socket read. Neither is corrected against the other:
the arrival lag is the data.

## 4. `null` is not `0` — the boundary, and why it is here

Delta spells an absent quote three ways: `"0"`, `""` and `null`. All three become `None` on
a price, a size or an implied volatility, because rendering one as `0.0` claims somebody bid
zero; a real zero in open interest or a greek stays `0.0`. `convert`'s `to_quote_number` and
`to_number` are that split, applied field by field in `wire.py`.

**The events cannot enforce it** — a string `"0"` handed to a pydantic `float` field is
coerced to `0.0` — which is why `lld/events.md` §4 assigns the rule here. It is pinned by
`tests/test_delta_adapter.py` from `tests/fixtures/tickers-absent-quotes.json`, whose
spellings are lifted verbatim onto both layouts. Red-green verified: `to_number` for
`to_quote_number` in `decode_ob_l2_top` turns a `"0"` bid into `0.0`.

**A non-finite number is absent, counted, and takes its size with it.** `Event` refuses
`NaN` outright — pydantic would serialise it to JSON `null`, indistinguishable from a quote
that was never there — and that refusal must not reach the socket reader, so the adapter
converts to `None` first and increments `non_finite`. An absent price drops its size, or the
size would describe an order at no price. Reachable, not defensive: `json.loads` accepts the
bare tokens `NaN` and `Infinity`; standard JSON does not.

## 5. Venue findings and measurements

The venue findings and their complete measured/derived table moved to [HLD evidence](../hld-evidence.md#7-adapter-evidence)
to keep this low-level note below the repository's 200-line limit. Nothing was deleted.

## 6. The shim, and the four fields it existed for

`LegacyQuoteBridge` carried the venue's whole frame past the adapter, because four things the
consumers read lived only there and had no field in the catalogue. #37 added all four and
deleted the shim, the record and the second bus with it. **Extending the catalogue was taken
over accepting the loss** for every row: each is a stored column or a browser field, and
losing them meant three columns of nulls and a fourth that quietly stopped producing rows.

| Was in no event | Read by | Now |
|---|---|---|
| `lts` | the quote bars' `last_lts` | `md.option_quote.lts` |
| `to[0]`, turnover | the reference bars' `turnover` | `md.option_reference.turnover` |
| `i`, `product_id` | `Leg.product_id`, which reaches the browser | `md.option_reference.product_id` |
| the ticker frame's own `q` bid and ask | the quote bars' fallback for a silent book, and the `from_book` provenance beside it | `md.option_reference.bid`/`ask`, and **the event type** for the provenance |

**`from_book` needed a decision of its own and got a different answer**, because a channel is
a venue's word and the design deliberately removed channels from the events. A tick built
from `md.option_quote` is a book tick; one built from `md.option_reference`'s bid and ask is
a fallback tick. The rejected alternative was a `channel` string on `md.option_quote`, which
would put a venue's vocabulary back on the bus to redraw a line the catalogue already draws
with two type names. `schema_version` stays at `1`: each field added is optional with a
default, which `events.md` calls compatible.

**The fifth thing, `null` is not `0` on `sp`.** #36 read it with `to_number`, so a spot
spelled `"0"` became `0.0`. #37 changed `wire.decode_ticker_extras` and the event path
**together**: the spot bars read `sp` through that function, and fixing one alone would have
made the event and the stored row disagree about one frame. `last_price`, off `ohlc[3]`, had
the same shape and moved with it. Both read `to_quote_number` now; `turnover` deliberately
does not, because a contract really can have turned over nothing.

## 7. The scripted fake, and its script language

`tests/fakes/scripted_adapter.ScriptedAdapter` implements the same protocol and does what it
is told. Four verbs, walked in order by `stream`:

- **`Frames(channel, frames)`** — these arrived; their events are published.
- **`Close(reason)`** — dropped and came back, **replaying every subscription**.
- **`Silence(seconds)`** — nothing arrives for this long; nothing is published.
- **`Resume()`** — the feed returns with the book it left with: the last `Frames` again.

So `[Frames(...), Close(), Silence(20.0), Resume()]` produces the events, then nothing,
then the events again — the ticket's sentence, run in `tests/test_scripted_adapter.py`.
#36's double reset its decoder on a reconnect so a ticker replay re-emitted its
`md.index_quote`; #37 removed the suppression that made that necessary, and the reset with
it.

**`Silence` does not wait.** The clock is injected, so twenty seconds are free and still
assertable — which lets #38 test a 15 s staleness bound (`assumed`, [HLD evidence](../hld-evidence.md#5-numbers-and-where-each-came-from)) cheaply.
**It emits no `feed.connection` events**: connection state is the controller's to decide and
#38 owns it, and a double that pre-empted the machine would make #38's tests assert against
the double. `Close` is observable through `closes`, `connections` and `replays`, where an
empty snapshot is a reconnect that replayed nothing — the healthy-connection-carrying-zero-
messages failure, made visible. **#38 will need a connection signal on the protocol
itself**; there is none today, by design.

## 8. Failure modes

| What goes wrong | What happens |
|---|---|
| Frame is not JSON | `feed.malformed` grows; the read loop continues |
| Frame is JSON and makes no sense | `adapter.undecodable` grows; **nothing reaches the bus**, so the chain cache never holds something it raises on every pass. The **first** is logged and the counter carries the rest; logging each would flood at 1,323 msg/s |
| `sy` is not a Delta option symbol | `unparseable_symbols` grows; no events at all |
| A price is `NaN` or `Infinity` | Carried as `None`; `non_finite` grows |
| The venue gives no `ts` | `ts_venue` is `null`; our clock is never substituted |
| `sp` or `ohlc[3]` spelled `"0"` | Absent, not zero. Closed in #37 on both paths at once — see §6 |
| Control traffic (`subscriptions`, `error`) | Dropped in `delta_socket._to_message`; never on a bus |
| `DELTA_LIVE_UNDERLYINGS` names an asset Delta does not list | Dropped, logged at error, BTC recorded |
| The venue is unreachable at start-up | `DeltaUnavailable`; REST serves, socket says `waiting` |

## 9. The seam the tests drive

`events_from_frame(channel, frame, received_at)` — three plain values, no socket, no bus, no
clock. Every captured frame in `tests/fixtures/ws-*.json` runs through it, so the boundary is
asserted against 136 real contracts on both channels. `tests/test_feed.py` drives the whole
loop, a scripted connection at one end and `md.option_quote` at the other, and
`tests/test_composition.py` is the tracer bullet from a scripted socket to a rendered
ladder and the bar writer's counters. Every consumer test decodes through this same
function via `tests/fakes/decoder.py`, so none drifts from it. The numbers formerly in this
document are in the same [HLD evidence table](../hld-evidence.md#7-adapter-evidence), including the capture, timing and live-run details.
