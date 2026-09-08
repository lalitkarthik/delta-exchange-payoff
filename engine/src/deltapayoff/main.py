"""The FastAPI app.

Two REST endpoints fixed by `docs/chain-contract.md`, one websocket that pushes the same
`ChainResponse` live, and one more REST endpoint — `/smile` — fixed by
`docs/smile-contract.md`.

`/smile` is the odd one out and deliberately so: the other three serve **Delta, now**,
while it serves what was already computed and stored. It calls nothing, solves nothing and
has no upstream to be unavailable, so it has no 502 and no 404. See `smile.py`.

`/recording` is odder still, and it is the **only mutating route in this engine** — every
other one is a `GET` or a websocket. It reports whether the store is writing and lets a
reader stop and start it, which is why `allow_methods` below is no longer `["GET"]` alone:
a `POST` against a `GET`-only allowance is refused at the preflight and surfaces in the
browser as a network error indistinguishable from the engine being down. Who may call it
is answered in `docs/recording-contract.md` rather than left unexamined.

`/ws/chain` exists so the screen updates without anyone pressing anything. It sends the
identical object `/chain` returns, so `web/components/ChainLadder.tsx` renders it
unchanged — the transport moved, the contract did not.

Behind it: one `DeltaFeed` for the whole process, publishing to a `FanOut`, with a
`ChainStream` holding the newest frame per contract. Sockets are per browser; the cache
and the connection to Delta are shared. A second tab costs a queue, not a connection.

The bus's second consumer is `BarWriter`, which aggregates the same stream into
one-minute bars and writes hive-partitioned Parquet. It is **not** folded into
`ChainStream`: that holds only the latest state per contract while the writer needs every
state, and sharing one structure would make them fight. It subscribes losslessly, and its
disk write runs in a worker thread — a flush on this event loop would stop the socket
reader, fill the receive buffer and get us disconnected.

**Every market-data event is stored, into three tables.** `md.option_quote` becomes the
quote bars; `md.option_reference` becomes the reference bars and `md.index_quote` the spot
bars, and the reference event also supplies the
quote bars' fallback for a contract whose book is silent. One writer takes one lossless
subscription and drives all three — a second writer would mean a second subscription
carrying the same messages and two watermarks drifting apart on two clocks.

`BarStore()` here names the quote table only; the writer derives the other two roots from
it, so there is exactly one place that decides where market data lands.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware

from . import log_events
from .adapters import Adapter, DeltaAdapter, DeltaFeed
from .chain import (
    UNDERLYINGS,
    ValidationError,
    normalise_underlying,
    validate_expiry,
)
from .compute import enrich
from .contract_bars import ContractBarsResponse, read_contract_bars
from .delta_client import DeltaClient, DeltaUnavailable
from .events import ConnectionState, ControlCommand, Event, FeedConnection
from .events.instrument import Instrument, InstrumentParseError
from .fanout import FanOut
from .historical import list_minutes, read_ladder_at
from .logging_setup import configure_logging, log_event
from .models import (
    AdapterHealth,
    ChainResponse,
    ExpiriesResponse,
    HealthReport,
    HistoricalMinutes,
    RecordingRequest,
    RecordingState,
    SmileResponse,
    WatchedPair,
)
from .realised_vol import ESTIMATORS
from .smile import read_smile
from .store import (
    COMPUTED_DATASET,
    COMPUTED_SCHEMA,
    INDEX_DATASET,
    INDEX_SCHEMA,
    REFERENCE_DATASET,
    REFERENCE_SCHEMA,
    SPOT_DATASET,
    SPOT_SCHEMA,
    BarStore,
    BarWriter,
    read_contract_ivs,
    read_index_bars,
    read_spot_bars,
)
from .stream import ChainStream, recompute_every_minute, recompute_forever
from .supervisor import FeedSupervisor
from .volatility import (
    ALIGNMENTS,
    INDEX_SOURCE,
    INTERVALS,
    SPOT_SOURCE,
    BoundsResponse,
    VolatilitySeries,
    contract_ivs_from_chains,
    lookback_bounds,
    volatility_series,
)

#: The Next.js dev server. Development only; production origins are a deploy concern.
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

#: Note that CORS does **not** cover `/ws/chain`. A websocket handshake is not subject to
#: it, so that route accepts any origin. The payload is public Delta market data, the
#: server binds to loopback and no credentials are involved, so the exposure is small
#: today — but it stops being small the moment this binds to a non-loopback interface or
#: the payload carries anything user-specific, and neither needs a code change here.

logger = logging.getLogger(__name__)

# **Every logger under `"deltapayoff"` inherits this the moment `main` is imported**,
# which is every test file in this suite and every real process. Idempotent — see
# `logging_setup.configure_logging` — so importing `main` more than once, which pytest
# does per test file, attaches the file and console handlers exactly once each.
configure_logging()

#: How often a connected browser is sent the chain. One second is well under what anyone
#: reads and far above what the eye needs, and it is one JSON push regardless of how many
#: messages arrived underneath. **Measured**: a 136-symbol chain's book delivers about
#: 268 messages a second, so pushing per message would be roughly 268x oversampled.
PUSH_INTERVAL_SECONDS = 1.0

#: The floor under `interval`, which arrives from the query string and is therefore
#: attacker-controlled. **Measured** without it: `?interval=0` pushed 207 chains in three
#: seconds, 69 a second against an intended one, each rebuilding a 69-strike ladder from
#: 136 cached frames and serialising it. A hand-edited URL pegs a core and starves the
#: event loop the Delta feed runs on. A negative value is a zero in disguise, because
#: `asyncio.sleep` returns immediately on one.
#:
#: 0.02 is chosen because the endpoint tests drive the parameter and a higher floor would
#: make the suite wait. It bounds the abuse rather than removing it — measured after the
#: fix, `?interval=0` gives 21 pushes a second instead of 69, still well above the
#: intended one. That is acceptable while this binds to loopback and serves public market
#: data; it would not be if either changed.
MIN_PUSH_INTERVAL_SECONDS = 0.02

#: Underlyings the live feed subscribes at start-up **when nothing says otherwise**.
#: Every listed option on each, both channels. That buys instant expiry switching with no
#: subscribe round trip. Narrowing the book subscription to the watched expiry would cut
#: it to roughly a third; see `docs/ingestion.md`.
#:
#: **This comment used to claim ~600 msg/s and ~300 KB/s for BTC alone. That number was
#: never run** — #33's spec had quoted it from here rather than from a probe, and #43
#: found no run behind it anywhere in the repository's history. `tools/measure_feed.py`,
#: run for both configurations on 2026-09-08, put it right: BTC alone (504 contracts)
#: is `measured` 1,095.1 msg/s at 547.3 KB/s; BTC+ETH together (782 contracts) is
#: `measured` 1,693.6 msg/s at 843.4 KB/s. Both are the same subscription shape this line
#: builds — every listed contract, both channels — so the two never disagreed about what
#: was subscribed, only about whether anyone had measured it. `docs/design/hld.md` §5 has
#: the full run detail; this comment now points there instead of repeating a number that
#: drifts with the live contract count.
LIVE_UNDERLYINGS = ("BTC", "ETH")

#: Comma-separated, e.g. `BTC,ETH`. Read at start-up rather than at import, so which
#: assets are recorded is a deployment decision and not a code change.
LIVE_UNDERLYINGS_ENV = "DELTA_LIVE_UNDERLYINGS"

#: How often the venue is asked what it lists, so contracts it lists **after** start-up
#: are subscribed rather than missed for the life of the process. `assumed`; the full
#: reasoning is `docs/design/lld/relisting.md` §3.
#:
#: One minute, because the store's resolution is one minute: a cadence of 60 s bounds the
#: hole in a newly listed contract's history at roughly one bar, which is the smallest
#: gap this store can even express. Five minutes would lose five bars of every new strike
#: for nothing but a saved REST call.
#:
#: Not faster, either. This is `/v2/tickers` with no expiry filter, the heaviest read this
#: engine makes: `measured` 2026-09-08 by `tools/probe_relist.py`, BTC is 520 contracts,
#: 644.8 KB and 735 ms, ETH 278 contracts, 342.2 KB and 737 ms. At this cadence that is
#: `derived` 987 KB a minute against the feed's own `measured` 843.4 KB/s — about 2% more
#: traffic — for a listing that changes a few times a day. Below a minute it re-reads the
#: same answer several times per bar it could not have improved.
RELIST_INTERVAL_SECONDS = 60.0

#: The most points `/volatility` will put in one response unless asked for fewer.
#:
#: A year at one-minute resolution is 525,600 points per series, and six series of that is
#: a payload no browser wants. The cap is applied by **computing at fewer timestamps**,
#: not by computing everything and throwing some away — each point still rests on its own
#: full window, so this is a coarser reading of the same rolling estimate rather than a
#: downsampling of it. The step actually used is reported as `step_seconds`, because a cap
#: nobody is told about reads as "we covered everything".
MAX_POINTS = 2000

#: Environment switch: set to "0" to serve the REST endpoints and the websocket without
#: opening a socket to Delta. Read at start-up rather than at import, so a test can set
#: it — the suite sets it in `conftest.py`, because nothing in it may touch the network.
LIVE_FEED_ENV = "DELTA_LIVE_FEED"


def live_feed_enabled() -> bool:
    return os.environ.get(LIVE_FEED_ENV, "1") != "0"


def live_underlyings() -> tuple[str, ...]:
    """Which underlyings to record, from the environment, defaulting to BTC alone.

    **An unknown name is dropped and logged at error rather than subscribed.** Delta
    answers a request for an underlying it does not list with an empty listing, so a
    typo would otherwise produce a feed that connects, subscribes nothing and records
    nothing, with no error anywhere — the silent failure this whole component exists to
    refuse. If nothing valid is left, the default stands, because recording BTC is a
    better answer to a bad config line than recording nothing.
    """
    raw = os.environ.get(LIVE_UNDERLYINGS_ENV, "")
    wanted = [name.strip().upper() for name in raw.split(",") if name.strip()]
    if not wanted:
        return LIVE_UNDERLYINGS

    known = [name for name in wanted if name in UNDERLYINGS]
    unknown = [name for name in wanted if name not in UNDERLYINGS]
    if unknown:
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "%s names %s, which Delta does not list; recording %s",
            LIVE_UNDERLYINGS_ENV,
            ", ".join(unknown),
            ", ".join(known) or ", ".join(LIVE_UNDERLYINGS),
        )
    return tuple(known) or LIVE_UNDERLYINGS


@dataclass
class FeedConnectionCache:
    """The latest `feed.connection` transition per adapter. #40's badge reads this.

    **Why this exists rather than reading `ConnectionController.state` directly.** The
    controller (#38) exposes *which* state it is in but not *since when*, in wall-clock
    terms — `_entered_at` is on its monotonic clock, held privately, and there is no
    accessor for it. A websocket that connects between two transitions still has to open
    with an accurate "since" for whatever state the feed was already in, and the only
    place that timestamp exists at all is on the `FeedConnection` event the controller
    published when it made that transition. So this remembers the event, not the state.

    **Updated synchronously, not through a subscription.** `build_feed_stack` wraps the
    `publish` callable handed to `FeedSupervisor` so this is written *before* the event
    reaches the bus, in the same call — no queue, no task, no lag, and critically no new
    consumer on the market-data bus: a raw `FanOut` subscription would receive every
    `md.option_quote` and `md.option_reference` too, at roughly 600 messages a second,
    to catch a `feed.connection` event that arrives a few times an hour.
    """

    latest: dict[str, FeedConnection] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        """Note `event` if it is a transition. Anything else passes unremembered."""
        if isinstance(event, FeedConnection):
            self.latest[event.adapter] = event

    def get(self, adapter: str) -> FeedConnection | None:
        return self.latest.get(adapter)


@dataclass
class FeedStack:
    """Every moving part of the live feed, wired to the bus and to each other.

    **A named record rather than five loose `app.state` attributes**, so that what the
    engine is made of can be built, started and stopped by three functions a reader can
    follow, and so that swapping one part is a change in one place. `app.state` still
    carries the same five names afterwards, because the tests and the route dependencies
    reach for them and this is a refactor, not a rename.
    """

    #: **The bus, and since #37 the only one.** The adapter publishes `md.option_quote`,
    #: `md.option_reference` and `md.index_quote` here; the chain cache and the bar writer
    #: subscribe, with different queue policies (see `fanout.py`). #36 ran a second bus
    #: beside it carrying the retired quote record — that was the expand half of an
    #: expand-contract, and it is gone with the record and the shim that filled it.
    events: FanOut
    stream: ChainStream
    writer: BarWriter
    #: The venue, behind `adapters.base.Adapter`. Everything venue-specific is inside it,
    #: including the two REST reads `/expiries` and `/chain` are answered from.
    adapter: Any
    #: **Who owns the connection**, since #39. One `ConnectionController` per adapter,
    #: started and stopped with the application, and the thing `/health` asks. Built
    #: unconditionally, like the writer, so a process with no live feed still has a
    #: report to give rather than a route that raises.
    supervisor: FeedSupervisor
    #: **#40's badge reads this.** The latest `feed.connection` per adapter, kept in step
    #: with the supervisor's own `publish` — see `FeedConnectionCache`.
    feed_cache: FeedConnectionCache
    #: The background tasks, empty until `start_feed_stack` runs. Cancelled on shutdown.
    #: **The feed is no longer among them** — the supervisor owns that task.
    tasks: list[asyncio.Task] = field(default_factory=list)
    #: Venue symbols already handed to `adapter.subscribe`, per underlying. **Added in
    #: #51**, and kept here rather than read back off the adapter because "what has this
    #: engine subscribed" is a question of the protocol's eight members, and none of them
    #: answers it: the registry lives inside `DeltaFeed`, which is a Delta detail, and a
    #: second venue would keep its own in its own shape. This is the set the re-list
    #: subtracts to find what is new, and it only ever grows — see `relist_instruments`
    #: for why a settled contract is not taken back out.
    listed: dict[str, set[str]] = field(default_factory=dict)

    @property
    def feed(self) -> Any:
        """The socket owner inside the adapter, for the counters #39's `/health` reads."""
        return self.adapter.feed


def build_feed_stack(client: DeltaClient) -> FeedStack:
    """Wire the bus, the chain cache, the bar writer and the adapter together.

    **Nothing here starts, connects or awaits.** Building is separated from starting so
    that a process with no live feed — every test, and any run with `DELTA_LIVE_FEED=0`
    — still has the whole structure present and introspectable.

    The writer is attached whether or not the feed runs, so `/health`-adjacent
    introspection and the tests can see the subscription exists and is lossless. With no
    feed nothing is published, so an undrained queue costs nothing.

    Table C is **sampled from the chain cache**, not folded from the bus, because our
    implied volatility and Greeks are produced by the recompute loop rather than arriving
    on the wire. The writer is handed the stream's reader, not the stream, so the store
    never learns that a chain cache exists.

    Since #44 that reader is `live_computed_chains` — the pairs a browser is watching —
    and the rest of the board reaches the same table once a minute through
    `recompute_every_minute`, which hands its ladders to `writer.sample_chains`. Two
    cadences, two paths, and neither re-folds the other's work; see that method for why
    the periodic sample must not meet an unwatched expiry's ladder five times over.

    `BarStore()` names the quote table only; the writer derives the other two roots from
    it, so there is exactly one place that decides where market data lands.

    Every collaborator is looked up in this module's globals **at call time**, which is
    what lets a test replace `DeltaClient`, `DeltaFeed`, `BarStore` or `BarWriter` with a
    stub and get a stack that never opens a socket. `DeltaFeed` reaches the adapter as a
    factory for exactly that reason: the adapter builds the socket owner around its own
    sink, and the name it builds is still this module's.
    """
    events = FanOut()
    stream = ChainStream()
    stream.attach(events)
    writer = BarWriter(BarStore(), chains=stream.live_computed_chains)
    writer.attach(events)
    adapter = DeltaAdapter(
        client=client,
        underlyings=live_underlyings(),
        feed_factory=DeltaFeed,
    )
    # #40's badge needs an accurate "since" for whatever state a browser's own websocket
    # connects into, and the controller keeps no wall-clock record of that — see
    # `FeedConnectionCache`. So the cache is updated **synchronously, in front of the
    # real bus**, rather than through a subscription of its own: the same `publish`
    # `FeedSupervisor` was already being handed, with one line added before it.
    feed_cache = FeedConnectionCache()

    def publish(event: Event) -> None:
        feed_cache.apply(event)
        events.publish(event)

    return FeedStack(
        events=events,
        stream=stream,
        writer=writer,
        adapter=adapter,
        # One adapter today and a list from the start, because the supervisor's whole
        # reason to exist is the second one — and a single-adapter shortcut here is the
        # thing that would have to be undone to add it.
        supervisor=FeedSupervisor([adapter], publish),
        feed_cache=feed_cache,
    )


async def relist_instruments(stack: FeedStack) -> int:
    """Ask the venue what it lists and subscribe whatever is not subscribed yet.

    **The whole of issue #51's first half.** This used to happen once, inline in
    `start_feed_stack`, and nothing ever asked again — so every contract Delta listed
    after the process started was never subscribed, never stored, and absent from every
    historical screen, while the live path went on answering `/chain` from a fresh REST
    read and looked perfectly healthy. Measured on the night of 2026-09-07: four strikes
    of one expiry first appear in the store at 06:32, when a *second* engine started, and
    one of them sits between two strikes recorded from midnight.

    **Additive, and additive is the whole safety argument.** `subscribe` registers rather
    than replaces and the registry is never cleared, so this can only ever make the
    reconnect replay larger. There is no moment at which the registry is empty, which
    matters because an empty registry is deliberately not announced as `OPENED` (#38,
    #39) — a re-list that briefly emptied it would put the connection badge through a
    false reconnect for as long as it took to fill again.

    **Settled contracts are kept, deliberately.** A contract that has expired drops out
    of the venue's listing but stays in `stack.listed` and in the socket registry, and is
    replayed on every reconnect for the life of the process. Dropping it would mean
    unsubscribing on a cadence, and the cadence is the problem: a contract leaves the
    listing at settlement, while its last book updates are still the most valuable and
    least repeatable rows in the record, and a re-list that fired in that window would
    take the subscription away mid-settlement to save a few hundred bytes of subscribe
    frame. The cost of keeping is a replay that grows by one day's expired contracts per
    day the process runs — `assumed` to be tolerable for a process restarted more often
    than weekly, and made visible rather than merely assumed: the `subscribed` count on
    every record below is how many contracts this engine holds for that underlying — not
    the socket registry, which is their union per channel — so the growth is in the log,
    beside the venue's own listing size it can be compared against.
    `docs/design/lld/relisting.md` §5 records the threshold at which this is revisited.

    Returns how many contracts were newly subscribed. Raises whatever the venue read
    raises — the caller decides whether that is fatal, and the two callers differ.
    """
    added = 0
    for underlying in stack.adapter.underlyings:
        listed = await stack.adapter.instruments(underlying)
        known = stack.listed.setdefault(underlying, set())
        fresh = [
            instrument
            for instrument in listed
            if instrument.venue_symbol and instrument.venue_symbol not in known
        ]
        if not fresh:
            continue
        stack.adapter.subscribe(fresh)
        known.update(instrument.venue_symbol for instrument in fresh)
        added += len(fresh)
        log_event(
            logger,
            logging.INFO,
            log_events.FEED_INSTRUMENTS,
            "subscribed %d newly listed %s contracts; %d subscribed in total",
            len(fresh),
            underlying,
            len(known),
            venue=stack.adapter.venue,
            underlying=underlying,
            listed=len(fresh),
            subscribed=len(known),
        )
    return added


async def relist_forever(
    stack: FeedStack,
    interval: float = RELIST_INTERVAL_SECONDS,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Re-list on the cadence until cancelled. **A failed listing is not an outage.**

    The venue's REST endpoint is a different service from its websocket and fails
    separately: it times out, it rate-limits, it is redeployed. None of that is a reason
    to end a feed that is delivering, so a failure here is a warning and a retry on the
    next tick — the cost of one missed cycle is that a contract listed in the last minute
    waits another minute, which is the same bounded cost the cadence already carries.

    An unbounded `except` for the same reason `recompute_forever` has one: a loop that
    dies leaves the engine recording the set it started with and saying nothing, which is
    the exact failure this task was written to end.
    """
    while True:
        await sleep(interval)
        try:
            await relist_instruments(stack)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                logger,
                logging.WARNING,
                log_events.FEED_INSTRUMENTS,
                "could not re-list instruments; retrying in %gs",
                interval,
                venue=stack.adapter.venue,
                exc_info=True,
            )


async def start_feed_stack(stack: FeedStack) -> None:
    """Subscribe every listed contract and start the five background tasks.

    **Which underlyings is the adapter's own answer**, not a second argument: the adapter
    was built with the configured set and `underlyings` is on the protocol precisely so
    there is one place to ask.

    Raises `DeltaUnavailable` if the venue cannot be asked what it lists — the caller
    decides whether that is fatal. Nothing is started when it raises, because the
    subscriptions happen first: a feed that connected with an empty registry is the
    silent failure `feed.py` exists to prevent. **That is why the first listing is this
    call and not the loop's first tick**: at start-up an unanswerable venue is fatal, and
    an hour later it is a warning, so the two cannot be the same call site.
    """
    await relist_instruments(stack)

    # **The feed is started through the supervisor**, not as a task of its own. The
    # controller wraps the adapter, so the events reach the bus through its sink and
    # every `feed.connection`, `heartbeat` and `alert` reaches it beside them — which is
    # the wiring #38 built and did not connect, and the reason nothing had ever observed
    # a transition from a live controller.
    stack.supervisor.start()
    stack.tasks = [
        asyncio.create_task(stack.stream.run(), name="chain-stream"),
        asyncio.create_task(recompute_forever(stack.stream), name="chain-recompute"),
        # **The second cadence, and it does not depend on a browser.** The task above
        # solves what somebody is looking at; this one closes each minute for every
        # expiry that had a frame in it and hands the ladders straight to the writer, so
        # table C — and with it the volatility screen — covers the whole board whether
        # or not the chain page is open anywhere. See `recompute_every_minute`.
        asyncio.create_task(
            recompute_every_minute(stack.stream, stack.writer.sample_chains),
            name="chain-minute-pass",
        ),
        asyncio.create_task(stack.writer.run(), name="bar-writer"),
        # **The third cadence, and the one #51 was missing.** The two above recompute
        # what is already subscribed; this one asks the venue what it lists now, so a
        # strike that did not exist when this process started is subscribed within a
        # minute instead of never. It is deliberately the slowest thing here — a REST
        # read on a loop that a feed running at `measured` 1,693.6 msg/s must not notice.
        asyncio.create_task(relist_forever(stack), name="instrument-relist"),
    ]
    for task in stack.tasks:
        task.add_done_callback(_report_finished_task)


async def stop_feed_stack(stack: FeedStack) -> None:
    """Cancel the tasks, then flush the open minute. Order matters and is the point.

    The final flush runs **after** the cancellations and not inside them. The open minute
    is a real observation and is written with its true tick counts rather than discarded
    for tidiness; doing it here rather than from inside the cancelled task means the
    flush is not itself racing a cancellation.
    """
    # The supervisor first, and it is awaited rather than cancelled: it stops each
    # controller, cancels its task and **detaches it from its adapter**, which a bare
    # cancellation of the task cannot be relied on to reach.
    await stack.supervisor.aclose()
    for task in stack.tasks:
        task.cancel()
    if not stack.tasks:
        return
    await asyncio.gather(*stack.tasks, return_exceptions=True)
    try:
        await stack.writer.aclose()
    except Exception:
        # A failed final flush costs the open minute and nothing else. It must not take
        # the shutdown with it and leave the HTTP client unclosed.
        log_event(
            logger,
            logging.ERROR,
            log_events.ENGINE_ERROR,
            "the final bar flush failed",
            exc_info=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One HTTP client, one websocket to Delta, one chain cache, for the whole process.

    The feed is started here rather than per request for the reason #3 gives: three
    consumers each opening their own connection would burn the 150-per-5-minutes budget
    and give three inconsistent views of one market.

    What is wired to what is `build_feed_stack`; this function owns only the process's
    lifetime and the decision to run live or not.
    """
    client = DeltaClient()
    await client.__aenter__()
    app.state.delta = client

    stack = build_feed_stack(client)
    app.state.stack = stack
    app.state.events = stack.events
    app.state.adapter = stack.adapter
    app.state.stream = stack.stream
    app.state.writer = stack.writer
    app.state.feed = stack.feed
    app.state.supervisor = stack.supervisor
    app.state.feed_cache = stack.feed_cache
    app.state.tasks = stack.tasks

    if live_feed_enabled():
        try:
            await start_feed_stack(stack)
            app.state.tasks = stack.tasks
        except DeltaUnavailable:
            # The REST endpoints still work and the websocket reports "waiting". A
            # start-up that dies because Delta was briefly unreachable is worse than one
            # that comes up degraded and says so.
            pass

    try:
        yield
    finally:
        await stop_feed_stack(stack)
        await client.aclose()
        # **Undo every assignment above.** `app.state` is a plain namespace on a
        # module-level singleton and Starlette does not clear it on shutdown, so a test
        # that runs the real lifespan and then exits `TestClient`'s `with` block leaves
        # every name here pointing at a *closed* stack — a supervisor whose controllers
        # are detached, a writer whose store is gone. #40's `get_feed_cache` was the
        # first dependency this ever visibly broke: a later test on a bare
        # `TestClient(app)`, never expecting a supervisor at all, read a stale one back
        # and got a `feed` message built from the previous test's shutdown. Setting these
        # back to the "no lifespan has run" value — `None`, which every reader here
        # already treats as "nothing to report" rather than an error — makes shutdown
        # actually mean shutdown for whichever test runs next.
        for name in (
            "delta",
            "stack",
            "events",
            "adapter",
            "stream",
            "writer",
            "feed",
            "supervisor",
            "feed_cache",
            "tasks",
        ):
            setattr(app.state, name, None)


app = FastAPI(
    title="delta-exchange-payoff engine",
    version="0.1.0",
    summary="Delta Exchange option chain, pivoted. Computes nothing else.",
    lifespan=lifespan,
)

#: `GET` alone until `/recording` arrived. A `POST` from the browser against a `GET`-only
#: allowance is refused at the **preflight**, which surfaces in the page as a network
#: error indistinguishable from the engine being down — the most misleading failure shape
#: available, because the one thing it does not look like is a CORS rule. The allowance
#: moves with the route. It is not access control: it constrains browsers and nothing
#: else, and `docs/recording-contract.md` says who may actually call the route.
ALLOWED_METHODS = ["GET", "POST"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=ALLOWED_METHODS,
    allow_headers=["*"],
)


def get_adapter() -> Adapter:
    """The venue, behind the protocol. Overridden in tests so nothing reaches the network.

    **`/expiries` and `/chain` are answered from here since #37**, not from a
    `DeltaClient` beside it. The two REST reads are on `adapters.base.Adapter` because a
    venue's client *is* part of knowing it: a second venue answers the same two questions
    from its
    own snapshot, and a route that reached for `app.state.delta` would be naming Delta in
    the one layer that must not. #36 put the reads on the adapter and left the routes
    where they were, which was the expand half; this is the contract half.
    """
    return app.state.adapter


def _report_finished_task(task: asyncio.Task) -> None:
    """Say something when a background task ends. It should never end on its own.

    `DeltaFeed.run` returns normally once its retry budget is exhausted, and a task that
    simply finishes raises nothing — so without this the feed can give up and the only
    symptom is `/ws/chain` reporting `waiting` forever while `/health` still says ok. An
    exception is worse: Python surfaces it as a "never retrieved" warning at garbage
    collection, which may never reach the log anyone is reading.
    """
    if task.cancelled():
        return  # shutdown, which is the one legitimate way for these to end
    log_event(
        logger,
        logging.ERROR,
        log_events.ENGINE_ERROR,
        "background task %s ended unexpectedly: %s",
        task.get_name(),
        task.exception() or "returned without raising",
    )


def get_computed_store() -> BarStore:
    """The table-C store `/smile` reads. Overridden in tests, which build their own.

    The **writer's** store, not a fresh one, and that is the whole point of the seam:
    the writer's buffer holds every minute sealed since the last five-minute flush, and
    a `/smile` that read only the files would hand the screen a right edge up to a full
    interval behind the live curve.

    A process with no writer — the lifespan has not run, which is every test that does
    not override this — still gets a reader over whatever is on disk, because "the
    engine is not collecting" and "the endpoint is broken" are different facts.
    """
    writer = getattr(app.state, "writer", None)
    if writer is not None:
        return writer.computed_store
    return BarStore(dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA)


def get_bar_writer() -> BarWriter:
    """The writer `/recording` reports on and switches. Sibling of the store seam above.

    **503 rather than a default when there is none.** A process without a writer is not
    a process that is paused; it is one where the question has no answer, and reporting
    `false` would tell a reader that recording is off and can be switched on when
    neither is true. The lifespan builds the writer unconditionally — whether or not the
    live feed runs — so the only process this can happen in is one whose lifespan never
    ran, which is every test that does not enter `TestClient` as a context manager.
    """
    writer = getattr(app.state, "writer", None)
    if writer is None:
        raise HTTPException(
            status_code=503,
            detail="the engine has no bar writer; recording state is unknown",
        )
    return writer


def get_watched_stream() -> ChainStream | None:
    """The chain cache for `/health`, or `None` in a process whose lifespan never ran.

    A sibling of `get_supervisor` and `None` for the same reason: `/health` is what a
    monitor hits to find out whether anything is wrong, and a report that 500s because
    there is no chain cache to describe tells it the engine is down when it is up. It is
    deliberately **not** `get_chain_stream`, which raises: that one serves `/ws/chain`,
    where a missing cache genuinely is a failure, and the tests override it with a
    hand-fed stream that has nothing to do with what the running app is solving.
    """
    return getattr(app.state, "stream", None)


def get_chain_stream() -> ChainStream:
    """Overridden in tests, which feed the stream by hand instead of over a socket."""
    return app.state.stream


class StoreVolatilitySource:
    """The volatility screen's read path: two tables, read lazily, converted once.

    A named object rather than two loose calls so the whole of it can be replaced in a
    test with something that holds bars in a list — `/volatility` must be exercised
    without a Parquet tree, and the suite must not learn to build one to test a
    query string.
    """

    def __init__(self, root: Any = None) -> None:
        self.spot = BarStore(root, dataset=SPOT_DATASET, schema=SPOT_SCHEMA)
        self.index = BarStore(root, dataset=INDEX_DATASET, schema=INDEX_SCHEMA)
        self.computed = BarStore(
            root, dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA
        )

    def spot_bars(self, underlying: str, **kwargs: Any) -> Any:
        return read_spot_bars(self.spot, underlying, **kwargs)

    def index_bars(self, underlying: str, **kwargs: Any) -> Any:
        """#54. Empty until `tools/backfill_index_bars.py` has been run."""
        return read_index_bars(self.index, underlying, **kwargs)

    def contract_ivs(self, underlying: str, **kwargs: Any) -> Any:
        return read_contract_ivs(self.computed, underlying, **kwargs)

    def live_contract_ivs(self, underlying: str) -> Any:
        """The chain cache's solved ladders as one more implied minute — the live edge.

        The store flushes every five minutes, so without this the implied series stops
        at the last flush and a screen showing "now" can be five minutes stale. The
        cache holds one frame per contract and no history, so this adds exactly one
        minute and could never add more.
        """
        stream = getattr(app.state, "stream", None)
        if stream is None:
            return {}
        chains = [
            chain
            for chain in stream.computed_chains()
            if chain.underlying == underlying
        ]
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return contract_ivs_from_chains(chains, at=minute)


def _implied_rows(source: Any, underlying: str) -> Any:
    """Stored implied minutes, plus the live one if the cache has a solved ladder.

    Both routes call this rather than reading the store directly, so `/volatility/bounds`
    cannot compute a floor from one set of minutes while `/volatility` draws from
    another — the floor is a quantile over exactly these minutes, so a difference of one
    would be a difference in the answer.
    """
    rows = dict(source.contract_ivs(underlying))
    live = getattr(source, "live_contract_ivs", None)
    if live is not None:
        rows.update(live(underlying))
    return rows


def _realised_bars(source: Any, underlying: str) -> tuple[Any, str]:
    """The realised series and the name of the table it came from. **#54.**

    The venue's own index candles when they have been backfilled, ours otherwise —
    **one source end to end, never a mixture.** R1 measured the two disagreeing on the
    per-minute range on 16 of 16 overlapping minutes (`docs/index-history.md` §5), so a
    rolling window straddling a seam between them would return an answer that depended
    on where it happened to fall, with nothing on the row to attribute it to. Preferring
    the index is the same finding read the other way: ours are narrower on every minute
    compared, consistent with the discretisation bias in `docs/iv-vs-rv.md` §4.

    Both routes call this rather than choosing for themselves, so `/volatility/bounds`
    cannot compute a range against one table while `/volatility` draws from the other.
    """
    reader = getattr(source, "index_bars", None)
    if reader is not None:
        bars = reader(underlying)
        if bars:
            return bars, INDEX_SOURCE
    return source.spot_bars(underlying), SPOT_SOURCE


def get_volatility_source() -> StoreVolatilitySource:
    """Overridden in tests, which hand the route bars instead of a directory tree."""
    return StoreVolatilitySource()


class HistoricalSource:
    """The historical chain's read path: quote, reference, computed and spot bars.

    Sibling of `StoreVolatilitySource`, for the same reason: a named object so the whole
    of it can be swapped in a test for four stores built on a `tmp_path`, without either
    route learning that a writer exists at all.
    """

    def __init__(
        self, quote: BarStore, reference: BarStore, computed: BarStore, spot: BarStore
    ) -> None:
        self.quote = quote
        self.reference = reference
        self.computed = computed
        self.spot = spot


def get_historical_source() -> HistoricalSource:
    """The writer's own four stores when a writer exists, so the buffer is included
    exactly as `/smile` includes it for table C — a parquet-only read would hand the
    slider's right edge a hole up to a flush interval wide. A process with no writer
    still gets readers over whatever is on disk; see `get_computed_store`.
    """
    writer = getattr(app.state, "writer", None)
    if writer is not None:
        return HistoricalSource(
            writer.store, writer.reference_store, writer.computed_store, writer.spot_store
        )
    return HistoricalSource(
        BarStore(),
        BarStore(dataset=REFERENCE_DATASET, schema=REFERENCE_SCHEMA),
        BarStore(dataset=COMPUTED_DATASET, schema=COMPUTED_SCHEMA),
        BarStore(dataset=SPOT_DATASET, schema=SPOT_SCHEMA),
    )


def get_supervisor() -> FeedSupervisor | None:
    """The supervisor, or `None` in a process whose lifespan never ran.

    `None` rather than a 503, which is the opposite call to `get_bar_writer`'s and for
    a reason: `/health` is the route a monitor hits to find out whether anything is
    wrong, and a health check that fails because there is no feed to describe tells the
    monitor the engine is down when it is up and merely not recording. So the report is
    still given, with no adapters in it and `feed` reading `stopped` — which is exactly
    true of a process with no feed.
    """
    return getattr(app.state, "supervisor", None)


def get_feed_cache() -> FeedConnectionCache | None:
    """The badge's own source, sibling of `get_supervisor`. `None` for the same reason:
    a process whose lifespan never ran has no cache to read, and `/ws/chain` simply
    sends no `feed` message rather than raising — see `live_chain`."""
    return getattr(app.state, "feed_cache", None)


@app.get("/health", response_model=HealthReport)
async def health(
    supervisor: Annotated[FeedSupervisor | None, Depends(get_supervisor)],
    stream: Annotated[ChainStream | None, Depends(get_watched_stream)],
) -> HealthReport:
    """Liveness **and** readiness, and the difference between them.

    This route used to answer `{"status": "ok"}` and mean the first while being read as
    the second: a process whose socket died at 02:00 answered `ok` all night. `status` is
    still there and still means liveness — nothing that reads it breaks — and everything
    beside it is readiness, per adapter and rolled up into `feed`. The shape is
    `models.HealthReport`, and it is the seam #40's badge, #41's commands and #44's
    watched set all read through.
    """
    report = (
        HealthReport(feed=ConnectionState.STOPPED)
        if supervisor is None
        else supervisor.report()
    )
    if stream is not None:
        # #44's watched set. Built here rather than in `supervisor.report()` because the
        # supervisor owns connections and knows nothing about a chain cache — and what
        # is being solved is not a property of any adapter.
        report.watched = [
            WatchedPair(
                underlying=underlying,
                expiry=expiry,
                viewers=viewers,
                grace_remaining_seconds=grace,
            )
            for underlying, expiry, viewers, grace in stream.watching()
        ]
    return report


#: The three verbs, in the order they read. The same tuple the catalogue's
#: `ControlCommand.command` literal fixes — kept here as a plain tuple so the route can
#: name the one it refused rather than handing back pydantic's own message about a
#: literal, which is about a type and not about a feed.
FEED_COMMANDS = ("pause", "resume", "reconnect")


@app.post("/feed/{adapter}/{command}", response_model=AdapterHealth)
async def feed_command(
    adapter: str,
    command: str,
    supervisor: Annotated[FeedSupervisor | None, Depends(get_supervisor)],
) -> AdapterHealth:
    """Pause, resume or reconnect one adapter. **The engine's second mutating route.**

    The route is thin on purpose: it checks the two names, builds one `control.command`
    and hands it to the supervisor, which puts it on the bus and gives it to the
    controller that owns the adapter. Everything a command *does* is the controller's,
    and `docs/design/lld/commands.md` is where it is written down.

    **It answers with the adapter's health line after the command has been applied, not
    before.** The ticket asks which of the two this is, because the report a route
    returns for an event delivered through a queue is a report from before the effect —
    a `POST .../pause` answering `connected` is technically true and reads as a failure.
    Delivery here is **synchronous**, in the same call, for the reason #40's
    `FeedConnectionCache` is: a consumer task on the market-data bus would be draining
    roughly 1,300 messages a second to catch an event that arrives a few times a day. So
    there is no window to wait out and nothing to poll — the three verbs are each a flag
    and a transition, none of them blocks, and by the time this returns the state has
    already moved. What has *not* finished is what happens next: `resume` answers
    `connecting`, truthfully, and the dial that follows it takes as long as it takes.

    Unknown adapter is a **404** naming it, because the thing addressed does not exist.
    Unknown verb is a **422** naming it, because the address is fine and the instruction
    is not. Both are checked before anything is published: a command nobody can carry out
    must not reach the bus, where a later reader would find it and assume it happened.
    """
    names = supervisor.names() if supervisor is not None else []
    matched = next((name for name in names if name.upper() == adapter.upper()), None)
    if supervisor is None or matched is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no adapter named {adapter!r}; this engine runs "
                f"{', '.join(names) if names else 'none'}"
            ),
        )
    if command not in FEED_COMMANDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{command!r} is not a feed command; use "
                f"{', '.join(FEED_COMMANDS[:-1])} or {FEED_COMMANDS[-1]}"
            ),
        )
    supervisor.command(
        ControlCommand(
            source="operator",
            ts_received=datetime.now(timezone.utc),
            adapter=matched,
            command=command,  # type: ignore[arg-type]  # checked against FEED_COMMANDS
        )
    )
    report = supervisor.report()
    return next(line for line in report.adapters if line.adapter == matched)


@app.get("/expiries", response_model=ExpiriesResponse)
async def expiries(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    adapter: Annotated[Adapter, Depends(get_adapter)],
) -> ExpiriesResponse:
    """Every listed expiry for one underlying, ascending. Source of the dropdown."""
    symbol = _validated(normalise_underlying, underlying)
    listed = await _venue(adapter.expiries(symbol))
    if not listed.expiries:
        raise HTTPException(
            status_code=404, detail=f"the venue lists no option contracts for {symbol}"
        )
    return listed


@app.get("/chain", response_model=ChainResponse)
async def chain(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    expiry: Annotated[str, Query(description="DD-MM-YYYY, as the venue spells it")],
    adapter: Annotated[Adapter, Depends(get_adapter)],
) -> ChainResponse:
    """The pivoted ladder for one underlying and one expiry."""
    symbol = _validated(normalise_underlying, underlying)
    date = _validated(validate_expiry, expiry)
    snapshot = await _venue(adapter.chain_snapshot(symbol, date))
    if not snapshot.rows:
        raise HTTPException(
            status_code=404,
            detail=f"the venue lists no option contracts for {symbol} expiring {date}",
        )
    # Enriched here and not in the adapter: our implied volatility and Greeks are the
    # pricing core's work, and an adapter returning them would be answering a question
    # about us. Enriched at all so the two transports return the same populated shape —
    # a REST reader given null Greeks where the websocket sends real ones would be
    # reading a different contract.
    return enrich(snapshot)


@app.get("/volatility/bounds", response_model=BoundsResponse)
async def volatility_bounds(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    source: Annotated[StoreVolatilitySource, Depends(get_volatility_source)],
    interval: Annotated[str, Query(description="sampling interval")] = "1m",
) -> BoundsResponse:
    """What `N` may be, before anyone has chosen one.

    A store holding nothing usable answers 200 with `usable: false` rather than an error.
    "No lookback works yet" is a real answer the screen can print, and printing it is the
    difference between an instrument that is honest about its range and one that looks
    broken for the first month of recording.
    """
    symbol = _validated(normalise_underlying, underlying)
    if interval not in INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"interval must be one of {', '.join(INTERVALS)}, not {interval!r}",
        )
    bars, _ = _realised_bars(source, symbol)
    bounds = lookback_bounds(
        spot_bars=bars,
        iv_rows=_implied_rows(source, symbol),
        interval=INTERVALS[interval],
    )
    return BoundsResponse(
        min_days=bounds.min_days,
        max_days=bounds.max_days,
        binding=bounds.binding,
        detail=bounds.detail,
        usable=bounds.usable,
        intervals=list(INTERVALS),
    )


@app.get("/volatility", response_model=VolatilitySeries)
async def volatility(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    lookback_days: Annotated[float, Query(description="N: drives both series")],
    source: Annotated[StoreVolatilitySource, Depends(get_volatility_source)],
    interval: Annotated[str, Query(description="sampling interval")] = "1m",
    estimators: Annotated[str, Query(description="comma-separated")] = ",".join(
        ESTIMATORS
    ),
    alignment: str = "contemporaneous",
    max_points: int = MAX_POINTS,
) -> VolatilitySeries:
    """Implied and realised volatility over one lookback, in the units the chart draws.

    `lookback_days` is `N`, and it is deliberately one parameter rather than two: it sets
    the realised lookback *and* the implied tenor, so the two lines always describe the
    same length of time. Two parameters would make a ten-minute realised volatility
    against a thirty-day implied one expressible, and the difference between them would
    look like a signal.

    A lookback outside the computed bounds is a **400 naming the binding constraint**,
    not an empty chart — in the first month of recording the bound moves every day and a
    screen that cannot say why it refused looks broken rather than honest.
    """
    symbol = _validated(normalise_underlying, underlying)

    if interval not in INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"interval must be one of {', '.join(INTERVALS)}, not {interval!r}",
        )
    wanted = [name.strip() for name in estimators.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in ESTIMATORS]
    if unknown or not wanted:
        raise HTTPException(
            status_code=400,
            detail=(
                f"estimators must be drawn from {', '.join(ESTIMATORS)}; "
                f"got {unknown or 'nothing'}"
            ),
        )
    if alignment not in ALIGNMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"alignment must be one of {', '.join(ALIGNMENTS)}",
        )

    step_interval = INTERVALS[interval]
    bars, realised_source = _realised_bars(source, symbol)
    iv_rows = _implied_rows(source, symbol)
    bounds = lookback_bounds(
        spot_bars=bars, iv_rows=iv_rows, interval=step_interval
    )

    if not bounds.usable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"no lookback works yet at {interval} sampling: the lower bound is "
                f"{bounds.min_days:.2f} days and the upper is {bounds.max_days:.2f}. "
                f"{bounds.detail}"
            ),
        )
    if not bounds.min_days <= lookback_days <= bounds.max_days:
        side = "below the lower" if lookback_days < bounds.min_days else "above the upper"
        raise HTTPException(
            status_code=400,
            detail=(
                f"lookback_days={lookback_days} is {side} bound "
                f"[{bounds.min_days:.2f}, {bounds.max_days:.2f}]. {bounds.detail}"
            ),
        )

    lookback = timedelta(days=lookback_days)
    if not bars:
        raise HTTPException(
            status_code=404, detail=f"no spot bars stored for {symbol}"
        )

    # Contemporaneous needs a full window behind the first point; lag needs a full one
    # ahead of the last. Either way the range is the part of the record that can answer.
    first, last = bars[0].at, bars[-1].at
    start = first + lookback if alignment == "contemporaneous" else first
    end = last

    # **The range starts where a comparison becomes possible, not where the bars do.**
    #
    # Realised is backfilled — thirty days of index candles and growing. Implied cannot
    # be: Delta's history carries no IV and no bid/ask, so it exists only for minutes
    # this engine was running. Drawing the union means twenty-five days of chart on which
    # one of the two lines cannot exist, and the cost is not merely blank space. The route
    # thins the range to at most `MAX_POINTS`, so a span dominated by realised-only days
    # sets a stride of twenty-one minutes and the implied minutes that *do* exist fall
    # between the plotted points — the series arrives as a handful of dots and reads as a
    # broken instrument rather than as a young record.
    #
    # Clamping to the first implied minute costs a reader nothing they could have used
    # and buys every implied minute a timestamp near enough to reach it.
    implied_minutes = sorted(iv_rows)
    if implied_minutes:
        start = max(start, implied_minutes[0])
    if start > end:
        raise HTTPException(
            status_code=400,
            detail=(
                f"a {lookback_days}-day window does not fit in the "
                f"{(last - first).days}-day record held"
            ),
        )

    span = end - start
    steps = max(1, int(span / step_interval))
    stride = max(1, -(-steps // max(1, max_points)))

    return volatility_series(
        spot_bars=bars,
        realised_source=realised_source,
        iv_rows=iv_rows,
        lookback=lookback,
        interval=step_interval,
        estimators=wanted,
        alignment=alignment,
        start=start,
        end=end,
        step=stride * step_interval,
        underlying=symbol,
        bounds=bounds,
    )


def _validated(check: Callable[[str], str], value: str) -> str:
    try:
        return check(value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _venue(awaitable):
    """Await one of the adapter's REST reads, turning an unreachable venue into a 502.

    The adapter raises `DeltaUnavailable` from inside its own client; a route's job is to
    say so in HTTP rather than to leak a 500 with a traceback. It is deliberately not
    caught deeper: a caller that wanted the exception — `start_feed_stack` does — must
    still be able to see it.
    """
    try:
        return await awaitable
    except DeltaUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/smile", response_model=SmileResponse)
def smile(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    expiry: Annotated[str, Query(description="DD-MM-YYYY, as Delta spells it")],
    store: Annotated[BarStore, Depends(get_computed_store)],
) -> SmileResponse:
    """Every stored minute of implied volatility for one expiry. `docs/smile-contract.md`.

    Reads the local store and never Delta, so there is no 502 and no 404 here: an
    underlying nobody has collected and an expiry nobody has stored both answer 200 with
    an empty series.

    **`def`, not `async def`, and that is the whole reason this route looks different
    from the two above it.** Those await a network client and yield the loop while they
    wait; this one opens Parquet files, which blocks. FastAPI runs a plain `def` route in
    a worker thread, so the read stays off the event loop the Delta feed's socket reader
    lives on — the same rule `BarWriter` follows for its flush, and for the same reason: a
    blocked reader fills the receive buffer and gets the process disconnected. The read is
    `measured` at 6.8 ms for a day of one expiry and `derived` at roughly 88 ms against
    the five-minute flush layout, which is far too long to hold the loop.
    """
    symbol = _validated(normalise_underlying, underlying)
    date = _validated(validate_expiry, expiry)
    return read_smile(store, symbol, date)


#: `2026-09-04T09:00:00Z`. Matches `historical.MINUTE_FORMAT` and `smile.MINUTE_FORMAT`.
_MINUTE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _validated_date(value: str) -> Date:
    """`YYYY-MM-DD`, the store's own partition spelling — not Delta's `DD-MM-YYYY`,
    which `expiry` already carries on this route. Malformed is a 400, not a 422: the
    same disposition `_validated` gives every other query parameter here."""
    try:
        return Date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"date must be YYYY-MM-DD; got {value!r}"
        ) from exc


def _validated_minute(value: str) -> datetime:
    """ISO 8601 UTC, second precision, `Z`-suffixed — the one spelling `smile` and the
    scrubber both use, so a stamp taken from either travels here unchanged."""
    try:
        return datetime.strptime(value, _MINUTE_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"minute must be YYYY-MM-DDTHH:MM:SSZ; got {value!r}",
        ) from exc


@app.get("/chain/minutes", response_model=HistoricalMinutes)
def chain_minutes(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    expiry: Annotated[str, Query(description="DD-MM-YYYY, as Delta spells it")],
    date: Annotated[str, Query(description="YYYY-MM-DD, the store's own spelling")],
    source: Annotated[HistoricalSource, Depends(get_historical_source)],
) -> HistoricalMinutes:
    """Every minute the store holds quotes for, on one day.

    `docs/historical-chain-contract.md`.

    The slider's domain, and — by what is missing from an otherwise-contiguous run —
    its gaps. Reads the local store and never Delta, so absence is 200 and empty exactly
    as `/smile` treats it: a day nobody has lived through yet is "nothing yet", not a 404.

    `def`, not `async def`, for the reason `/smile` gives: this opens Parquet files,
    which blocks, and FastAPI runs a plain `def` route off the event loop the feed's
    socket reader lives on.
    """
    symbol = _validated(normalise_underlying, underlying)
    expiry_date = _validated(validate_expiry, expiry)
    day = _validated_date(date)
    return HistoricalMinutes(
        underlying=symbol,
        expiry=expiry_date,
        date=date,
        minutes=list_minutes(source.quote, symbol, expiry_date, day),
    )


@app.get("/chain/at")
def chain_at(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    expiry: Annotated[str, Query(description="DD-MM-YYYY, as Delta spells it")],
    minute: Annotated[
        str,
        Query(description="ISO 8601 UTC, second precision, e.g. 2026-09-04T09:00:00Z"),
    ],
    source: Annotated[HistoricalSource, Depends(get_historical_source)],
) -> dict[str, Any]:
    """The ladder as it stood at one stored minute. `docs/historical-chain-contract.md`.

    Same envelope `/ws/chain` sends, so a client that already reads `chain`/`waiting`
    needs no third vocabulary to read this:

        {"type": "chain",   "data": {...ChainResponse, "minute": "..."}}
        {"type": "waiting", "detail": "..."}

    **`waiting`, never a neighbouring minute's rows.** A minute nobody quoted answers
    `waiting` exactly as an expiry nobody has pushed a live frame for does — the same
    "nothing here yet" the websocket already spells, for the same reason: an empty
    ladder and a ladder that was never asked for look identical on screen, and only one
    of them is what the store actually holds.
    """
    symbol = _validated(normalise_underlying, underlying)
    expiry_date = _validated(validate_expiry, expiry)
    when = _validated_minute(minute)
    ladder = read_ladder_at(
        source.quote,
        source.reference,
        source.computed,
        source.spot,
        symbol,
        expiry_date,
        when,
    )
    if ladder is None:
        return {
            "type": "waiting",
            "detail": f"no stored quotes for {symbol} expiring {expiry_date} at {minute}",
        }
    return {"type": "chain", "data": ladder.model_dump(mode="json")}


@app.get("/bars", response_model=ContractBarsResponse)
def bars(
    instrument: Annotated[
        str,
        Query(description="canonical string, e.g. DELTA-BTC-20260627-60000-C"),
    ],
    date: Annotated[str, Query(description="YYYY-MM-DD, the store's own spelling")],
    source: Annotated[HistoricalSource, Depends(get_historical_source)],
) -> ContractBarsResponse:
    """One contract's minute bars for one date, addressed by canonical string.
    `docs/bars-contract.md`.

    Reads `quote-bars` and `reference-bars` and never Delta, so there is no 502 and no
    404 here: a contract this store never recorded and a day nobody has lived through
    both answer 200 with an empty `bars` — the same disposition `/smile` and `/chain/at`
    take for their own kind of nothing-yet.

    `def`, not `async def`, for the reason `/smile` and the historical routes give: this
    opens Parquet files, which blocks, and FastAPI runs a plain `def` route off the event
    loop the feed's socket reader lives on.
    """
    try:
        parsed = Instrument.from_canonical(instrument)
    except InstrumentParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Normalised — case-insensitively, and rejected outside BTC/ETH — before it ever
    # reaches the store, exactly as every other route's `underlying` query parameter is.
    # `parsed` itself is left alone; the normalised value is what both the store filter
    # and the echoed response use, so the two cannot disagree about which underlying
    # answered.
    symbol = _validated(normalise_underlying, parsed.underlying)
    normalised = parsed.model_copy(update={"underlying": symbol})
    day = _validated_date(date)
    return ContractBarsResponse(
        instrument=normalised.canonical(),
        underlying=symbol,
        expiry=normalised.expiry.strftime("%d-%m-%Y"),
        date=date,
        bars=read_contract_bars(source.quote, source.reference, normalised, day),
    )


def _recording_state(writer: BarWriter) -> RecordingState:
    """One shape, built once, so `GET` and `POST` cannot drift into two."""
    return RecordingState(
        recording=writer.recording,
        buffered_rows=sum(store.buffered for store in writer.stores),
        rows_written=sum(store.rows_written for store in writer.stores),
    )


@app.get("/recording", response_model=RecordingState)
def recording_state(
    writer: Annotated[BarWriter, Depends(get_bar_writer)],
) -> RecordingState:
    """Whether the store is writing, read from the engine. `docs/recording-contract.md`.

    **The state lives here and nowhere else.** Not in the browser and not in
    `localStorage`: two tabs must not be able to disagree about whether the store is
    writing, and a reader arriving on a fresh page is told the truth rather than a
    default.
    """
    return _recording_state(writer)


@app.post("/recording", response_model=RecordingState)
async def set_recording(
    body: RecordingRequest,
    writer: Annotated[BarWriter, Depends(get_bar_writer)],
) -> RecordingState:
    """Stop or start the store. **The engine's only mutating route.**

    Answers with the state *after* the change, so a client needs no second request and
    cannot render a state that was never true. Idempotent: posting `false` twice is not
    an error, and the second one flushes an already empty buffer.

    Switching off **flushes what is buffered before it stops** — the buffer holds up to
    a five-minute interval of sealed bars, and discarding them would throw away data the
    engine already has, which is the exact loss that interval exists to reduce. Switching
    off does **not** stop the writer draining its subscription; see `BarWriter.ingest`.

    Who may call it is answered deliberately in `docs/recording-contract.md` rather than
    left unexamined: anything that can reach the port, and the port is loopback.
    Authentication is named there and not built.
    """
    await writer.set_recording(body.recording)
    return _recording_state(writer)


#: Second precision, `Z`-suffixed — `chain.py`'s `fetched_at` spelling, and now `feed`'s
#: `since`. One format for every wall-clock stamp this engine puts on the wire.
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _feed_message(event: FeedConnection) -> dict[str, Any]:
    """A `FeedConnection` event, in the shape `docs/live-chain-contract.md` fixes."""
    return {
        "type": "feed",
        "data": {
            "adapter": event.adapter,
            "state": event.to_state.value,
            "since": event.ts_received.strftime(_TIMESTAMP_FORMAT),
            "reason": event.reason,
        },
    }


@app.websocket("/ws/chain")
async def live_chain(
    websocket: WebSocket,
    underlying: str,
    expiry: str,
    stream: Annotated[ChainStream, Depends(get_chain_stream)],
    supervisor: Annotated[FeedSupervisor | None, Depends(get_supervisor)],
    feed_cache: Annotated[FeedConnectionCache | None, Depends(get_feed_cache)],
    interval: float = PUSH_INTERVAL_SECONDS,
) -> None:
    """Push the chain for one underlying and expiry until the browser goes away.

    The payload is the same `ChainResponse` `/chain` returns, wrapped in an envelope so
    the four things the socket can say are distinguishable — `docs/live-chain-contract.md`
    is the authority:

        {"type": "chain",   "data": {...}}   here is the ladder
        {"type": "waiting", "detail": "..."} nothing has arrived for this expiry yet
        {"type": "error",   "detail": "..."} the request cannot ever succeed
        {"type": "feed",    "data": {...}}   the venue connection's own state — #40

    **`waiting` is not an empty chain.** A `ChainResponse` with no rows renders as a
    blank ladder and reads as "Delta lists nothing", when the truth is that the socket
    has not spoken yet.

    A websocket cannot return 400, so a bad parameter is reported in an `error` message
    before closing. Closing silently would leave the browser reconnecting forever
    against a request that can never work.

    **`feed` is unrelated to this connection succeeding or not.** It reports the
    engine's own socket to Delta, read off `feed_cache` — see that class for why a cache
    rather than a fresh bus subscription — for whichever adapter the supervisor holds.
    `None` (no supervisor, no cache, or a supervisor with nothing registered — every test
    that does not override `get_supervisor`) means the caller cannot answer the question,
    and the socket simply never sends `feed`, exactly as it always behaved before this
    ticket.
    """
    await websocket.accept()
    interval = max(interval, MIN_PUSH_INTERVAL_SECONDS)

    # **Debug, both directions, bracketing the whole connection.** A browser tab opening
    # and closing is routine — #42 rules this off info precisely because volume is what
    # turns a log into noise nobody reads — and on no per-message path either: once per
    # connection, not once per push. Logged against the **raw** query values, because a
    # refused handshake (below) never gets as far as parsing them, and attach/detach
    # must still bracket that connection or a run of bad requests looks like a run that
    # never closed.
    log_event(
        logger,
        logging.DEBUG,
        log_events.WS_CLIENT_ATTACH,
        "ws /ws/chain attached: %s %s",
        underlying,
        expiry,
        underlying=underlying,
        expiry=expiry,
    )
    try:
        try:
            symbol = normalise_underlying(underlying)
            date = validate_expiry(expiry)
        except ValidationError as exc:
            await websocket.send_json({"type": "error", "detail": str(exc)})
            await websocket.close()
            return

        # **One adapter today** — see `docs/live-chain-contract.md`'s "one adapter
        # today". `underlying`/`expiry` do not select among adapters because there is
        # only the one to choose from; a second venue would need this to change.
        adapter_name: str | None = None
        if supervisor is not None and supervisor.controllers:
            adapter_name = supervisor.controllers[0].adapter_name

        #: The state last actually sent to *this* connection, so a `feed` message goes
        #: out only when it says something new — see `docs/live-chain-contract.md`'s
        #: "Coalescing". `None` before the first one, which is distinct from every real
        #: `ConnectionState.value` and so cannot be mistaken for one already sent.
        last_sent_state: str | None = None

        async def push_feed_update() -> None:
            nonlocal last_sent_state
            if adapter_name is None or feed_cache is None:
                return
            event = feed_cache.get(adapter_name)
            if event is None or event.to_state.value == last_sent_state:
                return
            last_sent_state = event.to_state.value
            await websocket.send_json(_feed_message(event))

        # **Interest, registered on accept and released on close.** This is what puts
        # the pair in the 100 ms live pass and on `/health`'s watched set; an expiry no
        # connection has registered is solved once a minute and not otherwise. Taken
        # *after* the parameters are validated, because a pair spelled wrongly is not a
        # pair and would sit in the watched set for its grace saying the engine was
        # solving something it cannot.
        stream.watch(symbol, date)
        try:
            while True:
                # Before `chain`/`waiting`, every pass — including the first, which is
                # what puts `feed` ahead of the very first ladder on a fresh connection.
                await push_feed_update()

                chain = stream.chain(symbol, date)
                if chain is None:
                    await websocket.send_json(
                        {
                            "type": "waiting",
                            "detail": f"no live quotes yet for {symbol} expiring {date}",
                        }
                    )
                else:
                    await websocket.send_json(
                        {"type": "chain", "data": chain.model_dump(mode="json")}
                    )
                await asyncio.sleep(interval)
        except WebSocketDisconnect:
            # The browser closed the tab. Ordinary, not a failure — and nothing to
            # clean up, because this connection owns no subscription of its own.
            return
        finally:
            # **However this connection ends, its interest goes.** A `return` above, a
            # disconnect, a cancelled task or an exception all land here, and a release
            # that did not happen would pin an expiry into the live pass for the life of
            # the process — the exact cost this ticket exists to stop paying. The pair
            # is not dropped: the grace starts, so reopening it is instant.
            stream.unwatch(symbol, date)
    finally:
        # However the connection ended — a bad parameter, a disconnect, or anything
        # else — the attach above gets its other half. `finally` runs on every `return`
        # above too.
        log_event(
            logger,
            logging.DEBUG,
            log_events.WS_CLIENT_DETACH,
            "ws /ws/chain detached: %s %s",
            underlying,
            expiry,
            underlying=underlying,
            expiry=expiry,
        )
