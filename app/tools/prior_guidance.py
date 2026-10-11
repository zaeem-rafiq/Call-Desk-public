"""search_prior_guidance: what did the company say last quarter?

Live: Snowflake Cortex Search service over prior-quarter transcripts and guidance.
Mock: keyword ranking over the same chunks loaded from data/prior/.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
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


def _table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-\s|:]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows


def _guidance_row_chunks(path: Path, meta: dict) -> list[dict]:
    """One short chunk per guidance-table row, so search hits land on the exact line."""
    text = path.read_text(errors="ignore")
    section = text.split("## Guidance table", 1)[-1].split("\n## ", 1)[0] if "## Guidance table" in text else ""
    rows = _table_rows(section)[1:]  # skip header
    low_end = "low end of the EPS range" in text
    out = []
    for cells in rows:
        if len(cells) < 2:
            continue
        metric, value = cells[0], cells[1]
        basis = f" ({cells[2].lower()})" if len(cells) > 2 else ""
        note = ""
        if low_end and "EPS" in metric:
            note = " On the Q2 call the CFO said EPS may be towards the low end of the range."
        out.append({"CHUNK": f"PepsiCo fiscal 2026 guidance as of {meta['QUARTER']} (July 9, 2026): {metric}: {value}{basis}.{note}",
                    **meta, "DOC_TITLE": f"PepsiCo {meta['QUARTER']} guidance table", "DOC_ID": f"{path.name}#row-{len(out)}"})
    return out


def load_prior_chunks(max_chars: int = 1200) -> list[dict]:
    """Split data/prior/* into paragraph-packed chunks. Shared by the seed script and the mock."""
    chunks: list[dict] = []
    for path in sorted((config.DATA / "prior").glob("*")):
        if path.suffix.lower() not in {".txt", ".md"}:
            continue
        meta = _doc_meta(path)
        if meta["DOC_TYPE"] == "guidance":
            chunks.extend(_guidance_row_chunks(path, meta))
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
        overlap = len(q & words)
        if not overlap:
            continue
        # Favour short, precise chunks (guidance-table rows) over long transcript passages.
        score = (overlap + 0.5 * len(boost & words & q) + (1.5 if chunk["DOC_TYPE"] == "guidance" else 0)) / math.log(len(words) + 8)
        scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    return _shape([c for _, c in scored[:limit]])


_UNSET = object()


def connect(database=_UNSET, schema=_UNSET):
    """Snowflake connection from .env. SNOWFLAKE_PASSWORD may hold a password or a programmatic access token."""
    import snowflake.connector

    e = config.env()
    kwargs = dict(
        account=e["SNOWFLAKE_ACCOUNT"].removesuffix(".snowflakecomputing.com"),
        user=e["SNOWFLAKE_USER"],
        password=e["SNOWFLAKE_PASSWORD"],
        warehouse=e["SNOWFLAKE_WAREHOUSE"],
        database=e["SNOWFLAKE_DATABASE"] if database is _UNSET else database,
        schema=e["SNOWFLAKE_SCHEMA"] if schema is _UNSET else schema,
        login_timeout=15,
        network_timeout=30,
        client_session_keep_alive=False,
    )
    if e.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = e["SNOWFLAKE_ROLE"]
    return snowflake.connector.connect(**{k: v for k, v in kwargs.items() if v is not None})


_shared_conn = None
_shared_lock = threading.Lock()


def _query_shared(sql: str, params: tuple):
    """Run one query on a long-lived connection (Snowflake logins take seconds); reconnect once on failure."""
    global _shared_conn
    with _shared_lock:
        for attempt in range(2):
            try:
                if _shared_conn is None or _shared_conn.is_closed():
                    _shared_conn = connect()
                cur = _shared_conn.cursor()
                cur.execute(sql, params)
                return cur.fetchone()
            except Exception:
                try:
                    if _shared_conn is not None:
                        _shared_conn.close()
                finally:
                    _shared_conn = None
                if attempt == 1:
                    raise


def warmup() -> None:
    _query_shared("SELECT 1", ())


def _cortex_query(query: str, limit: int) -> list[dict]:
    e = config.env()
    svc = f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{SERVICE}"
    body = json.dumps({"query": query, "columns": COLUMNS, "limit": limit})
    raw = _query_shared("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)", (svc, body))[0]
    return json.loads(raw).get("results", [])


def _cortex_query_fresh(query: str, limit: int) -> list[dict]:
    """Same query on a fresh connection (used by the seed script's self-test)."""
    e = config.env()
    with connect() as conn:
        svc = f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{SERVICE}"
        body = json.dumps({"query": query, "columns": COLUMNS, "limit": limit})
        cur = conn.cursor()
        cur.execute("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)", (svc, body))
        raw = cur.fetchone()[0]
    return json.loads(raw).get("results", [])


async def live_search_prior_guidance(query: str, limit: int = 4) -> list[dict]:
    rows = await asyncio.to_thread(_cortex_query, query, limit)
    return _shape(rows)
