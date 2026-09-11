"""Shared subscription relisting for the monolith and the dedicated feed process."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from . import log_events
from .logging_setup import log_event

logger = logging.getLogger(__name__)

RELIST_INTERVAL_SECONDS = 60.0


async def relist_instruments(stack: Any) -> int:
    """Subscribe every newly listed contract on each configured underlying."""
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
    stack: Any,
    interval: float = RELIST_INTERVAL_SECONDS,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Re-list on a cadence until cancelled; transient listing failures are retried."""
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


__all__ = ["RELIST_INTERVAL_SECONDS", "relist_forever", "relist_instruments"]
