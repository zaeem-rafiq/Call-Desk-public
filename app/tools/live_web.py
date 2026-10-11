"""search_live_web: corroborate or contradict a claim with live web results.

Live: Nimble web search (see docs/sdk_notes.md for the endpoint).
Mock: canned results from app/mocks/web_results.json, labelled as mock.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .. import config

MOCK_FILE = Path(__file__).resolve().parent.parent / "mocks" / "web_results.json"
_WORD = re.compile(r"[a-z0-9.%+-]+")


@lru_cache(maxsize=1)
def _mock_results() -> list[dict]:
    return json.loads(MOCK_FILE.read_text()) if MOCK_FILE.exists() else []


async def mock_search_live_web(query: str, limit: int = 5) -> list[dict]:
    q = set(_WORD.findall(query.lower()))
    scored = []
    for r in _mock_results():
        words = set(_WORD.findall(f"{r['title']} {r['snippet']}".lower()))
        score = len(q & words)
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [{**r, "mock": True} for _, r in scored[:limit]]


async def live_search_live_web(query: str, limit: int = 5) -> list[dict]:
    raise NotImplementedError("Nimble client is implemented in M4")
