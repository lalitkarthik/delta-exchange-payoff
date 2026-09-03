"""Where the bars go: hive-partitioned Parquet, and the only module that touches a file.

**Four tables, four dataset roots.** `quote-bars` is what the book did, `reference-bars`
is what the venue said a contract was worth, `spot-bars` is what the underlying did
underneath both, and `computed-bars` is what **we** made of all of it. They are four roots
and not one root with a `table` partition key: a shared root forces every scan to carry a
filter a directory should have answered, and puts four schemas in one dataset for
Parquet's metadata to reconcile on every read. They share the **same** partition keys, so
a reader joins spot to quotes to our volatility on `date` and `underlying` with no
translation.

**And the fourth one is filled differently.** Tables A, B and D are folded from ticks the
writer drains off the bus. Table C is **sampled** from `ChainStream`'s recompute cache at
each minute boundary, because our implied volatility and Greeks are produced there rather
than arriving on the wire. That is `BarWriter._sample_computed`, and it is the one place
in this module that reads something other than its own queue.

**Why columnar.** A CSV stores row by row, so reading one column of a billion rows means
reading all of them. Parquet stores column by column: every mid together, every strike
together. Reading one field touches only that field's bytes, and a column of similar
values compresses far better than a row of dissimilar ones — which is what makes the
dictionary encoding below the single largest win available, at 588 distinct symbols
repeated millions of times a day.

**Why `date/underlying` and nothing else.** Hive partitioning puts the filter in the
directory *name*, so a query for BTC on 4 September skips every other directory without
opening a single file. The key choice is therefore the whole design, and it is a
trade-off in both directions: too coarse and pruning buys nothing, too fine and the tree
fills with tiny files. **Expiry stays a column.** As a partition level it explodes into
thousands of directories holding a handful of rows each, and Parquet performs badly with
many small files — every one carries header and footer overhead and a reader has to open
all of them. Strike and option type are columns for the same reason, more so.

**Why hourly, and where the flush runs.** A file per bar would produce hundreds of tiny
files an hour. So sealed bars accumulate in memory and are written on a timer: hourly
caps crash loss at sixty minutes and produces files usable before any compaction runs.
A whole-day buffer was rejected — a crash at 23:00 loses the day. Fifteen minutes was
rejected as producing files small enough that the design would be leaning on compaction
to rescue a choice made on purpose.

**And the write must never happen in the socket reader's path.** If it did, the reader
would stop draining the connection while the disk worked, the operating system's receive
buffer would fill, and **Delta would close us** — nobody having enforced a limit; we
simply failed to keep up. `BarWriter` runs in its own task and hands the blocking write
to `asyncio.to_thread`, so neither the reader nor the event loop waits on a disk.

**Polars is not allowed to lay out the tree.** `write_parquet(partition_by=...)` names
its output `00000000.parquet` in every partition on every call, so the 10:00 flush would
silently overwrite the 09:00 one and the day would end holding only its last hour. The
file would be perfectly valid and simply short, which is the invisible kind of loss this
project keeps refusing. So the directories are built here and each flush writes its own
uniquely named file into them.

**The types are fixed here because they are expensive to change against a year of data.**
Prices are 64-bit: a 32-bit float carries about seven significant digits and a five-figure
BTC price with decimals already spends six, which is too close to the edge for a permanent
store. Timestamps are microsecond UTC, matching the venue's native resolution with no
conversion — a `us` timestamp that went through a float would round its last digits away
and nothing would say so. Symbol, expiry, option type and underlying are
dictionary-encoded.

Tick counts are `UInt32`. The ceiling is 118 ticks a minute measured; the type is far
wider than needed and still half of the obvious `Int64`, and it cannot go negative, which
a count never should.

`tzdata` is a runtime dependency on Windows, and only for reading. Polars stores the time
zone as a label and asks `zoneinfo` for it when converting a value back to a Python
`datetime`; without the package that call raises `ZoneInfoNotFoundError` from inside Rust
and takes the interpreter's error handling with it. The files are correct either way —
this is the reader's problem, not the store's.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import polars as pl

from .bars import (
    BUCKET_US,
    BarAggregator,
    ComputedAggregator,
    ReferenceAggregator,
    SpotAggregator,
    computed_ticks_from_chain,
    samples_from_ticker,
    tick_from_quote,
)

#: One directory per table. All four of #5's now exist.
#:
#: **A single root with a `table` partition key was rejected.** It forces every scan to
#: carry a filter that a directory should have answered, and it puts four schemas in one
#: dataset, which Parquet's own metadata then has to reconcile on every read.
#:
#: `computed-bars` is a root of its own for a second reason: it is the only table whose
#: rows can change meaning without any input changing, when the model behind them is
#: revised. Keeping it separable is what makes `model_version` something a reader can
#: filter a whole dataset on rather than a column buried among the venue's.
DATASET = "quote-bars"
REFERENCE_DATASET = "reference-bars"
SPOT_DATASET = "spot-bars"
COMPUTED_DATASET = "computed-bars"

#: Hourly. Sixty minutes is the crash-loss budget, stated as a number rather than implied.
FLUSH_SECONDS = 3600.0

#: How often the writer wakes to seal bars when the bus is quiet. Well under the grace
#: period, so a bar is written within a second or so of becoming eligible — and it costs
#: nothing, because a wakeup with nothing to seal is a dictionary scan of a few hundred
#: keys.
TICK_SECONDS = 1.0

#: The writer's queue watermark. Lossless, so this does not bound the queue — it is the
#: depth past which a backlog starts being counted. Measured, both channels together
#: deliver 1,322.9 msg/s, so 100,000 is about seventy-five seconds of feed: deep enough
#: that an ordinary flush never trips it and shallow enough that a real stall says so.
QUEUE_WATERMARK = 100_000

#: Column order and types, in one place. Read back and asserted in `tests/test_store.py`.
SCHEMA: dict[str, Any] = {
    "symbol": pl.Categorical,
    "expiry": pl.Categorical,
    "option_type": pl.Categorical,
    "strike": pl.Float64,
    "minute": pl.Datetime("us", "UTC"),
    "bid_open": pl.Float64,
    "bid_high": pl.Float64,
    "bid_low": pl.Float64,
    "bid_close": pl.Float64,
    "bid_ticks": pl.UInt32,
    "ask_open": pl.Float64,
    "ask_high": pl.Float64,
    "ask_low": pl.Float64,
    "ask_close": pl.Float64,
    "ask_ticks": pl.UInt32,
    "mid_open": pl.Float64,
    "mid_high": pl.Float64,
    "mid_low": pl.Float64,
    "mid_close": pl.Float64,
    "mid_ticks": pl.UInt32,
    #: Provenance. `Boolean` and **not nullable in practice**: every emitted bar came
    #: from one channel or the other, and a third state would mean a bar with no
    #: observation behind it, which this store does not write at all.
    "from_book": pl.Boolean,
    "last_lts": pl.Datetime("us", "UTC"),
}

#: Table B. Mark and the last traded price are prices and get a range; the eleven
#: reference levels below them get one value each, because an open/high/low/close of rho
#: would be four numbers describing nothing.
#:
#: Everything Delta publishes as its own opinion carries a `venue_` prefix. #5's table C
#: stores our computed Greeks and implied vol under the bare names, and two columns
#: called `delta` in one store is exactly the confusion `tests/test_no_delta_inputs.py`
#: exists to prevent — one of them would eventually be read as the other.
#:
#: Open interest is in **contracts** only. `oi[1]` on the wire is Delta's
#: `oi_change_usd_6h`, not a USD notional — verified against the REST snapshot captured
#: beside the frames, see `wire`'s docstring — so it is stored under that name and no
#: USD open interest is stored at all, because the channel does not carry one.
#:
#: `oi_contracts` and `turnover` are `Float64` rather than an integer type: Delta sends
#: them as decimal strings, turnover is genuinely fractional, and one numeric type across
#: the table is one fewer thing for a reader to remember.
REFERENCE_SCHEMA: dict[str, Any] = {
    "symbol": pl.Categorical,
    "expiry": pl.Categorical,
    "option_type": pl.Categorical,
    "strike": pl.Float64,
    "minute": pl.Datetime("us", "UTC"),
    "mark_open": pl.Float64,
    "mark_high": pl.Float64,
    "mark_low": pl.Float64,
    "mark_close": pl.Float64,
    "mark_ticks": pl.UInt32,
    "ltp_open": pl.Float64,
    "ltp_high": pl.Float64,
    "ltp_low": pl.Float64,
    "ltp_close": pl.Float64,
    "ltp_ticks": pl.UInt32,
    "oi_contracts": pl.Float64,
    "oi_change_usd_6h": pl.Float64,
    "turnover": pl.Float64,
    "venue_delta": pl.Float64,
    "venue_gamma": pl.Float64,
    "venue_rho": pl.Float64,
    "venue_theta": pl.Float64,
    "venue_vega": pl.Float64,
    "venue_bid_iv": pl.Float64,
    "venue_ask_iv": pl.Float64,
    "venue_mark_iv": pl.Float64,
}

#: Table D. 1,440 rows a day per underlying, and **no contract identity at all** — the
#: symbol that carried the frame is not a column, because spot belongs to BTC rather than
#: to `P-BTC-78500-040926` and storing the messenger would invite a reader to join on it.
#:
#: `underlying` is absent for the same reason as everywhere else: it is the partition
#: directory's name, and storing it twice invites the two copies to disagree.
SPOT_SCHEMA: dict[str, Any] = {
    "minute": pl.Datetime("us", "UTC"),
    "spot_open": pl.Float64,
    "spot_high": pl.Float64,
    "spot_low": pl.Float64,
    "spot_close": pl.Float64,
    #: Roughly 7,056 a bar, because every contract's ticker frame carries spot. Well
    #: inside `UInt32` and far outside `UInt16`, which 7,056 would have fitted today and
    #: overflowed the moment ETH was turned on.
    "spot_ticks": pl.UInt32,
}

#: Table C. **Our** numbers, under the bare names, beside nothing of Delta's — their
#: figures are table B's `venue_` columns and the separation is what makes any agreement
#: between the two evidence rather than construction.
#:
#: Four columns are per **chain** rather than per contract and are repeated down every
#: row of an expiry: `forward`, `discount`, `years_to_expiry` and `forward_method`. That
#: repetition costs almost nothing here — a run of one value is what dictionary encoding
#: and run-length compression are for — and it buys a row that can be checked on its own
#: without a join to a table that does not exist.
#:
#: `iv_leg`, `iv_reason`, `forward_method` and `model_version` are dictionary-encoded for
#: the same reason `symbol` is: each is one of a handful of short strings repeated
#: millions of times a day. `iv_reason` is the largest of them and the one that would
#: otherwise cost the most, at roughly fifty characters of sentence per unsolved strike.
#:
#: **`model_version` is on every row and not in a sidecar file.** A per-file or per-day
#: manifest would be one more thing to join, one more thing to lose, and it would answer
#: the wrong question the day a model changes mid-hour. A column answers it for the row
#: in front of you.
COMPUTED_SCHEMA: dict[str, Any] = {
    "symbol": pl.Categorical,
    "expiry": pl.Categorical,
    "option_type": pl.Categorical,
    "strike": pl.Float64,
    "minute": pl.Datetime("us", "UTC"),
    "iv": pl.Float64,
    "iv_leg": pl.Categorical,
    #: Null when solved. `ComputedLeg` spells "no reason" as `""` because it is a JSON
    #: payload; a store spells absence as null, and a column holding both for one fact
    #: is a column every reader has to guess at.
    "iv_reason": pl.Categorical,
    #: Ours, and **null together with `iv`**. Greeks at some default volatility would be
    #: five plausible numbers describing nothing, which is the failure `compute.py`
    #: refuses on the live path and this table refuses in storage.
    "delta": pl.Float64,
    "gamma": pl.Float64,
    "vega": pl.Float64,
    "theta": pl.Float64,
    "rho": pl.Float64,
    "forward": pl.Float64,
    "discount": pl.Float64,
    "years_to_expiry": pl.Float64,
    "forward_method": pl.Categorical,
    "model_version": pl.Categorical,
}

#: The partition columns. They live in the directory names, not in the files, which is
#: the whole point — the filter is answered by the path. All three tables share them, so
#: a reader joins spot to quotes on `date` and `underlying` without a schema translation.
HIVE_SCHEMA: dict[str, Any] = {"date": pl.Date, "underlying": pl.Categorical}


def default_root() -> Path:
    """`<repo>/data`. Git-ignored: market data is never committed."""
    return Path(__file__).resolve().parents[3] / "data"


class BarStore:
    """Sealed bars in, Parquet files out. Buffered; nothing is written until `flush`.

    One class, three tables. The layout, the partitioning, the file naming and the
    empty-scan behaviour are identical for all of them and only the column list differs,
    so `schema` is a constructor argument rather than three near-identical classes. A
    subclass per table would put the interesting decision — which columns — in the least
    visible place, and would have to re-inherit the file naming that is the one thing
    here that has already gone wrong once.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        dataset: str = DATASET,
        schema: dict[str, Any] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.dataset = dataset
        self.schema: dict[str, Any] = dict(SCHEMA if schema is None else schema)
        self._buffer: list[Any] = []
        self.flushes = 0
        self.rows_written = 0

    @property
    def path(self) -> Path:
        return self.root / self.dataset

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def add(self, bars: Iterable[Any]) -> int:
        """Buffer sealed bars. Returns how many are now waiting.

        A bar that cannot be partitioned is **refused loudly** rather than placed under a
        guess: `date` and `underlying` are directory names, and a wrong guess files
        quotes under a day they did not happen in, which is the kind of error that reads
        as data rather than as a bug.
        """
        for bar in bars:
            if not bar.underlying:
                raise ValueError(f"{bar.symbol!r} has no underlying to partition on")
            if bar.minute is None:
                raise ValueError(f"{bar.symbol!r} has no minute to partition on")
            self._buffer.append(bar)
        return len(self._buffer)

    def flush(self) -> int:
        """Write the buffer and empty it. Returns rows written. **Blocking IO.**

        Called from a worker thread by `BarWriter`, never from the socket reader's path.

        One file per `(date, underlying)` per flush, named from the flush ordinal and the
        earliest minute in it so a directory listing sorts chronologically and two flushes
        can never collide. An empty buffer writes nothing at all — an hourly flush over a
        quiet hour must not leave an empty file behind, which would be all overhead and no
        rows.
        """
        if not self._buffer:
            return 0

        buffered, self._buffer = self._buffer, []
        self.flushes += 1

        groups: dict[tuple[str, str], list[Any]] = {}
        for bar in buffered:
            key = (bar.minute.strftime("%Y-%m-%d"), bar.underlying)
            groups.setdefault(key, []).append(bar)

        written = 0
        for (day, underlying), bars in sorted(groups.items()):
            directory = self.path / f"date={day}" / f"underlying={underlying}"
            directory.mkdir(parents=True, exist_ok=True)
            earliest = min(bar.minute for bar in bars)
            name = f"{earliest.strftime('%Y%m%dT%H%M%SZ')}-{self.flushes:06d}.parquet"
            self._frame(bars).write_parquet(directory / name)
            written += len(bars)

        self.rows_written += written
        return written

    def _frame(self, bars: list[Any]) -> pl.DataFrame:
        """Bars to a typed frame. The partition columns are deliberately absent: they are
        the directory names, and storing them twice invites the two copies to disagree."""
        columns = {name: [getattr(bar, name) for bar in bars] for name in self.schema}
        return pl.DataFrame(columns, schema=self.schema)

    def scan(self) -> pl.LazyFrame:
        """The whole dataset, lazily, with the partition columns restored from the paths.

        Lazy on purpose: a filter on `date` or `underlying` is answered by the directory
        names before a file is opened, and that only happens if the filter reaches the
        scan — which `collect()`-then-filter would prevent.

        An empty store returns an empty frame with the real schema rather than raising.
        A reader opening it before the first flush is asking a legitimate question, and
        "nothing yet" is a legitimate answer — the same discipline as a minute with no
        row.
        """
        if not any(self.path.rglob("*.parquet")):
            return pl.LazyFrame(schema={**self.schema, **HIVE_SCHEMA})
        return pl.scan_parquet(
            self.path,
            hive_partitioning=True,
            hive_schema=HIVE_SCHEMA,
        )


class BarWriter:
    """The bus's second consumer: quotes in, bars on disk.

    Subscribes **losslessly**. Drop-oldest is right for a screen and wrong here, and not
    because a drop leaves a hole — under bars it perturbs a bar rather than removing a
    record. The problem is that drops happen under load, load is when price moves fastest,
    and so drop-oldest systematically shaves the highs and the lows, which are the only
    columns the bars exist to capture. A bias, not noise, and invisible in the output.

    It is not folded into `ChainStream`: that holds only the *latest* state per contract
    while this needs *every* state, and one structure serving both would make them fight.

    **One writer, one subscription, four tables.** Both channels arrive on the same
    queue, so a second writer would mean a second lossless subscription carrying the same
    messages and two watermarks drifting apart on two clocks. The four aggregators seal
    independently — they have different graces and different grains — but they are driven
    from one drain loop and flushed in one thread hop, so the socket reader waits on one
    disk trip an hour rather than four.

    **Table C does not come off that queue at all.** Our implied volatility and Greeks
    are made by `ChainStream`'s recompute loop, so the writer *samples* that loop's cache
    once a minute through the `chains` callable. A callable rather than the stream itself:
    this module has no business knowing a chain cache exists, and a test hands it a list.

    The other three stores are **derived from the quote store's root** rather than
    defaulted separately. A test that hands this a temporary directory must not have three
    of its four tables quietly write into the repository's own `data/`.

    `clock` is injected so tests drive sealing and flushing without waiting on a real one.
    """

    def __init__(
        self,
        store: BarStore | None = None,
        aggregator: BarAggregator | None = None,
        clock: Callable[[], float] = time.time,
        flush_seconds: float = FLUSH_SECONDS,
        tick_seconds: float = TICK_SECONDS,
        reference_store: BarStore | None = None,
        spot_store: BarStore | None = None,
        computed_store: BarStore | None = None,
        chains: Callable[[], Iterable[Any]] | None = None,
    ) -> None:
        self.store = store or BarStore()
        self.aggregator = aggregator or BarAggregator()
        self.reference_store = reference_store or BarStore(
            self.store.root, dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA
        )
        self.reference = ReferenceAggregator()
        self.spot_store = spot_store or BarStore(
            self.store.root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA
        )
        self.spot = SpotAggregator()
        self.computed_store = computed_store or BarStore(
            self.store.root, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA
        )
        self.computed = ComputedAggregator()
        #: Where table C comes from: a callable handing back the chains the recompute
        #: loop has already computed. A **callable** rather than the `ChainStream`
        #: itself, so this module never learns that a chain cache exists and a test can
        #: hand it a list. `None` means there is nothing to sample and table C stays
        #: empty, which is what the three-table tests and the REST-only app want.
        self.chains = chains
        #: The minute this last sampled in. Sampling is edge-triggered on the boundary
        #: rather than done every pass — see `_sample_computed`.
        self._sampled_minute_us: int | None = None
        self.clock = clock
        self.flush_seconds = flush_seconds
        self.tick_seconds = tick_seconds
        self._subscription = None
        self._last_flush: float | None = None
        #: Bus records that were neither channel, or carried no `ts`. Counted, because
        #: "the writer ignored most of the bus" should be a number and not a discovery.
        self.skipped = 0
        self.flush_errors = 0

    def attach(self, fanout, maxsize: int = QUEUE_WATERMARK, name: str = "bar-writer"):
        """Take a lossless queue on the bus. `run` drains it."""
        self._subscription = fanout.subscribe(name, maxsize=maxsize, lossless=True)
        return self._subscription

    def ingest(self, quote: Any) -> None:
        """One bus record into whichever aggregators it feeds. Pure arithmetic; no IO.

        A book frame feeds the quote bars alone. A ticker frame feeds all three: the
        reference bars and the spot bars own it outright, and the quote bars take its
        `q` array as the **fallback** they use only if the book stays silent for that
        contract-minute.

        The two converters do not overlap — `tick_from_quote` refuses `ticker` and
        `samples_from_ticker` refuses `ob_l2` — so no frame can be counted twice into
        one bar.
        """
        tick = tick_from_quote(quote)
        if tick is not None:
            self.aggregator.add(tick)
            return

        sample = samples_from_ticker(quote)
        if sample is None:
            self.skipped += 1
            return
        if sample.quote is not None:
            self.aggregator.add(sample.quote)
        if sample.reference is not None:
            self.reference.add(sample.reference)
        if sample.spot is not None:
            self.spot.add(sample.spot)

    async def run(self) -> None:
        """Drain, seal, flush, forever. Cancel to stop.

        The loop wakes on a message or on `tick_seconds`, whichever comes first, so a bar
        is sealed promptly on a quiet market and a burst is drained in one pass rather
        than one wakeup per message.

        **Cancellation propagates rather than being swallowed**, so a clean shutdown
        leaves the task *cancelled* rather than *returned* — `main._report_finished_task`
        reads exactly that difference to decide whether a dead background task is worth
        shouting about, and a task that suppressed its own cancellation would log an
        error on every orderly stop until nobody read the log at all.

        **The final flush is deliberately not here.** Flushing from inside a
        `CancelledError` handler means writing while being cancelled, and the partial
        bar — a real observation the ticket requires be kept — would be lost to a second
        cancellation arriving mid-write. The lifespan calls `aclose()` after it has
        gathered the cancelled tasks, where nothing is racing it.
        """
        if self._subscription is None:
            raise RuntimeError("attach() the writer to a FanOut before running it")
        queue = self._subscription.queue
        if self._last_flush is None:
            self._last_flush = self.clock()

        while True:
            try:
                self.ingest(
                    await asyncio.wait_for(queue.get(), timeout=self.tick_seconds)
                )
            except TimeoutError:
                pass
            # Whatever else is already queued, without awaiting. One wakeup per burst.
            while True:
                try:
                    self.ingest(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            now = self.clock()
            self._sample_computed(now)
            self._seal(now)
            await self._maybe_flush()

    def _sample_computed(self, now: float, *, force: bool = False) -> int:
        """Read the chain cache once, as a minute closes. Returns samples taken.

        **This is the one table that is sampled rather than folded.** Tables A, B and D
        are built from ticks arriving on the bus; our implied volatility and Greeks are
        produced by `ChainStream`'s 100 ms recompute loop and exist only in its cache, so
        the writer reads that cache instead of the queue. That also makes table C
        independent of the ticker work: it needs no new subscription and no new frame.

        **Edge-triggered on the minute boundary, and that is a cost decision.** The drain
        loop below spins on every message — measured, 1,322.9 a second — and flattening
        every listed contract on every pass would be the one piece of this writer capable
        of starving the socket reader. Once a minute it is a few hundred dataclasses on a
        task that is already awake.

        **It reads the cache and never asks it to recompute.** `ChainStream.chain()`
        recomputes a dirty expiry synchronously; calling it from here would move that
        work onto the writer's pass and duplicate what the recompute task already does.
        The provider hands back what has already been computed, which is also exactly
        what "sampled at bar close" means: the state the screen was showing.

        **Nothing is stamped here.** The tick carries the instant the *chain* was
        computed, so a cache that has stopped being recomputed yields samples that are
        late by `_Watermarked`'s existing rule and are counted and refused. Stamping them
        with the minute being closed instead would forward-fill a dead feed forever,
        which is the defect this whole store exists to refuse — and it is the sabotage
        `test_a_minute_with_no_computed_chain_gets_no_computed_row` was verified against.
        """
        if self.chains is None:
            return 0

        minute_us = int(now * 1e6) - int(now * 1e6) % BUCKET_US
        if not force:
            if self._sampled_minute_us is None or minute_us == self._sampled_minute_us:
                # The first pass only learns which minute it started in. Sampling here
                # would attribute a chain to a minute still open, and the boundary that
                # follows collects that same chain anyway.
                self._sampled_minute_us = minute_us
                return 0
        self._sampled_minute_us = minute_us

        taken = 0
        for chain in self.chains():
            for tick in computed_ticks_from_chain(chain):
                self.computed.add(tick)
                taken += 1
        return taken

    def _seal(self, now: float | None = None) -> None:
        """Move every eligible bar from the four aggregators into their stores.

        Each seals on its own watermark and its own key, so this is four independent
        decisions taken at **one** wall-clock reading rather than one decision applied
        four times. Reading the clock once matters: four readings could straddle a
        boundary and put one table's bar a minute away from another's — and table C's
        grace is zero, so it is the one most easily moved by a second reading.
        """
        now = self.clock() if now is None else now
        self.store.add(self.aggregator.seal(now))
        self.reference_store.add(self.reference.seal(now))
        self.spot_store.add(self.spot.seal(now))
        self.computed_store.add(self.computed.seal(now))

    async def _maybe_flush(self) -> None:
        """Write if the flush interval has elapsed. **Off the event loop.**

        `asyncio.to_thread` is what keeps a slow disk out of the socket reader's path.
        Without it a multi-second write would stop the loop, the reader with it, and Delta
        would close a connection we simply failed to drain.

        A failed flush must not kill the writer: the buffer is already emptied by then, so
        that flush's rows are lost, but a task that dies takes every later hour with it.
        Counted, because a silent loss is the lie this project keeps refusing.
        """
        now = self.clock()
        if self._last_flush is not None and now - self._last_flush < self.flush_seconds:
            return
        self._last_flush = now
        try:
            await asyncio.to_thread(self._flush_all)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.flush_errors += 1

    def _flush_all(self) -> int:
        """Write all three tables. **Blocking IO, and always on a worker thread.**

        One thread hop for three files rather than three: the hop is what keeps the disk
        off the event loop, and three of them an hour would be three chances for the
        socket reader to be descheduled instead of one.
        """
        return (
            self.store.flush()
            + self.reference_store.flush()
            + self.spot_store.flush()
            + self.computed_store.flush()
        )

    async def aclose(self) -> None:
        """Flush the partial bars from all three tables and write them out. For stop.

        The partial bars this produces carry their **true** tick counts and no flag —
        the counts already say they are short.
        """
        # The open minute's computed state is a real observation too, so the cache is
        # sampled once more before the aggregators are drained. A cache that stopped
        # being recomputed some minutes ago yields a sample that is late and refused, so
        # a stopped feed still contributes nothing on the way out.
        self._sample_computed(self.clock(), force=True)
        self.store.add(self.aggregator.flush())
        self.reference_store.add(self.reference.flush())
        self.spot_store.add(self.spot.flush())
        self.computed_store.add(self.computed.flush())
        await asyncio.to_thread(self._flush_all)

    def stats(self) -> dict[str, Any]:
        """The writer's own view, beside each aggregator's and the bus's.

        The quote table's counters stay at the top level, where they have always been.
        The other two are **nested rather than merged**: three aggregators publish the
        same six key names, and flattening them would either collide silently or need a
        prefix on every one, which is a nested dictionary with worse spelling.
        """
        queued = 0 if self._subscription is None else self._subscription.queue.qsize()
        return {
            "skipped": self.skipped,
            "flush_errors": self.flush_errors,
            "flushes": self.store.flushes,
            "rows_written": self.store.rows_written,
            "buffered": self.store.buffered,
            "queued": queued,
            **self.aggregator.stats(),
            "reference": {
                **self.reference.stats(),
                "flushes": self.reference_store.flushes,
                "rows_written": self.reference_store.rows_written,
                "buffered": self.reference_store.buffered,
            },
            "spot": {
                **self.spot.stats(),
                "flushes": self.spot_store.flushes,
                "rows_written": self.spot_store.rows_written,
                "buffered": self.spot_store.buffered,
            },
            "computed": {
                **self.computed.stats(),
                "flushes": self.computed_store.flushes,
                "rows_written": self.computed_store.rows_written,
                "buffered": self.computed_store.buffered,
            },
        }
