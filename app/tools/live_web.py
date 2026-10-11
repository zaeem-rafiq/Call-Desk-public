"""search_live_web: corroborate or contradict a claim with live web results.

Live: Nimble web search (see docs/sdk_notes.md for the endpoint).
Mock: canned results from app/mocks/web_results.json, labelled as mock.
"""

from __future__ import annotations

import asyncio
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


NIMBLE_BASE = "https://sdk.nimbleway.com/v2"
EXTRACT_TOP = 2  # pages to open for full text; the rest keep their search description


def _passages(markdown: str, query: str, max_chars: int = 700) -> str:
    """Pick the lines of a page most relevant to the query, favouring ones with numbers."""
    q = {w for w in _WORD.findall(query.lower()) if len(w) > 2}
    scored = []
    for line in re.split(r"(?<=[.!?])\s+|\n+", markdown):
        line = line.strip(" #*|-\t")
        if len(line) < 25:
            continue
        words = set(_WORD.findall(line.lower()))
        score = len(q & words) + (1.5 if re.search(r"\d+(\.\d+)?\s?(%|percent)", line) else 0)
        if score >= 2:
            scored.append((score, line))
    scored.sort(key=lambda x: -x[0])
    out = ""
    for _, line in scored:
        if len(out) + len(line) > max_chars:
            break
        out = f"{out} … {line}" if out else line
    return out


async def live_search_live_web(query: str, limit: int = 5) -> list[dict]:
    import httpx

    headers = {"Authorization": f"Bearer {config.get('NIMBLE_API_KEY')}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=NIMBLE_BASE, headers=headers, timeout=httpx.Timeout(12.0, connect=5.0)) as http:
        r = await http.post("/search", json={"query": query, "max_results": limit, "search_depth": "lite"})
        r.raise_for_status()
        results = [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", ""),
             "source": re.sub(r"^www\.", "", httpx.URL(x["url"]).host) if x.get("url") else "web"}
            for x in r.json().get("results", [])[:limit]
        ]

        async def enrich(item: dict) -> None:
            try:
                e = await http.post("/extract", json={"url": item["url"], "formats": ["markdown"], "markdown_backend": "main_content",
                                                      "request_timeout": 8000}, timeout=10.0)
                body = e.json()
                if body.get("status") == "success":
                    text = _passages((body.get("data") or {}).get("markdown") or "", query)
                    if text:
                        item["snippet"] = text
                        item["extracted"] = True
            except Exception:  # noqa: BLE001 - extraction is best effort; the search description stays
                pass

        await asyncio.gather(*(enrich(i) for i in results[:EXTRACT_TOP] if i["url"]))
    return results
