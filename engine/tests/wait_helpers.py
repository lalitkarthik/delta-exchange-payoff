"""Polling helpers shared by any test synchronising with a background loop.

Moved out of `test_store.py` (#93) so `test_recording.py` and `test_relisting.py` can
import the same two functions instead of redefining them. `wait_until` came first, for a
test with its own event loop to `await` on; `wait_until_sync` followed in #83 for a test
body with none — `TestClient(main.app)` runs the real application, background tasks
included, on its own event loop in another thread, and a synchronous test cannot `await`
that loop, only poll it from outside. Both exist because a fixed `time.sleep` guessing how
long a background pass takes is exactly the bet that fails under load: the loop is real
and scheduled by the OS, so how much of it lands inside a fixed sleep depends on how
promptly that other thread runs, not on anything the test controls. See #83 and #93.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def wait_until(
    condition: Callable[[], bool], *, timeout: float = 2.0, poll: float = 0.005
) -> None:
    """Poll a real-time condition until it is true, or fail loudly past `timeout`.

    For synchronising a test with a `BarWriter` task driven by a **fake** clock: the
    condition is always something the writer sets after doing the real work (a row
    count, a buffer length, `writer.loops`), never a guess at how long that work takes.
    `timeout` is real wall-clock slack for a loaded machine to schedule the writer's
    task and, where a flush is involved, its worker thread — it does not move the fake
    clock, which the test alone controls.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(poll)


def wait_until_sync(
    condition: Callable[[], bool],
    *,
    timeout: float = 5.0,
    poll: float = 0.01,
    message: str = "condition not met",
) -> None:
    """`wait_until`'s sibling for a test with no event loop of its own to await on.

    `TestClient(main.app)` runs the real application, background tasks included, on its
    own event loop in another thread; a synchronous test body cannot `await` that loop,
    only poll it from outside. #83: a fixed `time.sleep` guessing how long two passes of
    a shortened background loop take is exactly the bet that fails under load -- the
    loop is real and asyncio-scheduled, so how many passes land inside a fixed sleep
    depends on how promptly the OS runs that other thread, not on anything the test
    controls. This polls the condition itself instead, real wall-clock slack behind it
    (`timeout`) for a loaded machine to schedule that thread, and fails on a timeout
    with `message` rather than on whatever counter the caller was really asking about.
    """
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message} (timed out after {timeout}s)")
        time.sleep(poll)
