"""One controller per adapter, one lifetime, one answer for all of them.

**The failure this exists to refuse.** The whiteboard has many adapters behind one
feed-management box, and one health call has to answer for the box. Without something
that owns them all, "is the feed up?" becomes "is *an* adapter up?", and the answer a
caller gets depends on which one it happened to ask — so a process with Delta connected
and a second venue stopped for an hour reports green, truthfully, about the half of
itself that is working.

So this holds every controller, starts and stops them with the application, and reports
the **worst** state among them. Worst rather than first, or an average, or a count: a
feed is only as good as its sickest connection, and the number an operator acts on is the
one that says something is wrong. `docs/design/lld/supervisor.md` is the design.

## What a supervisor is

The Erlang idea, minus the language: a process whose only job is the lifecycle of the
processes under it. It does not do the work, it does not know what the work means, and it
survives its children — which is the whole point, because something has to be alive to
answer when a child is not. Ours is deliberately the smallest version of that: **it
restarts nothing.** A controller already owns its own reconnect, with a budget and a
backoff, and a supervisor that restarted a controller whose budget was spent would be
undoing the one decision the budget exists to make. What this supervisor adds is
ownership, aggregation and a shutdown that actually detaches.

## Liveness and readiness

`/health` answered `{"status": "ok"}` and meant **liveness**: this process is running and
can serve a request. It was read as **readiness**: the market data is flowing. Those come
apart for hours at a time — a process whose socket died at 02:00 answers `ok` all night —
and the gap is exactly where a silent failure lives. The report this module builds
carries both: `status` is unchanged and still liveness, and `feed` beside it is
readiness. Nothing that reads the old field breaks, and nothing that reads the new one is
lied to.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta

from .adapters.base import Adapter
from .controller import ConnectionController
from .events import ConnectionState, ControlCommand, Event
from .models import AdapterHealth, HealthReport

logger = logging.getLogger(__name__)

#: **Worst last.** The explicit order the ticket asks for, `connected` down to `stopped`,
#: written as a tuple rather than left to the enum's declaration order so that adding a
#: sixth state cannot silently change what "worst" means.
#:
#: `degraded` sits above `connecting` because a degraded connection has delivered data
#: and has merely gone quiet, while a connecting one has delivered none yet; and
#: `reconnecting` above both because it is a connection known to be down rather than one
#: on its way up.
SEVERITY: tuple[ConnectionState, ...] = (
    ConnectionState.CONNECTED,
    ConnectionState.DEGRADED,
    ConnectionState.CONNECTING,
    ConnectionState.RECONNECTING,
    ConnectionState.STOPPED,
)

#: What a controller that has never been started reports as. `None` is not a sixth state
#: and must not become one on the wire: a connection nobody started is not running, which
#: is what `stopped` means, and a `/health` reader given `null` would have to invent a
#: rule for it.
UNSTARTED = ConnectionState.STOPPED


def worst(states: Iterable[ConnectionState | None]) -> ConnectionState:
    """The worst of the states, by `SEVERITY`. **`stopped` when there are none.**

    An empty supervisor is a process with no feed at all, which is the strongest possible
    "not ready" — reporting `connected` because there was nothing to disagree would be
    the plausible-and-wrong answer, and it is the one an `any()` or a `max()` over an
    empty sequence tends to produce.
    """
    ranked = [SEVERITY.index(state or UNSTARTED) for state in states]
    if not ranked:
        return ConnectionState.STOPPED
    return SEVERITY[max(ranked)]


class FeedSupervisor:
    """Owns one `ConnectionController` per adapter, for the life of the application."""

    def __init__(
        self,
        adapters: Sequence[Adapter],
        publish: Callable[[Event], None],
        *,
        controller_factory: Callable[..., ConnectionController] = ConnectionController,
        **controller_kwargs: object,
    ) -> None:
        """**Nothing is started here**, and nothing connects.

        Building is separated from starting for the reason `main.build_feed_stack` gives:
        a process with no live feed — every test, and any run with `DELTA_LIVE_FEED=0` —
        still has the whole structure present and introspectable, and `/health` can
        answer about it truthfully rather than raising.

        Constructing a `ConnectionController` **registers it on its adapter**, so the
        controllers exist and are listening from this moment; `aclose()` is what takes
        them back off, and it is why this class owns them rather than handing them out.
        """
        self._publish = publish
        self._controllers = [
            controller_factory(adapter, publish, **controller_kwargs)
            for adapter in adapters
        ]
        self._tasks: list[asyncio.Task] = []

    @property
    def controllers(self) -> list[ConnectionController]:
        """The controllers, in the configured order. For `/health` and #41."""
        return list(self._controllers)

    @property
    def state(self) -> ConnectionState:
        """The worst state among the controllers. **The feed's state.**"""
        return worst(controller.state for controller in self._controllers)

    def names(self) -> list[str]:
        """The adapters this supervisor runs, in configured order. For #41's route.

        A route that has to refuse an unknown adapter by name needs to know which names
        exist, and asking each controller for its own is the only place that is true —
        the configured list and the running controllers cannot disagree if there is one
        of them.
        """
        return [controller.adapter_name for controller in self._controllers]

    def command(self, event: ControlCommand) -> bool:
        """Put one `control.command` on the bus, then hand it to the adapter it names.

        **The bus first, the effect second**, so that a log read in order shows the cause
        before the `feed.connection` events it produced. Both happen inside this call —
        see `docs/design/lld/commands.md` §3 for why delivery is synchronous rather than
        a consumer task, and what that buys the route that publishes it.

        Returns whether any controller took it. `False` cannot happen through the route,
        which refuses an unknown adapter before publishing anything; it is here so that a
        second caller — a replay, a future broker — is not left guessing.
        """
        self._publish(event)
        return self.dispatch_command(event)

    def dispatch_command(self, event: ControlCommand) -> bool:
        """Apply a command that has already been published to every controller."""
        accepted = False
        for controller in self._controllers:
            accepted = controller.command(event) or accepted
        return accepted

    def start(self) -> None:
        """One task per controller. Idempotent; a second call starts nothing new."""
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(
                controller.run(), name=f"feed-{controller.adapter_name.lower()}"
            )
            for controller in self._controllers
        ]
        for task in self._tasks:
            task.add_done_callback(_report_finished_controller)

    async def aclose(self) -> None:
        """Stop every controller, cancel its task, and **detach every one of them**.

        The detach is not tidiness. A controller registers itself on its adapter at
        construction, and one left registered goes on being told about a socket it no
        longer owns — answering a signal, in a reader it does not belong to, by raising
        `IllegalTransition`. `run()` detaches on its own way out, but a supervisor closed
        before it ever started, or one whose task is cancelled before `run` reaches its
        `finally`, has controllers `run()` never spoke for. `detach()` is idempotent
        precisely so this can be unconditional.
        """
        for controller in self._controllers:
            controller.stop(detail="the application is shutting down")
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for controller in self._controllers:
            controller.detach()

    # --- the report ----------------------------------------------------------------

    def report(self, now: datetime | None = None) -> HealthReport:
        """What `/health` returns. **The shape every later ticket reads through.**

        `status` is the old liveness field, unchanged and always `ok`, because a route
        that answers at all is a process that is alive and because two tests and an
        unknown number of scripts read it. `feed` beside it is readiness. See the module
        docstring for why those are two fields and not one.
        """
        wall = now or datetime.now(UTC)
        return HealthReport(
            status="ok",
            feed=self.state,
            adapters=[_adapter_health(c, wall) for c in self._controllers],
        )


def _adapter_health(controller: ConnectionController, now: datetime) -> AdapterHealth:
    """One controller's line of the report.

    **`last_message_at` is derived from the age, not stamped per message.** The
    controller measures on a monotonic clock, which is the only clock a staleness bound
    can be measured on — `time.time()` steps backwards under an NTP correction and would
    make an age negative — so the wall-clock instant is reconstructed here, once per
    request, rather than taking a `datetime` on the hot path at a `measured` 1,322.9
    messages a second. `None` age gives `None` instant: no message has ever arrived, and
    that is an unknown time, not the epoch.
    """
    age = controller.last_message_age()
    return AdapterHealth(
        adapter=controller.adapter_name,
        state=controller.state or UNSTARTED,
        reason=controller.reason,
        last_message_at=None if age is None else now - timedelta(seconds=age),
        last_message_age_seconds=None if age is None else round(age, 3),
        reconnects=controller.reconnects,
        budget_remaining=controller.budget_remaining,
        transitions=controller.transitions,
        empty_opens=_counter(getattr(controller.adapter, "feed", None), "empty_opens"),
        undecodable=_counter(controller.adapter, "undecodable"),
    )


def _counter(source: object, name: str) -> int | None:
    """One of an adapter's silent-failure counters, or `None` if it keeps none.

    **`None` rather than `0` when there is nothing to ask**, which is every adapter that
    is not Delta's and every fake. A zero would say "this has not happened", and the rule
    this repo keeps everywhere is that an absent number is `null` and a real zero is `0`.
    The difference matters more for these two than most: each is a counter whose entire
    signal is being **above** zero, so a reader watching for that must be able to tell
    "still nought" from "nobody is counting".
    """
    value = getattr(source, name, None)
    return value if isinstance(value, int) else None


def _report_finished_controller(task: asyncio.Task) -> None:
    """Say something when a controller's task ends. It should never end on its own.

    A controller returns from `run()` when its adapter is finished — a stop, or a spent
    budget — and a task that simply finishes raises nothing. Without this the feed can
    give up and the only symptom is a screen that stopped moving. The spent budget
    already alerts; this catches the endings that do not.
    """
    if task.cancelled():
        return  # shutdown, which is the one legitimate way for these to end
    logger.error(
        "the feed controller %s ended unexpectedly: %s",
        task.get_name(),
        task.exception() or "returned without raising",
    )
