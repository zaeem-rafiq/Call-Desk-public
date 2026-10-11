"""End-of-call IR brief written inside Snowflake. Owned by the briefing workstream; this is the interface stub.

call_brief(flags, info) -> CallBrief. Called by the pipeline when a run ends (published as SSE event "brief").
self_check() -> list of (name, ok, detail) rows for scripts/verify_live.py.
"""

from __future__ import annotations

from . import config
from .models import CallBrief, Flag


async def call_brief(flags: list[Flag], info: config.CallInfo) -> CallBrief:
    return CallBrief(status="failed", error="not built yet")


async def self_check() -> list[tuple[str, bool, str]]:
    return []
