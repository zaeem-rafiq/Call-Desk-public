"""Tail-latency guard shared by the listener and the fact-check agent."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

log = logging.getLogger("calldesk")
T = TypeVar("T")


async def hedged(make: Callable[[], Awaitable[T]], hedge_after: float, label: str = "call") -> T:
    """Run make(); if it has not finished after hedge_after seconds, start an identical twin.
    The first successful result wins and the other request is cancelled."""
    tasks = [asyncio.ensure_future(make())]
    try:
        done, _ = await asyncio.wait(tasks, timeout=hedge_after)
        if not done:
            log.info("%s slow after %.0fs; sending a hedged duplicate", label, hedge_after)
            tasks.append(asyncio.ensure_future(make()))
        pending, error = set(tasks), None
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    return task.result()
                error = task.exception()
        raise error  # type: ignore[misc]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
