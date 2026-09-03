"""Where the bars go: hive-partitioned Parquet, and the only module that touches a file.

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

from .bars import BarAggregator, QuoteBar, tick_from_quote

#: The dataset root. One directory per table; #5's other three tables get their own.
DATASET = "quote-bars"

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
    "last_lts": pl.Datetime("us", "UTC"),
}

#: The partition columns. They live in the directory names, not in the files, which is
#: the whole point — the filter is answered by the path.
HIVE_SCHEMA: dict[str, Any] = {"date": pl.Date, "underlying": pl.Categorical}


def default_root() -> Path:
    """`<repo>/data`. Git-ignored: market data is never committed."""
    return Path(__file__).resolve().parents[3] / "data"


class BarStore:
    """Sealed bars in, Parquet files out. Buffered; nothing is written until `flush`."""

    def __init__(self, root: Path | str | None = None, dataset: str = DATASET) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.dataset = dataset
        self._buffer: list[QuoteBar] = []
        self.flushes = 0
        self.rows_written = 0

    @property
    def path(self) -> Path:
        return self.root / self.dataset

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def add(self, bars: Iterable[QuoteBar]) -> int:
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

        groups: dict[tuple[str, str], list[QuoteBar]] = {}
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

    @staticmethod
    def _frame(bars: list[QuoteBar]) -> pl.DataFrame:
        """Bars to a typed frame. The partition columns are deliberately absent: they are
        the directory names, and storing them twice invites the two copies to disagree."""
        columns = {name: [getattr(bar, name) for bar in bars] for name in SCHEMA}
        return pl.DataFrame(columns, schema=SCHEMA)

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
            return pl.LazyFrame(schema={**SCHEMA, **HIVE_SCHEMA})
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

    `clock` is injected so tests drive sealing and flushing without waiting on a real one.
    """

    def __init__(
        self,
        store: BarStore | None = None,
        aggregator: BarAggregator | None = None,
        clock: Callable[[], float] = time.time,
        flush_seconds: float = FLUSH_SECONDS,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.store = store or BarStore()
        self.aggregator = aggregator or BarAggregator()
        self.clock = clock
        self.flush_seconds = flush_seconds
        self.tick_seconds = tick_seconds
        self._subscription = None
        self._last_flush: float | None = None
        #: Quotes taken off the bus that were not `ob_l2` frames with a `ts`. Counted,
        #: because "the writer ignored most of the bus" should be a number and not a
        #: discovery.
        self.skipped = 0
        self.flush_errors = 0

    def attach(self, fanout, maxsize: int = QUEUE_WATERMARK, name: str = "bar-writer"):
        """Take a lossless queue on the bus. `run` drains it."""
        self._subscription = fanout.subscribe(name, maxsize=maxsize, lossless=True)
        return self._subscription

    def ingest(self, quote: Any) -> None:
        """One bus record into the aggregator. Pure arithmetic; no IO."""
        tick = tick_from_quote(quote)
        if tick is None:
            self.skipped += 1
            return
        self.aggregator.add(tick)

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

            self.store.add(self.aggregator.seal(self.clock()))
            await self._maybe_flush()

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
            await asyncio.to_thread(self.store.flush)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.flush_errors += 1

    async def aclose(self) -> None:
        """Flush the partial bars and write everything out. For process stop.

        The partial bar this produces carries its **true** tick counts and no flag —
        the counts already say it is short.
        """
        self.store.add(self.aggregator.flush())
        await asyncio.to_thread(self.store.flush)

    def stats(self) -> dict[str, Any]:
        """The writer's own view, beside the aggregator's and the bus's."""
        queued = 0 if self._subscription is None else self._subscription.queue.qsize()
        return {
            "skipped": self.skipped,
            "flush_errors": self.flush_errors,
            "flushes": self.store.flushes,
            "rows_written": self.store.rows_written,
            "buffered": self.store.buffered,
            "queued": queued,
            **self.aggregator.stats(),
        }
