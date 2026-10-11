"""Seed Snowflake: prior-quarter transcripts and guidance, plus a Cortex Search service.

Creates <DB>.<SCHEMA>.PRIOR_DOCS (searchable chunks), <DB>.<SCHEMA>.PRIOR_GUIDANCE
(one row per guidance line), and the Cortex Search service PRIOR_GUIDANCE_SEARCH,
then runs a test query. Only pre-call documents (data/prior/) are loaded.

Usage: .venv/bin/python scripts/seed_snowflake.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

from app import config  # noqa: E402
from app.tools import guidance_history, prior_guidance, snowflake_agent  # noqa: E402

TEST_QUERY = "What was PepsiCo's core EPS guidance last quarter?"


def main() -> int:
    if config.is_mock("snowflake"):
        print(f"Snowflake stays in mock mode: {config.mock_reason('snowflake')}")
        return 1
    e = config.env()
    db, schema, wh = e["SNOWFLAKE_DATABASE"], e["SNOWFLAKE_SCHEMA"], e["SNOWFLAKE_WAREHOUSE"]
    chunks = prior_guidance.load_prior_chunks()
    guidance_rows = [c for c in chunks if "#row-" in c["DOC_ID"]]
    print(f"{len(chunks)} chunks ({len(guidance_rows)} guidance-table rows) from data/prior/")

    with prior_guidance.connect(database=None, schema=None) as conn:
        cur = conn.cursor()
        cur.execute(f"CREATE WAREHOUSE IF NOT EXISTS {wh} WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE")
        cur.execute(f"USE WAREHOUSE {wh}")
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {db}")
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {db}.{schema}")
        cur.execute(f"USE SCHEMA {db}.{schema}")
        cur.execute("""CREATE OR REPLACE TABLE PRIOR_DOCS (
            DOC_ID STRING, CHUNK STRING, DOC_TITLE STRING, QUARTER STRING, DOC_TYPE STRING,
            SOURCE_URL STRING, COMPANY STRING, TICKER STRING)""")
        cur.executemany(
            "INSERT INTO PRIOR_DOCS (DOC_ID, CHUNK, DOC_TITLE, QUARTER, DOC_TYPE, SOURCE_URL, COMPANY, TICKER) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'PepsiCo', 'PEP')",
            [(c["DOC_ID"], c["CHUNK"], c["DOC_TITLE"], c["QUARTER"], c["DOC_TYPE"], c["SOURCE_URL"]) for c in chunks],
        )
        cur.execute("""CREATE OR REPLACE TABLE PRIOR_GUIDANCE (
            TICKER STRING, AS_OF STRING, FISCAL_YEAR NUMBER, LINE STRING, SOURCE_DOC STRING)""")
        cur.executemany(
            "INSERT INTO PRIOR_GUIDANCE (TICKER, AS_OF, FISCAL_YEAR, LINE, SOURCE_DOC) VALUES ('PEP', %s, 2026, %s, %s)",
            [(c["QUARTER"], c["CHUNK"], c["DOC_ID"]) for c in guidance_rows],
        )
        lines = pd.read_csv(guidance_history.CSV, dtype=str, keep_default_na=False)
        cur.execute("""CREATE OR REPLACE TABLE GUIDANCE_LINES (
            TICKER STRING, FISCAL_YEAR NUMBER, AS_OF_DATE DATE, AS_OF_QUARTER STRING, AS_OF_EVENT STRING,
            METRIC STRING, METRIC_LABEL STRING, LOW FLOAT, HIGH FLOAT, UNIT STRING, VALUE_TEXT STRING,
            BASIS STRING, STATUS STRING, QUALIFIER STRING, SOURCE_URL STRING)
            COMMENT = 'Every FY guidance statement made before the call, one row per metric per event'""")
        cols = list(lines.columns)
        cur.executemany(
            f"INSERT INTO GUIDANCE_LINES ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            [tuple(v if v != "" else None for v in row) for row in lines.itertuples(index=False)],
        )
        print(f"{len(lines)} guidance lines loaded into GUIDANCE_LINES")
        started = time.monotonic()
        cur.execute(f"""CREATE OR REPLACE CORTEX SEARCH SERVICE {prior_guidance.SERVICE}
            ON CHUNK
            ATTRIBUTES QUARTER, DOC_TYPE, TICKER
            WAREHOUSE = {wh}
            TARGET_LAG = '1 day'
            AS (SELECT CHUNK, DOC_TITLE, QUARTER, DOC_TYPE, SOURCE_URL, TICKER, DOC_ID FROM PRIOR_DOCS)""")
        print(f"Cortex Search service {db}.{schema}.{prior_guidance.SERVICE} built in {time.monotonic() - started:.0f}s")
        n_transcript = cur.execute("SELECT COUNT(*) FROM PRIOR_DOCS WHERE DOC_ID ILIKE '%transcript%'").fetchone()[0]
        print(f"PRIOR_DOCS rows from transcript files: {n_transcript}")

        cur.execute("SHOW PARAMETERS LIKE 'CORTEX_ENABLED_CROSS_REGION' IN ACCOUNT")
        xr = cur.fetchone()
        print(f"CORTEX_ENABLED_CROSS_REGION = {xr[1] if xr else 'unknown'}")
        cur.execute(SEMANTIC_VIEW)
        print(f"semantic view {db}.{schema}.GUIDANCE_SV created")
        agent_ok = _create_agent(cur, db, schema, wh)

    rows = prior_guidance._cortex_query_fresh(TEST_QUERY, 3)
    print(f"test query: {TEST_QUERY}")
    for r in rows:
        print("  ->", json.dumps(r.get("CHUNK", ""))[:160])
    ok = bool(rows) and all(r.get("CHUNK") for r in rows) and n_transcript == 0
    if not ok:
        print("self-test FAILED: no rows, rows without CHUNK text, or transcript rows present")
    if agent_ok:
        _test_agent()
    return 0 if ok else 2


SEMANTIC_VIEW = """CREATE OR REPLACE SEMANTIC VIEW GUIDANCE_SV
  TABLES (
    guidance AS GUIDANCE_LINES
      PRIMARY KEY (TICKER, FISCAL_YEAR, AS_OF_DATE, METRIC)
      WITH SYNONYMS = ('outlook', 'forecast', 'prior guidance')
      COMMENT = 'Every fiscal-year guidance statement made before the call, one row per metric per event'
  )
  FACTS (
    guidance.low_value AS LOW COMMENT = 'Low end of the guided range; null when open-ended',
    guidance.high_value AS HIGH COMMENT = 'High end of the guided range; null when open-ended'
  )
  DIMENSIONS (
    guidance.ticker AS TICKER WITH SYNONYMS = ('symbol') COMMENT = 'Ticker, e.g. PEP',
    guidance.fiscal_year AS FISCAL_YEAR COMMENT = 'Fiscal year the guidance covers',
    guidance.as_of_date AS AS_OF_DATE COMMENT = 'Date the guidance was given',
    guidance.as_of_quarter AS AS_OF_QUARTER COMMENT = 'Reporting quarter of the event, e.g. Q2 2026',
    guidance.as_of_event AS AS_OF_EVENT COMMENT = 'Event, e.g. Q2 2026 earnings release and call',
    guidance.metric AS METRIC COMMENT = 'Metric slug: core_eps_growth, core_cc_eps_growth, organic_revenue_growth, net_revenue_growth, core_tax_rate, capex, fcf_conversion, cash_returns, fx',
    guidance.metric_label AS METRIC_LABEL WITH SYNONYMS = ('kpi', 'line item') COMMENT = 'Metric name, e.g. Core EPS growth',
    guidance.unit AS UNIT COMMENT = 'Unit: %, $B, pts, or % of net revenue',
    guidance.value_text AS VALUE_TEXT COMMENT = 'Guidance as written, e.g. approximately +5% to +7%',
    guidance.basis AS BASIS COMMENT = 'stated or implied',
    guidance.guidance_status AS STATUS COMMENT = 'first given, continues to expect, or affirmed',
    guidance.qualifier AS QUALIFIER COMMENT = 'Extra steer from management, e.g. towards the low end of the range',
    guidance.source_url AS SOURCE_URL COMMENT = 'Source document URL'
  )
  METRICS (
    guidance.guided_low AS MIN(guidance.low_value) COMMENT = 'Lowest guided low end',
    guidance.guided_high AS MAX(guidance.high_value) COMMENT = 'Highest guided high end'
  )
  COMMENT = 'Prior fiscal-year guidance, for fact-checking live earnings calls'"""


def _agent_spec(db: str, schema: str, wh: str, minimal: bool) -> str:
    model = config.get("SNOWFLAKE_AGENT_MODEL") or "auto"
    search_extra = "" if minimal else '\n    title_column: "DOC_TITLE"\n    id_column: "DOC_ID"'
    budget = "" if minimal else "\n  budget:\n    seconds: 30\n    tokens: 16000"
    return f"""
models:
  orchestration: {model}
orchestration:
  tool_not_accessible: reject{budget}
instructions:
  orchestration: "Use Guidance_History for the numbers: filter by TICKER and METRIC, order by AS_OF_DATE, and return AS_OF_DATE, AS_OF_EVENT, VALUE_TEXT, STATUS and QUALIFIER. Use Prior_Docs_Search only for wording and context. Never invent a number."
  response: "Start with exactly one verdict word: CONSISTENT, CONTRADICTS, NEW, or UNVERIFIABLE. Then at most two sentences: the latest guidance before the call with its date, and how it evolved."
tools:
  - tool_spec:
      type: "cortex_analyst_text_to_sql"
      name: "Guidance_History"
      description: "PepsiCo (PEP) fiscal 2026 guidance history before the Q3 2026 call: one row per metric per guidance event, Dec 2025 to Jul 2026, with ranges, wording, status and management qualifiers."
  - tool_spec:
      type: "cortex_search"
      name: "Prior_Docs_Search"
      description: "PepsiCo prior-quarter guidance documents: the Q2 2026 outlook text and guidance table."
tool_resources:
  Guidance_History:
    semantic_view: "{db}.{schema}.GUIDANCE_SV"
    execution_environment:
      type: "warehouse"
      warehouse: "{wh}"
      query_timeout: 20
  Prior_Docs_Search:
    search_service: "{db}.{schema}.{prior_guidance.SERVICE}"
    max_results: 4{search_extra}
"""


def _create_agent(cur, db: str, schema: str, wh: str) -> bool:
    for minimal in (False, True):
        try:
            cur.execute(f"""CREATE OR REPLACE AGENT {snowflake_agent.AGENT}
                COMMENT = 'Call Desk: checks live earnings-call claims against prior guidance'
                PROFILE = '{{"display_name": "Call Desk Guidance Analyst"}}'
                FROM SPECIFICATION $${_agent_spec(db, schema, wh, minimal)}$$""")
            print(f"Cortex Agent {db}.{schema}.{snowflake_agent.AGENT} created{' (minimal spec)' if minimal else ''}")
            return True
        except Exception as exc:  # noqa: BLE001 - report and try the minimal spec
            print(f"CREATE AGENT failed{' (minimal spec)' if minimal else ''}: {str(exc)[:300]}")
    return False


def _test_agent() -> None:
    import asyncio

    try:
        out = asyncio.run(snowflake_agent.live_check("PepsiCo", "PEP", "it's the margin piece that's driving the guide down in EPS",
                                                     "Core EPS growth", None))
        print(f"agent test: {out.verdict or '(no verdict)'} in {out.ms} ms · tools {out.tools} · {len(out.rows)} rows")
        print(f"  answer: {out.answer[:300]}")
        print(f"  sql: {out.sql[:300]}")
    except Exception as exc:  # noqa: BLE001 - the agent is an enrichment; the app shows the failure on each card
        print(f"agent test FAILED: {type(exc).__name__}: {str(exc)[:400]}")


if __name__ == "__main__":
    sys.exit(main())
