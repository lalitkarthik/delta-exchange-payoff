"""#102: what `tools/measure_bus_live.py` asks the bus for, and what it therefore reads.

`measure_bus_live.py` is an arrival-lag and throughput harness. Until #97 it subscribed
losslessly while stating no `group_start`, so its consumer group was created at `"0"` and,
against a non-empty stream, it drained up to the whole thirty-minute retention window at
Redis's replay speed before it reached a live entry. #97 flipped the default to `"$"`,
which is what the harness wants -- and that is the problem this file exists for. The tool
was then correct by inheritance. A measurement tool whose behaviour depends on a default
it never stated is one default change away from silently measuring something else, and
nothing would fail when it did.

So the criterion is not "the tool behaves correctly today". It is "the tool states what it
needs, and that statement is what produces the behaviour". Three tests, in that order:

* `test_the_harness_states_its_group_start_explicitly` reads the call site out of the
  source and asserts the keyword is *there*. This is the one that fails if a later
  refactor deletes the keyword and leans on the default again.
* `test_the_harness_options_do_not_replay_a_non_empty_stream` takes the options it just
  read out of the source and drives them through a real `RedisBus`, against a stream that
  already holds entries, and asserts the harness sees none of them. This is the one that
  fails if the stated value is wrong, or if the default moves back underneath an unstated
  one.
* `test_the_same_harness_options_with_a_zero_start_do_replay` is the control. It runs the
  identical scenario with `group_start="0"` and asserts the replay *does* arrive. Without
  it, the test above would still pass against a bus that had stopped delivering anything
  at all, and would prove nothing.

Nothing here imports the tool. It reads `tools/measure_bus_live.py` as source and resolves
its module-level constants from the same parse, so the test costs no venue client, no
adapter import and no `sys.path` surgery -- and it reads the file the way a reviewer does.

No network, no wall clock: the bus runs on `fakeredis` with an injected clock, exactly as
`test_bus_contract.py` does it.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest

from deltapayoff.events import Instrument, OptionQuote, Right
from deltapayoff.redis_bus import BusConfig, RedisBus

#: The tool under inspection, and the subscription inside it that this file pins.
TOOL = Path(__file__).resolve().parents[2] / "tools" / "measure_bus_live.py"
SUBSCRIBER = "store"

#: The same fixed instant `test_bus_contract.py` uses. Redis stamps entries with its own
#: clock; a trim floor taken from a clock behind that one can never trim what we publish,
#: which is what keeps this test free of the hour it runs in.
NOW = 1_788_000_000.0

INSTRUMENT = Instrument(
    venue="DELTA",
    underlying="BTC",
    expiry=date(2026, 6, 27),
    strike=Decimal("60000"),
    right=Right.CALL,
    venue_symbol="C-BTC-60000-270626",
)


def quote(bid: float) -> OptionQuote:
    """One `md.option_quote`, told apart by its bid."""
    return OptionQuote(
        source="delta",
        ts_received=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        instrument=INSTRUMENT,
        bid=bid,
    )


def _harness_subscription_options() -> dict[str, Any]:
    """The keyword arguments at the tool's lossless `bus.subscribe(...)` call site.

    Read out of the source rather than by importing the module: importing it would pull in
    the venue client and the Delta adapter for a question that is answered by the call
    site alone. Module-level constants (`STORE_WATERMARK`) are resolved from the same
    parse, so `maxsize` comes through as the number the tool actually passes.
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"), filename=str(TOOL))

    constants: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue

    def resolve(node: ast.expr) -> Any:
        if isinstance(node, ast.Name):
            if node.id not in constants:
                raise AssertionError(
                    f"{TOOL.name} passes {node.id!r}, which is not a module constant"
                )
            return constants[node.id]
        return ast.literal_eval(node)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "subscribe"):
            continue
        if not (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == SUBSCRIBER
        ):
            continue
        return {
            keyword.arg: resolve(keyword.value)
            for keyword in node.keywords
            if keyword.arg is not None
        }

    raise AssertionError(
        f"no `subscribe({SUBSCRIBER!r}, ...)` call site found in {TOOL}; this test "
        "pins a call site that no longer exists"
    )


def _bus(**overrides: Any) -> RedisBus:
    """A started-on-demand `RedisBus` over its own fakeredis server."""

    def factory(_config: BusConfig) -> Any:
        return fakeredis.aioredis.FakeRedis(decode_responses=False)

    config = BusConfig(
        underlyings=("BTC",),
        batch_ms=5,
        read_block_ms=20,
        read_count=500,
        **overrides,
    )
    return RedisBus(config, client_factory=factory, clock=lambda: NOW)


async def _drain(subscription, timeout: float = 1.0) -> list:
    """Everything queued, read until nothing more arrives. **The surplus is the point.**

    A replay is a delivery of entries nobody asked for, so a helper that asked for a count
    and was satisfied by it would be blind to exactly the defect this file is about.
    """
    got: list = []
    while True:
        try:
            got.append(await asyncio.wait_for(subscription.queue.get(), timeout))
        except TimeoutError:
            return got


async def _replay_scenario(**subscribe_kwargs: Any) -> list[float]:
    """Publish three entries, *then* subscribe, then publish one more. Return the bids.

    The three are what only a group created at `"0"` can see; the fourth is the live entry
    the harness is there to time. A tool that replays returns four bids, one that does not
    returns one, and the difference is the whole finding in #102.
    """
    bus = _bus()
    await bus.start()
    try:
        for n in range(3):
            bus.publish(quote(float(n)))
        await bus.flush()

        subscription = bus.subscribe(SUBSCRIBER, **subscribe_kwargs)
        await bus.ready()

        bus.publish(quote(99.0))
        await bus.flush()
        return [event.bid for event in await _drain(subscription)]
    finally:
        await bus.aclose()


def test_the_harness_states_its_group_start_explicitly() -> None:
    """The keyword is present at the call site, and it is the tail.

    This is the criterion in #102 that is about the *source* and not the behaviour: the
    tool must not go back to inheriting this. `test_bus_contract.py` already pins what an
    unstated subscriber gets; nothing pinned that this tool states anything at all.
    """
    options = _harness_subscription_options()

    assert options.get("lossless") is True, (
        "the subscription this file pins is the lossless one; if the harness stopped "
        "being lossless the whole question changed and this test should be rewritten, "
        f"not deleted. Got {options!r}"
    )
    assert "group_start" in options, (
        "tools/measure_bus_live.py subscribes losslessly without stating `group_start`. "
        "That is #102: an arrival-lag harness that inherits its replay behaviour from a "
        "default measures whatever the default happens to be that week"
    )
    assert options["group_start"] == "$", (
        f"the harness states group_start={options['group_start']!r}. `\"0\"` replays the "
        "retained window at Redis's replay speed, which is not a venue publish rate and "
        "not a live arrival lag"
    )


def test_the_harness_options_do_not_replay_a_non_empty_stream() -> None:
    """The options the tool actually passes, against a stream that already holds entries.

    Driven rather than read: #86 is this repository's standing proof that what a call site
    types and what the bus builds from it are two different questions.
    """
    options = _harness_subscription_options()

    bids = asyncio.run(_replay_scenario(**options))

    assert bids == [99.0], (
        f"the harness received {bids}. Anything before 99.0 is retained history arriving "
        "as if it were live -- the arrival lag of those entries is their age, and the "
        "rate they arrive at is Redis's, not the venue's"
    )


def test_the_same_harness_options_with_a_zero_start_do_replay() -> None:
    """The control, and the reason the test above is worth having.

    If the bus stopped delivering anything at all, or the scenario published nothing that
    could be replayed, the assertion above would still be green. This runs the identical
    scenario with the one value changed and requires the replay to show up, so a pass
    there means the stated `"$"` is doing the work.
    """
    options = _harness_subscription_options() | {"group_start": "0"}

    bids = asyncio.run(_replay_scenario(**options))

    assert bids == [0.0, 1.0, 2.0, 99.0], (
        f"a group created at `\"0\"` returned {bids}; it was expected to replay the "
        "three entries published before it subscribed. If this fails, the scenario is "
        "not exercising replay and the test above proves nothing"
    )


def test_the_drop_oldest_reader_states_no_group_start() -> None:
    """The screen reader is deliberately left alone, and this says so on purpose.

    #97 rejected making `group_start` a required argument partly because it is meaningless
    to a drop-oldest subscriber, which creates no consumer group at all. The harness's
    `screen` subscription is drop-oldest, so it has no opinion to state and stating one
    would be noise that implies a choice nobody made.
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"), filename=str(TOOL))

    screen = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "subscribe"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "screen"
    ]

    assert len(screen) == 1, (
        f"expected one `subscribe('screen', ...)`, found {len(screen)}"
    )
    stated = {keyword.arg for keyword in screen[0].keywords}
    assert "lossless" not in stated, "the screen reader is drop-oldest by omission"
    assert "group_start" not in stated, (
        "the screen reader creates no consumer group, so `group_start` would be an "
        "answer to a question it is not asking"
    )


@pytest.mark.parametrize("value", ["0", "$"])
def test_the_scenario_admits_both_values_the_bus_validates(value: str) -> None:
    """A guard on this file rather than on the tool.

    Both tests above go through `subscribe`'s validation. If that validation ever narrowed
    to one value, the control test would start failing for a reason that has nothing to do
    with the harness, and this names that reason first.
    """
    bus = _bus()

    subscription = bus.subscribe(
        f"probe-{value}", maxsize=10, lossless=True, group_start=value
    )

    assert subscription.group_start == value
