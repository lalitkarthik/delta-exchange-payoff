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

**Both channels the feed subscribes are stored, into three tables.** `ob_l2` becomes the
quote bars; `ticker` becomes the reference bars and the spot bars, and also supplies the
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

from .chain import (
    ValidationError,
    build_chain,
    build_expiries,
    normalise_underlying,
    validate_expiry,
)
from .compute import enrich
from .delta_client import DeltaClient, DeltaUnavailable
from .fanout import FanOut
from .feed import DeltaFeed
from .models import (
    ChainResponse,
    ExpiriesResponse,
    RecordingRequest,
    RecordingState,
    SmileResponse,
)
from .smile import read_smile
from .store import COMPUTED_DATASET, COMPUTED_SCHEMA, BarStore, BarWriter
from .stream import ChainStream, recompute_forever

#: The Next.js dev server. Development only; production origins are a deploy concern.
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]

#: Note that CORS does **not** cover `/ws/chain`. A websocket handshake is not subject to
#: it, so that route accepts any origin. The payload is public Delta market data, the
#: server binds to loopback and no credentials are involved, so the exposure is small
#: today — but it stops being small the moment this binds to a non-loopback interface or
#: the payload carries anything user-specific, and neither needs a code change here.

logger = logging.getLogger(__name__)

#: How often a connected browser is sent the chain. One second is well under what anyone
#: reads and far above what the eye needs, and it is one JSON push regardless of how many
#: messages arrived underneath. **Measured**: a 136-symbol chain on `ob_l2` delivers about
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

#: Underlyings the live feed subscribes at start-up. Every listed BTC option, both
#: channels — about 600 messages and 300 KB a second, measured. That buys instant expiry
#: switching with no subscribe round trip. Narrowing `ob_l2` to the watched expiry would
#: cut it to roughly a third; see `docs/ingestion.md`.
LIVE_UNDERLYINGS = ("BTC",)

#: Environment switch: set to "0" to serve the REST endpoints and the websocket without
#: opening a socket to Delta. Read at start-up rather than at import, so a test can set
#: it — the suite sets it in `conftest.py`, because nothing in it may touch the network.
LIVE_FEED_ENV = "DELTA_LIVE_FEED"


def live_feed_enabled() -> bool:
    return os.environ.get(LIVE_FEED_ENV, "1") != "0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """One HTTP client, one websocket to Delta, one chain cache, for the whole process.

    The feed is started here rather than per request for the reason #3 gives: three
    consumers each opening their own connection would burn the 150-per-5-minutes budget
    and give three inconsistent views of one market.
    """
    client = DeltaClient()
    await client.__aenter__()
    app.state.delta = client

    app.state.fanout = FanOut()
    app.state.stream = ChainStream()
    app.state.stream.attach(app.state.fanout)
    # The writer is attached whether or not the feed runs, so `/health`-adjacent
    # introspection and the tests can see the subscription exists and is lossless. With
    # no feed nothing is published, so an undrained queue costs nothing.
    # Table C is **sampled from the chain cache**, not folded from the bus, because our
    # implied volatility and Greeks are produced by the recompute loop rather than
    # arriving on the wire. The writer is handed the stream's reader, not the stream, so
    # the store never learns that a chain cache exists.
    app.state.writer = BarWriter(BarStore(), chains=app.state.stream.computed_chains)
    app.state.writer.attach(app.state.fanout)
    app.state.feed = DeltaFeed(app.state.fanout)
    app.state.tasks = []

    if live_feed_enabled():
        try:
            for underlying in LIVE_UNDERLYINGS:
                symbols = [
                    row["symbol"] for row in await client.tickers(underlying, None)
                ]
                app.state.feed.subscribe("ticker", symbols)
                app.state.feed.subscribe("ob_l2", symbols)
            app.state.tasks = [
                asyncio.create_task(app.state.feed.run(), name="delta-feed"),
                asyncio.create_task(app.state.stream.run(), name="chain-stream"),
                asyncio.create_task(
                    recompute_forever(app.state.stream), name="chain-recompute"
                ),
                asyncio.create_task(app.state.writer.run(), name="bar-writer"),
            ]
            for task in app.state.tasks:
                task.add_done_callback(_report_finished_task)
        except DeltaUnavailable:
            # The REST endpoints still work and the websocket reports "waiting". A
            # start-up that dies because Delta was briefly unreachable is worse than one
            # that comes up degraded and says so.
            pass

    try:
        yield
    finally:
        for task in app.state.tasks:
            task.cancel()
        if app.state.tasks:
            await asyncio.gather(*app.state.tasks, return_exceptions=True)
            # After the cancellations, not inside them. The open minute is a real
            # observation and is written with its true tick counts rather than
            # discarded for tidiness; doing it here rather than from inside the
            # cancelled task means the flush is not itself racing a cancellation.
            try:
                await app.state.writer.aclose()
            except Exception:
                # A failed final flush costs the open minute and nothing else. It must
                # not take the shutdown with it and leave the HTTP client unclosed.
                logger.exception("the final bar flush failed")
        await client.aclose()


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


def get_delta_client() -> DeltaClient:
    """Overridden in tests so nothing here ever reaches the network."""
    return app.state.delta


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
    logger.error(
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


def get_chain_stream() -> ChainStream:
    """Overridden in tests, which feed the stream by hand instead of over a socket."""
    return app.state.stream


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Says nothing about Delta."""
    return {"status": "ok"}


@app.get("/expiries", response_model=ExpiriesResponse)
async def expiries(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    delta: Annotated[DeltaClient, Depends(get_delta_client)],
) -> ExpiriesResponse:
    """Every listed expiry for one underlying, ascending. Source of the dropdown."""
    symbol = _validated(normalise_underlying, underlying)
    tickers = await _fetch(delta, symbol, None)
    if not tickers:
        raise HTTPException(
            status_code=404, detail=f"Delta lists no option contracts for {symbol}"
        )
    return build_expiries(symbol, tickers)


@app.get("/chain", response_model=ChainResponse)
async def chain(
    underlying: Annotated[str, Query(description="BTC or ETH")],
    expiry: Annotated[str, Query(description="DD-MM-YYYY, as Delta spells it")],
    delta: Annotated[DeltaClient, Depends(get_delta_client)],
) -> ChainResponse:
    """The pivoted ladder for one underlying and one expiry."""
    symbol = _validated(normalise_underlying, underlying)
    date = _validated(validate_expiry, expiry)
    tickers = await _fetch(delta, symbol, date)
    if not tickers:
        raise HTTPException(
            status_code=404,
            detail=f"Delta lists no option contracts for {symbol} expiring {date}",
        )
    # Enriched here as well as on the live path, so the two transports return the
    # same populated shape. A REST reader that got null Greeks where the websocket
    # sends real ones would be reading a different contract.
    return enrich(build_chain(symbol, date, tickers))


def _validated(check: Callable[[str], str], value: str) -> str:
    try:
        return check(value)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _fetch(
    delta: DeltaClient, underlying: str, expiry: str | None
) -> list[dict[str, Any]]:
    try:
        return await delta.tickers(underlying, expiry)
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


@app.websocket("/ws/chain")
async def live_chain(
    websocket: WebSocket,
    underlying: str,
    expiry: str,
    stream: Annotated[ChainStream, Depends(get_chain_stream)],
    interval: float = PUSH_INTERVAL_SECONDS,
) -> None:
    """Push the chain for one underlying and expiry until the browser goes away.

    The payload is the same `ChainResponse` `/chain` returns, wrapped in an envelope so
    the three things the socket can say are distinguishable:

        {"type": "chain",   "data": {...}}   here is the ladder
        {"type": "waiting", "detail": "..."} nothing has arrived for this expiry yet
        {"type": "error",   "detail": "..."} the request cannot ever succeed

    **`waiting` is not an empty chain.** A `ChainResponse` with no rows renders as a
    blank ladder and reads as "Delta lists nothing", when the truth is that the socket
    has not spoken yet.

    A websocket cannot return 400, so a bad parameter is reported in an `error` message
    before closing. Closing silently would leave the browser reconnecting forever
    against a request that can never work.
    """
    await websocket.accept()
    interval = max(interval, MIN_PUSH_INTERVAL_SECONDS)
    try:
        symbol = normalise_underlying(underlying)
        date = validate_expiry(expiry)
    except ValidationError as exc:
        await websocket.send_json({"type": "error", "detail": str(exc)})
        await websocket.close()
        return

    try:
        while True:
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
        # The browser closed the tab. Ordinary, not a failure — and nothing to clean up,
        # because this connection owns no subscription of its own.
        return
