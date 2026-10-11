"""search_prior_guidance: what did the company say last quarter?

Live: Snowflake Cortex Search service over prior-quarter transcripts and guidance.
Mock: keyword ranking over the same chunks loaded from data/prior/.
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import lru_cache
from pathlib import Path

from .. import config

SERVICE = "PRIOR_GUIDANCE_SEARCH"
COLUMNS = ["CHUNK", "DOC_TITLE", "QUARTER", "DOC_TYPE", "SOURCE_URL"]
_WORD = re.compile(r"[a-z0-9.%+-]+")


def _doc_meta(path: Path) -> dict:
    name = path.stem.lower()
    quarter = "Q2 2026" if "q2_2026" in name else ("Q1 2026" if "q1_2026" in name else ("Q4 2025" if "q4_2025" in name else "prior"))
    doc_type = "guidance" if "guidance" in name or "release" in name else "transcript"
    url = ""
    head = path.read_text(errors="ignore")[:2000]
    m = re.search(r"https?://\S+", head)
    if m:
        url = m.group(0).rstrip(").,>]")
    title = f"PepsiCo {quarter} {'earnings release outlook' if doc_type == 'guidance' else 'earnings call transcript'}"
    return {"DOC_TITLE": title, "QUARTER": quarter, "DOC_TYPE": doc_type, "SOURCE_URL": url}


def load_prior_chunks(max_chars: int = 1200) -> list[dict]:
    """Split data/prior/* into paragraph-packed chunks. Shared by the seed script and the mock."""
    chunks: list[dict] = []
    for path in sorted((config.DATA / "prior").glob("*")):
        if path.suffix.lower() not in {".txt", ".md"}:
            continue
        meta = _doc_meta(path)
        paras = [p.strip() for p in re.split(r"\n\s*\n", path.read_text(errors="ignore")) if p.strip()]
        buf = ""
        for p in paras:
            if buf and len(buf) + len(p) > max_chars:
                chunks.append({"CHUNK": buf, **meta, "DOC_ID": f"{path.name}#{len(chunks)}"})
                buf = ""
            buf = f"{buf}\n\n{p}" if buf else p
        if buf:
            chunks.append({"CHUNK": buf, **meta, "DOC_ID": f"{path.name}#{len(chunks)}"})
    return chunks


@lru_cache(maxsize=1)
def _mock_index() -> list[tuple[set[str], dict]]:
    return [(set(_WORD.findall(c["CHUNK"].lower())), c) for c in load_prior_chunks()]


def _shape(rows: list[dict]) -> list[dict]:
    return [
        {
            "doc_title": r.get("DOC_TITLE", ""),
            "quarter": r.get("QUARTER", ""),
            "doc_type": r.get("DOC_TYPE", ""),
            "source_url": r.get("SOURCE_URL", ""),
            "snippet": (r.get("CHUNK") or "")[:900],
        }
        for r in rows
    ]


async def mock_search_prior_guidance(query: str, limit: int = 4) -> list[dict]:
    q = set(_WORD.findall(query.lower()))
    boost = {"guidance", "outlook", "expect", "expects", "now", "core", "eps", "organic", "tax", "rate"}
    scored = []
    for words, chunk in _mock_index():
        score = len(q & words) + 0.5 * len(boost & words & q) + (1.5 if chunk["DOC_TYPE"] == "guidance" else 0)
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    return _shape([c for _, c in scored[:limit]])


def _cortex_query(query: str, limit: int) -> list[dict]:
    import snowflake.connector

    e = config.env()
    with snowflake.connector.connect(
        account=e["SNOWFLAKE_ACCOUNT"],
        user=e["SNOWFLAKE_USER"],
        password=e["SNOWFLAKE_PASSWORD"],
        warehouse=e["SNOWFLAKE_WAREHOUSE"],
        database=e["SNOWFLAKE_DATABASE"],
        schema=e["SNOWFLAKE_SCHEMA"],
        login_timeout=15,
        network_timeout=20,
    ) as conn:
        svc = f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{SERVICE}"
        body = json.dumps({"query": query, "columns": COLUMNS, "limit": limit})
        cur = conn.cursor()
        cur.execute("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)", (svc, body))
        raw = cur.fetchone()[0]
    return json.loads(raw).get("results", [])


async def live_search_prior_guidance(query: str, limit: int = 4) -> list[dict]:
    rows = await asyncio.to_thread(_cortex_query, query, limit)
    return _shape(rows)
