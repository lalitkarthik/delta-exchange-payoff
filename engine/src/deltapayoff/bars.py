"""Ticks in, sealed one-minute bars out. **Pure** — no socket, no clock, no filesystem.

The store cannot hold what the socket delivers. **Measured** on a live connection
(`tools/measure_feed.py`, 2026-09-03): both channels together carry 1,322.9 msg/s at
636.5 KB/s, which is ~114M rows and ~52 GB of raw JSON a day. Almost all of it is
repetition — the same contract's book republished 118 times a minute, most ticks
identical to the one before. A one-minute bar keeps the part that matters and discards
the part that does not, at ~45x fewer rows.

**A bar is a lossy summary chosen to keep the extremes.** It answers where the minute
started, how far it went either way, and where it ended. What it destroys is *path*: a
bar cannot say whether the high came before the low. That is an acceptable loss for
research over days and an unacceptable one for microstructure, and #5 chooses the former
deliberately.

---

**Aggregation is not forward-filling, and that distinction is the whole discipline.** A
bar summarises events that happened. A forward-fill invents events that did not. Delta's
own `/v2/history/candles` pads empty buckets with the last trade and does not say so:
`C-BTC-60000-270624` returns 801 daily bars of which **797 are fabricated**. So here, a
minute with no arrivals produces **no row** — not a row of nulls, and never the previous
close. Absence is the record of absence, and it is the reason this store can be trusted
at all. `tests/test_bars.py` pins it with a deliberate multi-minute silence.

**The mid is computed per tick and then aggregated, never derived from the bid and ask
bars.** `mid_open` is not `(bid_open + ask_open)/2`, because the highest bid and the
highest ask need not have occurred at the same instant — under a widening spread the two
extremes are different ticks and their midpoint is a price that never existed. This is
the failure that would otherwise be found from a chart six months later, so the mid is
stored rather than left to the reader, and the three separate tick counts are what make
a mid built from fewer samples than its bid explicable rather than baffling.

**A one-sided tick advances only its own series**, values and counts alike. A tick with a
bid and no ask has no mid, so `mid_ticks` does not move. Measured on a production
snapshot all 588 BTC options were two-sided, so this path is rare — which is exactly why
it is specified now rather than discovered in six months of data.

---

**Bars are bucketed on the venue's `ts`, never on our arrival time.** A bucket boundary
should be a property of the market rather than of our network, or a latency spike
silently moves an event across a boundary. `ob_l2`'s second stamp `lts` is carried as a
column and **never bucketed on**: measured, it sits a median 377 ms before `ts` with a
range from -13.7 ms to +7,979.5 ms, and its meaning is unverified. This project has been
caught three times by a plausible constant taken on trust and will not add a fourth by
guessing what that field means.

**Sealing is a decision about lateness, not about time.** A bar stays open until our wall
clock passes its boundary plus `GRACE_SECONDS`; anything arriving after that is
**counted** in `late` and discarded. `seal` takes the clock as an argument rather than
reading one, which is what keeps this module pure and lateness a test parameter.

`GRACE_SECONDS = 2.0` — `derived` from a **measured** distribution, not chosen because it
sounded reasonable. `tools/measure_arrival_lag.py`, 2026-09-04, 61,648 `ob_l2` frames over
45 s on the all-expiries BTC subscription: lag p50 212.6 ms, p95 226.6 ms, p99 365.3 ms,
p99.9 438.7 ms, max 510.3 ms, min 204.2 ms. Two seconds is ~3.9x the measured maximum.

The headroom over the tail is deliberate and the asymmetry is the argument. That lag is
transit **plus clock skew** — our `time.time()` and Delta's `ts` are unsynchronised
clocks and an NTP offset of tens of milliseconds is ordinary — so the figure drifts
between runs in a way a network measurement would not. It also excludes queue latency:
the watermark is read when the writer drains, so a backlog adds to observed lateness.
Against that, grace that is too short **discards real ticks**, while grace that is too
long delays a bar by two seconds inside an hourly flush, which nothing downstream can
notice. The costs are not symmetric, so the choice is not either.

**A partial bar at start or stop is flushed with its true tick counts and no flag.** The
counts already carry it — a bar with nine ticks beside neighbours with a hundred and
eighteen is self-evidently short — and a dedicated flag would be true for a few hundred
rows a day while inviting readers to treat it as the only kind of incomplete bar, which
it is not.

**Not aggregated here: the `ticker` channel.** Measured in the same run, its frames carry
a `ts` a median 3,176 ms and up to 5,298.8 ms behind our arrival — a full republish cycle
of staleness, because that stamp appears to mark the underlying quote rather than the
publish. Bucketing that channel on `ts` under any grace this side of six seconds would
call almost every frame late. It needs its own watermark and its own table (#5's table B)
and is out of scope for #10.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .chain import expiry_from_symbol

#: One bar's width. Not configurable: every count and estimate in #5 is against minutes,
#: and a second width would make two populations of rows indistinguishable in one table.
BUCKET_US = 60_000_000

#: How long past a bar's boundary we wait before sealing it. See the module docstring —
#: `derived` from a measured arrival-lag distribution, 2026-09-04.
GRACE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class Tick:
    """One contract's top of book at one venue instant.

    `exchange_us` is Delta's `ts`, microseconds since epoch, and is the **only** field
    that decides which bar this belongs to. `lts_us` travels to the bar as a column and
    decides nothing.

    Either price may be absent. `bid=None, ask=None` is not a quote at all and advances
    nothing; one side present advances that side alone.
    """

    symbol: str
    exchange_us: int
    bid: float | None = None
    ask: float | None = None
    lts_us: int | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2


@dataclass(frozen=True, slots=True)
class QuoteBar:
    """One contract's minute. Every price column is nullable because a series that
    received no tick in this minute has no open, no high, no low and no close — and
    inventing one is the defect this whole design refuses.

    `minute` is the bucket's **start**, UTC, microsecond precision. `strike`, `expiry`
    and `option_type` are columns rather than partition levels: expiry as a partition
    level explodes into thousands of tiny directories and makes Parquet slower than CSV.
    """

    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    minute: datetime

    bid_open: float | None
    bid_high: float | None
    bid_low: float | None
    bid_close: float | None
    bid_ticks: int

    ask_open: float | None
    ask_high: float | None
    ask_low: float | None
    ask_close: float | None
    ask_ticks: int

    mid_open: float | None
    mid_high: float | None
    mid_low: float | None
    mid_close: float | None
    mid_ticks: int

    #: The last `lts` seen in this minute, as a UTC timestamp. Carried, never bucketed on.
    last_lts: datetime | None


@dataclass(slots=True)
class _Series:
    """One series' running OHLC inside one open bar.

    `open` and `close` are chosen by the **venue's** clock, not by arrival order. Two
    ticks in one minute can reach us out of order — measured lag on `ob_l2` spreads from
    204.2 ms to 510.3 ms, which is most of a 508 ms republish interval — so taking the
    first and last *received* would let our network decide which price opened the minute.
    """

    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    ticks: int = 0
    open_us: int = 0
    close_us: int = 0

    def update(self, value: float, exchange_us: int) -> None:
        if self.ticks == 0:
            self.open = self.close = self.high = self.low = value
            self.open_us = self.close_us = exchange_us
            self.ticks = 1
            return
        self.ticks += 1
        if value > self.high:  # type: ignore[operator]
            self.high = value
        if value < self.low:  # type: ignore[operator]
            self.low = value
        if exchange_us < self.open_us:
            self.open, self.open_us = value, exchange_us
        # `>=` and not `>`: two ticks can share a microsecond, and the later-arriving of
        # an identically stamped pair is the better close — it is what the venue said
        # last. `>` would silently keep the first.
        if exchange_us >= self.close_us:
            self.close, self.close_us = value, exchange_us


@dataclass(slots=True)
class _OpenBar:
    bid: _Series = field(default_factory=_Series)
    ask: _Series = field(default_factory=_Series)
    mid: _Series = field(default_factory=_Series)
    last_lts_us: int | None = None
    last_lts_at: int = -1


def _to_utc(microseconds: int) -> datetime:
    """Microseconds since epoch to a UTC datetime, without losing the microseconds.

    `datetime.fromtimestamp(us / 1e6)` would go through a float and round: 1e6 seconds
    of epoch already spends more than a double's 15-16 significant digits on the integer
    part, so the last microsecond digit is not reliably representable. Dividing the
    integer instead keeps it exact.
    """
    seconds, remainder = divmod(int(microseconds), 1_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=remainder)


def _parse_symbol(symbol: str) -> tuple[str, str, float, str] | None:
    """`C-BTC-77600-040926` to `("BTC", "04-09-2026", 77600.0, "C")`, or `None`.

    A frame carries neither underlying, expiry, strike nor option type as a field; all
    four live only in the symbol. An unparseable one cannot be partitioned or filtered,
    so it is refused rather than stored under a guess.
    """
    parts = (symbol or "").split("-")
    if len(parts) < 4 or parts[0] not in ("C", "P"):
        return None
    expiry = expiry_from_symbol(symbol)
    if expiry is None:
        return None
    try:
        strike = float(parts[2])
    except ValueError:
        return None
    return parts[1].upper(), expiry, strike, parts[0]


class BarAggregator:
    """Every open bar, keyed by `(symbol, minute)`. Feed it ticks; ask it for bars.

    Nothing here reads a clock or touches a file. `seal(now)` is given the wall clock,
    which is what makes lateness a test parameter instead of a race.
    """

    def __init__(self, grace_seconds: float = GRACE_SECONDS) -> None:
        self.grace_seconds = grace_seconds
        self._open: dict[tuple[str, int], _OpenBar] = {}
        #: Symbol metadata, parsed once per contract rather than once per tick.
        self._meta: dict[str, tuple[str, str, float, str]] = {}
        #: The newest minute boundary already sealed. A tick for it or earlier is late.
        self._sealed_through_us: int | None = None

        self.ticks = 0
        #: Ticks that arrived after their bar was sealed. **Counted, never silent** — a
        #: discarded observation with no counter is the same lie as a silent drop.
        self.late = 0
        #: Ticks whose symbol could not be parsed into underlying, expiry, strike, type.
        self.unparseable = 0
        #: Ticks carrying neither a bid nor an ask. Not a quote; advances no series.
        self.empty = 0
        self.bars_emitted = 0

    def add(self, tick: Tick) -> None:
        """Fold one tick into its bar. Cheap and synchronous — the socket reader is
        upstream of this and must never wait on it."""
        meta = self._meta.get(tick.symbol)
        if meta is None:
            meta = _parse_symbol(tick.symbol)
            if meta is None:
                self.unparseable += 1
                return
            self._meta[tick.symbol] = meta

        if tick.bid is None and tick.ask is None:
            self.empty += 1
            return

        minute_us = tick.exchange_us - tick.exchange_us % BUCKET_US
        if self._sealed_through_us is not None and minute_us <= self._sealed_through_us:
            self.late += 1
            return

        self.ticks += 1
        bar = self._open.get((tick.symbol, minute_us))
        if bar is None:
            bar = self._open[(tick.symbol, minute_us)] = _OpenBar()

        if tick.bid is not None:
            bar.bid.update(tick.bid, tick.exchange_us)
        if tick.ask is not None:
            bar.ask.update(tick.ask, tick.exchange_us)
        mid = tick.mid
        if mid is not None:
            bar.mid.update(mid, tick.exchange_us)

        # Last by the venue's clock, like every other close in the bar. `lts` is stored
        # and never bucketed on, so this is the only thing it participates in.
        if tick.lts_us is not None and tick.exchange_us >= bar.last_lts_at:
            bar.last_lts_us, bar.last_lts_at = tick.lts_us, tick.exchange_us

    def seal(self, now: float) -> list[QuoteBar]:
        """Emit every bar whose minute closed at least `grace_seconds` ago by `now`.

        `now` is a wall clock in seconds, on the same epoch as the venue's `ts`. Bars
        come back sorted by minute then symbol, so a writer's output is deterministic and
        two runs over the same ticks produce byte-comparable files.

        **The watermark advances whether or not anything was open.** A quiet minute still
        seals: otherwise a tick that turned up ten minutes late for an empty bucket would
        open a fresh bar for a minute long since written, and the store would grow a row
        out of nothing.
        """
        boundary_us = int((now - self.grace_seconds) * 1e6) - BUCKET_US
        ready = [key for key in self._open if key[1] <= boundary_us]
        ordered = sorted(ready, key=lambda key: (key[1], key[0]))
        bars = [self._emit(key, self._open.pop(key)) for key in ordered]

        if self._sealed_through_us is None or boundary_us > self._sealed_through_us:
            self._sealed_through_us = boundary_us
        self.bars_emitted += len(bars)
        return bars

    def flush(self) -> list[QuoteBar]:
        """Emit every open bar regardless of the watermark, for process start and stop.

        The partial bar this produces carries its **true** tick counts and no flag. Nine
        ticks beside neighbours with a hundred and eighteen already says "short", and a
        flag would be true for a few hundred rows a day while implying it marks the only
        kind of incomplete bar. It does not: a genuinely quiet contract produces the same
        shape and is not incomplete at all.
        """
        keys = sorted(self._open, key=lambda k: (k[1], k[0]))
        bars = [self._emit(key, self._open.pop(key)) for key in keys]
        # The watermark moves with the flush. Without this a tick arriving afterwards
        # for a minute just written would open a *second* bar for it, and the store
        # would hold two rows for one contract-minute with no way to tell which is
        # whole. Emitted is emitted, however it was emitted.
        if keys:
            newest = max(minute for _, minute in keys)
            if self._sealed_through_us is None or newest > self._sealed_through_us:
                self._sealed_through_us = newest
        self.bars_emitted += len(bars)
        return bars

    def _emit(self, key: tuple[str, int], bar: _OpenBar) -> QuoteBar:
        symbol, minute_us = key
        underlying, expiry, strike, option_type = self._meta[symbol]
        return QuoteBar(
            symbol=symbol,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            minute=_to_utc(minute_us),
            bid_open=bar.bid.open,
            bid_high=bar.bid.high,
            bid_low=bar.bid.low,
            bid_close=bar.bid.close,
            bid_ticks=bar.bid.ticks,
            ask_open=bar.ask.open,
            ask_high=bar.ask.high,
            ask_low=bar.ask.low,
            ask_close=bar.ask.close,
            ask_ticks=bar.ask.ticks,
            mid_open=bar.mid.open,
            mid_high=bar.mid.high,
            mid_low=bar.mid.low,
            mid_close=bar.mid.close,
            mid_ticks=bar.mid.ticks,
            last_lts=None if bar.last_lts_us is None else _to_utc(bar.last_lts_us),
        )

    def stats(self) -> dict[str, int]:
        """What went in, what came out, and what was refused. Every refusal is counted;
        none of them is silent."""
        return {
            "ticks": self.ticks,
            "late": self.late,
            "unparseable": self.unparseable,
            "empty": self.empty,
            "bars_emitted": self.bars_emitted,
            "open_bars": len(self._open),
        }


def tick_from_quote(quote: Any) -> Tick | None:
    """A `feed.Quote` to a `Tick`, or `None` if it cannot be bucketed.

    This is the only place that knows a `Quote` exists, and it is here rather than in the
    writer so the aggregator's input stays one small dataclass with no pydantic model and
    no wire format behind it.

    **`ticker` frames are refused.** Their `ts` runs a median 3,176 ms and up to
    5,298.8 ms behind arrival (measured, `tools/measure_arrival_lag.py`, 2026-09-04)
    — a whole republish cycle — so bucketing them on the same watermark as the book
    would call almost every one of them late. They belong to #5's table B, with a
    watermark of their own.

    A frame with no `ts` is refused too. Bucketing it on our arrival time would be the
    one thing this module exists not to do.
    """
    if quote is None or quote.channel != "ob_l2":
        return None
    frame = quote.frame or {}
    exchange_us = frame.get("ts")
    if exchange_us is None:
        return None
    try:
        stamp = int(exchange_us)
    except (TypeError, ValueError):
        return None
    aux = frame.get("lts")
    try:
        lts_us = None if aux is None else int(aux)
    except (TypeError, ValueError):
        lts_us = None
    return Tick(
        symbol=quote.symbol,
        exchange_us=stamp,
        bid=quote.bid,
        ask=quote.ask,
        lts_us=lts_us,
    )
