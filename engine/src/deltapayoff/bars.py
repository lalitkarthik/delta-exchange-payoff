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

**The `ticker` channel has its own watermark, and that is the whole reason it needed
one.** Measured in the same run, its frames carry a `ts` a median 3,176 ms and up to
5,298.8 ms behind our arrival — a full republish cycle of staleness, because that stamp
appears to mark the underlying quote rather than the publish. Bucketing it on `ts` under
`ob_l2`'s 2.0 s grace would call almost every frame late, which is why #10 refused the
channel outright rather than storing a table that was mysteriously empty. See
`TICKER_GRACE_SECONDS`.

---

**Four tables are built here, and they have different grains on purpose.**

*Table A, quote bars,* per contract per minute, from the book channel with the ticker
channel as its **fallback**. Which one a minute's quotes came from is recorded in
`from_book`, because a bar sampled 118 times and a bar sampled 12 times are different
objects and a tick count alone cannot tell "quiet book" from "no book at all".

*Table B, reference bars,* per contract per minute, from the ticker channel: mark and
last traded price as OHLC, everything else last-value-in-bar. **Mark and LTP are prices
and they move, so they get a range; open interest, turnover, Delta's five Greeks and its
three implied vols are levels, and an OHLC of rho means nothing.** The LTP is the
**close** of Delta's rolling 24-hour candle and nothing else from it: the high, low and
open of that field are a 24-hour window that would be re-stored identically 1,440 times a
day. This knowingly discards eleven of every twelve samples of Delta's own vols and
Greeks, which republish every 5,001 ms. That loss is accepted — the finding it would
preserve, that their vol steps while ours moves continuously underneath it, is a live
observation to capture once, not a reason to store ten million rows a day forever.

*Table D, spot bars,* per **underlying** per minute. Spot is a property of the underlying
and not of a contract: measured, all 136 ticker frames captured inside a 0.06 s window
carried an identical `sp` of 77651.9. Putting it on contract rows would store the same
four numbers 588 times a minute and, worse, would let two contracts whose frames
straddled a boundary disagree about what spot was. It is also the best-sampled series in
the feed at roughly 7,056 observations a bar, because every contract's frame carries it.

*Table C, our computed values,* per contract per minute — and **it is the odd one out.**
The other three fold ticks that arrived on the bus. Our implied volatility, our five
Greeks, the fitted forward and the discount never arrive at all: they are produced by
`ChainStream`'s 100 ms recompute loop and, until #12, lived exactly as long as the
process did. So this table is **sampled from that cache at bar close** rather than
aggregated, it is bucketed on **our** clock rather than the venue's — the instant we
computed it is the only instant there is — and its grace is zero, because a sample has no
stragglers to wait for. Every row carries `model_version`, which is what stops a later
change to the model silently mixing two populations in one column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .chain import expiry_from_symbol
from .compute import MODEL_VERSION
from .wire import decode_ticker, decode_ticker_extras

#: One bar's width. Not configurable: every count and estimate in #5 is against minutes,
#: and a second width would make two populations of rows indistinguishable in one table.
BUCKET_US = 60_000_000

#: `ob_l2`'s own watermark. See the module docstring — `derived` from a measured
#: arrival-lag distribution, 2026-09-04. It is no longer what the quote bars seal on,
#: for the reason `QUOTE_GRACE_SECONDS` gives, and it stays here because it is still the
#: book channel's lateness bound and the number every `ob_l2` figure is argued from.
GRACE_SECONDS = 2.0

#: `ticker`'s watermark, measured the same way and **fifteen times larger**, because
#: that channel's `ts` is not a publish time.
#:
#: `tools/measure_arrival_lag.py`, 2026-09-04, 60 s, all 685 listed BTC options, lossless
#: queue: 8,220 frames, mean 3,078.7 ms, p50 2,882.5, p90 4,415.8, p95 4,557.4, p99
#: 4,691.4, p99.9 4,696.4, **max 4,696.6**, min 981.7. An earlier 45 s run the same day
#: saw a max of **5,298.8 ms** over 6,165 frames, and that larger figure is the one this
#: number is chosen against.
#:
#: The shape is structural rather than stochastic, which is what makes a modest multiple
#: safe: the stamp marks the quote the frame describes and the channel republishes every
#: 5,001 ms, so the lag is bounded by one republish interval plus transit — 5,001 ms plus
#: `ob_l2`'s measured 510.3 ms maximum is a **5,511 ms ceiling**. Eight seconds is 1.5x
#: the worst frame ever observed and 1.45x that ceiling, and the remaining 2.5 s absorbs
#: the two things the measurement cannot: unsynchronised clocks (our `time.time()` and
#: Delta's `ts` are two clocks and an NTP offset of tens of milliseconds is ordinary) and
#: queue latency, since the watermark is read when the writer drains.
TICKER_GRACE_SECONDS = 8.0

#: What the **quote** bars seal on, and it is the ticker's number rather than the book's.
#:
#: #10 sealed table A at 2.0 s because `ob_l2` was its only source. #11 gives it a
#: second: the ticker channel's `q` array is the fallback when the book is silent for a
#: contract, exactly as `wire.chain_from_frames` already overrides one with the other.
#: A bar that seals at 2.0 s has closed four seconds before its fallback could arrive,
#: so every fallback quote would be counted late, the fallback would be dead code and
#: the provenance flag would be a constant `True`. Sealing on the larger of the two
#: watermarks is the only choice that makes the fallback reachable at all.
#:
#: The cost is that a quote bar is written six seconds later than it used to be, inside
#: an hourly flush. Nothing downstream can notice. The cost of the alternative is a
#: whole column of quotes that never arrive.
QUOTE_GRACE_SECONDS = TICKER_GRACE_SECONDS

#: The two channel names, spelled once. `feed.Quote.channel` and `Tick.source` both
#: carry them and a typo in either would silently route every tick to the fallback.
BOOK_CHANNEL = "ob_l2"
TICKER_CHANNEL = "ticker"


@dataclass(frozen=True, slots=True)
class Tick:
    """One contract's top of book at one venue instant.

    `exchange_us` is Delta's `ts`, microseconds since epoch, and is the **only** field
    that decides which bar this belongs to. `lts_us` travels to the bar as a column and
    decides nothing.

    Either price may be absent. `bid=None, ask=None` is not a quote at all and advances
    nothing; one side present advances that side alone.

    `source` is the channel this came from — `"ob_l2"` or `"ticker"`. It decides which
    of a bucket's two sets of series the tick lands in and therefore what `from_book`
    says; it never decides which bucket, because both channels are bucketed on the
    venue's `ts`. It defaults to the book because the book is the source of every quote
    that is not a fallback.
    """

    symbol: str
    exchange_us: int
    bid: float | None = None
    ask: float | None = None
    lts_us: int | None = None
    source: str = BOOK_CHANNEL

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

    #: **True** when this minute's quotes came from the order book channel, **False**
    #: when the book was silent for this contract and the ticker channel's slower `q`
    #: array supplied them instead. Not decoration: a bar sampled 118 times and a bar
    #: sampled 12 times are different objects, and a tick count alone cannot distinguish
    #: a quiet book from no book at all. Twelve could be either.
    from_book: bool

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
class _Last:
    """One last-value-in-bar field, chosen by the **venue's** clock.

    The same argument as `_Series.close`, for the same reason: two ticks in one minute
    can reach us out of order, and taking the last *received* would let our network pick
    which sample the bar reports.
    """

    value: Any = None
    at: int = -1

    def update(self, value: Any, exchange_us: int) -> None:
        if exchange_us >= self.at:
            self.value, self.at = value, exchange_us


@dataclass(slots=True)
class _OpenBar:
    bid: _Series = field(default_factory=_Series)
    ask: _Series = field(default_factory=_Series)
    mid: _Series = field(default_factory=_Series)
    last_lts_us: int | None = None
    last_lts_at: int = -1

    @property
    def observed(self) -> bool:
        """Whether this source contributed anything at all to the bar. The mid is not
        consulted: a run of one-sided ticks is a real observation of this channel and
        must not read as silence."""
        return bool(self.bid.ticks or self.ask.ticks)


@dataclass(slots=True)
class _Sources:
    """One bucket's two candidate bars, one per channel, kept apart until the bar is
    emitted.

    They are not merged. `wire.chain_from_frames` overrides a ticker quote with a book
    quote **wholesale** for a live chain, and a bar has to make the same choice or its
    provenance flag would be answering for a mixture. A book bar with two stale ticker
    samples folded into its high and low is a bar nothing can describe.
    """

    book: _OpenBar = field(default_factory=_OpenBar)
    ticker: _OpenBar = field(default_factory=_OpenBar)

    def of(self, source: str) -> _OpenBar:
        return self.ticker if source == TICKER_CHANNEL else self.book


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


class _Watermarked:
    """The sealing machinery every table shares: bucket, watermark, seal, flush, count.

    Three tables aggregate on the same clock discipline and differ only in what they
    fold into a bucket, so the discipline lives here once. Writing it three times would
    give three chances to get lateness subtly different, and lateness is the rule most
    likely to be got subtly wrong — a bar sealed a beat early loses real observations and
    says nothing.

    A key is always `(name, minute_us)`: the contract for tables A and B, the underlying
    for table D. Bars come back sorted by minute then name, so two runs over the same
    ticks produce byte-comparable files.

    Nothing here reads a clock or touches a file. `seal(now)` is *given* the wall clock,
    which is what makes lateness a test parameter instead of a race.
    """

    def __init__(self, grace_seconds: float) -> None:
        self.grace_seconds = grace_seconds
        self._open: dict[tuple[str, int], Any] = {}
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
        #: Ticks carrying nothing to fold in. Not an observation; advances no series.
        self.empty = 0
        self.bars_emitted = 0

    def _parsed(self, symbol: str) -> tuple[str, str, float, str] | None:
        """Symbol metadata, cached. A symbol that cannot be parsed is **counted** and
        refused rather than stored under a guess: `underlying` is a directory name and a
        wrong guess files quotes under a day or an asset they did not happen in."""
        meta = self._meta.get(symbol)
        if meta is None:
            meta = _parse_symbol(symbol)
            if meta is None:
                self.unparseable += 1
                return None
            self._meta[symbol] = meta
        return meta

    def _bucket(self, exchange_us: int) -> int | None:
        """The minute this tick belongs to, or `None` if that minute is already sealed.

        Late is a **policy**, not an accident: the tick is refused and `late` moves.
        """
        minute_us = exchange_us - exchange_us % BUCKET_US
        if self._sealed_through_us is not None and minute_us <= self._sealed_through_us:
            self.late += 1
            return None
        return minute_us

    def seal(self, now: float) -> list[Any]:
        """Emit every bar whose minute closed at least `grace_seconds` ago by `now`.

        `now` is a wall clock in seconds, on the same epoch as the venue's `ts`.

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

    def flush(self) -> list[Any]:
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
        # would hold two rows for one key-minute with no way to tell which is whole.
        # Emitted is emitted, however it was emitted.
        if keys:
            newest = max(minute for _, minute in keys)
            if self._sealed_through_us is None or newest > self._sealed_through_us:
                self._sealed_through_us = newest
        self.bars_emitted += len(bars)
        return bars

    def _emit(self, key: tuple[str, int], state: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

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


class BarAggregator(_Watermarked):
    """Every open quote bar, keyed by `(symbol, minute)`. Feed it ticks; ask it for bars.

    Two sources reach the same bar and they do **not** mix. `ob_l2` is the book channel
    and owns the quotes; `ticker` carries the same two numbers about ten times more
    slowly and is the **fallback** for a contract whose book is silent — exactly the
    precedence `wire.chain_from_frames` already applies to a live chain, where a book
    frame overrides a ticker one wholesale rather than being averaged with it.

    So each bucket holds two independent sets of series, one per source, and the emitted
    bar takes the book's if it saw anything at all and the ticker's otherwise. Folding
    both into one set was rejected: a book bar with two stale ticker samples mixed into
    its high and low would be a bar whose provenance is unanswerable, and the provenance
    is the whole point of the flag.
    """

    def __init__(self, grace_seconds: float = QUOTE_GRACE_SECONDS) -> None:
        super().__init__(grace_seconds)

    def add(self, tick: Tick) -> None:
        """Fold one tick into its bar. Cheap and synchronous — the socket reader is
        upstream of this and must never wait on it.

        `tick.source` decides which of the bucket's two sets of series it lands in. It
        never decides which bucket: both channels are bucketed on the venue's `ts`, and
        the only difference between them is how long the bar waits before sealing.
        """
        if self._parsed(tick.symbol) is None:
            return

        if tick.bid is None and tick.ask is None:
            self.empty += 1
            return

        minute_us = self._bucket(tick.exchange_us)
        if minute_us is None:
            return

        self.ticks += 1
        sources = self._open.get((tick.symbol, minute_us))
        if sources is None:
            sources = self._open[(tick.symbol, minute_us)] = _Sources()
        bar = sources.of(tick.source)

        if tick.bid is not None:
            bar.bid.update(tick.bid, tick.exchange_us)
        if tick.ask is not None:
            bar.ask.update(tick.ask, tick.exchange_us)
        mid = tick.mid
        if mid is not None:
            bar.mid.update(mid, tick.exchange_us)

        # Last by the venue's clock, like every other close in the bar. `lts` is stored
        # and never bucketed on, so this is the only thing it participates in. Only the
        # book channel carries it; a `ticker` frame has no `lts` at all.
        if tick.lts_us is not None and tick.exchange_us >= bar.last_lts_at:
            bar.last_lts_us, bar.last_lts_at = tick.lts_us, tick.exchange_us

    def _emit(self, key: tuple[str, int], sources: _Sources) -> QuoteBar:
        """The chosen source wins the whole bar, not a column of it.

        `from_book` is not decoration. A bar built from 118 book samples and one built
        from 12 ticker samples are different objects, and a tick count alone cannot tell
        "quiet book" from "no book at all" — 12 could be either. The flag can.
        """
        symbol, minute_us = key
        underlying, expiry, strike, option_type = self._meta[symbol]
        from_book = sources.book.observed
        bar = sources.book if from_book else sources.ticker
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
            from_book=from_book,
            last_lts=None if bar.last_lts_us is None else _to_utc(bar.last_lts_us),
        )

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


# --- table D: spot bars, one row per underlying per minute -----------------------


@dataclass(frozen=True, slots=True)
class SpotTick:
    """One underlying's price at one venue instant, carried on a contract's frame.

    `symbol` is the contract the frame arrived on and is used for **nothing but working
    out which underlying this is**. It is deliberately not stored: spot belongs to BTC,
    not to `P-BTC-78500-040926`, and keeping the messenger on the row would invite a
    reader to join on it.
    """

    symbol: str
    exchange_us: int
    spot: float | None


@dataclass(frozen=True, slots=True)
class SpotBar:
    """One underlying's minute. 1,440 rows a day per underlying and no more.

    `underlying` is here because it is the partition directory's name; there is no
    contract identity on this row at all, which is the point of the table.
    """

    underlying: str
    minute: datetime

    spot_open: float | None
    spot_high: float | None
    spot_low: float | None
    spot_close: float | None
    spot_ticks: int


class SpotAggregator(_Watermarked):
    """Spot bars, keyed by `(underlying, minute)`.

    **The best-sampled series in the feed**: every one of ~588 contracts' ticker frames
    carries the same `sp`, so a minute holds roughly 7,056 observations of it against the
    118 an individual contract's book gets. That is why the tick count is worth storing —
    it is the one number that says whether the ingester was actually running.

    Sealed on the ticker watermark, because that is the channel it arrives on.
    """

    def __init__(self, grace_seconds: float = TICKER_GRACE_SECONDS) -> None:
        super().__init__(grace_seconds)

    def add(self, tick: SpotTick) -> None:
        meta = self._parsed(tick.symbol)
        if meta is None:
            return
        if tick.spot is None:
            # An absent `sp` is not a spot of zero. A bar opened on it would be a row
            # invented out of no observation.
            self.empty += 1
            return
        minute_us = self._bucket(tick.exchange_us)
        if minute_us is None:
            return

        self.ticks += 1
        key = (meta[0], minute_us)
        series = self._open.get(key)
        if series is None:
            series = self._open[key] = _Series()
        series.update(tick.spot, tick.exchange_us)

    def _emit(self, key: tuple[str, int], series: _Series) -> SpotBar:
        underlying, minute_us = key
        return SpotBar(
            underlying=underlying,
            minute=_to_utc(minute_us),
            spot_open=series.open,
            spot_high=series.high,
            spot_low=series.low,
            spot_close=series.close,
            spot_ticks=series.ticks,
        )


# --- table B: reference bars, one row per contract per minute --------------------


@dataclass(frozen=True, slots=True)
class ReferenceTick:
    """One ticker frame's worth of what a contract was *worth*, as opposed to quoted at.

    Every `venue_` field is **Delta's own opinion** and travels as a reference column.
    The prefix is not decoration either: #5's table C stores our computed Greeks and
    implied vol under the bare names, and two columns called `delta` in one store would
    be exactly the confusion `tests/test_no_delta_inputs.py` exists to prevent.

    Not carried, and deliberately: the price band, the 24-hour mark change, the symbol
    echo and the product id — static or derivable — and the ticker's own bid and ask,
    which the book channel already owns and which reach table A as a fallback rather than
    as columns of their own.
    """

    symbol: str
    exchange_us: int

    mark: float | None
    last_traded_price: float | None

    oi_contracts: float | None
    #: **Not** open interest in USD, whatever `wire.decode_ticker` calls it. Verified
    #: against the REST snapshot captured beside the frames: `oi[1]` equals Delta's
    #: `oi_change_usd_6h` on all 136 symbols, is not its `oi_value_usd` on 126 of them,
    #: and goes negative, which a notional cannot. The ticker channel carries no USD
    #: open interest, so this store does not pretend to one.
    oi_change_usd_6h: float | None
    turnover: float | None

    venue_delta: float | None
    venue_gamma: float | None
    venue_rho: float | None
    venue_theta: float | None
    venue_vega: float | None

    venue_bid_iv: float | None
    venue_ask_iv: float | None
    venue_mark_iv: float | None

    @property
    def observed(self) -> bool:
        """Whether this frame carried anything at all.

        A frame whose `d` list is empty — control traffic, a truncated payload, a
        contract Delta has stopped populating — decodes to fifteen `None`s. Opening a
        bucket on that would emit a row where every column is null and every count is
        zero: a row with **no observation behind it**, which is the same fabrication as a
        forward-fill wearing different clothes.

        One field is enough. A contract that has never traded has no last traded price
        and its mark still moves, and that bar is a real record of a real minute.
        """
        return any(
            value is not None
            for name, value in (
                (name, getattr(self, name))
                for name in self.__slots__
                if name not in ("symbol", "exchange_us")
            )
        )


@dataclass(frozen=True, slots=True)
class ReferenceBar:
    """One contract's minute of reference values.

    Two series get a range because they are prices that move; the eleven levels below
    them get one sample, because an open/high/low/close of rho would be four numbers
    describing nothing.
    """

    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    minute: datetime

    mark_open: float | None
    mark_high: float | None
    mark_low: float | None
    mark_close: float | None
    mark_ticks: int

    ltp_open: float | None
    ltp_high: float | None
    ltp_low: float | None
    ltp_close: float | None
    ltp_ticks: int

    oi_contracts: float | None
    oi_change_usd_6h: float | None
    turnover: float | None

    venue_delta: float | None
    venue_gamma: float | None
    venue_rho: float | None
    venue_theta: float | None
    venue_vega: float | None

    venue_bid_iv: float | None
    venue_ask_iv: float | None
    venue_mark_iv: float | None


@dataclass(slots=True)
class _OpenReference:
    mark: _Series = field(default_factory=_Series)
    ltp: _Series = field(default_factory=_Series)
    last: _Last = field(default_factory=_Last)


class ReferenceAggregator(_Watermarked):
    """Reference bars, keyed by `(symbol, minute)`, sealed on the ticker watermark.

    **Last-value-in-bar means the last frame's values, taken together.** The alternative
    — each field carrying its own most-recent non-null — was rejected: it produces a row
    whose delta came from 09:00:12 and whose vega came from 09:00:47, which is not a
    snapshot of anything and would quietly break any reader that assumed the eleven
    reference figures were mutually consistent. Taking one frame whole costs the
    occasional field that was null in the final sample and buys a row that describes a
    moment that actually existed.
    """

    def __init__(self, grace_seconds: float = TICKER_GRACE_SECONDS) -> None:
        super().__init__(grace_seconds)

    def add(self, tick: ReferenceTick) -> None:
        if self._parsed(tick.symbol) is None:
            return
        if not tick.observed:
            self.empty += 1
            return
        minute_us = self._bucket(tick.exchange_us)
        if minute_us is None:
            return

        self.ticks += 1
        state = self._open.get((tick.symbol, minute_us))
        if state is None:
            state = self._open[(tick.symbol, minute_us)] = _OpenReference()

        # Mark and LTP are prices. A contract that has never traded has no LTP at all —
        # Delta sends `ohlc: [null, null, null, null]` — and `ltp_ticks` stays at zero,
        # which is the honest record of "no trade has ever happened here".
        if tick.mark is not None:
            state.mark.update(tick.mark, tick.exchange_us)
        if tick.last_traded_price is not None:
            state.ltp.update(tick.last_traded_price, tick.exchange_us)
        state.last.update(tick, tick.exchange_us)

    def _emit(self, key: tuple[str, int], state: _OpenReference) -> ReferenceBar:
        symbol, minute_us = key
        underlying, expiry, strike, option_type = self._meta[symbol]
        last: ReferenceTick = state.last.value
        return ReferenceBar(
            symbol=symbol,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            minute=_to_utc(minute_us),
            mark_open=state.mark.open,
            mark_high=state.mark.high,
            mark_low=state.mark.low,
            mark_close=state.mark.close,
            mark_ticks=state.mark.ticks,
            ltp_open=state.ltp.open,
            ltp_high=state.ltp.high,
            ltp_low=state.ltp.low,
            ltp_close=state.ltp.close,
            ltp_ticks=state.ltp.ticks,
            oi_contracts=last.oi_contracts,
            oi_change_usd_6h=last.oi_change_usd_6h,
            turnover=last.turnover,
            venue_delta=last.venue_delta,
            venue_gamma=last.venue_gamma,
            venue_rho=last.venue_rho,
            venue_theta=last.venue_theta,
            venue_vega=last.venue_vega,
            venue_bid_iv=last.venue_bid_iv,
            venue_ask_iv=last.venue_ask_iv,
            venue_mark_iv=last.venue_mark_iv,
        )


@dataclass(frozen=True, slots=True)
class TickerSample:
    """What one `ticker` frame is worth to the store: a fallback quote, a row of
    reference values, and one observation of spot.

    Returned together because they come from one frame and one decode. Three separate
    converters would parse the same payload three times at 137 frames a second, and would
    give three places for the frame's shape to be assumed differently.
    """

    quote: Tick | None
    reference: ReferenceTick | None
    spot: SpotTick | None


def samples_from_ticker(quote: Any) -> TickerSample | None:
    """A `feed.Quote` from the `ticker` channel to everything the store wants from it.

    **The array offsets are not repeated here.** `wire.decode_ticker` and
    `wire.decode_ticker_extras` own them, and `tests/test_wire.py` checks their ordering
    against the REST snapshot captured beside the frames. Re-indexing `g` or `qiv` in a
    second place is precisely how a transposed index gets into a store that will outlive
    everyone's memory of the wire format — the numbers stay plausible and nothing
    crashes.

    A frame with no `ts` is refused whole. Bucketing it on our arrival time would be the
    one thing this module exists not to do, and a reference row without a bucket is not
    salvageable.
    """
    if quote is None or quote.channel != TICKER_CHANNEL:
        return None
    frame = quote.frame or {}
    try:
        stamp = int(frame["ts"])
    except (KeyError, TypeError, ValueError):
        return None

    _, leg = decode_ticker(frame)
    extras = decode_ticker_extras(frame)
    symbol = quote.symbol

    fallback = None
    if quote.bid is not None or quote.ask is not None:
        fallback = Tick(
            symbol=symbol,
            exchange_us=stamp,
            bid=quote.bid,
            ask=quote.ask,
            source=TICKER_CHANNEL,
        )

    return TickerSample(
        quote=fallback,
        reference=ReferenceTick(
            symbol=symbol,
            exchange_us=stamp,
            mark=leg.mark,
            last_traded_price=extras.last_traded_price,
            oi_contracts=leg.oi,
            oi_change_usd_6h=leg.oi_change_usd_6h,
            turnover=extras.turnover,
            venue_delta=leg.delta,
            venue_gamma=leg.gamma,
            venue_rho=leg.rho,
            venue_theta=leg.theta,
            venue_vega=leg.vega,
            venue_bid_iv=leg.bid_iv,
            venue_ask_iv=leg.ask_iv,
            venue_mark_iv=leg.mark_iv,
        ),
        spot=SpotTick(symbol=symbol, exchange_us=stamp, spot=extras.spot),
    )


# --- table C: our computed values, sampled from the chain cache ------------------


#: What table C seals on, and it is **zero on purpose**.
#:
#: Every other grace period in this module answers the question "how late can a real
#: observation still arrive?" — a quote is an event the venue timed and we discovered
#: some milliseconds later, so a bar has to wait for the stragglers. Table C has no
#: stragglers to wait for. The row is a *sample of a cache we own*, taken synchronously
#: at the minute boundary; nothing can turn up afterwards claiming to belong to the
#: minute just closed. Waiting would delay the row and admit nothing.
#:
#: It is also what makes the no-invention rule enforce itself. Sealing minute M the
#: instant M ends means a chain still stamped inside M when M+1 closes is **late** by
#: `_Watermarked`'s existing rule, and late is counted and refused. A dead feed leaves
#: the chain cache holding its last computed chain forever; without this it would be
#: re-sampled every minute and the store would fill with identical fabricated rows —
#: precisely the venue defect this project documented.
COMPUTED_GRACE_SECONDS = 0.0

#: The format `chain.build_chain` writes `fetched_at` in. Second resolution, which is
#: ample for a minute bucket and is why nothing here has to reason about microseconds.
FETCHED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class ComputedTick:
    """One contract's block of **our** numbers, as of one computed chain.

    `exchange_us` is the instant we computed it, taken from the chain's `fetched_at`, and
    this is the one table in the store that is **not** bucketed on the venue's clock.
    That is deliberate rather than an oversight: a quote is an event Delta timed and we
    discovered late, so bucketing it on our arrival would let our network move it across
    a boundary. A computed value is not an event on the wire at all — it is arithmetic we
    performed, and the only clock that knows when is ours. The cost is stated in
    `docs/storage.md`: the prices behind a chain computed at 09:01:00.05 were read from
    the venue roughly 200 ms earlier and belong to 09:00, so a sample landing within one
    transit lag of a boundary can be attributed to the wrong side of it.

    **No Delta-published figure appears here.** Their vols and Greeks are table B's
    `venue_` columns, and the separation is what makes any agreement between the two
    evidence rather than construction.
    """

    symbol: str
    exchange_us: int

    iv: float | None
    #: The out-of-the-money side this strike's volatility was solved on. On a row that
    #: was *not* solved it names the side that was attempted, which is `compute.enrich`'s
    #: own spelling and is kept rather than nulled — it is recoverable from `strike`
    #: against `forward` either way, so nulling it would hide nothing and would put the
    #: store out of agreement with what the screen showed.
    iv_leg: str | None
    #: **`None` when solved, never an empty string.** `ComputedLeg` spells "no reason" as
    #: `""` because it is a JSON payload; a store spells absence as null, and one column
    #: holding both `""` and `null` for one fact is a column readers have to guess at.
    iv_reason: str | None

    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    rho: float | None

    forward: float | None
    discount: float | None
    years_to_expiry: float | None
    #: What actually produced the forward — `F1`, `F1+assumed-rate` or `F2`. Stored per
    #: row rather than inferred from `model_version`, because it varies **between chains
    #: under one model** and it pins the largest single source of variation in everything
    #: above it independently of anyone remembering to bump a string.
    forward_method: str | None

    #: See `compute.MODEL_VERSION`.
    model_version: str


@dataclass(frozen=True, slots=True)
class ComputedBar:
    """One contract's minute of our numbers, sampled at bar close.

    **Not an OHLC.** The other three tables summarise events that arrived during the
    minute; this one records the state of a computed surface at the moment the minute
    ended. An open/high/low/close of a volatility recomputed six hundred times a minute
    would be four numbers nobody could reproduce from anything, and the whole point of
    this table is that a row can be checked against the quote bar beside it.
    """

    symbol: str
    underlying: str
    expiry: str
    strike: float
    option_type: str
    minute: datetime

    iv: float | None
    iv_leg: str | None
    iv_reason: str | None

    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    rho: float | None

    forward: float | None
    discount: float | None
    years_to_expiry: float | None
    forward_method: str | None

    model_version: str


def _stamp_us(fetched_at: Any) -> int | None:
    """A chain's `fetched_at` to microseconds since epoch, or `None`.

    Guarded rather than trusted even though `chain.build_chain` is the only writer of
    this field: a chain that cannot be placed on the clock cannot be bucketed, and
    guessing a minute for it would file our numbers under a minute they did not describe.
    """
    try:
        taken = datetime.strptime(fetched_at, FETCHED_AT_FORMAT)
    except (TypeError, ValueError):
        return None
    return int(taken.replace(tzinfo=timezone.utc).timestamp()) * 1_000_000


def computed_ticks_from_chain(chain: Any) -> list[ComputedTick]:
    """One enriched `ChainResponse` to one tick per listed leg. **Pure.**

    The grain is the **contract**, matching every other table, so a paired strike gives
    two rows carrying the same volatility. That is not a duplicate: `compute.enrich`
    recovers one number per strike from whichever leg is out of the money and writes it
    to both sides, and `iv_leg` on each row names the side it came from. Storing it once
    per strike instead would make table C the only table a reader could not join to the
    other three on `symbol`.

    A leg whose `computed` block is absent is **skipped, not stored as nulls**. That
    block is `None` until the chain has been through `enrich`, and a row of nulls would
    claim we tried and failed where the truth is that we never tried.
    """
    stamp = _stamp_us(getattr(chain, "fetched_at", None))
    if stamp is None:
        return []

    ticks: list[ComputedTick] = []
    for row in chain.rows:
        for leg in (row.call, row.put):
            if leg is None or leg.computed is None:
                continue
            block = leg.computed
            ticks.append(
                ComputedTick(
                    symbol=leg.symbol,
                    exchange_us=stamp,
                    iv=block.iv,
                    iv_leg=block.iv_leg,
                    iv_reason=block.iv_reason or None,
                    delta=block.delta,
                    gamma=block.gamma,
                    vega=block.vega,
                    theta=block.theta,
                    rho=block.rho,
                    forward=chain.forward,
                    discount=chain.discount,
                    years_to_expiry=chain.years_to_expiry,
                    forward_method=chain.forward_method,
                    model_version=MODEL_VERSION,
                )
            )
    return ticks


class ComputedAggregator(_Watermarked):
    """Our computed values by `(symbol, minute)`, **last sample in the minute wins.**

    This is the one aggregator that folds nothing. The other three summarise a minute of
    arrivals; here a minute holds a handful of samples of a surface recomputed every
    100 ms, and the row wanted is the one the screen was showing when the minute ended.
    So the fold is `_Last` on the sample clock, for the same reason every other close is
    chosen that way: two samples must not be ordered by the accident of which loop pass
    noticed them.

    Sealed at a grace of zero. See `COMPUTED_GRACE_SECONDS` — there is no straggler to
    wait for, and sealing on the boundary is what turns a stale chain into a **late**
    sample that is counted and refused rather than into a fabricated row.
    """

    def __init__(self, grace_seconds: float = COMPUTED_GRACE_SECONDS) -> None:
        super().__init__(grace_seconds)

    def add(self, tick: ComputedTick) -> None:
        if self._parsed(tick.symbol) is None:
            return
        minute_us = self._bucket(tick.exchange_us)
        if minute_us is None:
            return

        self.ticks += 1
        state = self._open.get((tick.symbol, minute_us))
        if state is None:
            state = self._open[(tick.symbol, minute_us)] = _Last()
        state.update(tick, tick.exchange_us)

    def _emit(self, key: tuple[str, int], state: _Last) -> ComputedBar:
        symbol, minute_us = key
        underlying, expiry, strike, option_type = self._meta[symbol]
        last: ComputedTick = state.value
        return ComputedBar(
            symbol=symbol,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            minute=_to_utc(minute_us),
            iv=last.iv,
            iv_leg=last.iv_leg,
            iv_reason=last.iv_reason,
            delta=last.delta,
            gamma=last.gamma,
            vega=last.vega,
            theta=last.theta,
            rho=last.rho,
            forward=last.forward,
            discount=last.discount,
            years_to_expiry=last.years_to_expiry,
            forward_method=last.forward_method,
            model_version=last.model_version,
        )
