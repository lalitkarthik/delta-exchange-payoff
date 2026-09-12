# Low-level design: the events package

**What is built inside `engine/src/deltapayoff/events/`.** What crosses between components is
[../events.md](../events.md), the authority on the names; this says how the code behind them
is shaped and why. Numbers are tagged `measured`, `assumed` or `derived` with their run.

**Landed by #35 as pure expand. Nothing imports this package at all** — not `main.py`, the
feed, the chain cache, the store, nor `fanout.py`, which is byte-for-byte unchanged. #36
brings the adapter producing the market-data events; #37 moves the feed, the chain cache and
the bar writer onto them and retires the `Quote` record carrying a raw `frame` dict.

---

## 1. The files, and why there are four

| File | Holds |
|---|---|
| `instrument.py` | `Instrument`, `Right`, `format_strike`, `InstrumentParseError` |
| `envelope.py` | `Event`, `register`, `registry`, `parse_event`, `UnknownEventType` |
| `catalogue.py` | The nine classes, `ConnectionState`, `BarTable`, `ChainStrike` |
| `bus.py` | The `Bus` protocol, and nothing else |

A package rather than one module because the instrument is used by things that do not care
about events at all — a cache key, a log field, a URL — and importing the whole catalogue to
spell a strike would be the wrong dependency. `bus.py` is separate for the same reason
inverted: it names two methods and must not drag pydantic behind it. It names
`fanout.Subscription` **only under `TYPE_CHECKING`**, and `fanout.py` imports nothing from
here, so the interface and its implementation stay one-way and unentangled.

## 2. The types and their invariants

**Every model is `frozen=True, extra="forbid"`.** Frozen because a consumer holding an event
must not be surprised by another; extra forbidden because a field nobody declared is either a
producer bug or a version this consumer does not understand, and both should stop loudly.

### `Instrument`

`venue`, `underlying`, `expiry` as a `date`, `strike` as a `Decimal`, `right` as `Right`,
and `venue_symbol` verbatim.

- **`strike` is a `Decimal` and never a float.** `60000`, not `60000.0`. A float prints a
  trailing zero into every key derived from it. `format_strike` normalises and then formats
  fixed-point, because `Decimal.normalize()` alone turns `60000` into `6E+4`, and an
  exponent in a cache key is a bug waiting a year to happen.
- **`Right` is spelled `C` and `P`**, which is what the store's `option_type` column already
  holds and what the canonical string prints. One spelling, no mapping table.
- **The canonical string is derived, never stored as truth.**
- **`venue_symbol` is not in the canonical string and is nullable.** The string names a
  contract, so two venues listing the same one produce the same string. That makes
  `from_canonical` a partial inverse: it takes `venue_symbol=` as a keyword and returns `None`
  when not given, so round-tripping an instrument that has one means handing it back.
- **`venue` and `underlying` may not contain `-`.** The string joins on it, so a part
  carrying one would make `canonical()` emit something `from_canonical` rejects. Refused
  where the record is built, so the two stay inverses.
- **A strike goes on the wire as a JSON number, never a quoted string.** Pydantic serialises
  a `Decimal` to `"60000"` by default, breaking a rule this project holds everywhere. A field
  serializer emits an `int` for an integral strike and a `float` otherwise, so `60000` stays
  `60000` — no quotes, no trailing zero — and reads back the same. So does `ChainStrike`.

### `Event`

The seven envelope fields of [../events.md](../events.md). Two decisions a reader would
otherwise have to infer:

- **`ts_received` is required and is never defaulted from a clock here.** The producer knows
  when the frame arrived; this package has no clock at all, which is what keeps every test of
  it deterministic and offline. `ts_venue` defaults to `None`: a venue that gives no stamp is
  ordinary.
- **`event_id` defaults to a random `uuid4().hex`.** Sequential would be cheaper to read, and
  there is no single counter to draw from once a second adapter exists.

**`ts_venue` is what the venue said, even when the venue's clock is wrong.** Not corrected,
clamped or replaced — the disagreement is the data. The arrival-lag column is
`ts_received - ts_venue`: `measured` p50 212.6 ms, p99 365.3 ms, max 510.3 ms on `ob_l2` and
a median 3,176 ms on `ticker` (`tools/measure_arrival_lag.py`, 2026-09-04, 61,648 frames
over 45 s). Neither stamp is a latency clock — `ts_received` comes from `time.time()`, which
steps backwards under an NTP correction; elapsed time is monotonic, as `timing.time_it` is.

### Three events refuse an instrument

`md.index_quote`, `computed.chain` and `feed.connection` narrow the inherited field to
`instrument: None`, so passing one fails validation — the catalogue's own sentence made
checkable. `heartbeat`, `alert` and `control.command` keep the nullable field because the
catalogue does not pin them. `md.option_bar` keeps it too, and names its underlying in the
payload, because a spot bar has no contract to name.

### `md.option_bar` carries a discriminator, not four event types

`table` is one of quote, reference, spot, computed; `columns` is a plain mapping. **The
store's four schemas are not re-declared here** — `store.py`'s `SCHEMA`, `REFERENCE_SCHEMA`,
`SPOT_SCHEMA` and `COMPUTED_SCHEMA` are their authority, and a second copy is exactly the drift
this catalogue prevents. The cost: `columns` is unvalidated and its values must be JSON scalars
to survive a wire round trip — #37's side of the bargain.

**Frozen stops at the field.** `event.columns = {...}` raises, but `event.columns["x"] = 1`
does not — `frozen` forbids assignment and does not deep-freeze a `dict`. Every other field
on every other event is a scalar, an enum or a tuple, so this is the one place it bites.

## 3. The registry, and the one thing it refuses

`@register` reads the type name off the class's own `type` field default, so a name is
written once. Registering a name twice raises, because two classes under one name would make
`parse_event` depend on import order. `registry()` returns a copy, so nobody registers by
mutation. Importing `deltapayoff.events` imports `catalogue`, which is what fills it.

`parse_event(payload)` takes JSON text, bytes, or a mapping, looks the type up, and calls
that class's validator. It raises **`UnknownEventType`** for a type nobody registered and for
a payload with no `type` at all, carrying the offending name so the log record says what was
on the wire. A registered type whose payload does not fit raises pydantic's
`ValidationError` — a different fault, which should read differently.

**A hand-written discriminated union.** Pydantic can build one over `type` with
`Field(discriminator=...)`; a dictionary lookup was chosen because the registry has to be open
— a decorator, filled at import time — and because a union raises a `ValidationError` listing
nine failed alternatives where the fault is one unknown name.

## 4. Versioning, and why version 1 will last

`schema_version` starts at `1` for every type and is **bumped when a field changes meaning,
never when one is added with a default.** Adding an optional field is compatible: an old
consumer ignores it, a new one reads it, and nothing true stops being true. Renaming a field,
changing its units, or changing when it is `null` is not, and that is what a bump announces.
The version is per type, so bumping one leaves the other eight at `1`. Expect all nine to sit
at `1` for a long time: #37 added nine fields across two types and bumped nothing.

**A consumer receiving a version it does not know must fail loudly rather than guess**, and
since #37 `parse_event` makes that check: `UnknownSchemaVersion`, naming the type and both
versions, for anything but the integer the class declares. What counts as "known" is the
consumer's business and there was no consumer when #35 landed. It is checked at **parse** and
not at construction — a producer builds at the version it was compiled with, and "do I
understand this?" only arises for a payload that crossed a boundary.

Three units are fixed and are **not** a versioning matter, because changing one is a bug:
IV is a decimal fraction on the wire and a percentage only on screen; every decimal is a
JSON number or `null`, never a string, converted once at the adapter boundary; `oi` is
contracts and `oi_value_usd` is a notional.

**These types carry those rules; they do not all enforce them.** Two are enforced here — a
strike leaves as a JSON number, and a non-finite float is refused outright. One is not: a
string `"0"` handed to a `float` field is coerced to `0.0` by pydantic, so **`null` is not
`0` is #36's boundary to hold**, where the venue's absent-quote spellings are known. A
nullable field lets the distinction be carried; it does not make it.

## 5. The bus

`Bus` is a `runtime_checkable` `Protocol` naming `publish(record)` and
`subscribe(name, maxsize, lossless)`. **It defines no behaviour.** The queue policy —
drop-oldest by default, lossless with `maxsize` as a watermark — belongs to `fanout.py`,
which argues it, and `tests/test_fanout.py`, which pins it.

**Conformance is structural, and `fanout.py` is not edited.** `FanOut` satisfies the
protocol by having the two methods; `tests/test_events.py` pins it with an `isinstance`
check and a `Bus`-annotated binding a type checker reads.

`class FanOut(Bus)` was written first and **reverted.** A `Protocol`'s methods are not
abstract, so a subclass implementing *neither* still constructs and inherits `...`-bodied
stubs returning `None`: a missing `publish` would stop raising `AttributeError` and start
dropping messages silently. It also makes the test unfalsifiable, since `isinstance`
against a nominal subclass is `True` whatever the class contains — where the structural
check does fail when a method goes missing — and it would have pulled pydantic into a
module whose imports are `asyncio` and nothing else.

## 6. Failure modes

| What goes wrong | What happens |
|---|---|
| Canonical string malformed | `InstrumentParseError`, naming which of the five parts was wrong |
| Expiry not eight digits | `InstrumentParseError` — `strptime` reads `2026627` as 2026-06-27, so length and digits are checked first |
| Strike `NaN` or `Infinity` | `InstrumentParseError` — both parse as `Decimal` and neither is a strike |
| Type not registered, or absent | `UnknownEventType`, carrying the name |
| Payload has an undeclared field | `ValidationError` — `extra="forbid"` |
| Two classes claim one type name | `ValueError` at import |
| A consumer mutates an event | `ValidationError` — frozen |
| `venue` or `underlying` contains `-` | `ValidationError` — it would break the canonical string's own inverse |
| A price is `NaN` or `Infinity` | `ValidationError` — otherwise it serialises to `null` and reads as an absent quote |
| `ChainStrike.iv` is `0` | `ValidationError` — a solved volatility of zero is not a thing |
| A `schema_version` nobody knows | `UnknownSchemaVersion` at parse, naming the type and both versions. Since #37 |

## 7. The seam the tests drive

`tests/test_events.py`, a **pure seam**: no I/O, no clock, no network, both timestamps passed
in. One sample of each of the nine, keyed by type name, with the round-trip, envelope,
immutability and extra-field tests parametrised over it; a guard asserts the sample set equals
the registry, so a tenth event with no sample fails rather than skipping every assertion.

**Two tests read [../events.md](../events.md) so the document and the code cannot drift apart
quietly.** The first asserts the registry holds exactly the nine names in its `###` headings.
The second checks that every field name the document lists in an event's **Payload** bullet
exists on that event's class — 50 names across the nine — which makes `catalogue.py`'s "field
for field" claim checkable rather than asserted. Both key off code spans rather than line
numbers, and both assert they extracted something, so a regex matching nothing cannot pass.

The second is a **subset** check: the code carries two fields the document describes in prose
rather than a code span — `md.option_bar`'s `columns` and `computed.chain`'s `solver` — so
equality would fail on a difference that is not drift. It catches the direction that matters,
a field renamed or dropped in code while the document still names it.

`measured` on this branch, 2026-09-07, `python -m pytest -q` and `python -m ruff check .` from
`engine/`: 95 tests here, suite 671 passed in 23.07 s against a 576-passing baseline, ruff clean.
