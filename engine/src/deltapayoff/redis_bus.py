"""The same bus, behind Redis Streams. `fanout.py`'s policies, over a wire.

**Nothing above this file changes.** `events/bus.py` named `publish` and `subscribe` so
that a broker could replace what is behind the seam without a producer or a consumer being
opened, and this is that promise cashed: `ChainStream.attach` and `BarWriter.attach` call
the same two methods and read the same `Subscription`, which is why this class hands back
`fanout.Subscription` itself rather than a lookalike. Selection is one environment
variable and **the default stays in-process**, so nothing changes for anyone who does not
ask.

The two policies are the whole of the problem, and Redis does not offer either one:

**Lossless is a consumer group, acked on receipt, replayed from a recorded id.** The store
records the id of the last message it *flushed* and reads forward from there on restart —
not from the pending list, which carries only what was never acked and says nothing about
what reached a Parquet file. That is `docs/design/cloud/redis-hosting.md` §5, and it is
deliberately not the textbook pattern.

**Drop-oldest is a reader outside every group, and it must skip.** A reader that reads `>`
in a group never drops: it accumulates a pending list and falls further behind while
reporting nothing, which is exactly wrong for a screen where a four-second-old quote is
worthless. So this one asks for at most a queue's worth per cycle and, when more than
that is waiting, **jumps to the head of the stream and counts what it jumped over**. Redis
trims silently; ours never has, and `skipped` is that promise kept across a broker that
does not make it.

**The publisher batches, and the batch is where the trim rides.** `publish` appends to
an outbox and returns — it is called between two reads of the venue's socket, so an
`await` in it would put a network round trip inside the receive path and get us
disconnected, the failure `fanout.py` exists to prevent. A flusher task pipelines the
and one `XTRIM MINID ~` per stream touched, thirty minutes back. `measured` (#69): 623 µs
an unpipelined `XADD` is 82% of a core at our rate; pipelined at 100 ms it is 31.8 µs an
entry, and the trim in the same pipeline costs nothing measurable.

**An outbox is bounded or it is a memory leak with good manners.** If Redis stops
answering, the flusher retries and the outbox fills; past `max_outbox` the oldest entries
go and are counted, for the same reason the fan-out counts a drop. A publisher that cannot
reach Redis **at startup** does not get that far: `start()` raises `BusUnavailable`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import log_events
from .events.redis_wire import decode, encode, stream_name, stream_names, stream_type
from .fanout import Subscription
from .logging_setup import log_event

logger = logging.getLogger(__name__)

#: Which bus the process runs on. `fanout` is the default and stays the default: this
#: value is the only thing that switches the engine onto Redis.
BUS_ENV = "DELTA_BUS"
REDIS_BUS = "redis"
FANOUT_BUS = "fanout"

REDIS_URL_ENV = "DELTA_REDIS_URL"
BATCH_MS_ENV = "DELTA_BUS_BATCH_MS"
RETENTION_ENV = "DELTA_BUS_RETENTION_SECONDS"
INSTANCE_ENV = "DELTA_BUS_INSTANCE"

#: **Chosen by measurement in #61**, not by taste — `tools/measure_bus_live.py` against
#: the live feed at 10, 50 and 100 ms. The interval is added to every consumer's latency,
#: and buys back publisher CPU; the run and the reasoning are in
#: `docs/design/lld/redis-bus.md`.
DEFAULT_BATCH_MS = 50

#: Thirty minutes, by age, never by count. A count is a guess about rate; thirty minutes
#: is the promise made to a restarting `store`. `docs/design/cloud/redis-hosting.md` §4.
DEFAULT_RETENTION_SECONDS = 30 * 60.0

#: What a start-up dial is allowed to take before it is a failure. #57's user story 22:
#: a feed that starts with no Redis fails loudly rather than buffering forever.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0

#: How long a reader blocks in one `XREAD`, or **0 to poll instead of blocking**.
#:
#: Blocking is right on a native host: the read returns the moment an entry lands and an
#: idle reader costs nothing. It is not free everywhere. `measured` 2026-09-09 against
#: Docker Desktop on Windows, whose loopback goes through a WSL2 port forward: a blocked
#: reader wakes about **24 ms** after the entry it was waiting for, against **1.1 ms** for
#: a poll that finds one — and at 1,850 events a second that 24 ms is the difference
#: between a reader that keeps up and one that falls behind for good. So the knob exists,
#: 0 means "one `XREAD` with no `BLOCK`, then `idle_sleep_seconds`", and the default stays
#: blocking because prod is Linux beside the Redis and this is a laptop's problem.
DEFAULT_READ_BLOCK_MS = 500

#: The most entries one lossless read takes at once. At `derived` 1,849.8 events/s this is
#: about a quarter of a second of traffic, which is a batch a consumer can absorb without
#: the queue depth jumping by more than the watermark it is measured against.
DEFAULT_READ_COUNT = 500

#: How many **consecutive** failures a reader survives before it gives up and says so.
#:
#: **The symmetry with the publisher is deliberate and it is not total** (#103). The
#: publisher retries forever because its alternative is worse: the batch it is holding
#: exists nowhere else, its outbox is bounded and counted, and a publisher that stopped
#: would lose the venue's own data. A reader holds nothing — everything it has not read
#: is still in the stream — so retrying forever buys it nothing, and on a subscription
#: that is genuinely broken rather than briefly unreachable it spins silently while the
#: retention window eats the backlog. That is the failure this ticket is about, reached
#: by the other road.
#:
#: So the retry is bounded and **giving up is loud**: an `alert` on the bus, an error
#: record, the subscription marked not alive, and every stream it holds reported behind
#: so the store's seal clock stops advancing over data nobody read. Bounded retry would
#: be strictly worse than retrying forever without those; with them it is strictly
#: better, because a reader that cannot recover is a fact an operator should be told
#: rather than one a counter should hide.
#:
#: Five, `assumed`. `derived` about 15.5 s of disturbance ridden out at the backoff
#: below, which covers a Redis restart (`measured` #61: a `redis:7-alpine` container is
#: answering again inside 2 s) and the 2.0 s socket timeout that started this several
#: times over, while staying far short of the 30-minute retention window.
DEFAULT_READ_RETRIES = 5

#: The first wait after a failed read, doubling to the ceiling below. No jitter, for the
#: same reason `controller-policies.md` C5 gives: one reader per process per stream, so
#: there is no herd to disperse.
DEFAULT_READ_RETRY_SECONDS = 0.5

#: The longest a reader waits between retries. `derived` 0.5+1+2+4+8 = 15.5 s over the
#: five attempts above.
DEFAULT_READ_RETRY_CEILING_SECONDS = 8.0

#: The floor under "this reader has not come round recently", in seconds. The real bound
#: is derived from the socket timeout — see `BusConfig.stale_after_seconds`.
MIN_READER_STALE_SECONDS = 5.0

#: The outbox ceiling, in entries. `derived` about 108 seconds at 1,849.8 events/s — long
#: enough to ride out a Redis restart, short enough that it is a bounded number of bytes
#: rather than "until the process dies".
DEFAULT_MAX_OUTBOX = 200_000


class BusUnavailable(RuntimeError):
    """Configured for Redis, and Redis did not answer. Fatal at start-up, by design."""


@dataclass(frozen=True, slots=True)
class Position:
    """A stream id together with the ordinal at which the id was observed."""

    id: str
    index: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"position index must not be negative: {self.index}")


@dataclass(frozen=True, slots=True)
class Span:
    """A recorded pause boundary, exclusive at the start and inclusive at the end."""

    frm: Mapping[str, str]
    to: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class StreamLag:
    """One consumer group's position in one stream, as Redis reports it.

    **Everything here comes out of `XINFO`, and the two questions it answers are not the
    same question** (#103). *How far behind* is a number with a threshold on it, because
    a store half a second behind on a busy tick is working. *Whether the group has been
    trimmed past* is exact and needs no threshold at all: when the group's
    `last-delivered-id` is older than the oldest entry the stream still holds, entries
    existed, were never delivered, and are gone.
    """

    stream: str
    #: Redis's own `lag`: entries not yet delivered to the group. `None` when Redis
    #: cannot compute it, which it says rather than guessing.
    lag: int | None
    entries_read: int | None
    entries_added: int | None
    length: int | None
    last_delivered_id: str | None
    first_retained_id: str | None

    @property
    def trimmed_past(self) -> bool:
        """The exact test for a replay gap, and it is one comparison."""
        if self.last_delivered_id is None or self.first_retained_id is None:
            return False
        return _id_before(self.last_delivered_id, self.first_retained_id)

    @property
    def lost(self) -> int | None:
        """Entries trimmed away that this group had not read. `None` if unknowable.

        The same arithmetic `replay_gaps` uses at start-up -- `entries-added` minus
        `length` is what was trimmed -- with the group's own `entries-read` in place of
        the saved watermark's index. Redis keeps `entries-read` on the same logical scale
        as `entries-added`, including for a group created at `$`, so the subtraction is
        exact rather than an estimate.
        """
        if self.entries_added is None or self.length is None:
            return None
        if self.entries_read is None:
            return None
        return max(0, (self.entries_added - self.length) - self.entries_read)


@dataclass(frozen=True, slots=True)
class ReplayGap:
    """Facts about entries trimmed before a saved stream position."""

    stream: str
    saved_id: str
    first_retained_id: str | None
    lost: int | None
    trimmed: int


@dataclass(frozen=True)
class BusConfig:
    """Everything about the bus that is a deployment decision rather than a code one."""

    url: str = "redis://127.0.0.1:6379"
    #: The venue this process publishes for. Answers `{VENUE}` for the two events that
    #: name neither an instrument nor an adapter — see `events/redis_wire.py`.
    venue: str = "DELTA"
    #: The underlyings whose streams exist. **The same configured list the feed is
    #: given**, because a reader builds its key list from configuration, never from the
    #: keyspace.
    underlyings: tuple[str, ...] = ("BTC", "ETH")
    batch_ms: int = DEFAULT_BATCH_MS
    retention_seconds: float = DEFAULT_RETENTION_SECONDS
    #: The `{instance}` half of a consumer name, `{service}-{instance}`. The container's
    #: short id in prod, `1` on a laptop.
    instance: str = "1"
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_block_ms: int = DEFAULT_READ_BLOCK_MS
    read_count: int = DEFAULT_READ_COUNT
    max_outbox: int = DEFAULT_MAX_OUTBOX
    read_retries: int = DEFAULT_READ_RETRIES
    read_retry_seconds: float = DEFAULT_READ_RETRY_SECONDS
    read_retry_ceiling_seconds: float = DEFAULT_READ_RETRY_CEILING_SECONDS
    #: How long a reader may go without completing a pass before it is treated as having
    #: stopped. `None` derives it from the socket timeout, which is the longest a healthy
    #: pass can take.
    reader_stale_seconds: float | None = None
    #: What a reader waits after a read that returned nothing. **Not a poll interval and
    #: not optional**: a blocking `XREAD` already waits, so this only fires when the block
    #: expired empty — but a client that does not honour `BLOCK` (the in-memory fake is
    #: one) would turn the loop into a spin that never yields, starving the event loop the
    #: venue's socket runs on. That is the failure `fanout.py` exists to prevent, reached
    #: from the consumer's end instead of the producer's.
    idle_sleep_seconds: float = 0.005

    @classmethod
    def from_env(cls, underlyings: Iterable[str] | None = None) -> BusConfig:
        """Read at start-up rather than at import, so the bus is a deployment decision."""
        return cls(
            url=os.environ.get(REDIS_URL_ENV, cls.url),
            underlyings=tuple(underlyings) if underlyings else cls.underlyings,
            batch_ms=int(os.environ.get(BATCH_MS_ENV, cls.batch_ms)),
            retention_seconds=float(
                os.environ.get(RETENTION_ENV, cls.retention_seconds)
            ),
            instance=os.environ.get(INSTANCE_ENV, cls.instance),
        )

    def client_kwargs(self) -> dict[str, Any]:
        """How the client is built. **Both timeouts bounded**: an unbounded dial is not a
        failure, it is a process that never reports one."""
        return {
            "socket_connect_timeout": self.connect_timeout_seconds,
            "socket_timeout": max(
                self.connect_timeout_seconds, self.read_block_ms / 1000 * 2 + 1
            ),
            # The wire is bytes. Decoding here would turn `payload` into a `str` and then
            # back into bytes for `json.loads`, and would decode ids nobody reads as text.
            "decode_responses": False,
        }

    def stale_after_seconds(self) -> float:
        """How long a silent reader is given before it is presumed stopped.

        **Derived from the socket timeout, not chosen beside it.** A healthy pass cannot
        outlast the timeout on the socket it reads through — `measured` in the incident,
        that was 2.0 s — so twice it is a bound no working reader reaches, and the floor
        keeps it sane on a configuration with a very short timeout.

        The cost of the two mistakes is not symmetric, which is why the bound is tight
        rather than generous. A reader wrongly called stopped seals a minute late and the
        next pass corrects it. A reader wrongly called caught up seals a minute **empty**,
        and a sealed minute is closed: nothing corrects it, and 97 of them are gone.
        """
        if self.reader_stale_seconds is not None:
            return self.reader_stale_seconds
        socket_timeout = float(self.client_kwargs()["socket_timeout"])
        return max(MIN_READER_STALE_SECONDS, socket_timeout * 2)

    def streams(self) -> tuple[str, ...]:
        return stream_names(
            venues=(self.venue,), underlyings=self.underlyings
        )


def selected_bus() -> str:
    """`redis` or `fanout`. **Anything unrecognised is the default**, which is
    in-process — a typo must not silently pick a broker."""
    return REDIS_BUS if os.environ.get(BUS_ENV, "").lower() == REDIS_BUS else FANOUT_BUS


def trim_floor_ms(now: float, retention_seconds: float) -> int:
    """The `MINID` a batch trims to: the id of an entry written exactly `retention`
    seconds ago. Stream ids are milliseconds since the epoch, which is what makes an age
    expressible as an id at all."""
    return max(0, int((now - retention_seconds) * 1000))


class RedisSubscription(Subscription):
    """A `fanout.Subscription` with the facts only a broker can produce.

    Subclassed rather than reimplemented so `offer` — the drop-oldest eviction and the
    lossless watermark, both already pinned by `tests/test_fanout.py` — is the same code
    on both sides of the seam. A consumer reads `.queue` and cannot tell which it has.
    """

    __slots__ = (
        "streams",
        "group",
        "consumer",
        "start_ids",
        "group_start",
        "skip",
        "last_ids",
        "positions",
        "behind",
        "span_dropped",
        "skipped",
        "resyncs",
        "undecodable",
        "positioned",
        "alive",
        "supervised",
        "retries",
        "retries_total",
        "gave_up",
        "failure",
        "last_pass",
        "stale_after",
        "monotonic",
        "_delivered",
        "_baseline",
        "_group_missing",
        "_replaying",
    )

    def __init__(
        self,
        name: str,
        maxsize: int,
        lossless: bool,
        *,
        streams: tuple[str, ...],
        consumer: str,
        start_ids: Mapping[str, Position] | None,
        group_start: str,
        skip: Sequence[Span],
        stale_after: float = MIN_READER_STALE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name, maxsize, lossless=lossless)
        self.streams = streams
        #: The group name is the **service name**, one word, no environment and no venue
        #: in it: the key already carries both. Nomenclature §5.
        self.group = name
        self.consumer = consumer
        self.start_ids = dict(start_ids or {})
        self.group_start = group_start
        self.skip = tuple(skip)
        #: The id of the last entry taken off each stream. What a consumer records at its
        #: flush and hands back as `start_id` after a restart.
        self.last_ids: dict[str, str] = {}
        self.positions: dict[str, Position] = {
            key: self.start_ids.get(key, Position("0-0", 0)) for key in streams
        }
        self.behind: dict[str, bool] = dict.fromkeys(streams, False)
        self.span_dropped = 0
        #: Entries this reader never received, counted at the jump. **Redis trims
        #: silently and ours never has.** Always zero on a lossless subscription.
        self.skipped = 0
        #: How many times this reader was far enough behind to jump to the head.
        self.resyncs = 0
        #: Entries that would not decode. Logged at error, never fatal to the reader — one
        #: unreadable entry must not stop a consumer for the rest of the day.
        self.undecodable = 0
        #: Set once this reader has taken its starting position — the head of each
        #: stream, or its group. **Nothing published before that point reaches a
        #: drop-oldest reader**, because `$` means "from now" and its "now" is here.
        #: `RedisBus.ready()` is what a caller waits on rather than guessing.
        self.positioned = asyncio.Event()
        #: Whether this subscription's reader task is running. **False until it starts**,
        #: which is the honest answer: a reader that has not started has read nothing.
        self.alive = False
        #: Whether a done-callback is watching that task. #103: nothing was.
        self.supervised = False
        #: Consecutive failed reads right now, and the lifetime count beside it.
        #: **Consecutive**, the same sense `controller-policies.md` C6 gives the
        #: reconnect budget: one successful read restores it in full.
        self.retries = 0
        self.retries_total = 0
        #: Set when the retry bound was reached. The reader is not coming back.
        self.gave_up = False
        #: What the last failure was, as text, so a reader's own state carries its cause.
        self.failure: str | None = None
        #: A monotonic reading taken at the top of every read pass. The clock is
        #: monotonic because this is an elapsed duration and never a time of day.
        self.monotonic = monotonic
        self.last_pass = monotonic()
        self.stale_after = stale_after
        self._delivered: dict[str, int] = dict.fromkeys(streams, 0)
        self._baseline: dict[str, int] = dict.fromkeys(streams, 0)
        self._group_missing: set[str] = set()
        self._replaying: set[str] = set()

    def reader_lost(self) -> bool:
        """Whether this subscription's reader has stopped keeping its answers fresh.

        Two ways, and the second is not the first: a task that **died**, and a loop that
        is still a task but has not come round. The incident was the first; the socket
        timeout makes the second unlikely rather than impossible, and both produce the
        same lie if only the first is checked.
        """
        if not self.alive:
            return True
        return (self.monotonic() - self.last_pass) > self.stale_after

    def behind_streams(self) -> tuple[str, ...]:
        """Configured streams whose reader still trails its log.

        **Derived, never a cached flag** (#103). `behind` used to be a dict the read loop
        wrote and nothing else touched, initialised `False`. When the loop died it froze
        at its last value — caught up — and `store.py`'s seal clock, which is literally
        `min(wall clock, the times of the behind streams)`, went on advancing on wall
        clock over two hours of entries nobody had read. Those minutes sealed empty, and
        a sealed minute is closed for good.

        So the question is asked of the reader's own liveness first. **A reader that is
        not running cannot know it is caught up**, and every stream it holds a position
        on is behind, at the position where it stopped.

        A stream whose position is still `0-0` is left out, and that is not a loophole.
        `0-0` is a stream nobody has written to; its time is the Unix epoch, and handing
        the seal clock a `min` of zero would stop this store sealing anything ever again
        — a worse failure than the one being repaired.
        """
        if self.reader_lost():
            return tuple(
                sorted(
                    key
                    for key in self.streams
                    if (self.positions.get(key) or Position("0-0", 0)).id != "0-0"
                )
            )
        return tuple(sorted(key for key, behind in self.behind.items() if behind))


class RedisBus:
    """Redis Streams behind `events.Bus`. Built, then started, then published to."""

    def __init__(
        self,
        config: BusConfig | None = None,
        *,
        client_factory: Any = None,
        clock: Any = time.time,
    ) -> None:
        self.config = config or BusConfig()
        self._client_factory = client_factory or _default_client
        #: `time.time`, and it is the right clock here rather than `perf_counter`: the
        #: trim floor is compared against Redis's own wall-clock entry ids.
        self._clock = clock
        self._client: Any = None
        self._subscriptions: dict[str, RedisSubscription] = {}
        self._readers: dict[str, asyncio.Task] = {}
        #: Every background task that exited when it should not have, and why. Written
        #: by the done-callbacks below and read by whatever serves `/health`.
        self._reader_exits: dict[str, str] = {}
        self._prepared_groups: dict[str, set[str]] = {}
        self._outbox: list[tuple[str, dict[str, bytes]]] = []
        self._flusher: asyncio.Task | None = None
        self._flushing = asyncio.Lock()

        self.published = 0
        self._written = 0
        self._batches = 0
        self._trims = 0
        self._failures = 0
        self._unroutable = 0
        self._outbox_dropped = 0
        self._flush_seconds = 0.0
        self._flush_peak_seconds = 0.0

    # --- the producer's side ------------------------------------------------------

    def publish(self, record: Any) -> None:
        """Put `record` on its stream's outbox. **Synchronous, and never blocks.**

        The whole contract, unchanged from the fan-out's: the handler calls this between
        reads of the socket, so anything that could suspend here suspends the socket.

        **Nothing raises out of here.** An event that cannot be keyed or encoded is logged
        and counted; raising would stop the handler reading, fill the receive buffer and
        get the connection closed — which is a much larger failure than one lost event.
        """
        self.published += 1
        try:
            key = stream_name(record, venue=self.config.venue)
            fields = encode(record)
        except Exception:
            self._unroutable += 1
            log_event(
                logger,
                logging.ERROR,
                log_events.ENGINE_ERROR,
                "an event could not be put on a stream and was dropped: %r",
                getattr(record, "type", type(record).__name__),
                exc_info=True,
            )
            return

        self._outbox.append((key, fields))
        if len(self._outbox) > self.config.max_outbox:
            # The fan-out's rule, one layer up: bounded, oldest first, and counted.
            overflow = len(self._outbox) - self.config.max_outbox
            del self._outbox[:overflow]
            self._outbox_dropped += overflow
            log_event(
                logger,
                logging.ERROR,
                log_events.QUEUE_DROP,
                "the Redis outbox is at its ceiling of %d; %d of the oldest entries "
                "were dropped",
                self.config.max_outbox,
                overflow,
            )

    async def flush(self) -> int:
        """Write everything queued, and trim every stream this batch touched.

        One pipeline: the batch's `XADD`s and one `XTRIM MINID ~` per stream. The trim
        rides with the write because `measured` (#69) it is free at our shape — 31.8 µs an
        entry with it and 31.8 µs without — and because a separate trim timer would be one
        more thing that can stop.

        A failed batch is **put back**, not discarded: Redis being briefly unreachable is
        a retry, and losing a batch to it silently would be the drop this bus refuses.
        """
        async with self._flushing:
            if self._client is None or not self._outbox:
                return 0
            batch = self._outbox
            self._outbox = []
            touched = sorted({key for key, _ in batch})
            floor = trim_floor_ms(self._clock(), self.config.retention_seconds)

            pipe = self._client.pipeline(transaction=False)
            for key, fields in batch:
                pipe.xadd(key, fields)
            for key in touched:
                pipe.xtrim(key, minid=floor, approximate=True)

            started = time.perf_counter()
            try:
                await pipe.execute()
            except Exception:
                self._failures += 1
                self._outbox[:0] = batch
                log_event(
                    logger,
                    logging.ERROR,
                    log_events.ENGINE_ERROR,
                    "a batch of %d entries could not be written to Redis; it stays in "
                    "the outbox, which now holds %d",
                    len(batch),
                    len(self._outbox),
                    exc_info=True,
                )
                return 0
            elapsed = time.perf_counter() - started

            self._written += len(batch)
            self._batches += 1
            self._trims += len(touched)
            self._flush_seconds += elapsed
            self._flush_peak_seconds = max(self._flush_peak_seconds, elapsed)
            return len(batch)

    def publisher(self) -> dict[str, float]:
        """What the publisher has written, and what it cost. Read by the measurement tool
        and by anything that wants to know whether the outbox is draining."""
        return {
            "published": self.published,
            "written": self._written,
            "batches": self._batches,
            "trims": self._trims,
            "failures": self._failures,
            "unroutable": self._unroutable,
            "outbox": len(self._outbox),
            "outbox_dropped": self._outbox_dropped,
            "flush_seconds": self._flush_seconds,
            "flush_peak_seconds": self._flush_peak_seconds,
        }

    # --- the consumers' side ------------------------------------------------------

    def subscribe(
        self,
        name: str,
        maxsize: int,
        lossless: bool = False,
        *,
        start_ids: Mapping[str, Position] | None = None,
        group_start: str = "$",
        skip: Sequence[Span] = (),
        event_types: Iterable[str] | None = None,
    ) -> RedisSubscription:
        """Register a consumer, exactly as the fan-out does, plus one keyword.

        `start_id` is the id a lossless consumer last **flushed**. Given one, the reader
        replays from it before joining its group's live tail — the five-minute loss window
        #57 names, closed. The fan-out has no answer to it and does not need one: nothing
        outlives the process there.

        `maxsize` keeps both meanings: a ceiling under drop-oldest, a watermark under
        lossless.

        **`group_start` defaults to `"$"`, and `"0"` has to be typed (#97).** It picks the
        id a consumer group is *created* at, so it is read once per group and only under
        `lossless=True`. The two values are not symmetric in what they cost when they are
        the wrong one. `"$"` creates the group at the head: a caller that wanted history
        gets none, and finds out immediately, because it asked for something and received
        nothing. `"0"` creates the group at the bottom and hands the reader everything
        Redis still holds — a whole retention window — as if it had just arrived; for the
        one lossless reader that writes anything down that is thirty minutes refolded into
        bars that already exist. That is #84 (five entries folded twice at a restart seam)
        at the scale of the window, and #94 is the same value sitting in a document rather
        than running. One default fails loudly and reversibly, the other silently and
        destructively, so the default is the loud one. `"0"` is not removed: still
        validated below, still reachable, now deliberate.

        **Required-instead-of-defaulted was weighed and rejected (#97).** Two reasons,
        neither about what is easier to type. First, `group_start` is meaningless to a
        drop-oldest subscriber, which creates no consumer group at all: `main.py`'s two
        feed-state readers, `stream.py`'s ladder reader and `store_main.py`'s control
        reader correctly state no opinion, and a required argument would make every caller
        answer a question only lossless callers are asking. Second, `subscribe` is one
        half of the `events/bus.py` seam, whose signature is `(name, maxsize,
        lossless=False)` and whose other implementation, `FanOut`, has no such parameter;
        a required keyword on one of two implementations of a shared seam is not a
        stronger contract, it is a broken one. And explicitness at the call site is not
        the protection it looks like — in #86 all three service callers *had* typed
        `group_start="$"` and the consumer still posted its own downtime, because `$`
        positions a group only when that group is created. See `_ensure_group`.
        """
        if maxsize < 1:
            raise ValueError(f"maxsize must be at least 1; got {maxsize}")
        if name in self._subscriptions:
            raise ValueError(f"a subscriber named {name!r} already exists")
        if group_start not in {"0", "$"}:
            raise ValueError(f"group_start for {name!r} must be '0' or '$'")
        if not lossless and (start_ids is not None or skip):
            raise ValueError(f"{name!r} is not lossless; replay options are invalid")
        streams = self.config.streams()
        if event_types is not None:
            selected_types = tuple(event_types)
            if not selected_types:
                raise ValueError("event_types must not be empty")
            configured_types = {stream_type(stream) for stream in streams}
            missing = set(selected_types) - configured_types
            if missing:
                raise ValueError(
                    f"event type(s) not configured: {', '.join(sorted(missing))}"
                )
            streams = tuple(
                stream for stream in streams if stream_type(stream) in selected_types
            )
        selected_start_ids = {
            key: position for key, position in (start_ids or {}).items() if key in streams
        }
        ignored = sorted(set(start_ids or {}) - set(selected_start_ids))
        if ignored:
            log_event(
                logger,
                logging.WARNING,
                log_events.BUS_SELECTED,
                "%r ignored saved positions for unconfigured streams: %s",
                name,
                ", ".join(ignored),
            )
        subscription = RedisSubscription(
            name,
            maxsize,
            lossless,
            streams=streams,
            consumer=f"{name}-{self.config.instance}",
            start_ids=selected_start_ids,
            group_start=group_start,
            skip=skip,
            stale_after=self.config.stale_after_seconds(),
        )
        self._subscriptions[name] = subscription
        if self._client is not None:
            self._readers[name] = self._start_reader(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a consumer. Ordinary, not a failure.

        **The consumer group is left in place.** It is what a restarted reader rejoins,
        and destroying it here would turn a browser tab closing into a lost replay.
        """
        self._subscriptions.pop(subscription.name, None)
        reader = self._readers.pop(subscription.name, None)
        if reader is not None:
            reader.cancel()

    def stats(self) -> dict[str, dict[str, int | bool]]:
        """What each consumer received and what it could not keep up with.

        The fan-out's six, plus the three only a broker produces. `skipped` is the one to
        watch on a screen's subscription and it should be zero on a store's.
        """
        return {
            name: {
                "offered": s.offered,
                "dropped": s.dropped,
                "queued": s.queue.qsize(),
                "lossless": s.lossless,
                "over_capacity": s.over_capacity,
                "backlog_peak": s.backlog_peak,
                "skipped": s.skipped,
                "resyncs": s.resyncs,
                "undecodable": s.undecodable,
            }
            for name, s in self._subscriptions.items()
        }

    # --- lifetime -----------------------------------------------------------------

    @property
    def client(self) -> Any:
        """The connection, or `None` before `start()`. For tests and the measure tool."""
        return self._client

    async def start(self, *, start_readers: bool = True) -> None:
        """Dial, prove the connection, then start the flusher and every reader.

        **A publisher that cannot reach Redis fails here**, loudly and within the connect
        timeout, rather than buffering into an outbox nobody is draining. That is #57's
        user story 22 and it is the reason this method exists at all rather than the bus
        connecting lazily on its first write.
        """
        if self._client is not None:
            return
        client = self._client_factory(self.config)
        try:
            await asyncio.wait_for(
                client.ping(), timeout=self.config.connect_timeout_seconds
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.aclose()
            timeout = self.config.connect_timeout_seconds
            detail = exc if str(exc) else f"no answer in {timeout}s"
            raise BusUnavailable(
                f"the event bus is configured for Redis at {self.config.url} and it did "
                f"not answer: {detail}. Start it, or unset "
                f"{BUS_ENV} to run on the in-process fan-out."
            ) from exc

        self._client = client
        self._flusher = asyncio.create_task(self._flush_forever(), name="bus-flush")
        self._flusher.add_done_callback(self._flusher_exited)
        if start_readers:
            await self.start_readers()

    async def start_readers(self) -> None:
        """Start subscriptions after a caller has prepared their replay cursors.

        The store needs one small window between group creation and reader start: it asks
        Redis how much of a saved position was trimmed, then adjusts the replay base index
        before any entry can be delivered. Ordinary callers keep the original `start()`
        behaviour, which calls this method immediately.
        """
        if self._client is None:
            raise RuntimeError("start the Redis bus before starting its readers")
        for name, subscription in self._subscriptions.items():
            if name not in self._readers:
                self._readers[name] = self._start_reader(subscription)
        await self.ready()

    def _start_reader(self, subscription: RedisSubscription) -> asyncio.Task:
        """Create a reader task **and watch it**. The two are one operation (#103).

        Both create sites were bare `asyncio.create_task` calls with nothing attached.
        `self._readers` holds a strong reference, so a task that raised was never
        garbage collected and Python never printed even its own "Task exception was
        never retrieved" warning -- the one free safety net the language offers was held
        shut by the dictionary that was meant to own the task. **A task that can die
        unobserved is the root of all five of this incident's symptoms.**
        """
        task = asyncio.create_task(
            self._read(subscription), name=f"bus-read-{subscription.name}"
        )
        task.add_done_callback(
            lambda done: self._reader_exited(subscription.name, done)
        )
        subscription.supervised = True
        return task

    def _reader_exited(self, name: str, task: asyncio.Task) -> None:
        """Record and escalate a reader that stopped. **Never raises.**

        A cancellation is not a death: `aclose` and `unsubscribe` both cancel, and a
        supervisor that cried wolf at every shutdown is a supervisor someone turns off.

        **It does not restart the reader, and that is a decision rather than an
        omission.** Re-entering `_read` on a lossless subscription re-runs `_replay`,
        which reads forward from `start_ids` -- the position saved in the *checkpoint*,
        not the position this reader has since reached. Everything between the two has
        already been delivered and folded, so an automatic restart would re-fold it: #84
        at the scale of the whole run, which is the bug `_replay`'s bound exists to
        prevent. Rebasing `start_ids` onto the live positions first would make a restart
        safe, and that is its own change with its own test.
        """
        subscription = self._subscriptions.get(name)
        if subscription is not None:
            subscription.alive = False
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:  # pragma: no cover - only during interpreter shutdown
            return
        detail = (
            f"{type(error).__name__}: {error}"
            if error is not None
            else "the reader loop returned, which it must never do"
        )
        self._reader_exits[name] = detail
        log_event(
            logger,
            logging.ERROR,
            log_events.BUS_READER,
            "the bus reader for %r exited and is no longer consuming: %s",
            name,
            detail,
        )
        if subscription is None or not subscription.gave_up:
            # `_reader_failed` has already raised the alert when it gave up; this is the
            # exit nothing else accounted for.
            self._alert(
                "bus.reader_stopped",
                f"the bus reader for {name!r} exited unexpectedly and is no longer "
                f"consuming: {detail}",
            )

    def _flusher_exited(self, task: asyncio.Task) -> None:
        """The publisher's loop is the other unsupervised task in this file."""
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:  # pragma: no cover - only during interpreter shutdown
            return
        detail = (
            f"{type(error).__name__}: {error}"
            if error is not None
            else "the flusher loop returned, which it must never do"
        )
        self._reader_exits["bus-flush"] = detail
        log_event(
            logger,
            logging.ERROR,
            log_events.BUS_READER,
            "the bus flusher exited and is no longer writing: %s",
            detail,
        )

    def _alert(self, code: str, detail: str, *, severity: str = "error") -> None:
        """Say it on the bus as well as in the log.

        `publish` is synchronous and never raises, which is what makes this callable
        from a done-callback. If Redis is the thing that is broken the alert waits in
        the outbox until it is not, so the log record is the immediate signal and this
        is the one that reaches Discord.
        """
        from .events import Alert

        self.publish(
            Alert(
                source="bus",
                ts_received=datetime.fromtimestamp(self._clock(), tz=timezone.utc),
                severity=severity,
                code=code,
                detail=detail,
            )
        )

    def readers(self) -> dict[str, dict[str, Any]]:
        """Per subscription: is its reader running, what has it survived, has it given up.

        **What `/health` had no way to ask** (#103). The store answered `200 OK` with a
        hardcoded `"status": "ok"` for two hours after its reader had died.
        """
        return {
            name: {
                "alive": s.alive,
                "supervised": s.supervised,
                "lossless": s.lossless,
                "retries": s.retries,
                "retries_total": s.retries_total,
                "gave_up": s.gave_up,
                "failure": s.failure,
                "behind": list(s.behind_streams()),
            }
            for name, s in self._subscriptions.items()
        }

    def reader_exits(self) -> dict[str, str]:
        """Background tasks that exited when they should not have, and why."""
        return dict(self._reader_exits)

    async def ensure_groups(self, subscription: RedisSubscription) -> None:
        """Create or join a lossless subscription's groups without starting its reader."""
        if self._client is None:
            raise RuntimeError("start the Redis bus before ensuring consumer groups")
        if not subscription.lossless:
            raise ValueError(f"{subscription.name!r} is not lossless")
        self._prepared_groups[subscription.name] = await self._ensure_group(
            subscription
        )

    async def ready(self) -> None:
        """Wait until every subscriber's reader has taken its starting position.

        Awaited by `start()`, and available to anything that subscribes afterwards. It is
        not a nicety: a drop-oldest reader positions at the head of the stream, so a
        publisher that ran before it got there is a publisher whose events that reader
        never had, and nothing would say so.
        """
        pending = [s.positioned.wait() for s in self._subscriptions.values()]
        if pending:
            await asyncio.gather(*pending)

    async def aclose(self) -> None:
        """Stop the readers, write what is left, and close the connection."""
        for reader in self._readers.values():
            reader.cancel()
        if self._readers:
            await asyncio.gather(*self._readers.values(), return_exceptions=True)
        self._readers.clear()
        if self._flusher is not None:
            self._flusher.cancel()
            await asyncio.gather(self._flusher, return_exceptions=True)
            self._flusher = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self.flush()
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    async def _flush_forever(self) -> None:
        """One batch every `batch_ms`. The interval is the latency this bus adds.

        **A period, not a pause.** The wait is the interval *minus what the last flush
        took*, so 100 ms means a batch every 100 ms rather than every 100 ms plus however
        long Redis and this event loop needed. When a flush takes longer than the interval
        the next one starts at once, and the interval has become a floor the process
        cannot meet — which is a fact `tools/measure_bus_live.py` reports rather than one
        this loop hides by sleeping anyway.

        The interval is read once a tick rather than once a task, so a running bus can be
        re-pointed at another one — which is how that tool compares three of them against
        one live feed rather than against three feeds.
        """
        last = time.perf_counter()
        while True:
            wait = self.config.batch_ms / 1000 - (time.perf_counter() - last)
            await asyncio.sleep(max(0.0, wait))
            last = time.perf_counter()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - `flush` handles its own failures
                log_event(
                    logger,
                    logging.ERROR,
                    log_events.ENGINE_ERROR,
                    "the bus flusher raised; it keeps running",
                    exc_info=True,
                )

    # --- the two readers ----------------------------------------------------------

    async def _read(self, sub: RedisSubscription) -> None:
        """One task per subscription, and the policy decides which loop it runs.

        **Positioning happens first and is announced.** A drop-oldest reader starts at
        the head, so anything written before it got there is not its business — and
        `start()` waits for every reader to be in place before it returns, so "the bus is
        started" means "nothing published from now on is missed by a subscriber that
        already existed".
        """
        sub.alive = True
        sub.last_pass = sub.monotonic()
        try:
            try:
                if sub.lossless:
                    existing = self._prepared_groups.pop(sub.name, None)
                    if existing is None:
                        existing = await self._ensure_group(sub)
                else:
                    existing = await self._position_at_head(sub)
            finally:
                # Set even on a failure: a reader that could not position is a logged
                # error, and `ready()` must not hang the process waiting for one.
                sub.positioned.set()
            if sub.lossless:
                await self._read_group(sub, existing or set())
            else:
                await self._read_head(sub)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                logger,
                logging.ERROR,
                log_events.BUS_READER,
                "the bus reader for %r stopped",
                sub.name,
                exc_info=True,
            )
            raise
        finally:
            # **Before the done-callback, not instead of it.** Positioning and `_replay`
            # run outside the retry driver below -- they are not idempotent, so a failure
            # in either ends the reader -- and this is what stops `behind_streams` saying
            # "caught up" in the window before the callback has run.
            sub.alive = False

    async def _run_reader(
        self, sub: RedisSubscription, pass_once: Callable[[], Any]
    ) -> None:
        """Turn one read pass into a loop that survives a transient. **#103's fix.**

        `_flush_forever` already had this shape: catch, log "it keeps running", loop.
        The reader had the other one -- catch, log "it stopped", re-raise -- and the two
        sat eleven lines apart in the same file by the same hand. `feed` kept publishing
        through the disturbance that killed every reader in the stack for exactly that
        reason.

        The stamp at the top of each pass is what `reader_lost` reads. It is taken
        before the read rather than after it, so a pass that never returns ages.
        """
        while True:
            sub.last_pass = sub.monotonic()
            try:
                await pass_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._reader_failed(sub, error)
            else:
                if sub.retries:
                    self._reader_recovered(sub)

    async def _reader_failed(self, sub: RedisSubscription, error: Exception) -> None:
        """Back off and go round again, or give up loudly at the bound."""
        sub.retries += 1
        sub.retries_total += 1
        sub.failure = f"{type(error).__name__}: {error}"
        if sub.retries > self.config.read_retries:
            sub.gave_up = True
            log_event(
                logger,
                logging.ERROR,
                log_events.BUS_READER,
                "the bus reader for %r gave up after %d consecutive failures and is no "
                "longer consuming: %s",
                sub.name,
                sub.retries,
                sub.failure,
                exc_info=True,
            )
            self._alert(
                "bus.reader_stopped",
                f"the bus reader for {sub.name!r} gave up after {sub.retries} "
                f"consecutive failures and is no longer consuming; every stream it "
                f"reads now reports behind. Last failure: {sub.failure}",
            )
            raise error
        wait = min(
            self.config.read_retry_ceiling_seconds,
            self.config.read_retry_seconds * 2 ** (sub.retries - 1),
        )
        log_event(
            logger,
            logging.WARNING,
            log_events.BUS_READER,
            "the bus reader for %r raised; it keeps running -- retry %d of %d in "
            "%.3fs: %s",
            sub.name,
            sub.retries,
            self.config.read_retries,
            wait,
            sub.failure,
            exc_info=True,
        )
        await asyncio.sleep(wait)

    def _reader_recovered(self, sub: RedisSubscription) -> None:
        """One good read restores the budget in full, as `controller-policies.md` C6
        already says of the reconnect budget. The lifetime count is what remembers."""
        log_event(
            logger,
            logging.WARNING,
            log_events.BUS_READER,
            "the bus reader for %r recovered after %d consecutive failures",
            sub.name,
            sub.retries,
        )
        sub.retries = 0

    async def _read_group(self, sub: RedisSubscription, existing: set[str]) -> None:
        """Lossless: a consumer group, acked on receipt, replayed from a recorded id."""
        await self._replay(sub, existing)

        streams = dict.fromkeys(sub.streams, ">")
        await self._run_reader(sub, lambda: self._read_group_pass(sub, streams))

    async def _read_group_pass(
        self, sub: RedisSubscription, streams: dict[str, str]
    ) -> None:
        """One `XREADGROUP`, acked and delivered. The whole of what a retry repeats."""
        got = await self._client.xreadgroup(
            sub.group,
            sub.consumer,
            streams,
            count=self.config.read_count,
            block=self.config.read_block_ms or None,
        )
        if not got:
            for key in sub.streams:
                sub.behind[key] = False
            await asyncio.sleep(self.config.idle_sleep_seconds)
            return
        # **Acked on receipt, before the work.** The flush is the durability boundary,
        # not the read, so a per-message ack after the work would say nothing true.
        pipe = self._client.pipeline(transaction=False)
        full_reads: dict[str, bool] = {}
        returned = {_text(key) for key, entries in got if entries}
        for key in sub.streams:
            if key not in returned:
                sub.behind[key] = False
        for key, entries in got:
            if entries:
                pipe.xack(key, sub.group, *[entry_id for entry_id, _ in entries])
        await pipe.execute()
        for key, entries in got:
            name = _text(key)
            full_reads[name] = len(entries) >= self.config.read_count
            self._deliver(sub, name, entries)
        for key, is_full in full_reads.items():
            sub.behind[key] = is_full

    async def _read_head(self, sub: RedisSubscription) -> None:
        """Drop-oldest: no group, everything a read gives, and a jump when far behind.

        **The queue does the dropping**, exactly as it does on the fan-out: a read hands
        every entry it got to `offer`, which evicts the oldest and counts it, so the
        newest survive on this side of the seam for the same reason and by the same code.

        A full read — `COUNT` entries, all of them — is the signal that there is probably
        more waiting, and that is when the lag is checked. Below that the reader is
        keeping up and no round trip is spent asking.
        """
        await self._run_reader(sub, lambda: self._read_head_pass(sub))

    async def _read_head_pass(self, sub: RedisSubscription) -> None:
        """One `XREAD` from the recorded cursor, and the jump when far behind."""
        count = self.config.read_count
        cursor = {key: sub.last_ids[key] for key in sub.streams}
        got = await self._client.xread(
            cursor, count=count, block=self.config.read_block_ms or None
        )
        taken = 0
        for key, entries in got or ():
            taken += self._deliver(sub, _text(key), entries)
        if taken >= count:
            await self._resync_if_behind(sub)
        elif not taken:
            await asyncio.sleep(self.config.idle_sleep_seconds)

    def _deliver(self, sub: RedisSubscription, key: str, entries: Any) -> int:
        """Decode a stream's entries onto the subscription's queue. Never raises.

        The stream name is handed to `decode` so that an entry whose `type` disagrees with
        the key it arrived on is refused: the name is a claim about the contents and a
        consumer that trusts it must be able to.
        """
        delivered = 0
        for entry_id, fields in entries:
            entry_id = _text(entry_id)
            sub.last_ids[key] = entry_id
            previous = sub.positions.get(key, Position("0-0", 0))
            sub.positions[key] = Position(entry_id, previous.index + 1)
            sub._delivered[key] = sub._delivered.get(key, 0) + 1
            delivered += 1
            if _in_skip_span(sub, key, entry_id):
                sub.span_dropped += 1
                continue
            try:
                sub.offer(decode(fields, stream=key))
            except Exception:
                sub.undecodable += 1
                log_event(
                    logger,
                    logging.ERROR,
                    log_events.ENGINE_ERROR,
                    "an entry on %s could not be decoded and was dropped for %r",
                    key,
                    sub.name,
                    exc_info=True,
                )
        return delivered

    async def _ensure_group(self, sub: RedisSubscription) -> set[str]:
        """`XGROUP CREATE ... MKSTREAM` on every configured stream, at a resolved id.

        The id is chosen per stream, in this order: the caller's saved position for that
        stream if it passed one; else the stream's head, taken here once as a concrete id,
        if `group_start` is `"$"`; else `"0"`. **`"$"` is what a caller that said nothing
        gets**, since #97 — `subscribe` records why, and why `"0"` was kept reachable
        rather than deleted.

        Returns the streams whose group **already existed**, which is what tells a replay
        where the previous instance of this consumer had got to.

        Whether a newly created group has anything in front of it depends on which of the
        three ids it was created at. At a saved position it starts where the reader left
        off and `_replay` covers the suffix ahead of it. At the head it starts from now
        and there is nothing to replay, deliberately — everything older is being declined.
        At `0` it starts at the bottom and already carries everything Redis still holds,
        so again nothing is replayed in front of it, for the opposite reason.

        **`$` positions a group only when that group is created.** A `BUSYGROUP` answer
        means it already exists and is rejoined wherever its last-delivered id sits;
        `group_start` has no say over that, which is #86 and why the Discord consumer
        drops stale alerts in its own loop rather than here.
        """
        import redis.exceptions

        existing: set[str] = set()
        heads = (
            await self._stream_heads(sub.streams) if sub.group_start == "$" else {}
        )
        for key in sub.streams:
            if key in sub.start_ids:
                group_id = sub.start_ids[key].id
            elif sub.group_start == "$":
                group_id, added = heads.get(key, ("0-0", 0))
                sub.positions[key] = Position(group_id, added)
                sub.last_ids[key] = group_id
            else:
                group_id = "0"
            try:
                await self._client.xgroup_create(
                    key, sub.group, id=group_id, mkstream=True
                )
            except redis.exceptions.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
                existing.add(key)
            else:
                if key in sub.start_ids:
                    sub._group_missing.add(key)
        return existing

    async def _replay(self, sub: RedisSubscription, existing: set[str]) -> None:
        """Read forward from the recorded id up to where the group had already got to.

        The two halves meet exactly, and only because this one is bounded at **both**
        ends. `XREAD` takes a start and no end, so a batch can run past the group's
        `last-delivered-id`; everything past it is the group's `>` read's to deliver, and
        handing it to the consumer here as well is how a restart folded the tail of its
        replay twice (#84). Entries past the target are dropped here and the stream is
        finished with. This covers `(start_id, last-delivered]`, the group's `>` covers
        everything after `last-delivered`. No gap, and no entry twice.
        """
        targets = await self._group_positions(sub, existing)
        cursor = {key: sub.start_ids[key].id for key in targets if key in sub.start_ids}
        sub._replaying.update(cursor)
        for key in cursor:
            sub.behind[key] = True
        while cursor:
            pending = {
                key: at for key, at in cursor.items() if _id_before(at, targets[key])
            }
            if not pending:
                for key in cursor:
                    sub.behind[key] = False
                    sub._replaying.discard(key)
                return
            got = await self._client.xread(pending, count=self.config.read_count)
            if not got:
                for key in cursor:
                    sub.behind[key] = False
                    sub._replaying.discard(key)
                return
            for key, entries in got:
                name = _text(key)
                target = targets[name]
                bounded = [
                    entry
                    for entry in entries
                    if not _id_before(target, _text(entry[0]))
                ]
                if bounded:
                    self._deliver(sub, name, bounded)
                if entries and len(bounded) == len(entries):
                    cursor[name] = _text(entries[-1][0])
                else:
                    # The batch reached past the target, so there is nothing before it
                    # left to read: park the cursor on the target and this stream drops
                    # out of `pending`. What was dropped arrives from the group's `>`.
                    cursor[name] = target

    async def _group_positions(
        self, sub: RedisSubscription, existing: set[str]
    ) -> dict[str, str]:
        """Each pre-existing group's `last-delivered-id`, per stream."""
        positions: dict[str, str] = {}
        for key in sub.streams:
            if key not in existing:
                continue
            try:
                groups = await self._client.xinfo_groups(key)
            except Exception:  # noqa: BLE001 - a missing key is simply nothing to replay
                continue
            for group in groups:
                if _text(group.get("name")) == sub.group:
                    positions[key] = _text(group.get("last-delivered-id", "0-0"))
        return positions

    async def replay_gaps(
        self,
        subscription: RedisSubscription,
        start_ids: Mapping[str, Position],
    ) -> dict[str, ReplayGap]:
        """Report trimming facts for saved positions with one stream-info pipeline."""
        keys = [key for key in subscription.streams if key in start_ids]
        if not keys:
            return {}
        pipe = self._client.pipeline(transaction=False)
        for key in keys:
            pipe.xinfo_stream(key)
        results = await pipe.execute(raise_on_error=False)
        gaps: dict[str, ReplayGap] = {}
        for key, info in zip(keys, results, strict=True):
            saved = start_ids[key]
            if not isinstance(info, dict):
                gaps[key] = ReplayGap(key, saved.id, None, None, 0)
                continue
            added = int(_info_value(info, "entries-added", 0))
            length = int(_info_value(info, "length", 0))
            trimmed = max(0, added - length)
            first = _info_value(info, "first-entry")
            first_id = _text(first[0]) if first else None
            if key in subscription._group_missing:
                lost: int | None = None
            elif first_id is None:
                lost = 0
            else:
                lost = max(0, trimmed - saved.index)
            gaps[key] = ReplayGap(key, saved.id, first_id, lost, trimmed)
        return gaps

    async def consumer_lag(
        self, subscription: RedisSubscription
    ) -> dict[str, StreamLag]:
        """Where this subscription's group stands in each of its streams.

        **One pipeline, two `XINFO` calls a stream, and no state of our own.** The
        numbers come from Redis every time they are asked for, which is the property the
        cached `behind` flag did not have. Cheap enough to run on a timer: the ticket's
        own evidence was captured with exactly these two commands.

        A stream that does not exist, or a group that is not on it, is an answer rather
        than a failure -- `raise_on_error=False`, and the fields come back `None`.
        """
        if self._client is None:
            return {}
        keys = list(subscription.streams)
        pipe = self._client.pipeline(transaction=False)
        for key in keys:
            pipe.xinfo_stream(key)
            pipe.xinfo_groups(key)
        results = await pipe.execute(raise_on_error=False)
        lags: dict[str, StreamLag] = {}
        for index, key in enumerate(keys):
            info = results[index * 2]
            groups = results[index * 2 + 1]
            first_id: str | None = None
            entries_added: int | None = None
            length: int | None = None
            if isinstance(info, dict):
                first = _info_value(info, "first-entry")
                first_id = _text(first[0]) if first else None
                entries_added = int(_info_value(info, "entries-added", 0) or 0)
                length = int(_info_value(info, "length", 0) or 0)
            record: Mapping[Any, Any] | None = None
            if isinstance(groups, (list, tuple)):
                for group in groups:
                    if not isinstance(group, Mapping):
                        continue
                    if _text(_info_value(group, "name", "")) == subscription.group:
                        record = group
                        break
            if record is None:
                lags[key] = StreamLag(
                    key, None, None, entries_added, length, None, first_id
                )
                continue
            raw_lag = _info_value(record, "lag")
            raw_read = _info_value(record, "entries-read")
            raw_last = _info_value(record, "last-delivered-id")
            lags[key] = StreamLag(
                stream=key,
                # Redis answers `-1` through some clients where the specification says
                # nil; both mean "unknown", and neither is a lag of minus one.
                lag=None if raw_lag is None or int(raw_lag) < 0 else int(raw_lag),
                entries_read=None if raw_read is None else int(raw_read),
                entries_added=entries_added,
                length=length,
                last_delivered_id=None if raw_last is None else _text(raw_last),
                first_retained_id=first_id,
            )
        return lags

    async def _position_at_head(self, sub: RedisSubscription) -> set[str]:
        """Start at `$` — but as a **concrete id**, taken once.

        Literal `$` is resolved by Redis per call, so a reader that passed it every time
        would silently skip everything that arrived between two reads. Taken once, it
        means "from now", which is what a screen wants, and the `entries-added` counter
        read with it is the baseline the skip count is measured from.
        """
        heads = await self._stream_heads(sub.streams)
        for key in sub.streams:
            last_id, added = heads.get(key, ("0-0", 0))
            sub.last_ids[key] = last_id
            sub._baseline[key] = added
            sub._delivered[key] = 0
        return set()

    async def _resync_if_behind(self, sub: RedisSubscription) -> None:
        """Jump to the newest entries of any stream this reader is far behind on, and
        count what the jump went over.

        **It jumps to the newest `capacity` entries and not to the very head.** The head
        is where nothing is; a screen wants the last few quotes, which is what its queue
        would have held anyway. `XREVRANGE ... COUNT` is that, and it is one round trip.

        The arithmetic is exact to the round trip: `entries-added` and `last-generated-id`
        come out of the same `XINFO STREAM`, so between the baseline and now the stream
        added `added - baseline` entries, this reader took `delivered` of them, and the
        difference is what it did not get — whether it jumped over them or **Redis trimmed
        them away before it arrived**, which is the case nothing else here would notice.
        """
        heads = await self._stream_heads(sub.streams)
        for key, (_last_id, added) in heads.items():
            outstanding = added - sub._baseline.get(key, 0) - sub._delivered.get(key, 0)
            if outstanding <= sub.capacity:
                continue
            newest = list(reversed(await self._client.xrevrange(key, count=sub.capacity)))
            sub.skipped += max(0, outstanding - len(newest))
            sub.resyncs += 1
            sub._baseline[key] = added
            sub._delivered[key] = 0
            log_event(
                logger,
                logging.WARNING,
                log_events.BUS_SELECTED,
                "%r was %d entries behind on %s and jumped to its newest %d; %d skipped "
                "in total",
                sub.name,
                outstanding,
                key,
                len(newest),
                sub.skipped,
            )
            self._deliver(sub, key, newest)

    async def _stream_heads(
        self, streams: Iterable[str]
    ) -> dict[str, tuple[str, int]]:
        """`(last-generated-id, entries-added)` per stream, in one round trip.

        `raise_on_error=False` because a stream nobody has written to yet does not exist,
        and "no such key" is an answer — an empty stream — rather than a failure.

        **A stream that exists and holds nothing is the other half of that answer**, and
        it does not report the same way. Real Redis gives `last-generated-id` as `0-0`;
        `fakeredis` gives `None` (`measured` 2026-09-12, `fakeredis.aioredis` in the main
        checkout's venv: `xinfo_stream` on a stream created by `XGROUP CREATE` with
        `MKSTREAM` answers `'last-generated-id': None`). A present-but-`None` field
        defeats a `dict.get` default, and `_text(None)` is the string `"None"`, which is
        not a stream id — every command taking it fails with `Invalid stream ID
        specified as stream command argument`. `0-0` is the true last-generated id of a
        stream with no entries, which is real Redis's answer, so both normalise to it.
        Reached by every `$` subscriber and, before #97 made `$` the default, already by
        every drop-oldest one through `_position_at_head`.
        """
        keys = list(streams)
        pipe = self._client.pipeline(transaction=False)
        for key in keys:
            pipe.xinfo_stream(key)
        results = await pipe.execute(raise_on_error=False)
        heads: dict[str, tuple[str, int]] = {}
        for key, info in zip(keys, results, strict=True):
            if not isinstance(info, dict):
                heads[key] = ("0-0", 0)
                continue
            last_id = _info_value(info, "last-generated-id", "0-0")
            heads[key] = (
                "0-0" if last_id is None else _text(last_id),
                int(_info_value(info, "entries-added", 0) or 0),
            )
        return heads


def _default_client(config: BusConfig) -> Any:
    """redis-py's asyncio client. Imported here so the module costs nothing to import."""
    import redis.asyncio

    return redis.asyncio.Redis.from_url(config.url, **config.client_kwargs())


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


def _info_value(info: Mapping[Any, Any], key: str, default: Any = None) -> Any:
    return info.get(key, info.get(key.encode(), default))


def _id_before(left: str, right: str) -> bool:
    """Stream id ordering. `ms-seq`, both integers, and neither is a string comparison —
    `"9-0"` sorts after `"10-0"` as text and before it as an id."""
    return _id_parts(left) < _id_parts(right)


def _in_skip_span(sub: RedisSubscription, key: str, entry_id: str) -> bool:
    for span in sub.skip:
        frm = span.frm.get(key)
        if frm is None or not _id_before(frm, entry_id):
            continue
        if span.to is None:
            if key in sub._replaying:
                return True
            continue
        to = span.to.get(key)
        if to is not None and not _id_before(to, entry_id):
            return True
    return False


def _id_parts(entry_id: str) -> tuple[int, int]:
    ms, _, seq = entry_id.partition("-")
    return int(ms or 0), int(seq or 0)


__all__ = [
    "BUS_ENV",
    "BusConfig",
    "BusUnavailable",
    "FANOUT_BUS",
    "Position",
    "REDIS_BUS",
    "ReplayGap",
    "RedisBus",
    "RedisSubscription",
    "Span",
    "StreamLag",
    "selected_bus",
    "trim_floor_ms",
]
