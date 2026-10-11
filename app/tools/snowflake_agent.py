"""Snowflake Cortex Agent: a second, Snowflake-native check of each claim.

The agent object CALLDESK.PRIOR.GUIDANCE_AGENT (created by scripts/seed_snowflake.py) has two tools:
  Guidance_History   Cortex Analyst text-to-SQL over the semantic view GUIDANCE_SV (table GUIDANCE_LINES)
  Prior_Docs_Search  Cortex Search over prior-quarter transcripts and guidance text
It runs through SNOWFLAKE.CORTEX.DATA_AGENT_RUN over the existing connector, so it needs no extra
credentials. It takes several seconds, so it runs after the verdict and fills in the card.
Mock: a deterministic answer built from the same guidance history, labelled MOCK.
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time

from .. import config
from ..models import SnowflakeAgent
from . import guidance_history

AGENT = "GUIDANCE_AGENT"
VERDICTS = ("CONSISTENT", "CONTRADICTS", "NEW", "UNVERIFIABLE")
STATEMENT_TIMEOUT_S = 38  # below the 45 s cap in factcheck, so the worker thread ends with the await
_idle: queue.LifoQueue = queue.LifoQueue(maxsize=4)  # idle connections; each agent run takes its own, so flags run in parallel


def agent_name() -> str:
    e = config.env()
    return f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{AGENT}"


def question(company: str, ticker: str, quote: str, metric_label: str, new_value: str | None) -> str:
    return (f"Claim heard live on {company}'s ({ticker}) Q3 2026 earnings call on October 8, 2026: \"{quote}\". "
            f"Metric: {metric_label}. Value heard: {new_value or 'none stated'}. "
            f"What was the fiscal 2026 guidance for this metric before this call, and how did it evolve through 2026? "
            f"Is the claim consistent with that history?")


def _run_sql(body: str, abandoned: threading.Event | None = None) -> str:
    """One DATA_AGENT_RUN on a pooled connection. No retry: a timed-out agent run is reported, not repeated."""
    from .prior_guidance import connect

    if abandoned is not None and abandoned.is_set():
        raise TimeoutError("abandoned before start")
    try:
        conn = _idle.get_nowait()
        if conn.is_closed():
            conn = connect()
    except queue.Empty:
        conn = connect()
    ok = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(%s, %s)", (agent_name(), body), timeout=STATEMENT_TIMEOUT_S)
            out = cur.fetchone()[0]
        ok = True
        return out
    finally:
        if ok:
            try:
                _idle.put_nowait(conn)
            except queue.Full:
                conn.close()
        else:
            conn.close()


def _rows(result_set: dict) -> list[dict]:
    meta = (result_set.get("resultSetMetaData") or result_set.get("result_set_meta_data") or {}).get("rowType") or []
    names = [c.get("name", f"c{i}") for i, c in enumerate(meta)]
    return [dict(zip(names, r)) for r in (result_set.get("data") or [])][:12]


TOOL_LABELS = {"Guidance_History": "Cortex Analyst · semantic view", "Prior_Docs_Search": "Cortex Search",
               "system_execute_sql": "SQL on warehouse"}


def parse(response: dict) -> SnowflakeAgent:
    """Read the agent's final response (content items: text, tool_use, tool_result, table, thinking, ...)."""
    out = SnowflakeAgent(status="done")
    texts: list[str] = []
    for item in response.get("content") or []:
        t = item.get("type")
        inner = item.get(t) if isinstance(item.get(t), dict) else {}
        if t == "text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
            for a in item.get("annotations") or []:
                title = a.get("doc_title") if isinstance(a, dict) else None
                if title and title not in out.citations:
                    out.citations.append(title)
        elif t == "tool_use" and inner.get("name"):
            out.tools.append(TOOL_LABELS.get(inner["name"], inner["name"]))
        elif t == "tool_result":
            for c in inner.get("content") or []:
                j = c.get("json") if isinstance(c, dict) else None
                if not isinstance(j, dict):
                    continue
                if isinstance(j.get("sql"), str) and not out.sql:
                    out.sql = j["sql"].strip()
                if isinstance(j.get("result_set"), dict) and not out.rows:
                    out.rows = _rows(j["result_set"])
                for r in j.get("search_results") or []:
                    title = r.get("doc_title") or r.get("DOC_TITLE") if isinstance(r, dict) else None
                    if title and title not in out.citations:
                        out.citations.append(str(title))
        elif t == "table" and isinstance(inner.get("result_set"), dict) and not out.rows:
            out.rows = _rows(inner["result_set"])
    # The last text block is the answer; earlier ones are progress notes ("I'll check the guidance history...").
    final = next((x for x in reversed(texts) if re.search(r"\b(" + "|".join(VERDICTS) + r")\b", x.upper())), texts[-1] if texts else "")
    out.answer = re.sub(r"\s+", " ", final).strip()[:900]
    m = re.search(r"\b(" + "|".join(VERDICTS) + r")\b", out.answer.upper())
    out.verdict = m.group(1) if m else ""
    out.tools = list(dict.fromkeys(out.tools))
    out.citations = out.citations[:4]
    return out


async def live_check(company: str, ticker: str, quote: str, metric_label: str, new_value: str | None) -> SnowflakeAgent:
    body = json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": question(company, ticker, quote, metric_label, new_value)}]}]})
    started = time.monotonic()
    abandoned = threading.Event()
    try:
        raw = await asyncio.to_thread(_run_sql, body, abandoned)
    except BaseException:
        abandoned.set()  # a cancelled or timed-out await: the worker must not start a run nobody waits for
        raise
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict) and data.get("code") and not data.get("content"):
        raise RuntimeError(f"agent error {data.get('code')}: {str(data.get('message'))[:200]}")
    out = parse(data or {})
    out.ms = int((time.monotonic() - started) * 1000)
    return out


async def mock_check(company: str, ticker: str, quote: str, metric_label: str, new_value: str | None, metric: str = "") -> SnowflakeAgent:
    await asyncio.sleep(1.5)
    history = await guidance_history.mock_guidance_history(ticker, metric) if metric else []
    if not history:
        return SnowflakeAgent(status="done", verdict="UNVERIFIABLE", mock=True, ms=1500, tools=["Guidance_History"],
                              answer=f"UNVERIFIABLE: no fiscal 2026 guidance line for {metric_label} in GUIDANCE_LINES.",
                              sql=guidance_history.SQL.replace("%s", f"'{ticker}'", 1).replace("%s", f"'{metric}'", 1))
    last = history[-1]
    note = f" {last['qualifier']}." if last["qualifier"] else ""
    return SnowflakeAgent(
        status="done", verdict="CONSISTENT", mock=True, ms=1500, tools=["Guidance_History", "Prior_Docs_Search"],
        answer=(f"CONSISTENT: before this call, {company} guided fiscal 2026 {metric_label.lower()} to {last['value_text']} "
                f"({last['event']}, {last['as_of_date']}).{note} The same range had stood since {history[0]['as_of_date']}."),
        sql=guidance_history.SQL.replace("%s", f"'{ticker}'", 1).replace("%s", f"'{metric}'", 1),
        rows=[{"AS_OF_DATE": h["as_of_date"], "VALUE_TEXT": h["value_text"], "STATUS": h["status"]} for h in history],
        citations=[f"PepsiCo {last['as_of_quarter']} guidance table"],
    )
