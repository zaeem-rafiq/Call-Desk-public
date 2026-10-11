"""Gemini TTS voice alerts for cut / raise flags. Owned by the voice workstream; this is the interface stub.

alert_for(flag, info) -> (audio_url or None, spoken text). Called by factcheck._enrich after the verdict.
self_check() -> list of (name, ok, detail) rows for scripts/verify_live.py.
"""

from __future__ import annotations

from . import config
from .models import Flag


async def alert_for(flag: Flag, info: config.CallInfo) -> tuple[str | None, str]:
    return None, ""


async def self_check() -> list[tuple[str, bool, str]]:
    return []
