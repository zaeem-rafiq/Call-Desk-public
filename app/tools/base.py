"""Shared wrapper for the agent's tools: mock switch, timeout, retries, tracing.

A tool failure returns an error result (which lowers confidence); it never
raises into the pipeline and never silently swaps in mock data.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .. import config, observability
from ..models import ToolCall


@dataclass
class ToolResult:
    ok: bool
    data: Any
    call: ToolCall


async def run_tool(
    name: str,
    backend: str,
    args: dict[str, Any],
    live: Callable[..., Awaitable[Any]],
    mock: Callable[..., Awaitable[Any]],
    timeout_s: float = 12.0,
    retries: int = 1,
) -> ToolResult:
    is_mock = config.is_mock(backend)
    started = time.monotonic()
    error: str | None = None
    data: Any = None
    with observability.span(
        "gen_ai.execute_tool",
        f"execute_tool {name}",
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.type": "function",
            "gen_ai.agent.name": "call-desk-fact-checker",
            "gen_ai.tool.call.arguments": json.dumps(args, default=str),
            "calldesk.backend": backend,
            "calldesk.mock": is_mock,
        },
    ) as span:
        for attempt in range(retries + 1):
            try:
                fn = mock if is_mock else live
                data = await asyncio.wait_for(fn(**args), timeout=timeout_s)
                error = None
                break
            except Exception as exc:  # noqa: BLE001 - every failure is reported, none crash the feed
                error = f"{type(exc).__name__}: {exc}"[:300]
                if attempt == retries:
                    observability.capture(exc)
                else:
                    await asyncio.sleep(0.5 * (attempt + 1))
        span.set_data("gen_ai.tool.call.result", json.dumps(data, default=str)[:2000] if error is None else error)
        span.set_status("ok" if error is None else "internal_error")
    call = ToolCall(
        tool=name,
        backend=backend,
        args=args,
        ok=error is None,
        ms=int((time.monotonic() - started) * 1000),
        mock=is_mock,
        error=error,
    )
    return ToolResult(ok=error is None, data=data, call=call)
