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
from . import guidance_history

SERVICE = "PRIOR_GUIDANCE_SEARCH"
COLUMNS = ["CHUNK", "DOC_TITLE", "QUARTER", "DOC_TYPE", "SOURCE_URL"]
_WORD = re.compile(r"[a-z0-9.%+-]+")


def _doc_meta(path: Path, company: str, ticker: str) -> dict:
    name = path.stem.lower()
    q = re.search(r"q([1-4])_(20\d\d)", name)
    quarter = f"Q{q.group(1)} {q.group(2)}" if q else "prior"
    doc_type = "guidance" if "guidance" in name or "release" in name else "transcript"
    url = ""
    head = path.read_text(errors="ignore")[:2000]
    m = re.search(r"https?://\S+", head)
    if m:
        url = m.group(0).rstrip(").,>]")
    title = f"{company} {quarter} {'earnings release outlook' if doc_type == 'guidance' else 'earnings call transcript'}"
    return {"DOC_TITLE": title, "QUARTER": quarter, "DOC_TYPE": doc_type, "SOURCE_URL": url, "COMPANY": company, "TICKER": ticker}


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
    when = re.search(r"\(([A-Z][a-z]+ \d{1,2}, \d{4})\)", text[:400])  # the date in the document's title line
    as_of = f"{meta['QUARTER']} ({when.group(1)})" if when else meta["QUARTER"]
    out = []
    for cells in rows:
        if len(cells) < 2:
            continue
        metric, value = cells[0], cells[1]
        basis = f" ({cells[2].lower()})" if len(cells) > 2 else ""
        note = ""
        if low_end and "EPS" in metric:
            note = f" On the {meta['QUARTER'].split()[0]} call the CFO said EPS may be towards the low end of the range."
        out.append({"CHUNK": f"{meta['COMPANY']} fiscal {meta['QUARTER'].split()[-1]} guidance as of {as_of}: {metric}: {value}{basis}.{note}",
                    **meta, "DOC_TITLE": f"{meta['COMPANY']} {meta['QUARTER']} guidance table", "DOC_ID": f"{path.name}#row-{len(out)}"})
    return out


def prior_sources() -> list[tuple[Path, str, str]]:
    """(prior_dir, company, ticker) for every call in data/calls/, once per folder."""
    out: list[tuple[Path, str, str]] = []
    for c in config.list_calls():
        info = config.call_info(c["id"])
        if info.prior_dir not in [d for d, _, _ in out]:
            out.append((info.prior_dir, info.company, info.ticker))
    return out


def load_prior_chunks(max_chars: int = 1200, sources: list[tuple[Path, str, str]] | None = None) -> list[dict]:
    """Split every call's prior/ guidance documents into paragraph-packed chunks, each tagged with its TICKER.

    Shared by the seed script and the mock.
    """
    chunks: list[dict] = []
    for prior_dir, company, ticker in sources if sources is not None else prior_sources():
        for path in sorted(Path(prior_dir).glob("*")):
            # Guidance documents only: third-party call transcripts stay on this machine (owner decision, Oct 11).
            if path.suffix.lower() not in {".txt", ".md"} or "transcript" in path.stem.lower():
                continue
            meta = _doc_meta(path, company, ticker)
            if meta["DOC_TYPE"] == "guidance":
                chunks.extend(_guidance_row_chunks(path, meta))
            paras = [p.strip() for p in re.split(r"\n\s*\n", path.read_text(errors="ignore")) if p.strip()]
            buf = ""
            for p in paras:
                if buf and len(buf) + len(p) > max_chars:
                    chunks.append({"CHUNK": buf, **meta, "DOC_ID": f"{ticker}/{path.name}#{len(chunks)}"})
                    buf = ""
                buf = f"{buf}\n\n{p}" if buf else p
            if buf:
                chunks.append({"CHUNK": buf, **meta, "DOC_ID": f"{ticker}/{path.name}#{len(chunks)}"})
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


def _with_history(hits: list[dict], history: list[dict]) -> dict:
    """Search hits first (the top guidance row stays [P1]), then the metric's guidance history as one more source."""
    if history:
        last = history[-1]
        hits = hits + [{"doc_title": f"Guidance history: {last['metric_label']} (Snowflake SQL, GUIDANCE_LINES)", "quarter": last["as_of_quarter"],
                        "doc_type": "guidance_history", "source_url": last["source_url"],
                        "snippet": guidance_history.summary_line(history)}]
    return {"hits": hits, "history": history}


async def mock_search_prior_guidance(query: str, metric: str = "", ticker: str = "PEP", limit: int = 4) -> dict:
    history = await guidance_history.mock_guidance_history(ticker, metric) if metric else []
    return _with_history(_mock_ranked(query, limit, ticker), history)


def _mock_ranked(query: str, limit: int, ticker: str = "PEP") -> list[dict]:
    q = set(_WORD.findall(query.lower()))
    boost = {"guidance", "outlook", "expect", "expects", "now", "core", "eps", "organic", "tax", "rate"}
    scored = []
    for words, chunk in _mock_index():
        if chunk.get("TICKER", ticker) != ticker:
            continue
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
        network_timeout=15,
        client_session_keep_alive=False,
    )
    if e.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = e["SNOWFLAKE_ROLE"]
    return snowflake.connector.connect(**{k: v for k, v in kwargs.items() if v is not None})


_shared_conn = None
_shared_lock = threading.Lock()


def query_shared(sql: str, params: tuple, fetch: str = "one"):
    """Run one query on a long-lived connection (Snowflake logins take seconds); reconnect once on failure."""
    global _shared_conn
    with _shared_lock:
        for attempt in range(2):
            try:
                if _shared_conn is None or _shared_conn.is_closed():
                    _shared_conn = connect()
                with _shared_conn.cursor() as cur:
                    cur.execute(sql, params, timeout=10)
                    return cur.fetchall() if fetch == "all" else cur.fetchone()
            except Exception:
                try:
                    if _shared_conn is not None:
                        _shared_conn.close()
                finally:
                    _shared_conn = None
                if attempt == 1:
                    raise


def warmup() -> None:
    query_shared("SELECT 1", ())


def _cortex_query(query: str, limit: int, ticker: str = "PEP") -> list[dict]:
    e = config.env()
    svc = f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{SERVICE}"
    body = json.dumps({"query": query, "columns": COLUMNS, "limit": limit, "filter": {"@eq": {"TICKER": ticker}}})
    raw = query_shared("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)", (svc, body))[0]
    return [{k.upper(): v for k, v in r.items()} for r in json.loads(raw).get("results", [])]


def _cortex_query_fresh(query: str, limit: int) -> list[dict]:
    """Same query on a fresh connection (used by the seed script's self-test)."""
    e = config.env()
    with connect() as conn:
        svc = f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{SERVICE}"
        body = json.dumps({"query": query, "columns": COLUMNS, "limit": limit})
        cur = conn.cursor()
        cur.execute("SELECT SNOWFLAKE.CORTEX.SEARCH_PREVIEW(%s, %s)", (svc, body))
        raw = cur.fetchone()[0]
    return [{k.upper(): v for k, v in r.items()} for r in json.loads(raw).get("results", [])]


async def live_search_prior_guidance(query: str, metric: str = "", ticker: str = "PEP", limit: int = 4) -> dict:
    """Cortex Search (filtered to the ticker) plus the metric's guidance history by SQL; both on the shared connection."""
    rows = await asyncio.to_thread(_cortex_query, query, limit, ticker)
    history = await guidance_history.live_guidance_history(ticker, metric) if metric else []
    return _with_history(_shape(rows), history)
