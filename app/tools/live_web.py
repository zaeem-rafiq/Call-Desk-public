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


async def mock_search_live_web(query: str, metric_label: str = "", company: str = "PepsiCo", limit: int = 5) -> list[dict]:
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
EXTRACT_BUDGET_S = 3.0


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


IR_DOMAINS = ["investors.pepsico.com", "pepsico.com", "sec.gov"]
GUIDANCE_ROW = re.compile(r"(?i)^(core|organic|net revenue|reported|effective tax|tax rate|capital spending|capex|free cash flow|"
                          r"total cash|cash returns|dividends|share repurchases|foreign exchange|fx|acquisitions)")


def guidance_table(content: str, max_rows: int = 12) -> str:
    """Guidance-table rows ("| Core EPS | +2.5% to +3.5% | Low-end of +5% to +7% |") from a page's markdown."""
    rows = []
    for line in content.splitlines():
        cells = [c.strip(" *") for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        cells = [c for c in cells if c]
        if len(cells) >= 2 and GUIDANCE_ROW.match(cells[0]) and re.search(r"\d", " ".join(cells[1:])):
            rows.append(" | ".join(cells[:3]))
    return "; ".join(list(dict.fromkeys(rows))[:max_rows])


def ir_hit(x: dict) -> dict | None:
    """A company or SEC document whose search content carries the guidance table, shaped as a web hit."""
    table = guidance_table((x.get("content") or "") + "\n" + (x.get("description") or ""))  # standard depth puts page text in description
    if not table:
        return None
    url = x.get("url", "")
    return {"title": x.get("title", ""), "url": url, "source": re.sub(r"^www\.", "", url.split("/")[2]) if "//" in url else "web",
            "snippet": f"Fiscal 2026 guidance table (metric | current | previous): {table}", "primary": True}


def ir_query(company: str, metric_label: str) -> str:
    return f"{company} Q3 2026 fiscal 2026 guidance {metric_label}"


def recall_query(company: str, metric_label: str) -> str:
    """Fixed second query: catches guidance-change coverage when the model's query is too specific."""
    return f"{company} lowers OR raises OR reaffirms 2026 {metric_label} guidance"


def interleave(batches: list[list[dict]], limit: int) -> list[dict]:
    """Merge result lists round-robin, dropping repeated URLs."""
    out, seen = [], set()
    for i in range(max((len(b) for b in batches), default=0)):
        for b in batches:
            if i < len(b) and b[i].get("url") and b[i]["url"] not in seen:
                seen.add(b[i]["url"])
                out.append(b[i])
    return out[:limit]


async def live_search_live_web(query: str, metric_label: str = "", company: str = "PepsiCo", limit: int = 5) -> list[dict]:
    import httpx

    headers = {"Authorization": f"Bearer {config.get('NIMBLE_API_KEY')}", "Content-Type": "application/json"}
    queries = [query] + ([recall_query(company, metric_label)] if metric_label else [])
    started = asyncio.get_running_loop().time()
    async with httpx.AsyncClient(base_url=NIMBLE_BASE, headers=headers, timeout=httpx.Timeout(5.0, connect=3.0)) as http:
        async def primary() -> list[dict]:
            """The company's own release and prepared remarks (standard depth returns page content, ~1 s)."""
            r = await http.post("/search", json={"query": ir_query(company, metric_label), "max_results": 4, "search_depth": "standard",
                                                 "include_domains": IR_DOMAINS, "start_date": "2026-10-08"})
            r.raise_for_status()
            return [h for h in (ir_hit(x) for x in r.json().get("results", [])) if h][:2]

        async def one(q: str) -> list[dict]:
            r = await http.post("/search", json={"query": q, "max_results": limit, "search_depth": "lite"})
            r.raise_for_status()
            return [
                {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("description", ""),
                 "source": re.sub(r"^www\.", "", httpx.URL(x["url"]).host) if x.get("url") else "web"}
                for x in r.json().get("results", [])[:limit]
            ]

        batches = await asyncio.gather(primary(), *(one(q) for q in queries), return_exceptions=True)
        ok = [b for b in batches if not isinstance(b, BaseException)]
        if not ok:
            raise batches[0]
        top = ok[0] if not isinstance(batches[0], BaseException) else []
        results = top + [h for h in interleave([b for b in ok if b is not top], limit + 3) if h["url"] not in {t["url"] for t in top}]

        async def enrich(item: dict) -> None:
            try:
                e = await http.post("/extract", json={"url": item["url"], "formats": ["markdown"], "markdown_backend": "main_content",
                                                      "request_timeout": 4000}, timeout=EXTRACT_BUDGET_S + 1)
                body = e.json()
                if body.get("status") == "success":
                    text = _passages((body.get("data") or {}).get("markdown") or "", query)
                    if text:
                        item["snippet"] = text
                        item["extracted"] = True
            except Exception:  # noqa: BLE001 - extraction is best effort; the search description stays
                pass

        budget = min(EXTRACT_BUDGET_S, 6.5 - (asyncio.get_running_loop().time() - started))  # whole tool stays under 8 s
        pending = [asyncio.create_task(enrich(i)) for i in [r for r in results if not r.get("primary")][:EXTRACT_TOP] if i["url"]]
        if pending and budget > 0.5:  # extraction is a bonus: keep search descriptions for pages that miss the time box
            _, late = await asyncio.wait(pending, timeout=budget)
            for task in late:
                task.cancel()
        else:
            for task in pending:
                task.cancel()
    return results
