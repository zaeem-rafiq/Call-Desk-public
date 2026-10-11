"""End-of-call IR brief written inside Snowflake.

When a run ends, the pipeline calls call_brief(checked_flags, info) and publishes the result as SSE event "brief".
Live:
  1. CREATE TABLE IF NOT EXISTS <DB>.<SCHEMA>.CALL_FLAGS. Structured fields only: the owner's rule is that no call
     transcript text (quotes, speaker names) is stored in Snowflake.
  2. INSERT this run's flags under one RUN_ID.
  3. One AI_COMPLETE query reads those rows inside Snowflake (LISTAGG) and returns JSON {headline, bullets} for a
     peer company's IR / FP&A team. claude-sonnet-4-6 first, llama3.3-70b if that model errors.
call_brief never raises and is capped at CAP_S. Mock (no Snowflake keys, or MOCK_SNOWFLAKE=1): an instant,
deterministic brief built from the same flags.
self_check() -> (name, ok, detail) rows for scripts/verify_live.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from . import config, observability
from .models import CallBrief, Flag

TABLE = "CALL_FLAGS"
MODELS = ("claude-sonnet-4-6", "llama3.3-70b")
CAP_S = 44.0  # below the pipeline's 45 s wait_for, so the brief reports its own timeout
FIRST_MODEL_TIMEOUT_S = 26  # leaves time for the fallback model inside CAP_S
STEP_TIMEOUT_S = 10  # DDL and INSERT

COLUMNS = ("RUN_ID", "FLAG_ID", "TICKER", "CALL_TS", "METRIC", "METRIC_LABEL", "DIRECTION", "PRIOR_VALUE", "NEW_VALUE",
           "NEW_VALUE_SOURCE", "WHAT_CHANGED", "WHY_IT_MATTERS", "CONFIDENCE", "MATERIALITY", "WRITTEN_AT")


def ddl(table: str = TABLE) -> str:
    return f"""CREATE TABLE IF NOT EXISTS {table} (
            RUN_ID STRING, FLAG_ID STRING, TICKER STRING, CALL_TS TIMESTAMP_TZ, METRIC STRING, METRIC_LABEL STRING,
            DIRECTION STRING, PRIOR_VALUE STRING, NEW_VALUE STRING, NEW_VALUE_SOURCE STRING, WHAT_CHANGED STRING,
            WHY_IT_MATTERS STRING, CONFIDENCE FLOAT, MATERIALITY FLOAT, WRITTEN_AT TIMESTAMP_TZ)
            COMMENT = 'Call Desk checked flags, one row per flag per run. Structured fields only: no quotes or speaker text.'"""


def insert_sql(table: str) -> str:
    return f"INSERT INTO {table} ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})"


# Placeholders are pyformat (%(name)s): the connector binds them as escaped literals. No other % may appear here.
BRIEF_SQL = """WITH run AS (
  SELECT COUNT(*) AS N,
         LISTAGG(METRIC_LABEL || ' | ' || DIRECTION
                 || ' | prior: ' || COALESCE(PRIOR_VALUE, 'none')
                 || ' | now: ' || COALESCE(NEW_VALUE, 'not stated') || ' (' || COALESCE(NEW_VALUE_SOURCE, 'spoken') || ')'
                 || ' | ' || COALESCE(WHAT_CHANGED, '') || ' | ' || COALESCE(WHY_IT_MATTERS, '')
                 || ' | confidence ' || COALESCE(TO_VARCHAR(ROUND(CONFIDENCE, 2)), 'n/a'), '\\n')
           WITHIN GROUP (ORDER BY MATERIALITY DESC, CALL_TS) AS LINES
  FROM {table}
  WHERE RUN_ID = %(run_id)s
)
SELECT AI_COMPLETE(
         %(model)s,
         %(instructions)s || '\\n\\n' || LINES,
         {'temperature': 0, 'max_tokens': 800},
         {'type': 'json', 'schema': {'type': 'object',
           'properties': {'headline': {'type': 'string'},
                          'bullets': {'type': 'array', 'items': {'type': 'string'}}},
           'required': ['headline', 'bullets']}}
       ) AS BRIEF,
       N
FROM run"""


def table_name() -> str:
    e = config.env()
    return f"{e['SNOWFLAKE_DATABASE']}.{e['SNOWFLAKE_SCHEMA']}.{TABLE}"


def instructions(info: config.CallInfo) -> str:
    return (f"You write the end-of-call brief for the investor relations and FP&A team of a peer company in the same industry. "
            f"Below are the fact-checked guidance flags from {info.title} ({info.ticker}), one per line: "
            "metric | direction | prior guidance | new value (where the number came from) | what changed | why it matters | "
            "confidence from 0 to 1. Return a headline of at most 14 words and 3 to 5 bullets of at most 20 words each, "
            "most material first. Plain CFO tone. Use only numbers that appear in the rows: never compute, round or invent "
            "a figure. Do not quote the call.")


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def display_sql(table: str, run_id: str, model: str, instr: str) -> str:
    """The brief query as run, with its bound values written in, for the UI."""
    return (BRIEF_SQL.replace("{table}", table).replace("%(run_id)s", _literal(run_id))
            .replace("%(model)s", _literal(model)).replace("%(instructions)s", _literal(instr)))


def new_run_id(info: config.CallInfo, prefix: str = "") -> str:
    return f"{prefix}{info.ticker}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def flag_row(run_id: str, f: Flag, ticker: str, written_at: str) -> tuple:
    """One CALL_FLAGS row. Deliberately leaves out quote and speaker (no transcript text in Snowflake)."""
    return (run_id, f.id, ticker, f.call_ts, f.metric, f.metric_label, f.direction, f.prior_value, f.new_value,
            f.new_value_source or "spoken", f.what_changed or None, f.why_it_matters or None, f.confidence,
            f.materiality, written_at)


def _clean(text: object, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip().lstrip("-•* ").strip()[:limit]


def parse_brief(raw: object) -> tuple[str, list[str]]:
    """AI_COMPLETE's structured output (an OBJECT, or its JSON text) -> (headline, 1..5 bullets)."""
    data = raw
    for _ in range(2):  # an OBJECT arrives as JSON text; a model may also wrap its JSON in a string or code fence
        if isinstance(data, str):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", data.strip())
            data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("structured_output"), list):  # show_details shape
        data = (data["structured_output"][0] or {}).get("raw_message", data)
    if not isinstance(data, dict):
        raise ValueError("AI_COMPLETE returned no JSON object")
    headline = _clean(data.get("headline"), 200)
    bullets = [b for b in (_clean(x, 320) for x in data.get("bullets") or []) if b][:5]
    if not headline or not bullets:
        raise ValueError("AI_COMPLETE returned no headline or bullets")
    return headline, bullets


def _write_and_brief(table: str, rows: list[tuple], run_id: str, instr: str, deadline: float,
                     abandoned: threading.Event) -> dict:
    """Blocking: DDL, INSERT, then AI_COMPLETE (with model fallback) on one fresh connection."""
    from .tools.prior_guidance import connect

    def check() -> None:
        if abandoned.is_set():
            raise TimeoutError("abandoned")

    with connect() as conn, conn.cursor() as cur:
        check()
        cur.execute(ddl(table), timeout=STEP_TIMEOUT_S)
        check()
        cur.executemany(insert_sql(table), rows, timeout=STEP_TIMEOUT_S)
        written = cur.rowcount if isinstance(cur.rowcount, int) and cur.rowcount >= 0 else len(rows)
        errors: list[str] = []
        for i, model in enumerate(MODELS):
            check()
            left = deadline - time.monotonic()
            if left < 4:
                errors.append("no time left")
                break
            timeout = int(min(left - 1, FIRST_MODEL_TIMEOUT_S if i == 0 else left - 1))
            try:
                cur.execute(BRIEF_SQL.replace("{table}", table),
                            {"run_id": run_id, "model": model, "instructions": instr}, timeout=timeout)
                raw, n = cur.fetchone()
                headline, bullets = parse_brief(raw)
                return {"model": model, "headline": headline, "bullets": bullets, "written": written, "read": n}
            except Exception as exc:  # noqa: BLE001 - try the next model, report all errors if none works
                errors.append(f"{model}: {type(exc).__name__}: {str(exc)[:160]}")
        raise RuntimeError("AI_COMPLETE failed: " + " | ".join(errors))


async def _live_brief(flags: list[Flag], info: config.CallInfo, run_id: str, started: float) -> CallBrief:
    table = table_name()
    instr = instructions(info)
    written_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [flag_row(run_id, f, info.ticker, written_at) for f in flags]
    abandoned = threading.Event()
    try:
        out = await asyncio.to_thread(_write_and_brief, table, rows, run_id, instr, started + CAP_S - 1, abandoned)
    except BaseException:
        abandoned.set()  # a timed-out or cancelled await: the worker must not start the next statement
        raise
    return CallBrief(status="done", headline=out["headline"], bullets=out["bullets"],
                     sql=display_sql(table, run_id, out["model"], instr), rows_written=out["written"],
                     ms=int((time.monotonic() - started) * 1000), model=out["model"])


VERBS = {"cut": "cut", "raise": "raised", "reaffirm": "reaffirmed", "new": "introduced", "n/a": "flagged"}


def mock_brief(flags: list[Flag], info: config.CallInfo, run_id: str) -> CallBrief:
    """Deterministic brief from the flags themselves, shaped like the live one."""
    ranked = sorted(flags, key=lambda f: (-(f.materiality or 0.0), f.call_ts))
    top = ranked[0]
    headline = f"{info.company} {VERBS.get(top.direction, 'flagged')} {top.metric_label}"
    if top.new_value:
        headline += f" to {top.new_value}"
    if top.prior_value:
        headline += f" (was {top.prior_value})"
    bullets = []
    for f in ranked[:5]:
        line = f"{f.metric_label}: {f.prior_value or 'no prior guidance'} → {f.new_value or 'no number stated'} ({f.direction.upper()})"
        bullets.append(line + (f", confidence {f.confidence * 100:.0f}%." if f.confidence is not None else "."))
    if len(bullets) < 3:
        counts: dict[str, int] = {}
        for f in flags:
            counts[f.direction] = counts.get(f.direction, 0) + 1
        bullets.append(f"{len(flags)} checked flag{'s' if len(flags) != 1 else ''}: "
                       + ", ".join(f"{n} {d}" for d, n in counts.items()) + ".")
    if len(bullets) < 3:
        bullets.append("No other guidance line was flagged on the call.")
    return CallBrief(status="done", headline=headline, bullets=bullets[:5], rows_written=len(flags), ms=0,
                     model="mock", mock=True, sql=display_sql(table_name(), run_id, MODELS[0], instructions(info)))


async def call_brief(flags: list[Flag], info: config.CallInfo, *, run_id: str | None = None) -> CallBrief:
    """Never raises (except cancellation, so Stop still cancels the run). Capped at CAP_S."""
    started = time.monotonic()
    mock = config.is_mock("snowflake")
    flags = [f for f in flags if f is not None]
    if not flags:
        return CallBrief(status="failed", error="no checked flags to brief", mock=mock)
    run_id = run_id or new_run_id(info)
    try:
        if mock:
            return mock_brief(flags, info, run_id)
        return await asyncio.wait_for(_live_brief(flags, info, run_id, started), timeout=CAP_S)
    except TimeoutError:
        return CallBrief(status="failed", error=f"timed out after {CAP_S:.0f} s", mock=mock,
                         ms=int((time.monotonic() - started) * 1000))
    except Exception as exc:  # noqa: BLE001 - the brief is optional; the run must finish
        observability.capture(exc)
        return CallBrief(status="failed", error=f"{type(exc).__name__}: {exc}"[:200], mock=mock,
                         ms=int((time.monotonic() - started) * 1000))


def self_check_flags(info: config.CallInfo) -> list[Flag]:
    ts = info.call_ts(0).isoformat(timespec="seconds")
    return [
        Flag(id="selfcheck-eps", status="checked", call_ts=ts, t_offset=0.0, speaker="", quote="", claim_type="guidance",
             metric="core_eps_growth", metric_label="Core EPS growth", direction="cut",
             prior_value="low end of +5% to +7%", new_value="+2.5% to +3.5%",
             what_changed="Fiscal 2026 core EPS growth guidance cut below the prior range.",
             why_it_matters="Margin pressure is larger than guided in July.", confidence=0.86, materiality=0.95),
        Flag(id="selfcheck-organic", status="checked", call_ts=ts, t_offset=0.0, speaker="", quote="", claim_type="guidance",
             metric="organic_revenue_growth", metric_label="Organic revenue growth", direction="reaffirm",
             prior_value="+2% to +4%", new_value="~+3%",
             what_changed="Organic revenue growth narrowed to the middle of the prior range.",
             why_it_matters="Top line holds; the cut is in margins.", confidence=0.78, materiality=0.7),
    ]


def _delete_run(run_id: str) -> int:
    from .tools.prior_guidance import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table_name()} WHERE RUN_ID = %s", (run_id,), timeout=STEP_TIMEOUT_S)
        return cur.rowcount or 0


async def self_check() -> list[tuple[str, bool, str]]:
    """Live: write two synthetic flags, brief them with AI_COMPLETE, then delete them. Never raises."""
    if config.is_mock("snowflake"):
        return [("Snowflake live", False, f"mock mode: {config.mock_reason('snowflake')}")]
    out: list[tuple[str, bool, str]] = []
    info = config.call_info()
    run_id = new_run_id(info, prefix="selfcheck-")
    brief, took = None, 0.0
    try:
        started = time.monotonic()
        brief = await call_brief(self_check_flags(info), info, run_id=run_id)
        took = time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        out.append(("call_brief", False, f"{type(exc).__name__}: {exc}"[:160]))
    try:
        # The DELETE's row count is an independent check that the rows really landed in CALL_FLAGS.
        deleted = await asyncio.wait_for(asyncio.to_thread(_delete_run, run_id), timeout=30)
        out.append(("rows written to CALL_FLAGS", deleted == 2, f"{deleted} rows under {run_id} (deleted after the check)"))
    except Exception as exc:  # noqa: BLE001
        out.append(("rows written to CALL_FLAGS", False, f"cleanup DELETE failed: {type(exc).__name__}: {exc}"[:160]))
    if brief is not None:
        ok = brief.status == "done" and bool(brief.headline) and 3 <= len(brief.bullets) <= 5
        out.append(("AI_COMPLETE brief returned (headline + bullets)", ok,
                    f"{brief.model} · {len(brief.bullets)} bullets · {brief.headline[:70]}" if ok else (brief.error or brief.status)[:160]))
        out.append(("under 45 s", took < 45, f"{took:.1f}s"))
    return out
